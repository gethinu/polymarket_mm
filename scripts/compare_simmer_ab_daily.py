#!/usr/bin/env python3
"""
Compare Simmer A/B observation results for the same window.

Inputs are baseline/candidate metrics-log-state triples and a shared time window.
Outputs a concise comparison summary with pass/fail gates.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from report_simmer_observation import (
    _compute_kpis,
    _compute_total_pnl_from_state,
    _count_events,
    _discord_url,
    _load_state,
    _parse_ts,
    _post_json,
    iter_metrics,
)


def _load_rows(path: str, since: dt.datetime, until: dt.datetime):
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8", errors="replace") as f:
        return [r for r in iter_metrics(f) if (since <= r.ts <= until)]


def _variant_stats(name: str, metrics_file: str, log_file: str, state_file: str, since: dt.datetime, until: dt.datetime) -> dict:
    rows = _load_rows(metrics_file, since=since, until=until)
    state = _load_state(state_file)
    counts = _count_events(log_file, since=since, until=until)
    kpis = _compute_kpis(rows, counts=counts, since=since, until=until)

    total_pnl = _compute_total_pnl_from_state(state)
    day_key = str(state.get("day_key") or "") if isinstance(state, dict) else ""
    day_anchor = float(state.get("day_pnl_anchor") or 0.0) if isinstance(state, dict) else 0.0
    pnl_today = total_pnl - day_anchor if day_key else total_pnl
    halted = bool(state.get("halted", False)) if isinstance(state, dict) else False

    return {
        "name": name,
        "samples": int(len(rows)),
        "fills_buy": int(counts.get("fill_buys") or 0),
        "fills_sell": int(counts.get("fill_sells") or 0),
        "errors": int(counts.get("errors") or 0),
        "halts": int(counts.get("halts") or 0),
        "halted": halted,
        "turnover_per_day": float(kpis.get("turnover_per_day") or 0.0),
        "closed_cycles": int(kpis.get("closed_cycles") or 0),
        "median_hold_sec": float(kpis.get("median_hold_sec") or 0.0),
        "expectancy_est": float(kpis.get("expectancy_est") or 0.0),
        "total_pnl": float(total_pnl),
        "pnl_today": float(pnl_today),
    }


def _expectancy_gate(base: float, cand: float, ratio: float) -> bool:
    if base > 0:
        return cand >= (base * ratio)
    if base < 0:
        # For negative expectancy baseline, candidate should be no worse.
        return cand >= base
    return cand >= 0.0


def _fmt_delta(x: float) -> str:
    return f"{x:+.4f}"


def _fmt_ok(ok: bool) -> str:
    return "OK" if ok else "NG"


def _compare_result(baseline: dict, candidate: dict, expectancy_ratio: float) -> dict:
    d_turnover = candidate["turnover_per_day"] - baseline["turnover_per_day"]
    d_hold_sec = candidate["median_hold_sec"] - baseline["median_hold_sec"]
    d_expectancy = candidate["expectancy_est"] - baseline["expectancy_est"]
    d_errors = candidate["errors"] - baseline["errors"]
    d_halts = candidate["halts"] - baseline["halts"]
    d_total_pnl = candidate["total_pnl"] - baseline["total_pnl"]
    d_pnl_today = candidate["pnl_today"] - baseline["pnl_today"]

    gate_turnover = candidate["turnover_per_day"] >= baseline["turnover_per_day"]
    gate_hold = candidate["median_hold_sec"] <= baseline["median_hold_sec"]
    gate_expectancy = _expectancy_gate(
        base=float(baseline["expectancy_est"]),
        cand=float(candidate["expectancy_est"]),
        ratio=float(expectancy_ratio),
    )
    gate_stability = (candidate["errors"] <= baseline["errors"]) and (candidate["halts"] <= baseline["halts"])
    overall = gate_turnover and gate_hold and gate_expectancy and gate_stability
    total_buys = int(baseline["fills_buy"] + candidate["fills_buy"])
    total_sells = int(baseline["fills_sell"] + candidate["fills_sell"])
    total_fills = int(total_buys + total_sells)
    base_has_buy = int(baseline["fills_buy"]) > 0
    base_has_sell = int(baseline["fills_sell"]) > 0
    base_has_cycles = int(baseline["closed_cycles"]) > 0
    cand_has_buy = int(candidate["fills_buy"]) > 0
    cand_has_sell = int(candidate["fills_sell"]) > 0
    cand_has_cycles = int(candidate["closed_cycles"]) > 0
    baseline_data_sufficient = base_has_buy and base_has_sell and base_has_cycles
    candidate_data_sufficient = cand_has_buy and cand_has_sell and cand_has_cycles
    data_sufficient = baseline_data_sufficient and candidate_data_sufficient
    insufficient_reasons: list[str] = []
    if not base_has_buy:
        insufficient_reasons.append("baseline_buy=0")
    if not base_has_sell:
        insufficient_reasons.append("baseline_sell=0")
    if not base_has_cycles:
        insufficient_reasons.append("baseline_closed_cycles=0")
    if not cand_has_buy:
        insufficient_reasons.append("candidate_buy=0")
    if not cand_has_sell:
        insufficient_reasons.append("candidate_sell=0")
    if not cand_has_cycles:
        insufficient_reasons.append("candidate_closed_cycles=0")

    return {
        "deltas": {
            "turnover_per_day": float(d_turnover),
            "median_hold_sec": float(d_hold_sec),
            "expectancy_est": float(d_expectancy),
            "errors": int(d_errors),
            "halts": int(d_halts),
            "total_pnl": float(d_total_pnl),
            "pnl_today": float(d_pnl_today),
        },
        "gates": {
            "turnover": bool(gate_turnover),
            "median_hold": bool(gate_hold),
            "expectancy": bool(gate_expectancy),
            "stability": bool(gate_stability),
        },
        "total_fills": int(total_fills),
        "total_buys": int(total_buys),
        "total_sells": int(total_sells),
        "baseline_data_sufficient": bool(baseline_data_sufficient),
        "candidate_data_sufficient": bool(candidate_data_sufficient),
        "insufficient_reasons": insufficient_reasons,
        "data_sufficient": bool(data_sufficient),
        "overall": bool(overall),
    }


def build_compare_report(
    baseline: dict,
    candidate: dict,
    since: dt.datetime,
    until: dt.datetime,
    compare: dict,
) -> str:
    deltas = compare.get("deltas") if isinstance(compare, dict) else {}
    gates = compare.get("gates") if isinstance(compare, dict) else {}
    lines: list[str] = []
    lines.append(f"A/B Compare Window: {since:%Y-%m-%d %H:%M:%S} -> {until:%Y-%m-%d %H:%M:%S} (local)")
    lines.append(
        "Baseline: "
        f"samples={baseline['samples']} fills={baseline['fills_buy']}/{baseline['fills_sell']} "
        f"turnover/day={baseline['turnover_per_day']:.2f} "
        f"median_hold={baseline['median_hold_sec']/60.0:.1f}m "
        f"expectancy={baseline['expectancy_est']:+.4f} "
        f"errors={baseline['errors']} halts={baseline['halts']} "
        f"pnl_today={baseline['pnl_today']:+.4f} total={baseline['total_pnl']:+.4f}"
    )
    lines.append(
        "Candidate: "
        f"samples={candidate['samples']} fills={candidate['fills_buy']}/{candidate['fills_sell']} "
        f"turnover/day={candidate['turnover_per_day']:.2f} "
        f"median_hold={candidate['median_hold_sec']/60.0:.1f}m "
        f"expectancy={candidate['expectancy_est']:+.4f} "
        f"errors={candidate['errors']} halts={candidate['halts']} "
        f"pnl_today={candidate['pnl_today']:+.4f} total={candidate['total_pnl']:+.4f}"
    )
    lines.append(
        "Delta(C-B): "
        f"turnover/day={_fmt_delta(float(deltas.get('turnover_per_day') or 0.0))} "
        f"median_hold_sec={_fmt_delta(float(deltas.get('median_hold_sec') or 0.0))} "
        f"expectancy={_fmt_delta(float(deltas.get('expectancy_est') or 0.0))} "
        f"errors={int(deltas.get('errors') or 0):+d} halts={int(deltas.get('halts') or 0):+d} "
        f"pnl_today={_fmt_delta(float(deltas.get('pnl_today') or 0.0))} total={_fmt_delta(float(deltas.get('total_pnl') or 0.0))}"
    )
    lines.append(
        "Gates: "
        f"turnover({_fmt_ok(bool(gates.get('turnover')) )}) "
        f"median_hold({_fmt_ok(bool(gates.get('median_hold')) )}) "
        f"expectancy({_fmt_ok(bool(gates.get('expectancy')) )}) "
        f"stability({_fmt_ok(bool(gates.get('stability')) )})"
    )
    lines.append(
        "Data: "
        f"sufficient={'yes' if bool(compare.get('data_sufficient')) else 'no'} "
        f"baseline(ok={str(bool(compare.get('baseline_data_sufficient'))).lower()} "
        f"buy/sell/cycles={baseline['fills_buy']}/{baseline['fills_sell']}/{baseline['closed_cycles']}) "
        f"candidate(ok={str(bool(compare.get('candidate_data_sufficient'))).lower()} "
        f"buy/sell/cycles={candidate['fills_buy']}/{candidate['fills_sell']}/{candidate['closed_cycles']}) "
        f"fills(total buy/sell)={int(compare.get('total_buys') or 0)}/{int(compare.get('total_sells') or 0)}"
    )
    reasons = compare.get("insufficient_reasons")
    if isinstance(reasons, list) and reasons:
        lines.append(f"Data Reason: {', '.join([str(x) for x in reasons])}")
    if not bool(compare.get("data_sufficient")):
        decision = "INSUFFICIENT"
    else:
        decision = "PASS" if bool(compare.get("overall")) else "FAIL"
    lines.append(f"Decision: {decision}")
    return "\n".join(lines)


def _append_history(path: str, payload: dict) -> None:
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    if p.exists():
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = str(line or "").strip()
                if not s:
                    continue
                try:
                    o = json.loads(s)
                    if isinstance(o, dict):
                        existing.append(o)
                except Exception:
                    continue

    def _window_key(row: dict) -> tuple[str, str] | None:
        since = str(row.get("since") or "").strip()
        until = str(row.get("until") or "").strip()
        if not since or not until:
            return None
        return (since, until)

    def _row_rank(row: dict) -> tuple[dt.datetime, dt.datetime]:
        ts = _parse_ts(str(row.get("ts") or "")) if str(row.get("ts") or "").strip() else None
        until = _parse_ts(str(row.get("until") or "")) if str(row.get("until") or "").strip() else None
        return (ts or dt.datetime.min, until or dt.datetime.min)

    by_window: dict[tuple[str, str], dict] = {}
    pass_through: list[dict] = []
    for row in existing:
        key = _window_key(row)
        if key is None:
            pass_through.append(row)
            continue
        prev = by_window.get(key)
        if prev is None or _row_rank(row) >= _row_rank(prev):
            by_window[key] = row

    key = _window_key(payload)
    if key is None:
        pass_through.append(payload)
    else:
        by_window[key] = payload

    normalized = list(by_window.values())
    normalized.sort(
        key=lambda r: (
            str(r.get("until") or ""),
            str(r.get("since") or ""),
            str(r.get("ts") or ""),
        )
    )
    out_rows = pass_through + normalized

    with p.open("w", encoding="utf-8", errors="replace", newline="\n") as f:
        for row in out_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    p = argparse.ArgumentParser(description="Compare Simmer A/B observation daily")
    p.add_argument("--baseline-metrics-file", default=str(root / "logs" / "simmer-ab-baseline-metrics.jsonl"))
    p.add_argument("--baseline-log-file", default=str(root / "logs" / "simmer-ab-baseline.log"))
    p.add_argument("--baseline-state-file", default=str(root / "logs" / "simmer_ab_baseline_state.json"))
    p.add_argument("--candidate-metrics-file", default=str(root / "logs" / "simmer-ab-candidate-metrics.jsonl"))
    p.add_argument("--candidate-log-file", default=str(root / "logs" / "simmer-ab-candidate.log"))
    p.add_argument("--candidate-state-file", default=str(root / "logs" / "simmer_ab_candidate_state.json"))
    p.add_argument("--hours", type=float, default=24.0, help="Lookback window in hours")
    p.add_argument("--since", default="", help='Start timestamp "YYYY-MM-DD HH:MM:SS" (overrides --hours)')
    p.add_argument("--until", default="", help='End timestamp "YYYY-MM-DD HH:MM:SS" (default: now)')
    p.add_argument(
        "--expectancy-ratio-threshold",
        type=float,
        default=0.90,
        help="Candidate expectancy must be >= baseline * threshold when baseline expectancy > 0",
    )
    p.add_argument("--output-file", default="", help="Optional output file path")
    p.add_argument("--history-file", default="", help="Optional JSONL history output path")
    p.add_argument("--discord", action="store_true", help="Post compare summary to Discord webhook (if configured)")
    args = p.parse_args()

    now = dt.datetime.now()
    until = _parse_ts(args.until) if args.until else now
    since = _parse_ts(args.since) if args.since else (until - dt.timedelta(hours=float(args.hours)))

    baseline = _variant_stats(
        name="baseline",
        metrics_file=args.baseline_metrics_file,
        log_file=args.baseline_log_file,
        state_file=args.baseline_state_file,
        since=since,
        until=until,
    )
    candidate = _variant_stats(
        name="candidate",
        metrics_file=args.candidate_metrics_file,
        log_file=args.candidate_log_file,
        state_file=args.candidate_state_file,
        since=since,
        until=until,
    )
    compare = _compare_result(
        baseline=baseline,
        candidate=candidate,
        expectancy_ratio=float(args.expectancy_ratio_threshold),
    )
    report = build_compare_report(
        baseline=baseline,
        candidate=candidate,
        since=since,
        until=until,
        compare=compare,
    )

    print(report)

    if args.output_file:
        out = Path(args.output_file)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report + "\n", encoding="utf-8")

    if args.history_file:
        if not bool(compare.get("data_sufficient")):
            decision = "INSUFFICIENT"
        else:
            decision = "PASS" if bool(compare.get("overall")) else "FAIL"
        payload = {
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "since": since.strftime("%Y-%m-%d %H:%M:%S"),
            "until": until.strftime("%Y-%m-%d %H:%M:%S"),
            "expectancy_ratio_threshold": float(args.expectancy_ratio_threshold),
            "decision": decision,
            "gates": compare.get("gates") if isinstance(compare, dict) else {},
            "data_sufficient": bool(compare.get("data_sufficient")),
            "baseline_data_sufficient": bool(compare.get("baseline_data_sufficient")),
            "candidate_data_sufficient": bool(compare.get("candidate_data_sufficient")),
            "insufficient_reasons": compare.get("insufficient_reasons") if isinstance(compare, dict) else [],
            "total_fills": int(compare.get("total_fills") or 0),
            "total_buys": int(compare.get("total_buys") or 0),
            "total_sells": int(compare.get("total_sells") or 0),
            "deltas": compare.get("deltas") if isinstance(compare, dict) else {},
            "baseline": baseline,
            "candidate": candidate,
        }
        _append_history(args.history_file, payload)

    if args.discord:
        url = _discord_url()
        if not url:
            print("Discord webhook not configured (CLOBBOT_DISCORD_WEBHOOK_URL).")
            return 3
        content = report
        if len(content) > 1900:
            content = content[:1900] + "\n...(truncated)"
        try:
            _post_json(url, {"content": f"```text\n{content}\n```"})
        except Exception as e:
            code = getattr(e, "code", None)
            if isinstance(code, int):
                print(f"Discord post failed: HTTP {code}")
            else:
                print(f"Discord post failed: {type(e).__name__}")
            return 4

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
