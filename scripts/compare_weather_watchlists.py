#!/usr/bin/env python3
"""
Compare weather consensus watchlist against one baseline watchlist.

Observe-only helper:
  - reads watchlist JSON outputs (`top` or `rows`)
  - writes A/B dryrun report JSON/Markdown under logs/
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional


def now_iso_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_path(raw: str, default_name: str) -> Path:
    logs = repo_root() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if not raw.strip():
        return logs / default_name
    p = Path(raw)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs / p.name
    return repo_root() / p


def as_float(v: Any, default: Any = 0.0) -> Any:
    try:
        out = float(v)
    except (TypeError, ValueError):
        return default
    if isinstance(out, float) and not math.isfinite(out):
        return default
    return out


def as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def normalize_side(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if s in {"yes", "no", "none"}:
        return s
    if s in {"y", "1", "long"}:
        return "yes"
    if s in {"n", "-1", "short"}:
        return "no"
    return "none"


def first_present_float(mapping: dict, keys: List[str]) -> Optional[float]:
    for k in keys:
        if k in mapping and mapping.get(k) not in (None, ""):
            return as_float(mapping.get(k), None)
    return None


def load_rows(path: Path) -> List[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if not isinstance(raw, dict):
        raise ValueError(f"unsupported JSON type: {type(raw).__name__}")
    if isinstance(raw.get("top"), list):
        return [x for x in raw.get("top", []) if isinstance(x, dict)]
    if isinstance(raw.get("rows"), list):
        return [x for x in raw.get("rows", []) if isinstance(x, dict)]
    raise ValueError("no list field found (expected `top` or `rows`)")


def normalize_rows(rows: List[dict], top_n: int) -> List[dict]:
    out: List[dict] = []
    seen: Dict[str, int] = {}
    for i, r in enumerate(rows):
        market_id = str(r.get("market_id") or r.get("id") or r.get("token_id") or "").strip()
        question = str(r.get("question") or r.get("label") or "").strip()
        key = market_id or question or f"row:{i + 1}"
        if key in seen:
            continue
        seen[key] = 1
        out.append(
            {
                "key": key,
                "rank": as_int(r.get("rank"), i + 1),
                "market_id": market_id,
                "question": question,
                "side": normalize_side(r.get("side_hint") or r.get("side")),
                "entry_price": as_float(r.get("entry_price"), as_float(r.get("no_price"), 0.0)),
                "yield_per_day": first_present_float(r, ["net_yield_per_day", "gross_yield_per_day"]),
                "liquidity": as_float(r.get("liquidity_num"), 0.0),
                "volume_24h": as_float(r.get("volume_24h"), as_float(r.get("volume24h"), 0.0)),
                "hours_to_end": as_float(r.get("hours_to_end"), 0.0),
                "score": as_float(r.get("score_total"), 0.0),
            }
        )
    out.sort(key=lambda x: (x.get("rank") or 0))
    if top_n > 0:
        out = out[:top_n]
    return out


def summarize(values: List[float]) -> dict:
    if not values:
        return {"count": 0, "min": None, "median": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
        "mean": statistics.fmean(values),
    }


def side_mix(rows: List[dict]) -> dict:
    counts = {"yes": 0, "no": 0, "none": 0}
    for r in rows:
        counts[normalize_side(r.get("side"))] += 1
    total = max(1, sum(counts.values()))
    return {
        "counts": counts,
        "ratios": {k: (v / total) for k, v in counts.items()},
    }


def build_report(consensus_rows: List[dict], baseline_rows: List[dict], consensus_name: str, baseline_name: str) -> dict:
    c_keys = {r["key"] for r in consensus_rows}
    b_keys = {r["key"] for r in baseline_rows}
    overlap_keys = c_keys.intersection(b_keys)
    c_only_keys = c_keys.difference(b_keys)
    b_only_keys = b_keys.difference(c_keys)

    c_only = [r for r in consensus_rows if r["key"] in c_only_keys]
    b_only = [r for r in baseline_rows if r["key"] in b_only_keys]

    c_yield = summarize([float(r["yield_per_day"]) for r in consensus_rows if r.get("yield_per_day") is not None])
    b_yield = summarize([float(r["yield_per_day"]) for r in baseline_rows if r.get("yield_per_day") is not None])
    c_liq = summarize([float(r["liquidity"]) for r in consensus_rows])
    b_liq = summarize([float(r["liquidity"]) for r in baseline_rows])
    c_hours = summarize([float(r["hours_to_end"]) for r in consensus_rows])
    b_hours = summarize([float(r["hours_to_end"]) for r in baseline_rows])

    c_mix = side_mix(consensus_rows)
    b_mix = side_mix(baseline_rows)

    overlap_ratio_consensus = len(overlap_keys) / max(1, len(c_keys))
    overlap_ratio_baseline = len(overlap_keys) / max(1, len(b_keys))
    c_y_med = as_float(c_yield.get("median"), None)
    b_y_med = as_float(b_yield.get("median"), None)
    c_liq_med = as_float(c_liq.get("median"), 0.0)
    b_liq_med = as_float(b_liq.get("median"), 0.0)
    c_no_ratio = as_float((c_mix.get("ratios") or {}).get("no"), 0.0)

    positive_signals: List[str] = []
    cautions: List[str] = []

    if overlap_ratio_consensus >= 0.60:
        positive_signals.append("overlap_ratio_consensus>=0.60")
    else:
        cautions.append("overlap_ratio_consensus<0.60")

    if c_y_med is None or b_y_med is None:
        cautions.append("yield_per_day_missing_in_one_side")
    elif c_y_med >= b_y_med:
        positive_signals.append("consensus_median_yield>=baseline_median_yield")
    else:
        cautions.append("consensus_median_yield<baseline_median_yield")

    if c_liq_med >= (0.80 * b_liq_med):
        positive_signals.append("consensus_median_liquidity>=0.8x_baseline")
    else:
        cautions.append("consensus_median_liquidity<0.8x_baseline")

    if c_no_ratio > 0.95:
        cautions.append("consensus_side_concentration_no>95%")

    score = len(positive_signals)
    if score >= 3:
        assessment = "favorable"
    elif score == 2:
        assessment = "mixed"
    else:
        assessment = "weak"

    return {
        "generated_utc": now_iso_utc(),
        "assessment": assessment,
        "inputs": {
            "consensus_name": consensus_name,
            "baseline_name": baseline_name,
            "consensus_count": len(consensus_rows),
            "baseline_count": len(baseline_rows),
        },
        "overlap": {
            "count": len(overlap_keys),
            "ratio_vs_consensus": overlap_ratio_consensus,
            "ratio_vs_baseline": overlap_ratio_baseline,
            "consensus_only_count": len(c_only_keys),
            "baseline_only_count": len(b_only_keys),
        },
        "side_mix": {
            "consensus": c_mix,
            "baseline": b_mix,
        },
        "metrics": {
            "yield_per_day": {
                "consensus": c_yield,
                "baseline": b_yield,
            },
            "liquidity": {
                "consensus": c_liq,
                "baseline": b_liq,
            },
            "hours_to_end": {
                "consensus": c_hours,
                "baseline": b_hours,
            },
        },
        "signals": positive_signals,
        "cautions": cautions,
        "consensus_only_top": c_only[:10],
        "baseline_only_top": b_only[:10],
    }


def fmt_optional(v: Any, digits: int = 6) -> str:
    if v is None:
        return "N/A"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "N/A"
    if not math.isfinite(n):
        return "N/A"
    return f"{n:.{digits}f}"


def format_md(report: dict) -> str:
    inp = report.get("inputs") or {}
    ov = report.get("overlap") or {}
    m = report.get("metrics") or {}
    y = m.get("yield_per_day") or {}
    lq = m.get("liquidity") or {}

    lines: List[str] = []
    lines.append("# Weather A/B Dryrun")
    lines.append("")
    lines.append(f"- generated_utc: `{report.get('generated_utc')}`")
    lines.append(f"- assessment: `{report.get('assessment')}`")
    lines.append(f"- consensus: `{inp.get('consensus_name')}`")
    lines.append(f"- baseline: `{inp.get('baseline_name')}`")
    lines.append("")
    lines.append("## Overlap")
    lines.append(f"- overlap_count: `{ov.get('count')}`")
    lines.append(f"- overlap_ratio_vs_consensus: `{as_float(ov.get('ratio_vs_consensus')):.4f}`")
    lines.append(f"- overlap_ratio_vs_baseline: `{as_float(ov.get('ratio_vs_baseline')):.4f}`")
    lines.append("")
    lines.append("## Median Metrics")
    c_y = as_float((y.get("consensus") or {}).get("median"), None)
    b_y = as_float((y.get("baseline") or {}).get("median"), None)
    lines.append(f"- yield_per_day: consensus `{fmt_optional(c_y)}` / baseline `{fmt_optional(b_y)}`")
    lines.append(
        f"- liquidity: consensus `{as_float((lq.get('consensus') or {}).get('median')):.2f}` / "
        f"baseline `{as_float((lq.get('baseline') or {}).get('median')):.2f}`"
    )
    lines.append("")
    lines.append("## Signals")
    for s in report.get("signals") or []:
        lines.append(f"- {s}")
    lines.append("")
    lines.append("## Cautions")
    cautions = report.get("cautions") or []
    if not cautions:
        lines.append("- none")
    else:
        for c in cautions:
            lines.append(f"- {c}")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compare weather consensus watchlist vs baseline watchlist")
    p.add_argument("--consensus-json", default="logs/weather_7acct_auto_consensus_watchlist_latest.json")
    p.add_argument("--baseline-json", default="logs/weather_7acct_auto_no_longshot_latest.json")
    p.add_argument("--consensus-name", default="consensus")
    p.add_argument("--baseline-name", default="baseline")
    p.add_argument("--top-n", type=int, default=30)
    p.add_argument("--out-json", default="logs/weather_ab_dryrun_latest.json")
    p.add_argument("--out-md", default="")
    p.add_argument("--pretty", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    consensus_json = resolve_path(str(args.consensus_json), "weather_7acct_auto_consensus_watchlist_latest.json")
    baseline_json = resolve_path(str(args.baseline_json), "weather_7acct_auto_no_longshot_latest.json")
    out_json = resolve_path(str(args.out_json), "weather_ab_dryrun_latest.json")
    out_md = resolve_path(str(args.out_md), "") if str(args.out_md or "").strip() else None

    if not consensus_json.exists():
        print(f"consensus json not found: {consensus_json}")
        return 2
    if not baseline_json.exists():
        print(f"baseline json not found: {baseline_json}")
        return 2

    try:
        c_rows_raw = load_rows(consensus_json)
        b_rows_raw = load_rows(baseline_json)
    except Exception as exc:
        print(f"failed to load rows: {exc}")
        return 2

    c_rows = normalize_rows(c_rows_raw, top_n=max(1, int(args.top_n)))
    b_rows = normalize_rows(b_rows_raw, top_n=max(1, int(args.top_n)))
    report = build_report(c_rows, b_rows, str(args.consensus_name), str(args.baseline_name))
    report["inputs"]["consensus_json"] = str(consensus_json)
    report["inputs"]["baseline_json"] = str(baseline_json)
    report["inputs"]["top_n"] = int(args.top_n)

    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        if args.pretty:
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.write("\n")
        else:
            json.dump(report, f, ensure_ascii=False, separators=(",", ":"))

    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(format_md(report), encoding="utf-8")

    ov = report.get("overlap") or {}
    y = (report.get("metrics") or {}).get("yield_per_day") or {}
    cy = as_float((y.get("consensus") or {}).get("median"), None)
    by = as_float((y.get("baseline") or {}).get("median"), None)
    print(
        f"assessment={report.get('assessment')} "
        f"overlap={as_int(ov.get('count'))}/{as_int(report.get('inputs', {}).get('consensus_count'))} "
        f"yield_med(consensus/baseline)={fmt_optional(cy)}/{fmt_optional(by)}"
    )
    print(f"wrote: {out_json}")
    if out_md is not None:
        print(f"wrote: {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
