#!/usr/bin/env python3
"""
Validate social/media profit claims using realized daily PnL artifacts (observe-only).

Defaults are aligned with one common claim set:
- "$500-$800 daily"
- "$75k in a month"
- "$20k -> $215k in 30 days"
- "$25/hour"
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import math
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def logs_dir() -> Path:
    p = repo_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_path(raw: str, default_name: str) -> Path:
    s = str(raw or "").strip()
    if not s:
        return logs_dir() / default_name
    p = Path(s)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs_dir() / p.name
    return repo_root() / p


def _as_float(v) -> Optional[float]:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n):
        return None
    return n


def _iter_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def _extract_day(row: dict) -> str:
    day = str(row.get("day") or row.get("date") or "").strip()
    if len(day) >= 10 and day[4:5] == "-" and day[7:8] == "-":
        return day[:10]
    ts = str(row.get("ts") or row.get("generated_utc") or "").strip()
    if len(ts) >= 10 and ts[4:5] == "-" and ts[7:8] == "-":
        return ts[:10]
    return ""


def _extract_realized_pnl(row: dict) -> Optional[float]:
    keys = (
        "realized_pnl_usd",
        "pnl_realized_usd",
        "realized_pnl",
        "pnl_realized",
        "realized",
    )
    for key in keys:
        if key in row:
            n = _as_float(row.get(key))
            if n is not None:
                return n
    return None


def _percentile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    xs = sorted(float(v) for v in values)
    if len(xs) == 1:
        return xs[0]
    qq = min(1.0, max(0.0, float(q)))
    idx = qq * (len(xs) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return xs[lo]
    frac = idx - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def _build_input_file_list(input_files: Sequence[str], input_glob: str) -> List[Path]:
    out: List[Path] = []

    defaults = [
        logs_dir() / "clob_arb_realized_daily.jsonl",
        logs_dir() / "strategy_realized_pnl_daily.jsonl",
    ]
    for p in defaults:
        if p.exists():
            out.append(p)

    for raw in input_files:
        s = str(raw or "").strip()
        if not s:
            continue
        p = Path(s)
        if not p.is_absolute():
            p = repo_root() / p
        if p.exists():
            out.append(p)

    g = str(input_glob or "").strip()
    if g:
        gp = Path(g)
        if gp.is_absolute():
            for x in glob.glob(g):
                out.append(Path(x))
        else:
            for x in glob.glob(str(repo_root() / g)):
                out.append(Path(x))

    deduped: List[Path] = []
    seen = set()
    for p in out:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    return deduped


def _rolling_best_sum(days: List[str], pnl: List[float], window_days: int) -> dict:
    n = len(pnl)
    w = max(1, int(window_days))
    if n <= 0:
        return {"window_days": w, "best_sum_usd": 0.0, "start_day": "", "end_day": "", "observed_days_in_window": 0}
    if n < w:
        total = float(sum(pnl))
        return {
            "window_days": w,
            "best_sum_usd": total,
            "start_day": days[0],
            "end_day": days[-1],
            "observed_days_in_window": n,
        }

    cur = float(sum(pnl[:w]))
    best = cur
    best_end = w - 1
    for i in range(w, n):
        cur += float(pnl[i]) - float(pnl[i - w])
        if cur > best:
            best = cur
            best_end = i
    best_start = best_end - w + 1
    return {
        "window_days": w,
        "best_sum_usd": float(best),
        "start_day": days[best_start],
        "end_day": days[best_end],
        "observed_days_in_window": w,
    }


def _status(observed_days: int, min_days: int, condition: bool) -> str:
    if observed_days < int(min_days):
        return "INSUFFICIENT_DATA"
    return "SUPPORTED" if bool(condition) else "NOT_SUPPORTED"


def _fmt_usd(v: Optional[float]) -> str:
    if v is None:
        return "-"
    return f"${float(v):,.2f}"


def render_markdown(payload: dict) -> str:
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    claims = payload.get("claims") if isinstance(payload.get("claims"), list) else []

    lines: List[str] = []
    lines.append("# Social Profit Claim Validation (Observe-only)")
    lines.append("")
    lines.append(f"- generated_utc: `{meta.get('generated_utc', '')}`")
    lines.append(f"- observed_days: `{summary.get('observed_days', 0)}`")
    lines.append(f"- input_files: `{len(meta.get('input_files', []))}`")
    lines.append("")
    lines.append("## Daily PnL Summary")
    lines.append("")
    lines.append(f"- total_realized_pnl_usd: `{_fmt_usd(summary.get('total_realized_pnl_usd'))}`")
    lines.append(f"- daily_mean_usd: `{_fmt_usd(summary.get('daily_mean_usd'))}`")
    lines.append(f"- daily_median_usd: `{_fmt_usd(summary.get('daily_median_usd'))}`")
    lines.append(f"- daily_p10_usd: `{_fmt_usd(summary.get('daily_p10_usd'))}`")
    lines.append(f"- daily_p90_usd: `{_fmt_usd(summary.get('daily_p90_usd'))}`")
    lines.append("")
    lines.append("## Claim Results")
    lines.append("")
    for c in claims:
        if not isinstance(c, dict):
            continue
        lines.append(f"### {c.get('id', 'claim')}")
        lines.append(f"- status: `{c.get('status', 'UNKNOWN')}`")
        lines.append(f"- claim: {c.get('claim_text', '')}")
        lines.append(f"- observed_metric: `{c.get('observed_metric', '')}`")
        lines.append(f"- observed_value: `{c.get('observed_value', '')}`")
        lines.append(f"- required_condition: `{c.get('required_condition', '')}`")
        lines.append(f"- reason: {c.get('reason', '')}")
        wnd = c.get("supporting_window") if isinstance(c.get("supporting_window"), dict) else {}
        if wnd:
            lines.append(
                "- supporting_window: "
                f"`{wnd.get('start_day', '')} -> {wnd.get('end_day', '')}` "
                f"(sum={_fmt_usd(_as_float(wnd.get('best_sum_usd')))})"
            )
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description="Validate social/media profit claims from realized daily PnL artifacts")
    p.add_argument("--input-file", action="append", default=[], help="Repeatable realized-daily JSONL file path")
    p.add_argument("--input-glob", default="", help="Additional JSONL glob pattern (relative to repo if not absolute)")
    p.add_argument("--min-days", type=int, default=30, help="Minimum observed days for support/no-support judgments")
    p.add_argument("--daily-range-min", type=float, default=500.0, help="Lower bound of daily claim range (USD)")
    p.add_argument("--daily-range-max", type=float, default=800.0, help="Upper bound of daily claim range (USD)")
    p.add_argument("--monthly-target-usd", type=float, default=75000.0, help="Rolling 30-day target (USD)")
    p.add_argument("--growth-start-usd", type=float, default=20000.0, help="Starting bankroll in growth claim (USD)")
    p.add_argument("--growth-end-usd", type=float, default=215000.0, help="Ending bankroll in growth claim (USD)")
    p.add_argument("--growth-days", type=int, default=30, help="Window days for growth claim")
    p.add_argument("--hourly-usd", type=float, default=25.0, help="Hourly claim in USD")
    p.add_argument("--active-hours-per-day", type=float, default=8.0, help="Assumed active hours per day")
    p.add_argument("--out-json", default="", help="Output JSON path (default logs/social_profit_claims_latest.json)")
    p.add_argument("--out-md", default="", help="Output markdown path (default logs/social_profit_claims_latest.md)")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = p.parse_args()

    in_files = _build_input_file_list(args.input_file, args.input_glob)
    per_day: Dict[str, float] = {}
    for path in in_files:
        for row in _iter_jsonl(path):
            day = _extract_day(row)
            pnl = _extract_realized_pnl(row)
            if not day or pnl is None:
                continue
            per_day[day] = per_day.get(day, 0.0) + float(pnl)

    day_keys = sorted(per_day.keys())
    pnl_values = [float(per_day[d]) for d in day_keys]
    observed_days = len(day_keys)

    total_realized = float(sum(pnl_values)) if pnl_values else 0.0
    mean_daily = (total_realized / observed_days) if observed_days > 0 else None
    median_daily = _percentile(pnl_values, 0.5)
    p10_daily = _percentile(pnl_values, 0.10)
    p90_daily = _percentile(pnl_values, 0.90)
    min_daily = min(pnl_values) if pnl_values else None
    max_daily = max(pnl_values) if pnl_values else None

    monthly_window = 30
    best_30d = _rolling_best_sum(day_keys, pnl_values, monthly_window)
    growth_window = max(1, int(args.growth_days))
    best_growth_window = _rolling_best_sum(day_keys, pnl_values, growth_window)

    daily_low = float(args.daily_range_min)
    daily_high = float(args.daily_range_max)
    daily_in_range = (mean_daily is not None) and (mean_daily >= daily_low) and (mean_daily <= daily_high)

    monthly_target = float(args.monthly_target_usd)
    monthly_supported = float(best_30d.get("best_sum_usd") or 0.0) >= monthly_target

    growth_profit_required = float(args.growth_end_usd) - float(args.growth_start_usd)
    growth_supported = float(best_growth_window.get("best_sum_usd") or 0.0) >= growth_profit_required

    growth_ratio = None
    growth_daily_geom = None
    if args.growth_start_usd > 0 and args.growth_days > 0:
        growth_ratio = float(args.growth_end_usd) / float(args.growth_start_usd)
        try:
            growth_daily_geom = growth_ratio ** (1.0 / float(args.growth_days)) - 1.0
        except Exception:
            growth_daily_geom = None

    hourly_target_daily = float(args.hourly_usd) * float(args.active_hours_per_day)
    hourly_supported = (mean_daily is not None) and (mean_daily >= hourly_target_daily)

    min_days = max(1, int(args.min_days))
    claims: List[dict] = [
        {
            "id": "daily_range_claim",
            "claim_text": f"Daily profit is around ${daily_low:.0f}-${daily_high:.0f}.",
            "observed_metric": "daily_mean_usd",
            "observed_value": mean_daily,
            "required_condition": f"{daily_low:.2f} <= daily_mean_usd <= {daily_high:.2f}",
            "status": _status(observed_days, min_days, daily_in_range),
            "reason": (
                f"observed_days={observed_days} < min_days={min_days}"
                if observed_days < min_days
                else f"daily_mean_usd={mean_daily:.2f}"
            ),
        },
        {
            "id": "monthly_target_claim",
            "claim_text": f"Profit can reach ${monthly_target:,.0f} in 30 days.",
            "observed_metric": "best_30d_sum_usd",
            "observed_value": float(best_30d.get("best_sum_usd") or 0.0),
            "required_condition": f"best_30d_sum_usd >= {monthly_target:.2f}",
            "status": _status(observed_days, max(min_days, monthly_window), monthly_supported),
            "reason": (
                f"observed_days={observed_days} < min_days={max(min_days, monthly_window)}"
                if observed_days < max(min_days, monthly_window)
                else f"best_30d_sum_usd={float(best_30d.get('best_sum_usd') or 0.0):.2f}"
            ),
            "supporting_window": best_30d,
        },
        {
            "id": "growth_claim",
            "claim_text": (
                f"Capital can grow from ${float(args.growth_start_usd):,.0f} "
                f"to ${float(args.growth_end_usd):,.0f} in {growth_window} days."
            ),
            "observed_metric": f"best_{growth_window}d_sum_usd",
            "observed_value": float(best_growth_window.get("best_sum_usd") or 0.0),
            "required_condition": f"best_{growth_window}d_sum_usd >= {growth_profit_required:.2f}",
            "status": _status(observed_days, max(min_days, growth_window), growth_supported),
            "reason": (
                f"observed_days={observed_days} < min_days={max(min_days, growth_window)}"
                if observed_days < max(min_days, growth_window)
                else (
                    f"required_profit={growth_profit_required:.2f}, "
                    f"required_daily_geom={(growth_daily_geom * 100.0):.3f}%"
                    if growth_daily_geom is not None
                    else f"required_profit={growth_profit_required:.2f}"
                )
            ),
            "supporting_window": best_growth_window,
            "required_growth_ratio": growth_ratio,
            "required_daily_geometric_return": growth_daily_geom,
        },
        {
            "id": "hourly_claim",
            "claim_text": (
                f"Average profit is ${float(args.hourly_usd):.2f}/hour "
                f"for {float(args.active_hours_per_day):.2f} active hours/day."
            ),
            "observed_metric": "daily_mean_usd",
            "observed_value": mean_daily,
            "required_condition": f"daily_mean_usd >= {hourly_target_daily:.2f}",
            "status": _status(observed_days, min_days, hourly_supported),
            "reason": (
                f"observed_days={observed_days} < min_days={min_days}"
                if observed_days < min_days
                else f"daily_mean_usd={mean_daily:.2f}, target_daily_usd={hourly_target_daily:.2f}"
            ),
        },
    ]

    payload = {
        "meta": {
            "generated_utc": now_utc().isoformat(),
            "tool": "report_social_profit_claims.py",
            "observe_only": True,
            "input_files": [str(p) for p in in_files],
            "input_glob": str(args.input_glob or ""),
            "min_days": min_days,
        },
        "summary": {
            "observed_days": observed_days,
            "day_min": day_keys[0] if day_keys else "",
            "day_max": day_keys[-1] if day_keys else "",
            "total_realized_pnl_usd": total_realized,
            "daily_mean_usd": mean_daily,
            "daily_median_usd": median_daily,
            "daily_p10_usd": p10_daily,
            "daily_p90_usd": p90_daily,
            "daily_min_usd": min_daily,
            "daily_max_usd": max_daily,
            "best_30d_sum_usd": float(best_30d.get("best_sum_usd") or 0.0),
            "best_30d_window": best_30d,
            "best_growth_window": best_growth_window,
        },
        "claims": claims,
        "daily_rows": [{"day": d, "realized_pnl_usd": per_day[d]} for d in day_keys],
    }

    out_json = resolve_path(str(args.out_json), "social_profit_claims_latest.json")
    out_md = resolve_path(str(args.out_md), "social_profit_claims_latest.md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)

    with out_json.open("w", encoding="utf-8") as f:
        if args.pretty:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        else:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    out_md.write_text(render_markdown(payload), encoding="utf-8")

    print(f"[social-claims] observed_days={observed_days} input_files={len(in_files)}")
    for c in claims:
        print(f"[social-claims] {c['id']}: {c['status']}")
    print(f"[social-claims] wrote json: {out_json}")
    print(f"[social-claims] wrote md: {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

