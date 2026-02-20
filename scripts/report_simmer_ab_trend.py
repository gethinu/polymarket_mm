#!/usr/bin/env python3
"""
Summarize A/B compare trend from daily history JSONL.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path


def _parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(str(s or "").strip(), "%Y-%m-%d %H:%M:%S")


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


def _record_until_ts(r: dict) -> dt.datetime | None:
    try:
        return _parse_ts(str(r.get("until") or ""))
    except Exception:
        return None


def build_report(rows: list[dict], days: float, last: int) -> str:
    now = dt.datetime.now()
    cutoff = now - dt.timedelta(days=float(days))
    filtered: list[dict] = []
    for r in rows:
        u = _record_until_ts(r)
        if u is None:
            continue
        if u >= cutoff:
            filtered.append(r)
    filtered.sort(key=lambda x: str(x.get("until") or ""))

    lines: list[str] = []
    lines.append(
        f"A/B Trend Window: {cutoff:%Y-%m-%d %H:%M:%S} -> {now:%Y-%m-%d %H:%M:%S} (local) | rows={len(filtered)}"
    )
    if not filtered:
        lines.append("No history rows in range.")
        return "\n".join(lines)

    passes = sum(1 for r in filtered if str(r.get("decision") or "").upper() == "PASS")
    fails = sum(1 for r in filtered if str(r.get("decision") or "").upper() == "FAIL")
    insufficient = sum(1 for r in filtered if str(r.get("decision") or "").upper() == "INSUFFICIENT")
    decidable = [r for r in filtered if str(r.get("decision") or "").upper() in {"PASS", "FAIL"}]

    d_turn = [float((r.get("deltas") or {}).get("turnover_per_day") or 0.0) for r in filtered]
    d_hold = [float((r.get("deltas") or {}).get("median_hold_sec") or 0.0) for r in filtered]
    d_exp = [float((r.get("deltas") or {}).get("expectancy_est") or 0.0) for r in filtered]
    d_err = [int((r.get("deltas") or {}).get("errors") or 0) for r in filtered]
    d_halt = [int((r.get("deltas") or {}).get("halts") or 0) for r in filtered]

    denom = max(1, passes + fails)
    lines.append(
        f"Decision: PASS={passes} FAIL={fails} INSUFFICIENT={insufficient} pass_rate={(100.0 * passes / denom):.1f}%"
    )
    lines.append(
        "Mean Delta(C-B): "
        f"turnover/day={_mean(d_turn):+.4f} "
        f"median_hold_sec={_mean(d_hold):+.2f} "
        f"expectancy={_mean(d_exp):+.4f} "
        f"errors={_mean([float(x) for x in d_err]):+.2f} "
        f"halts={_mean([float(x) for x in d_halt]):+.2f}"
    )

    fail_turnover = sum(1 for r in decidable if not bool((r.get("gates") or {}).get("turnover", False)))
    fail_hold = sum(1 for r in decidable if not bool((r.get("gates") or {}).get("median_hold", False)))
    fail_expectancy = sum(1 for r in decidable if not bool((r.get("gates") or {}).get("expectancy", False)))
    fail_stability = sum(1 for r in decidable if not bool((r.get("gates") or {}).get("stability", False)))
    lines.append(
        f"Gate Fail Counts: turnover={fail_turnover} hold={fail_hold} expectancy={fail_expectancy} stability={fail_stability}"
    )

    tail = filtered[-max(1, int(last)) :]
    lines.append(f"Last {len(tail)}:")
    for r in tail:
        since = str(r.get("since") or "")
        until = str(r.get("until") or "")
        decision = str(r.get("decision") or "").upper()
        d = r.get("deltas") or {}
        lines.append(
            f"- {since} -> {until} {decision} "
            f"d_turn={float(d.get('turnover_per_day') or 0.0):+.3f} "
            f"d_hold={float(d.get('median_hold_sec') or 0.0):+.1f}s "
            f"d_exp={float(d.get('expectancy_est') or 0.0):+.4f}"
        )

    return "\n".join(lines)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    p = argparse.ArgumentParser(description="Summarize Simmer A/B daily compare trend")
    p.add_argument("--history-file", default=str(root / "logs" / "simmer-ab-daily-compare-history.jsonl"))
    p.add_argument("--days", type=float, default=30.0, help="Lookback window in days")
    p.add_argument("--last", type=int, default=14, help="Show last N rows")
    p.add_argument("--output-file", default="", help="Optional output file path")
    args = p.parse_args()

    rows = _load_history(args.history_file)
    report = build_report(rows=rows, days=float(args.days), last=max(1, int(args.last)))
    print(report)

    if args.output_file:
        out = Path(args.output_file)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
