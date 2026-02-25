#!/usr/bin/env python3
"""
Analyze CLOB arb metrics and extract sports market-type / time-window quality.

Observe-only analytics helper:
- groups rows by inferred sports market type (draw/total/moneyline/spread/btts/other)
- reports edge stats per type
- finds time-of-day windows where positive (or near-zero) edges clustered
- emits a recommended allowlist for --sports-market-types
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


VALID_TYPES = {"moneyline", "spread", "total", "draw", "btts", "other", "non_sports_or_other"}


def _parse_ts(s: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.strptime(str(s or "").strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _norm_type(v: str) -> str:
    s = str(v or "").strip().lower()
    if s in VALID_TYPES:
        return s
    if s in {"winner", "win"}:
        return "moneyline"
    if s in {"ou", "totals"}:
        return "total"
    if s in {"handicap"}:
        return "spread"
    return ""


def _infer_type_from_title(title: str) -> str:
    q = str(title or "").lower()
    if "both teams to score" in q or "btts" in q:
        return "btts"
    if "draw" in q or "end in a draw" in q:
        return "draw"
    if "o/u" in q or "over/under" in q:
        return "total"
    if "spread:" in q or "handicap" in q:
        return "spread"
    if "moneyline" in q or " to win" in q or " vs. " in q or " vs " in q or " @ " in q or " at " in q:
        return "moneyline"
    return "non_sports_or_other"


def _bucket_minutes(ts: dt.datetime, bucket_minutes: int) -> str:
    step = max(1, int(bucket_minutes))
    m = (ts.minute // step) * step
    return f"{ts.hour:02d}:{m:02d}"


def _mean(xs: Iterable[float]) -> float:
    vals = list(xs)
    return float(sum(vals) / len(vals)) if vals else 0.0


def _pctl(xs: List[float], p: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    i = min(len(ys) - 1, max(0, int(round((len(ys) - 1) * p))))
    return float(ys[i])


def load_rows(path: Path, since: Optional[dt.datetime], until: Optional[dt.datetime], bucket_minutes: int) -> List[dict]:
    rows: List[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                o = json.loads(s)
            except Exception:
                continue

            ts = _parse_ts(o.get("ts"))
            if ts is None:
                continue
            if since and ts < since:
                continue
            if until and ts > until:
                continue

            edge = float(o.get("net_edge_raw") or 0.0)
            title = str(o.get("title") or "")
            t = _norm_type(o.get("sports_market_type") or "")
            if not t:
                t = _infer_type_from_title(title)

            rows.append(
                {
                    "ts": ts,
                    "bucket": _bucket_minutes(ts, bucket_minutes),
                    "type": t if t else "non_sports_or_other",
                    "edge": edge,
                    "title": title,
                }
            )
    return rows


def build_summary(rows: List[dict], near_zero_floor: float, min_bucket_samples: int, recommend_top_n: int) -> dict:
    out: dict = {}
    out["rows"] = int(len(rows))
    if not rows:
        return out

    sports_types = {"moneyline", "spread", "total", "draw", "btts"}
    per_type_edges: Dict[str, List[float]] = defaultdict(list)
    per_bucket_edges: Dict[str, List[float]] = defaultdict(list)
    per_type_bucket_edges: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    positives = 0
    near_zero = 0

    for r in rows:
        t = str(r["type"])
        e = float(r["edge"])
        b = str(r["bucket"])
        per_type_edges[t].append(e)
        per_bucket_edges[b].append(e)
        per_type_bucket_edges[(t, b)].append(e)
        if e > 0:
            positives += 1
        if e >= near_zero_floor:
            near_zero += 1

    type_stats: List[dict] = []
    for t, xs in per_type_edges.items():
        xs_sorted = sorted(xs)
        n = len(xs_sorted)
        type_stats.append(
            {
                "type": t,
                "samples": int(n),
                "mean_edge": float(_mean(xs_sorted)),
                "median_edge": float(_pctl(xs_sorted, 0.5)),
                "p90_edge": float(_pctl(xs_sorted, 0.9)),
                "best_edge": float(xs_sorted[-1]),
                "worst_edge": float(xs_sorted[0]),
                "positive_count": int(sum(1 for x in xs_sorted if x > 0)),
                "near_zero_count": int(sum(1 for x in xs_sorted if x >= near_zero_floor)),
                "near_zero_rate": float(sum(1 for x in xs_sorted if x >= near_zero_floor) / n),
            }
        )
    type_stats.sort(key=lambda x: (x["mean_edge"], x["best_edge"]), reverse=True)

    bucket_stats: List[dict] = []
    for b, xs in per_bucket_edges.items():
        if len(xs) < max(1, int(min_bucket_samples)):
            continue
        bucket_stats.append(
            {
                "bucket": b,
                "samples": int(len(xs)),
                "mean_edge": float(_mean(xs)),
                "best_edge": float(max(xs)),
                "positive_count": int(sum(1 for x in xs if x > 0)),
                "near_zero_count": int(sum(1 for x in xs if x >= near_zero_floor)),
            }
        )
    bucket_stats.sort(key=lambda x: (x["positive_count"], x["best_edge"], x["mean_edge"]), reverse=True)

    type_bucket_stats: List[dict] = []
    for (t, b), xs in per_type_bucket_edges.items():
        if t not in sports_types:
            continue
        if len(xs) < max(1, int(min_bucket_samples)):
            continue
        type_bucket_stats.append(
            {
                "type": t,
                "bucket": b,
                "samples": int(len(xs)),
                "mean_edge": float(_mean(xs)),
                "best_edge": float(max(xs)),
                "positive_count": int(sum(1 for x in xs if x > 0)),
                "near_zero_count": int(sum(1 for x in xs if x >= near_zero_floor)),
            }
        )
    type_bucket_stats.sort(
        key=lambda x: (x["positive_count"], x["near_zero_count"], x["best_edge"], x["mean_edge"]),
        reverse=True,
    )

    sports_ranked = [x for x in type_stats if x["type"] in sports_types]
    recommended_types = [x["type"] for x in sports_ranked[: max(1, int(recommend_top_n))]]

    out["positives"] = int(positives)
    out["near_zero_floor"] = float(near_zero_floor)
    out["near_zero_count"] = int(near_zero)
    out["near_zero_rate"] = float(near_zero / len(rows))
    out["type_stats"] = type_stats
    out["bucket_stats_top10"] = bucket_stats[:10]
    out["type_bucket_stats_top15"] = type_bucket_stats[:15]
    out["recommended_market_types"] = recommended_types
    return out


def print_report(summary: dict) -> None:
    rows = int(summary.get("rows") or 0)
    if rows <= 0:
        print("No rows in selected window.")
        return

    print(f"Rows: {rows}")
    print(
        "Edge hits: "
        f"positive={int(summary.get('positives') or 0)} "
        f"near_zero(>= {float(summary.get('near_zero_floor') or 0.0):+.4f})="
        f"{int(summary.get('near_zero_count') or 0)} "
        f"rate={float(summary.get('near_zero_rate') or 0.0):.2%}"
    )

    print("")
    print("By market type:")
    for row in summary.get("type_stats") or []:
        print(
            "  "
            f"{str(row.get('type')):>18} "
            f"n={int(row.get('samples') or 0):>5d} "
            f"mean={float(row.get('mean_edge') or 0.0):>+8.4f} "
            f"best={float(row.get('best_edge') or 0.0):>+8.4f} "
            f"near0={int(row.get('near_zero_count') or 0):>5d} "
            f"pos={int(row.get('positive_count') or 0):>5d}"
        )

    print("")
    print("Top time buckets (all types):")
    for row in summary.get("bucket_stats_top10") or []:
        print(
            "  "
            f"{str(row.get('bucket'))} "
            f"n={int(row.get('samples') or 0):>4d} "
            f"mean={float(row.get('mean_edge') or 0.0):>+8.4f} "
            f"best={float(row.get('best_edge') or 0.0):>+8.4f} "
            f"near0={int(row.get('near_zero_count') or 0):>4d} "
            f"pos={int(row.get('positive_count') or 0):>4d}"
        )

    print("")
    print("Top time buckets (sports type x bucket):")
    for row in summary.get("type_bucket_stats_top15") or []:
        print(
            "  "
            f"{str(row.get('type')):>10} @ {str(row.get('bucket'))} "
            f"n={int(row.get('samples') or 0):>4d} "
            f"mean={float(row.get('mean_edge') or 0.0):>+8.4f} "
            f"best={float(row.get('best_edge') or 0.0):>+8.4f} "
            f"near0={int(row.get('near_zero_count') or 0):>4d} "
            f"pos={int(row.get('positive_count') or 0):>4d}"
        )

    rec = summary.get("recommended_market_types") or []
    print("")
    print(f"Recommended --sports-market-types: {','.join(rec) if rec else 'n/a'}")


def main() -> int:
    p = argparse.ArgumentParser(description="Analyze CLOB arb sports metrics by market type and time window")
    p.add_argument(
        "--metrics-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "clob-arb-monitor-metrics.jsonl"),
        help="Path to metrics JSONL",
    )
    p.add_argument("--hours", type=float, default=0.0, help="Lookback hours (0=all)")
    p.add_argument("--since", default="", help='Start time "YYYY-MM-DD HH:MM:SS"')
    p.add_argument("--until", default="", help='End time "YYYY-MM-DD HH:MM:SS"')
    p.add_argument(
        "--near-zero-floor",
        type=float,
        default=-0.01,
        help="Treat edge >= this USD as near-zero window when positive edges are absent",
    )
    p.add_argument("--bucket-minutes", type=int, default=15, help="Time bucket size in minutes for window stats")
    p.add_argument("--min-bucket-samples", type=int, default=10, help="Min samples per time bucket row")
    p.add_argument("--recommend-top-n", type=int, default=3, help="Number of recommended market types")
    p.add_argument("--out-json", default="", help="Optional output JSON path")
    args = p.parse_args()

    metrics_file = Path(args.metrics_file)
    if not metrics_file.exists():
        print(f"metrics file not found: {metrics_file}")
        return 1

    until = _parse_ts(args.until) if args.until else None
    since = _parse_ts(args.since) if args.since else None
    if since is None and float(args.hours or 0.0) > 0:
        end = until or dt.datetime.now()
        since = end - dt.timedelta(hours=float(args.hours))
        until = end

    rows = load_rows(path=metrics_file, since=since, until=until, bucket_minutes=int(args.bucket_minutes))
    summary = build_summary(
        rows=rows,
        near_zero_floor=float(args.near_zero_floor),
        min_bucket_samples=int(args.min_bucket_samples),
        recommend_top_n=int(args.recommend_top_n),
    )

    if rows:
        ts_min = min(r["ts"] for r in rows)
        ts_max = max(r["ts"] for r in rows)
        summary["window_start"] = ts_min.strftime("%Y-%m-%d %H:%M:%S")
        summary["window_end"] = ts_max.strftime("%Y-%m-%d %H:%M:%S")
    summary["metrics_file"] = str(metrics_file)

    print_report(summary)

    out_json = str(args.out_json or "").strip()
    if out_json:
        out_path = Path(out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, ensure_ascii=True, indent=2), encoding="utf-8")
        print(f"\nWrote: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
