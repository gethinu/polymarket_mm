#!/usr/bin/env python3
"""
Judge Simmer 30-day observe A/B decision from daily compare history.

This script is observe-only analytics. It does not place trades.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path


def _parse_ts(s: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(str(s or "").strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _as_float(v: object, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _as_int(v: object, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _parse_date(s: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(str(s or "").strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def _mean(xs: list[float]) -> float:
    return float(sum(xs) / len(xs)) if xs else 0.0


def _load_history(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    with p.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = str(line or "").strip()
            if not s:
                continue
            try:
                o = json.loads(s)
                if isinstance(o, dict):
                    out.append(o)
            except Exception:
                continue
    return out


def _window_days(row: dict) -> float:
    since = _parse_ts(str(row.get("since") or ""))
    until = _parse_ts(str(row.get("until") or ""))
    if since is None or until is None or until <= since:
        return 1.0
    sec = (until - since).total_seconds()
    return max(1e-9, float(sec / 86400.0))


def _expectancy_gate(base: float, cand: float, ratio: float) -> bool:
    if base > 0:
        return cand >= (base * ratio)
    if base < 0:
        return cand >= base
    return cand >= 0.0


def _choose_daily_rows(rows: list[dict]) -> list[dict]:
    """
    Collapse raw rows into one record per local `until` date.
    Preference order:
    1) data_sufficient=true rows
    2) latest `until`
    3) latest `ts`
    """
    by_day: dict[str, tuple[tuple[int, dt.datetime, dt.datetime], dict]] = {}

    for row in rows:
        until = _parse_ts(str(row.get("until") or ""))
        if until is None:
            continue
        ts = _parse_ts(str(row.get("ts") or "")) or until
        day_key = until.date().isoformat()
        sufficient = 1 if bool(row.get("data_sufficient")) else 0
        key = (sufficient, until, ts)
        prev = by_day.get(day_key)
        if prev is None or key > prev[0]:
            by_day[day_key] = (key, row)

    selected = [v[1] for _, v in sorted(by_day.items(), key=lambda kv: kv[0])]
    return selected


def _coverage(rows: list[dict]) -> tuple[str, str]:
    since_ts: list[dt.datetime] = []
    until_ts: list[dt.datetime] = []
    for row in rows:
        s = _parse_ts(str(row.get("since") or ""))
        u = _parse_ts(str(row.get("until") or ""))
        if s is not None:
            since_ts.append(s)
        if u is not None:
            until_ts.append(u)
    if not since_ts or not until_ts:
        return ("N/A", "N/A")
    return (
        min(since_ts).strftime("%Y-%m-%d %H:%M:%S"),
        max(until_ts).strftime("%Y-%m-%d %H:%M:%S"),
    )


def _interim_milestone_status(
    *,
    data_sufficient_days: int,
    target_days: int,
    conditions_pass: bool,
) -> dict:
    days = max(0, int(data_sufficient_days))
    target = max(1, int(target_days))
    reached = days >= target
    if not reached:
        decision = "PENDING"
    else:
        decision = "GO" if bool(conditions_pass) else "NO_GO"
    return {
        "target_days": int(target),
        "data_sufficient_days": int(days),
        "remaining_days": int(max(0, target - days)),
        "reached": bool(reached),
        "conditions_pass": bool(conditions_pass),
        "decision": str(decision),
    }


def judge(
    rows: list[dict],
    min_days: int,
    expectancy_ratio_threshold: float,
    decision_date: dt.date,
    today: dt.date,
) -> dict:
    daily_rows = _choose_daily_rows(rows)
    decidable_rows = [r for r in daily_rows if bool(r.get("data_sufficient"))]

    base_turnovers: list[float] = []
    cand_turnovers: list[float] = []
    base_holds: list[float] = []
    cand_holds: list[float] = []
    base_expectancies: list[float] = []
    cand_expectancies: list[float] = []

    base_errors = 0
    cand_errors = 0
    base_halts = 0
    cand_halts = 0
    total_days = 0.0

    for r in decidable_rows:
        b = r.get("baseline") if isinstance(r.get("baseline"), dict) else {}
        c = r.get("candidate") if isinstance(r.get("candidate"), dict) else {}
        base_turnovers.append(_as_float(b.get("turnover_per_day")))
        cand_turnovers.append(_as_float(c.get("turnover_per_day")))
        base_holds.append(_as_float(b.get("median_hold_sec")))
        cand_holds.append(_as_float(c.get("median_hold_sec")))
        base_expectancies.append(_as_float(b.get("expectancy_est")))
        cand_expectancies.append(_as_float(c.get("expectancy_est")))
        base_errors += _as_int(b.get("errors"))
        cand_errors += _as_int(c.get("errors"))
        base_halts += _as_int(b.get("halts"))
        cand_halts += _as_int(c.get("halts"))
        total_days += _window_days(r)

    base_turnover_mean = _mean(base_turnovers)
    cand_turnover_mean = _mean(cand_turnovers)
    base_hold_mean = _mean(base_holds)
    cand_hold_mean = _mean(cand_holds)
    base_expectancy_mean = _mean(base_expectancies)
    cand_expectancy_mean = _mean(cand_expectancies)

    denom_days = max(1e-9, float(total_days))
    base_error_per_day = float(base_errors) / denom_days
    cand_error_per_day = float(cand_errors) / denom_days
    base_halt_per_day = float(base_halts) / denom_days
    cand_halt_per_day = float(cand_halts) / denom_days

    if decidable_rows:
        c1_turnover = cand_turnover_mean >= base_turnover_mean
        c2_hold = cand_hold_mean < base_hold_mean
        c3_expectancy = _expectancy_gate(
            base=base_expectancy_mean,
            cand=cand_expectancy_mean,
            ratio=float(expectancy_ratio_threshold),
        )
        c4_stability = (cand_error_per_day <= base_error_per_day) and (cand_halt_per_day <= base_halt_per_day)
    else:
        c1_turnover = False
        c2_hold = False
        c3_expectancy = False
        c4_stability = False

    min_days_pass = int(len(decidable_rows)) >= int(min_days)
    conditions_pass = c1_turnover and c2_hold and c3_expectancy and c4_stability
    go = min_days_pass and conditions_pass
    interim_7d = _interim_milestone_status(
        data_sufficient_days=int(len(decidable_rows)),
        target_days=7,
        conditions_pass=conditions_pass,
    )
    interim_14d = _interim_milestone_status(
        data_sufficient_days=int(len(decidable_rows)),
        target_days=14,
        conditions_pass=conditions_pass,
    )

    days_until_decision = int((decision_date - today).days)
    if today < decision_date:
        timing_status = "BEFORE_DECISION_DATE"
    elif today == decision_date:
        timing_status = "DECISION_DAY"
    else:
        timing_status = "AFTER_DECISION_DATE"
    decision_date_reached = bool(today >= decision_date)
    decision_stage = "FINAL" if decision_date_reached else "PROVISIONAL"
    final_decision_ready = decision_date_reached and min_days_pass

    if decision_date_reached:
        if go:
            recommended_action = (
                "FINAL_GO: Candidate settings can be adopted. "
                "Keep execution observe-only unless live mode is explicitly requested."
            )
        elif not min_days_pass:
            recommended_action = (
                "FINAL_NO_GO: Decision date reached without minimum data days. "
                "Extend observe window and re-judge."
            )
        else:
            recommended_action = (
                "FINAL_NO_GO: Re-tune candidate parameters (asset_quotas / max_hold_sec / "
                "sell_target_decay_cents_per_min) and start the next 30-day observe cycle."
            )
    else:
        if go:
            recommended_action = (
                "PROVISIONAL_GO: Conditions are currently passing before decision date. "
                "Continue observe runs through decision date."
            )
        elif not min_days_pass:
            recommended_action = (
                "NO_GO: Continue daily observe runs until data-sufficient days reach --min-days, then re-judge."
            )
        else:
            recommended_action = (
                "NO_GO: Re-tune candidate parameters (asset_quotas / max_hold_sec / "
                "sell_target_decay_cents_per_min) and continue observe runs until decision date."
            )

    all_since, all_until = _coverage(rows)
    dec_since, dec_until = _coverage(decidable_rows)

    return {
        "decision": "GO" if go else "NO_GO",
        "summary": {
            "raw_rows": int(len(rows)),
            "daily_rows": int(len(daily_rows)),
            "data_sufficient_days": int(len(decidable_rows)),
            "min_days_required": int(min_days),
            "min_days_pass": bool(min_days_pass),
            "decision_date": decision_date.isoformat(),
            "today_local": today.isoformat(),
            "decision_timing_status": timing_status,
            "days_until_decision": int(days_until_decision),
            "decision_date_reached": bool(decision_date_reached),
            "decision_stage": decision_stage,
            "final_decision_ready": bool(final_decision_ready),
            "coverage_all": {"since": all_since, "until": all_until},
            "coverage_data_sufficient": {"since": dec_since, "until": dec_until},
            "interim_milestones": {
                "tentative_7d": interim_7d,
                "intermediate_14d": interim_14d,
            },
        },
        "conditions": {
            "turnover_day_ge_baseline": {
                "pass": bool(c1_turnover),
                "baseline_mean": float(base_turnover_mean),
                "candidate_mean": float(cand_turnover_mean),
                "delta_candidate_minus_baseline": float(cand_turnover_mean - base_turnover_mean),
            },
            "median_hold_shorter_than_baseline": {
                "pass": bool(c2_hold),
                "baseline_mean_sec": float(base_hold_mean),
                "candidate_mean_sec": float(cand_hold_mean),
                "delta_candidate_minus_baseline_sec": float(cand_hold_mean - base_hold_mean),
            },
            "expectancy_not_worse_than_10pct": {
                "pass": bool(c3_expectancy),
                "baseline_mean": float(base_expectancy_mean),
                "candidate_mean": float(cand_expectancy_mean),
                "ratio_threshold": float(expectancy_ratio_threshold),
                "delta_candidate_minus_baseline": float(cand_expectancy_mean - base_expectancy_mean),
            },
            "error_halt_frequency_not_higher": {
                "pass": bool(c4_stability),
                "baseline_errors_per_day": float(base_error_per_day),
                "candidate_errors_per_day": float(cand_error_per_day),
                "baseline_halts_per_day": float(base_halt_per_day),
                "candidate_halts_per_day": float(cand_halt_per_day),
            },
        },
        "recommended_action": recommended_action,
        "last_daily_rows": [
            {
                "since": str(r.get("since") or ""),
                "until": str(r.get("until") or ""),
                "decision": str(r.get("decision") or "").upper(),
                "data_sufficient": bool(r.get("data_sufficient")),
            }
            for r in daily_rows[-5:]
        ],
    }


def render_text(result: dict) -> str:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    conditions = result.get("conditions") if isinstance(result.get("conditions"), dict) else {}

    c1 = conditions.get("turnover_day_ge_baseline") if isinstance(conditions.get("turnover_day_ge_baseline"), dict) else {}
    c2 = conditions.get("median_hold_shorter_than_baseline") if isinstance(conditions.get("median_hold_shorter_than_baseline"), dict) else {}
    c3 = conditions.get("expectancy_not_worse_than_10pct") if isinstance(conditions.get("expectancy_not_worse_than_10pct"), dict) else {}
    c4 = conditions.get("error_halt_frequency_not_higher") if isinstance(conditions.get("error_halt_frequency_not_higher"), dict) else {}

    lines: list[str] = []
    lines.append("Simmer A/B Decision Judge (observe-only)")
    lines.append(
        "Rows: "
        f"raw={int(summary.get('raw_rows') or 0)} "
        f"daily={int(summary.get('daily_rows') or 0)} "
        f"data_sufficient_days={int(summary.get('data_sufficient_days') or 0)} "
        f"(min_days={int(summary.get('min_days_required') or 0)})"
    )
    lines.append(
        "Coverage(all): "
        f"{str((summary.get('coverage_all') or {}).get('since') or 'N/A')} "
        f"-> {str((summary.get('coverage_all') or {}).get('until') or 'N/A')}"
    )
    lines.append(
        "Coverage(data_sufficient): "
        f"{str((summary.get('coverage_data_sufficient') or {}).get('since') or 'N/A')} "
        f"-> {str((summary.get('coverage_data_sufficient') or {}).get('until') or 'N/A')}"
    )
    lines.append(
        f"Precheck(min_days): {'PASS' if bool(summary.get('min_days_pass')) else 'FAIL'}"
    )
    lines.append(
        "Decision Timing: "
        f"today={str(summary.get('today_local') or 'N/A')} "
        f"decision_date={str(summary.get('decision_date') or 'N/A')} "
        f"status={str(summary.get('decision_timing_status') or 'UNKNOWN')} "
        f"days_until={_as_int(summary.get('days_until_decision'), 0):+d}"
    )
    lines.append(
        "Final Readiness: "
        f"{'YES' if bool(summary.get('final_decision_ready')) else 'NO'} "
        f"(decision_stage={str(summary.get('decision_stage') or 'UNKNOWN')})"
    )
    interim = summary.get("interim_milestones") if isinstance(summary.get("interim_milestones"), dict) else {}
    m7 = interim.get("tentative_7d") if isinstance(interim.get("tentative_7d"), dict) else {}
    m14 = interim.get("intermediate_14d") if isinstance(interim.get("intermediate_14d"), dict) else {}
    if m7 or m14:
        lines.append(
            "Interim Milestones: "
            f"7d={str(m7.get('decision') or 'UNKNOWN')} "
            f"({int(m7.get('data_sufficient_days') or 0)}/{int(m7.get('target_days') or 7)}) "
            f"14d={str(m14.get('decision') or 'UNKNOWN')} "
            f"({int(m14.get('data_sufficient_days') or 0)}/{int(m14.get('target_days') or 14)})"
        )
    lines.append(
        "Condition 1 turnover/day >= baseline: "
        f"{'PASS' if bool(c1.get('pass')) else 'FAIL'} "
        f"(baseline={_as_float(c1.get('baseline_mean')):.4f}, "
        f"candidate={_as_float(c1.get('candidate_mean')):.4f}, "
        f"delta={_as_float(c1.get('delta_candidate_minus_baseline')):+.4f})"
    )
    lines.append(
        "Condition 2 median_hold shorter than baseline: "
        f"{'PASS' if bool(c2.get('pass')) else 'FAIL'} "
        f"(baseline={_as_float(c2.get('baseline_mean_sec')):.2f}s, "
        f"candidate={_as_float(c2.get('candidate_mean_sec')):.2f}s, "
        f"delta={_as_float(c2.get('delta_candidate_minus_baseline_sec')):+.2f}s)"
    )
    lines.append(
        "Condition 3 expectancy not worse than 10%: "
        f"{'PASS' if bool(c3.get('pass')) else 'FAIL'} "
        f"(baseline={_as_float(c3.get('baseline_mean')):+.6f}, "
        f"candidate={_as_float(c3.get('candidate_mean')):+.6f}, "
        f"threshold={_as_float(c3.get('ratio_threshold'), 0.9):.2f})"
    )
    lines.append(
        "Condition 4 error/halt frequency <= baseline: "
        f"{'PASS' if bool(c4.get('pass')) else 'FAIL'} "
        f"(errors/day b={_as_float(c4.get('baseline_errors_per_day')):.4f} "
        f"c={_as_float(c4.get('candidate_errors_per_day')):.4f}; "
        f"halts/day b={_as_float(c4.get('baseline_halts_per_day')):.4f} "
        f"c={_as_float(c4.get('candidate_halts_per_day')):.4f})"
    )
    lines.append(
        f"Overall: {str(result.get('decision') or 'NO_GO')} "
        f"({str(summary.get('decision_stage') or 'UNKNOWN')})"
    )
    lines.append(f"Recommended Action: {str(result.get('recommended_action') or '')}")

    tail = result.get("last_daily_rows")
    if isinstance(tail, list) and tail:
        lines.append(f"Last Daily Rows ({len(tail)}):")
        for r in tail:
            if not isinstance(r, dict):
                continue
            lines.append(
                f"- {str(r.get('since') or '')} -> {str(r.get('until') or '')} "
                f"decision={str(r.get('decision') or '').upper()} "
                f"data_sufficient={'yes' if bool(r.get('data_sufficient')) else 'no'}"
            )

    return "\n".join(lines)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    p = argparse.ArgumentParser(description="Judge Simmer 30-day observe A/B adoption decision")
    p.add_argument("--history-file", default=str(root / "logs" / "simmer-ab-daily-compare-history.jsonl"))
    p.add_argument("--min-days", type=int, default=25, help="Minimum required data-sufficient days")
    p.add_argument(
        "--expectancy-ratio-threshold",
        type=float,
        default=0.90,
        help="Condition #3 threshold when baseline expectancy > 0 (candidate >= baseline * threshold)",
    )
    p.add_argument(
        "--decision-date",
        default="2026-03-22",
        help="Final decision date in YYYY-MM-DD (default: 2026-03-22).",
    )
    p.add_argument(
        "--today",
        default="",
        help="Override local today date in YYYY-MM-DD for simulation/testing.",
    )
    p.add_argument(
        "--fail-on-final-no-go",
        action="store_true",
        help="Return non-zero when decision stage is FINAL and overall decision is not GO.",
    )
    p.add_argument("--output-file", default="", help="Optional text output path")
    p.add_argument("--output-json", default="", help="Optional JSON output path")
    args = p.parse_args()

    decision_date = _parse_date(args.decision_date)
    if decision_date is None:
        print(f"Invalid --decision-date: {args.decision_date!r} (expected YYYY-MM-DD)")
        return 2
    if str(args.today or "").strip():
        today = _parse_date(args.today)
        if today is None:
            print(f"Invalid --today: {args.today!r} (expected YYYY-MM-DD)")
            return 2
    else:
        today = dt.date.today()

    rows = _load_history(args.history_file)
    result = judge(
        rows=rows,
        min_days=max(1, int(args.min_days)),
        expectancy_ratio_threshold=float(args.expectancy_ratio_threshold),
        decision_date=decision_date,
        today=today,
    )
    text = render_text(result)
    print(text)

    if args.output_file:
        out = Path(args.output_file)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")

    if args.output_json:
        outj = Path(args.output_json)
        outj.parent.mkdir(parents=True, exist_ok=True)
        outj.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.fail_on_final_no_go:
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        stage = str(summary.get("decision_stage") or "")
        decision = str(result.get("decision") or "")
        if stage == "FINAL" and decision != "GO":
            print(f"Exit Gate: FINAL_NO_GO (decision={decision})")
            return 6

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
