#!/usr/bin/env python3
"""
Estimate practical monthly return range from weather CLOB arb observe logs.

Observe-only analytics helper:
  - parses "summary(XXs)" lines from clob weather observe logs
  - computes thresholded opportunity stats (rows + contiguous positive segments)
  - projects monthly return under configurable capture ratios
  - outputs deterministic JSON/TXT artifacts under logs/
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional


SUMMARY_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] "
    r"summary\((?P<window>\d+)s\): "
    r"candidates=(?P<candidates>\d+) \| "
    r"EDGE \$?(?P<edge>-?\d+(?:\.\d+)?) "
    r"\((?P<edge_pct>-?\d+(?:\.\d+)?)%\) \| "
    r"cost \$?(?P<cost>\d+(?:\.\d+)?) \| "
    r"payout \$?(?P<payout>\d+(?:\.\d+)?) \| "
    r"legs=(?P<legs>\d+) \| "
    r"(?P<title>.*)$"
)


@dataclass(frozen=True)
class SummaryRow:
    ts: dt.datetime
    window_sec: int
    candidates: int
    edge_usd: float
    edge_pct: float
    cost: float
    payout: float
    legs: int
    title: str


@dataclass(frozen=True)
class Segment:
    event: str
    sign: str
    start_ts: dt.datetime
    end_ts: dt.datetime
    count: int
    edge_usd_min: float
    edge_usd_max: float
    edge_usd_mean: float
    edge_pct_mean: float
    cost_mean: float


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def now_local() -> dt.datetime:
    return dt.datetime.now()


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_mean(values: List[float], default: float = 0.0) -> float:
    if not values:
        return default
    return float(sum(values)) / float(len(values))


def safe_median(values: List[float], default: float = 0.0) -> float:
    if not values:
        return default
    return float(statistics.median(values))


def percentile(values: List[float], p: float, default: float = 0.0) -> float:
    if not values:
        return default
    xs = sorted(values)
    p = max(0.0, min(1.0, float(p)))
    idx = p * float(len(xs) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(xs[lo])
    w = idx - float(lo)
    return float(xs[lo] * (1.0 - w) + xs[hi] * w)


def sign_of_edge(edge_usd: float) -> str:
    if edge_usd > 0:
        return "pos"
    if edge_usd < 0:
        return "neg"
    return "zero"


def iter_rows(lines: Iterable[str]) -> Iterable[SummaryRow]:
    for line in lines:
        m = SUMMARY_RE.match(line.strip())
        if not m:
            continue
        try:
            yield SummaryRow(
                ts=parse_ts(m.group("ts")),
                window_sec=int(m.group("window")),
                candidates=int(m.group("candidates")),
                edge_usd=float(m.group("edge")),
                edge_pct=float(m.group("edge_pct")) / 100.0,
                cost=float(m.group("cost")),
                payout=float(m.group("payout")),
                legs=int(m.group("legs")),
                title=m.group("title"),
            )
        except Exception:
            continue


def parse_float_csv(raw: str) -> List[float]:
    vals: List[float] = []
    for tok in str(raw or "").split(","):
        s = tok.strip()
        if not s:
            continue
        vals.append(float(s))
    return vals


def parse_thresholds_cents(raw: str) -> List[float]:
    xs = parse_float_csv(raw)
    return sorted(set(float(x) / 100.0 for x in xs))


def parse_capture_ratios(raw: str) -> List[float]:
    xs = parse_float_csv(raw)
    out = sorted(set(max(0.0, min(1.0, float(x))) for x in xs))
    return out


def build_segments(rows: List[SummaryRow]) -> List[Segment]:
    if not rows:
        return []

    rows = sorted(rows, key=lambda r: r.ts)
    out: List[Segment] = []

    cur_event = rows[0].title
    cur_sign = sign_of_edge(rows[0].edge_usd)
    start_ts = rows[0].ts
    end_ts = rows[0].ts
    edge_usd_vals: List[float] = [rows[0].edge_usd]
    edge_pct_vals: List[float] = [rows[0].edge_pct]
    cost_vals: List[float] = [rows[0].cost]

    def flush() -> None:
        nonlocal out, cur_event, cur_sign, start_ts, end_ts, edge_usd_vals, edge_pct_vals, cost_vals
        if not edge_usd_vals:
            return
        out.append(
            Segment(
                event=cur_event,
                sign=cur_sign,
                start_ts=start_ts,
                end_ts=end_ts,
                count=len(edge_usd_vals),
                edge_usd_min=min(edge_usd_vals),
                edge_usd_max=max(edge_usd_vals),
                edge_usd_mean=safe_mean(edge_usd_vals),
                edge_pct_mean=safe_mean(edge_pct_vals),
                cost_mean=safe_mean(cost_vals),
            )
        )

    for r in rows[1:]:
        sign = sign_of_edge(r.edge_usd)
        if r.title == cur_event and sign == cur_sign:
            end_ts = r.ts
            edge_usd_vals.append(r.edge_usd)
            edge_pct_vals.append(r.edge_pct)
            cost_vals.append(r.cost)
            continue

        flush()
        cur_event = r.title
        cur_sign = sign
        start_ts = r.ts
        end_ts = r.ts
        edge_usd_vals = [r.edge_usd]
        edge_pct_vals = [r.edge_pct]
        cost_vals = [r.cost]

    flush()
    return out


def project_monthly_return(
    edge_usd_per_trade: float,
    opportunities_per_day: float,
    capture_ratio: float,
    days_per_month: float,
    assumed_bankroll_usd: float,
) -> dict:
    if edge_usd_per_trade <= 0.0:
        return {
            "monthly_profit_usd": 0.0,
            "monthly_return": 0.0,
            "monthly_return_pct": 0.0,
        }
    if opportunities_per_day <= 0.0:
        return {
            "monthly_profit_usd": 0.0,
            "monthly_return": 0.0,
            "monthly_return_pct": 0.0,
        }
    c = max(0.0, min(1.0, float(capture_ratio)))
    trades_per_month = max(0.0, float(opportunities_per_day) * float(days_per_month))
    expected_profit_per_trade = float(edge_usd_per_trade) * c
    monthly_profit_usd = expected_profit_per_trade * trades_per_month
    if float(assumed_bankroll_usd) <= 0.0:
        monthly_return = 0.0
    else:
        monthly_return = monthly_profit_usd / float(assumed_bankroll_usd)
    return {
        "monthly_profit_usd": float(monthly_profit_usd),
        "monthly_return": float(monthly_return),
        "monthly_return_pct": float(monthly_return) * 100.0,
    }


def threshold_stats(
    rows: List[SummaryRow],
    segments: List[Segment],
    threshold_usd: float,
    capture_ratios: List[float],
    base_capture_ratio: float,
    span_days: float,
    max_assumed_trades_per_day: float,
    days_per_month: float,
    assumed_bankroll_usd: float,
) -> dict:
    row_hits = [r for r in rows if r.edge_usd >= threshold_usd]
    row_hit_ratio = (float(len(row_hits)) / float(len(rows))) if rows else 0.0
    row_unique_events = len({r.title for r in row_hits})

    seg_hits = [s for s in segments if s.sign == "pos" and s.edge_usd_max >= threshold_usd]
    seg_unique_events = len({s.event for s in seg_hits})
    opp_per_day_raw = (float(len(seg_hits)) / span_days) if span_days > 0 else 0.0
    opp_per_day_capped = min(opp_per_day_raw, float(max_assumed_trades_per_day))

    edge_pct_mean = safe_mean([s.edge_pct_mean for s in seg_hits], default=0.0)
    edge_pct_p50 = safe_median([s.edge_pct_mean for s in seg_hits], default=0.0)
    edge_pct_p90 = percentile([s.edge_pct_mean for s in seg_hits], 0.90, default=0.0)
    edge_usd_mean = safe_mean([s.edge_usd_mean for s in seg_hits], default=0.0)
    cost_mean = safe_mean([s.cost_mean for s in seg_hits], default=0.0)

    scenarios: List[dict] = []
    for c in capture_ratios:
        proj = project_monthly_return(
            edge_usd_per_trade=edge_usd_mean,
            opportunities_per_day=opp_per_day_capped,
            capture_ratio=c,
            days_per_month=days_per_month,
            assumed_bankroll_usd=assumed_bankroll_usd,
        )
        scenarios.append(
            {
                "capture_ratio": float(c),
                "monthly_profit_usd": float(proj["monthly_profit_usd"]),
                "monthly_return": float(proj["monthly_return"]),
                "monthly_return_pct": float(proj["monthly_return_pct"]),
            }
        )

    if scenarios:
        base = min(scenarios, key=lambda x: abs(float(x["capture_ratio"]) - float(base_capture_ratio)))
    else:
        base = {"capture_ratio": float(base_capture_ratio), "monthly_return": 0.0, "monthly_return_pct": 0.0}

    return {
        "threshold_usd": float(threshold_usd),
        "threshold_cents": float(threshold_usd) * 100.0,
        "rows_hit": len(row_hits),
        "rows_total": len(rows),
        "rows_hit_ratio": float(row_hit_ratio),
        "row_unique_events": int(row_unique_events),
        "segments_hit": len(seg_hits),
        "segments_unique_events": int(seg_unique_events),
        "opportunities_per_day_raw": float(opp_per_day_raw),
        "opportunities_per_day_capped": float(opp_per_day_capped),
        "segment_edge_pct_mean": float(edge_pct_mean),
        "segment_edge_pct_p50": float(edge_pct_p50),
        "segment_edge_pct_p90": float(edge_pct_p90),
        "segment_edge_usd_mean": float(edge_usd_mean),
        "segment_cost_mean": float(cost_mean),
        "scenarios": scenarios,
        "base_scenario": base,
    }


def choose_threshold(
    stats: List[dict],
    min_opportunities_per_day: float,
    min_unique_events: int,
    min_rows_hit_ratio: float,
) -> tuple[Optional[dict], bool]:
    if not stats:
        return None, False

    qualified = [
        s
        for s in stats
        if float(s.get("opportunities_per_day_capped") or 0.0) >= float(min_opportunities_per_day)
        and int(s.get("segments_unique_events") or 0) >= int(min_unique_events)
        and float(s.get("rows_hit_ratio") or 0.0) >= float(min_rows_hit_ratio)
    ]

    pool = qualified if qualified else stats
    selected = max(
        pool,
        key=lambda s: (
            float((s.get("base_scenario") or {}).get("monthly_return") or 0.0),
            float(s.get("opportunities_per_day_capped") or 0.0),
            float(s.get("threshold_usd") or 0.0),
        ),
    )
    return selected, bool(qualified)


def summarize_top_events(rows: List[SummaryRow], top_n: int = 8) -> List[dict]:
    by_event: dict[str, List[SummaryRow]] = {}
    for r in rows:
        by_event.setdefault(r.title, []).append(r)
    items: List[dict] = []
    for event, xs in by_event.items():
        edge_pcts = [x.edge_pct for x in xs]
        edge_usds = [x.edge_usd for x in xs]
        pos_count = len([x for x in xs if x.edge_usd > 0.0])
        items.append(
            {
                "event": event,
                "samples": len(xs),
                "positive_samples": pos_count,
                "positive_ratio": (float(pos_count) / float(len(xs))) if xs else 0.0,
                "best_edge_pct": max(edge_pcts) if edge_pcts else 0.0,
                "median_edge_pct": safe_median(edge_pcts, 0.0),
                "mean_edge_pct": safe_mean(edge_pcts, 0.0),
                "best_edge_usd": max(edge_usds) if edge_usds else 0.0,
            }
        )
    items.sort(
        key=lambda x: (
            float(x["best_edge_pct"]),
            float(x["positive_ratio"]),
            int(x["samples"]),
        ),
        reverse=True,
    )
    return items[: max(1, int(top_n))]


def build_text_report(result: dict) -> str:
    window = result.get("window") or {}
    summary = result.get("summary") or {}
    decision = result.get("decision") or {}
    selected = result.get("selected_threshold") or {}
    base = selected.get("base_scenario") if isinstance(selected, dict) else None

    lines: List[str] = []
    lines.append("Weather Arb Profit Window")
    lines.append(
        f"Observed: {window.get('start_local')} -> {window.get('end_local')} | "
        f"span_hours={float(window.get('span_hours') or 0.0):.2f}"
    )
    lines.append(
        f"Rows={int(summary.get('rows') or 0)} | events={int(summary.get('unique_events') or 0)} | "
        f"positive_rows={float(summary.get('positive_rows_ratio') or 0.0) * 100.0:.1f}%"
    )
    lines.append(
        f"Edge pct median={float(summary.get('edge_pct_median') or 0.0) * 100.0:.2f}% "
        f"p90={float(summary.get('edge_pct_p90') or 0.0) * 100.0:.2f}% "
        f"max={float(summary.get('edge_pct_max') or 0.0) * 100.0:.2f}%"
    )

    if selected:
        lines.append("Selected threshold:")
        lines.append(
            f"  >= {float(selected.get('threshold_cents') or 0.0):.2f}c | "
            f"opp/day(capped)={float(selected.get('opportunities_per_day_capped') or 0.0):.2f} | "
            f"segment_edge_mean={float(selected.get('segment_edge_pct_mean') or 0.0) * 100.0:.2f}% | "
            f"unique_events={int(selected.get('segments_unique_events') or 0)}"
        )
        if isinstance(base, dict):
            lines.append(
                f"  base capture={float(base.get('capture_ratio') or 0.0) * 100.0:.0f}% "
                f"=> projected monthly={float(base.get('monthly_return_pct') or 0.0):+.2f}%"
            )
        sc = selected.get("scenarios") if isinstance(selected, dict) else []
        if isinstance(sc, list) and sc:
            lines.append("  scenarios:")
            for x in sc:
                lines.append(
                    f"    capture {float(x.get('capture_ratio') or 0.0) * 100.0:.0f}% "
                    f"=> monthly {float(x.get('monthly_return_pct') or 0.0):+.2f}%"
                )

    lines.append(
        f"Decision: {decision.get('decision', 'NO_GO')} | "
        f"target_monthly={float((decision.get('target_monthly_return') or 0.0) * 100.0):.2f}%"
    )
    reasons = decision.get("reasons") if isinstance(decision.get("reasons"), list) else []
    for r in reasons:
        lines.append(f"  - {str(r)}")
    return "\n".join(lines)


def resolve_path(root: Path, raw: str, default_rel: str) -> Path:
    p = Path(raw.strip()) if str(raw or "").strip() else Path(default_rel)
    if p.is_absolute():
        return p
    return root / p


def main() -> int:
    p = argparse.ArgumentParser(description="Estimate weather arb monthly return range from observe logs (observe-only).")
    p.add_argument("--log-file", default="logs/clob-arb-weather-observe.log")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--since", default="", help='Local time "YYYY-MM-DD HH:MM:SS" (overrides --hours)')
    p.add_argument("--until", default="", help='Local time "YYYY-MM-DD HH:MM:SS" (default now)')
    p.add_argument("--thresholds-cents", default="1,1.5,2,3,4")
    p.add_argument("--capture-ratios", default="0.25,0.35,0.50")
    p.add_argument("--base-capture-ratio", type=float, default=0.35)
    p.add_argument("--days-per-month", type=float, default=30.0)
    p.add_argument("--assumed-bankroll-usd", type=float, default=100.0)
    p.add_argument("--max-assumed-trades-per-day", type=float, default=8.0)
    p.add_argument("--target-monthly-return-pct", type=float, default=15.0)
    p.add_argument("--min-span-hours", type=float, default=6.0)
    p.add_argument("--min-rows", type=int, default=120)
    p.add_argument("--min-positive-rows-pct", type=float, default=30.0)
    p.add_argument("--min-opportunities-per-day", type=float, default=2.0)
    p.add_argument("--min-unique-events", type=int, default=4)
    p.add_argument("--min-rows-hit-pct", type=float, default=5.0)
    p.add_argument("--out-json", default="logs/weather_arb_profit_window_latest.json")
    p.add_argument("--out-txt", default="logs/weather_arb_profit_window_latest.txt")
    p.add_argument("--pretty", action="store_true")
    args = p.parse_args()

    root = Path(__file__).resolve().parents[1]
    log_file = resolve_path(root, args.log_file, "logs/clob-arb-weather-observe.log")
    out_json = resolve_path(root, args.out_json, "logs/weather_arb_profit_window_latest.json")
    out_txt = resolve_path(root, args.out_txt, "logs/weather_arb_profit_window_latest.txt")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_txt.parent.mkdir(parents=True, exist_ok=True)

    if not log_file.exists():
        print(f"log file not found: {log_file}")
        return 2

    until = parse_ts(args.until) if str(args.until).strip() else now_local()
    since = parse_ts(args.since) if str(args.since).strip() else (until - dt.timedelta(hours=float(args.hours)))

    thresholds_usd = parse_thresholds_cents(args.thresholds_cents)
    if not thresholds_usd:
        thresholds_usd = [0.01, 0.015, 0.02, 0.03, 0.04]
    capture_ratios = parse_capture_ratios(args.capture_ratios)
    if not capture_ratios:
        capture_ratios = [0.25, 0.35, 0.5]

    rows_all = list(iter_rows(log_file.read_text(encoding="utf-8", errors="replace").splitlines()))
    rows = sorted([r for r in rows_all if since <= r.ts <= until], key=lambda r: r.ts)
    segments = build_segments(rows)

    if rows:
        span_sec = max(0.0, (rows[-1].ts - rows[0].ts).total_seconds())
        span_hours = span_sec / 3600.0
        span_days = span_sec / 86400.0
    else:
        span_hours = 0.0
        span_days = 0.0

    edges_pct = [r.edge_pct for r in rows]
    edges_usd = [r.edge_usd for r in rows]
    pos_rows = [r for r in rows if r.edge_usd > 0.0]
    neg_rows = [r for r in rows if r.edge_usd < 0.0]
    unique_events = len({r.title for r in rows})

    tstats = [
        threshold_stats(
            rows=rows,
            segments=segments,
            threshold_usd=t,
            capture_ratios=capture_ratios,
            base_capture_ratio=float(args.base_capture_ratio),
            span_days=span_days,
            max_assumed_trades_per_day=float(args.max_assumed_trades_per_day),
            days_per_month=float(args.days_per_month),
            assumed_bankroll_usd=float(args.assumed_bankroll_usd),
        )
        for t in thresholds_usd
    ]

    selected, selected_qualified = choose_threshold(
        stats=tstats,
        min_opportunities_per_day=float(args.min_opportunities_per_day),
        min_unique_events=int(args.min_unique_events),
        min_rows_hit_ratio=float(args.min_rows_hit_pct) / 100.0,
    )

    target_monthly = float(args.target_monthly_return_pct) / 100.0
    min_positive_rows_ratio = float(args.min_positive_rows_pct) / 100.0

    decision_reasons: List[str] = []
    hard_ok = True

    if span_hours < float(args.min_span_hours):
        hard_ok = False
        decision_reasons.append(
            f"span_hours {span_hours:.2f} < min_span_hours {float(args.min_span_hours):.2f}"
        )
    else:
        decision_reasons.append(
            f"span_hours {span_hours:.2f} >= min_span_hours {float(args.min_span_hours):.2f}"
        )

    if len(rows) < int(args.min_rows):
        hard_ok = False
        decision_reasons.append(f"rows {len(rows)} < min_rows {int(args.min_rows)}")
    else:
        decision_reasons.append(f"rows {len(rows)} >= min_rows {int(args.min_rows)}")

    pos_ratio = (float(len(pos_rows)) / float(len(rows))) if rows else 0.0
    if pos_ratio < min_positive_rows_ratio:
        hard_ok = False
        decision_reasons.append(
            f"positive_rows_ratio {pos_ratio * 100.0:.1f}% < min_positive_rows_pct {float(args.min_positive_rows_pct):.1f}%"
        )
    else:
        decision_reasons.append(
            f"positive_rows_ratio {pos_ratio * 100.0:.1f}% >= min_positive_rows_pct {float(args.min_positive_rows_pct):.1f}%"
        )

    if unique_events < int(args.min_unique_events):
        hard_ok = False
        decision_reasons.append(
            f"unique_events {unique_events} < min_unique_events {int(args.min_unique_events)}"
        )
    else:
        decision_reasons.append(
            f"unique_events {unique_events} >= min_unique_events {int(args.min_unique_events)}"
        )

    if not selected:
        hard_ok = False
        decision_reasons.append("no threshold stats available")
        selected_base_monthly = 0.0
    else:
        base = selected.get("base_scenario") if isinstance(selected.get("base_scenario"), dict) else {}
        selected_base_monthly = float(base.get("monthly_return") or 0.0)
        if not selected_qualified:
            hard_ok = False
            decision_reasons.append("selected threshold failed opportunity/event/hit-ratio qualifiers")
        else:
            decision_reasons.append("selected threshold passed opportunity/event/hit-ratio qualifiers")
        if selected_base_monthly < target_monthly:
            hard_ok = False
            decision_reasons.append(
                f"projected_monthly {selected_base_monthly * 100.0:.2f}% < target {target_monthly * 100.0:.2f}%"
            )
        else:
            decision_reasons.append(
                f"projected_monthly {selected_base_monthly * 100.0:.2f}% >= target {target_monthly * 100.0:.2f}%"
            )

    decision = "GO" if hard_ok else "NO_GO"

    result = {
        "generated_utc": now_utc_iso(),
        "meta": {
            "observe_only": True,
            "source": "scripts/report_weather_arb_profit_window.py",
            "log_file": str(log_file),
        },
        "window": {
            "since_local": since.strftime("%Y-%m-%d %H:%M:%S"),
            "until_local": until.strftime("%Y-%m-%d %H:%M:%S"),
            "start_local": rows[0].ts.strftime("%Y-%m-%d %H:%M:%S") if rows else None,
            "end_local": rows[-1].ts.strftime("%Y-%m-%d %H:%M:%S") if rows else None,
            "span_hours": float(span_hours),
            "span_days": float(span_days),
        },
        "settings": {
            "thresholds_cents": [float(t) * 100.0 for t in thresholds_usd],
            "capture_ratios": [float(c) for c in capture_ratios],
            "base_capture_ratio": float(args.base_capture_ratio),
            "days_per_month": float(args.days_per_month),
            "assumed_bankroll_usd": float(args.assumed_bankroll_usd),
            "max_assumed_trades_per_day": float(args.max_assumed_trades_per_day),
            "target_monthly_return_pct": float(args.target_monthly_return_pct),
            "min_span_hours": float(args.min_span_hours),
            "min_rows": int(args.min_rows),
            "min_positive_rows_pct": float(args.min_positive_rows_pct),
            "min_opportunities_per_day": float(args.min_opportunities_per_day),
            "min_unique_events": int(args.min_unique_events),
            "min_rows_hit_pct": float(args.min_rows_hit_pct),
        },
        "summary": {
            "rows": len(rows),
            "segments": len(segments),
            "unique_events": unique_events,
            "positive_rows": len(pos_rows),
            "negative_rows": len(neg_rows),
            "positive_rows_ratio": float(pos_ratio),
            "edge_usd_min": min(edges_usd) if edges_usd else 0.0,
            "edge_usd_p50": safe_median(edges_usd, 0.0),
            "edge_usd_p90": percentile(edges_usd, 0.90, 0.0),
            "edge_usd_max": max(edges_usd) if edges_usd else 0.0,
            "edge_pct_median": safe_median(edges_pct, 0.0),
            "edge_pct_p90": percentile(edges_pct, 0.90, 0.0),
            "edge_pct_max": max(edges_pct) if edges_pct else 0.0,
            "candidates_mean": safe_mean([r.candidates for r in rows], 0.0),
        },
        "threshold_stats": tstats,
        "selected_threshold": selected,
        "selected_threshold_qualified": bool(selected_qualified),
        "top_events": summarize_top_events(rows),
        "decision": {
            "decision": decision,
            "target_monthly_return": float(target_monthly),
            "projected_monthly_return": float(selected_base_monthly),
            "reasons": decision_reasons,
        },
        "artifacts": {
            "out_json": str(out_json),
            "out_txt": str(out_txt),
        },
    }

    text = build_text_report(result)
    out_txt.write_text(text + "\n", encoding="utf-8")
    if args.pretty:
        out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        out_json.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"[weather-profit] decision={decision} projected_monthly={selected_base_monthly * 100.0:+.2f}%")
    print(f"[weather-profit] wrote {out_json}")
    print(f"[weather-profit] wrote {out_txt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
