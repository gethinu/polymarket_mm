#!/usr/bin/env python3
"""
Build observe-only weather mimic scanner settings from cohort autopsy JSON.

Input:
  - output JSON from scripts/analyze_trader_cohort.py

Outputs:
  - profile JSON with derived thresholds + commands (under logs/ by default)
  - optional bot supervisor config for periodic observe scans
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_tag() -> str:
    return now_utc().strftime("%Y%m%d_%H%M%S")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def as_float(value, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def finite_or(value: float, fallback: float) -> float:
    return value if math.isfinite(value) else fallback


def fmt_float(x: float, digits: int = 6) -> str:
    return f"{x:.{digits}f}".rstrip("0").rstrip(".")


def resolve_path(raw: str, default_name: str) -> Path:
    logs_dir = repo_root() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    if not raw.strip():
        return logs_dir / default_name
    p = Path(raw)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs_dir / p.name
    return repo_root() / p


def to_cmdline(args: Iterable[str]) -> str:
    return subprocess.list2cmdline(list(args))


def parse_include_regex(filter_obj: dict) -> str:
    # Use a stable weather-core regex to avoid accidental matches such as "rain" inside "train".
    # Cohort city tokens are intentionally not injected here, because they can overfit/noise-match.
    _ = filter_obj
    return r"weather|temperature|precipitation|forecast|\brain\b|\bsnow\b|\bwind\b|humidity"


def derive_thresholds(cohort: dict) -> dict:
    template = cohort.get("mimic_template") if isinstance(cohort.get("mimic_template"), dict) else {}
    entry = template.get("entry_price_profile") if isinstance(template.get("entry_price_profile"), dict) else {}

    p10 = as_float(entry.get("buy_price_p10"))
    p50 = as_float(entry.get("buy_price_p50"))
    p90 = as_float(entry.get("buy_price_p90"))
    low_share = finite_or(as_float(entry.get("weighted_low_price_buy_share_pct")), 20.0)
    high_share = finite_or(as_float(entry.get("weighted_high_price_buy_share_pct")), 40.0)

    low_max_from_prices = clamp(finite_or(p10, 0.02) * 4.0, 0.02, 0.15)
    low_max_from_share = 0.04 + clamp(low_share, 0.0, 60.0) / 60.0 * 0.12
    yes_low_max = clamp(max(low_max_from_prices, low_max_from_share), 0.02, 0.15)
    yes_low_min = clamp(min(0.01, yes_low_max * 0.25), 0.001, yes_low_max - 0.001)

    share_floor = 0.82
    if high_share >= 60.0:
        share_floor = 0.87
    elif high_share >= 40.0:
        share_floor = 0.85

    yes_high_min = clamp(max(finite_or(p50, 0.90), share_floor), 0.80, 0.98)
    yes_high_max = clamp(max(yes_high_min + 0.02, finite_or(p90, 0.99) + 0.002), yes_high_min + 0.01, 0.995)

    # Keep lateprob horizon broad enough so active weather boards are discoverable without fallback logic.
    lateprob_max_hours = 24.0

    no_longshot_max_hours = 24.0
    if low_share >= 35.0:
        no_longshot_max_hours = 72.0
    elif low_share >= 20.0:
        no_longshot_max_hours = 48.0

    return {
        "buy_price_p10": finite_or(p10, 0.02),
        "buy_price_p50": finite_or(p50, 0.50),
        "buy_price_p90": finite_or(p90, 0.95),
        "weighted_low_price_buy_share_pct": low_share,
        "weighted_high_price_buy_share_pct": high_share,
        "yes_low_min": yes_low_min,
        "yes_low_max": yes_low_max,
        "yes_high_min": yes_high_min,
        "yes_high_max": yes_high_max,
        "lateprob_max_hours_to_end": lateprob_max_hours,
        "no_longshot_max_hours_to_end": no_longshot_max_hours,
    }


def build_commands(
    profile_name: str,
    include_regex: str,
    thresholds: dict,
    scan_max_pages: int,
    scan_page_size: int,
    min_liquidity: float,
    min_volume_24h: float,
    top_n: int,
    consensus_score_mode: str,
    consensus_weight_overlap: Optional[float],
    consensus_weight_net_yield: Optional[float],
    consensus_weight_max_profit: Optional[float],
    consensus_weight_liquidity: Optional[float],
    consensus_weight_volume: Optional[float],
    consensus_require_overlap: bool,
    consensus_max_per_correlation_bucket: int,
    consensus_min_turnover_ratio: float,
    consensus_max_hours_to_end: float,
    no_longshot_per_trade_cost: float,
    no_longshot_min_net_yield_per_day: float,
    lateprob_per_trade_cost: float,
    lateprob_max_active_stale_hours: float,
    lateprob_disable_weather_filter: bool,
) -> Dict[str, List[str]]:
    no_longshot_csv = f"logs/{profile_name}_no_longshot_latest.csv"
    no_longshot_json = f"logs/{profile_name}_no_longshot_latest.json"
    lateprob_csv = f"logs/{profile_name}_lateprob_latest.csv"
    lateprob_json = f"logs/{profile_name}_lateprob_latest.json"
    consensus_csv = f"logs/{profile_name}_consensus_watchlist_latest.csv"
    consensus_json = f"logs/{profile_name}_consensus_watchlist_latest.json"

    no_longshot_cmd = [
        "python",
        "scripts/polymarket_no_longshot_observe.py",
        "screen",
        "--max-pages",
        str(int(scan_max_pages)),
        "--page-size",
        str(int(scan_page_size)),
        "--yes-min",
        fmt_float(float(thresholds["yes_low_min"])),
        "--yes-max",
        fmt_float(float(thresholds["yes_low_max"])),
        "--min-days-to-end",
        "0",
        "--max-hours-to-end",
        fmt_float(float(thresholds["no_longshot_max_hours_to_end"])),
        "--min-liquidity",
        fmt_float(float(min_liquidity)),
        "--min-volume-24h",
        fmt_float(float(min_volume_24h)),
        "--sort-by",
        "net_yield_per_day_desc",
        "--include-regex",
        include_regex,
        "--top-n",
        str(int(top_n)),
        "--out-csv",
        no_longshot_csv,
        "--out-json",
        no_longshot_json,
    ]
    if float(no_longshot_per_trade_cost) > 0:
        no_longshot_cmd.extend(["--per-trade-cost", fmt_float(float(no_longshot_per_trade_cost), digits=4)])
    if float(no_longshot_min_net_yield_per_day) > 0:
        no_longshot_cmd.extend(
            ["--min-net-yield-per-day", fmt_float(float(no_longshot_min_net_yield_per_day), digits=6)]
        )

    lateprob_cmd = [
        "python",
        "scripts/polymarket_lateprob_observe.py",
        "screen",
        "--max-pages",
        str(int(scan_max_pages)),
        "--page-size",
        str(int(scan_page_size)),
        "--min-hours-to-end",
        "0",
        "--max-hours-to-end",
        fmt_float(float(thresholds["lateprob_max_hours_to_end"])),
        "--side-mode",
        "both",
        "--yes-high-min",
        fmt_float(float(thresholds["yes_high_min"])),
        "--yes-high-max",
        fmt_float(float(thresholds["yes_high_max"])),
        "--yes-low-min",
        fmt_float(float(thresholds["yes_low_min"])),
        "--yes-low-max",
        fmt_float(float(thresholds["yes_low_max"])),
        "--min-liquidity",
        fmt_float(float(min_liquidity)),
        "--min-volume-24h",
        fmt_float(float(min_volume_24h)),
        "--include-regex",
        ("" if bool(lateprob_disable_weather_filter) else include_regex),
        "--top-n",
        str(int(top_n)),
        "--out-csv",
        lateprob_csv,
        "--out-json",
        lateprob_json,
    ]
    if float(lateprob_per_trade_cost) > 0:
        lateprob_cmd.extend(["--per-trade-cost", fmt_float(float(lateprob_per_trade_cost), digits=4)])
    if float(lateprob_max_active_stale_hours) > 0:
        lateprob_cmd.extend(["--max-active-stale-hours", fmt_float(float(lateprob_max_active_stale_hours), digits=4)])
    consensus_cmd = [
        "python",
        "scripts/build_weather_consensus_watchlist.py",
        "--no-longshot-csv",
        no_longshot_csv,
        "--lateprob-csv",
        lateprob_csv,
        "--profile-name",
        profile_name,
        "--top-n",
        str(int(top_n)),
        "--min-liquidity",
        fmt_float(float(min_liquidity)),
        "--min-volume-24h",
        fmt_float(float(min_volume_24h)),
        "--score-mode",
        str(consensus_score_mode).strip().lower() or "balanced",
        "--out-csv",
        consensus_csv,
        "--out-json",
        consensus_json,
    ]
    if consensus_weight_overlap is not None:
        consensus_cmd.extend(["--weight-overlap", fmt_float(float(consensus_weight_overlap))])
    if consensus_weight_net_yield is not None:
        consensus_cmd.extend(["--weight-net-yield", fmt_float(float(consensus_weight_net_yield))])
    if consensus_weight_max_profit is not None:
        consensus_cmd.extend(["--weight-max-profit", fmt_float(float(consensus_weight_max_profit))])
    if consensus_weight_liquidity is not None:
        consensus_cmd.extend(["--weight-liquidity", fmt_float(float(consensus_weight_liquidity))])
    if consensus_weight_volume is not None:
        consensus_cmd.extend(["--weight-volume", fmt_float(float(consensus_weight_volume))])
    if consensus_require_overlap:
        consensus_cmd.append("--require-overlap")
    if int(consensus_max_per_correlation_bucket) > 0:
        consensus_cmd.extend(["--max-per-correlation-bucket", str(int(consensus_max_per_correlation_bucket))])
    if float(consensus_min_turnover_ratio) > 0:
        consensus_cmd.extend(["--min-turnover-ratio", fmt_float(float(consensus_min_turnover_ratio), digits=4)])
    if float(consensus_max_hours_to_end) > 0:
        consensus_cmd.extend(["--max-hours-to-end", fmt_float(float(consensus_max_hours_to_end), digits=4)])
    return {
        "no_longshot_screen": no_longshot_cmd,
        "lateprob_screen": lateprob_cmd,
        "consensus_watchlist": consensus_cmd,
    }


def build_supervisor_config(
    profile_name: str,
    commands: Dict[str, List[str]],
    scan_interval_sec: float,
) -> dict:
    delay = max(30.0, float(scan_interval_sec))
    restart_cap = max(6, int(math.ceil(3600.0 / max(delay, 1.0))) + 4)
    consensus_delay = delay + 60.0
    return {
        "name": f"{profile_name}-observe-suite",
        "description": "Observe-only periodic scanners generated from cohort weather mimic profile.",
        "log_file": f"logs/{profile_name}_supervisor.log",
        "state_file": f"logs/{profile_name}_supervisor_state.json",
        "jobs": [
            {
                "name": f"{profile_name}_no_longshot",
                "enabled": True,
                "command": commands["no_longshot_screen"],
                "restart": "always",
                "restart_delay_sec": delay,
                "max_restarts_per_hour": restart_cap,
            },
            {
                "name": f"{profile_name}_lateprob",
                "enabled": True,
                "command": commands["lateprob_screen"],
                "restart": "always",
                "restart_delay_sec": delay,
                "max_restarts_per_hour": restart_cap,
            },
            {
                "name": f"{profile_name}_consensus",
                "enabled": True,
                "command": commands["consensus_watchlist"],
                "restart": "always",
                "restart_delay_sec": consensus_delay,
                "max_restarts_per_hour": restart_cap,
            },
        ],
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Build weather mimic observe profile from cohort analysis JSON")
    p.add_argument("cohort_json", help="Input JSON produced by scripts/analyze_trader_cohort.py")
    p.add_argument("--profile-name", default="weather_mimic", help="Output prefix for logs/jobs")
    p.add_argument("--scan-max-pages", type=int, default=80, help="Max Gamma pages per generated scan command")
    p.add_argument("--scan-page-size", type=int, default=500, help="Page size per generated scan command")
    p.add_argument("--scan-interval-sec", type=float, default=300.0, help="Cycle interval for generated supervisor jobs")
    p.add_argument("--min-liquidity", type=float, default=500.0, help="Minimum liquidity filter for generated commands")
    p.add_argument("--min-volume-24h", type=float, default=100.0, help="Minimum 24h volume filter for generated commands")
    p.add_argument("--top-n", type=int, default=30, help="Top-N rows for generated scanner commands")
    p.add_argument(
        "--consensus-score-mode",
        choices=("balanced", "liquidity", "edge"),
        default="balanced",
        help="Scoring mode used for generated consensus watchlist command.",
    )
    p.add_argument("--consensus-weight-overlap", type=float, default=None, help="Optional consensus weight override")
    p.add_argument("--consensus-weight-net-yield", type=float, default=None, help="Optional consensus weight override")
    p.add_argument("--consensus-weight-max-profit", type=float, default=None, help="Optional consensus weight override")
    p.add_argument("--consensus-weight-liquidity", type=float, default=None, help="Optional consensus weight override")
    p.add_argument("--consensus-weight-volume", type=float, default=None, help="Optional consensus weight override")
    p.add_argument(
        "--consensus-require-overlap",
        action="store_true",
        help="Require consensus rows to exist in both no_longshot and lateprob scans",
    )
    p.add_argument(
        "--consensus-max-per-correlation-bucket",
        type=int,
        default=0,
        help="Optional cap for rows per inferred correlation bucket in consensus output",
    )
    p.add_argument(
        "--consensus-min-turnover-ratio",
        type=float,
        default=0.0,
        help="Minimum volume_24h/liquidity ratio passed to consensus watchlist command (0 disables)",
    )
    p.add_argument(
        "--consensus-max-hours-to-end",
        type=float,
        default=0.0,
        help="Maximum hours_to_end passed to consensus watchlist command (0 disables)",
    )
    p.add_argument(
        "--no-longshot-per-trade-cost",
        type=float,
        default=0.0,
        help="Per-trade cost passed to generated no_longshot command (0 keeps scanner default)",
    )
    p.add_argument(
        "--no-longshot-min-net-yield-per-day",
        type=float,
        default=0.0,
        help="Minimum net_yield_per_day passed to generated no_longshot command (0 disables)",
    )
    p.add_argument(
        "--lateprob-per-trade-cost",
        type=float,
        default=0.0,
        help="Per-trade cost passed to generated lateprob command (0 keeps scanner default)",
    )
    p.add_argument(
        "--lateprob-max-active-stale-hours",
        type=float,
        default=0.0,
        help="Maximum active stale hours passed to generated lateprob command (0 keeps scanner default)",
    )
    p.add_argument(
        "--lateprob-disable-weather-filter",
        action="store_true",
        help="Set generated lateprob command include-regex to empty string (disable weather-only filter).",
    )
    p.add_argument("--out-json", default="", help="Profile JSON output path (simple filename goes under logs/)")
    p.add_argument(
        "--out-supervisor-config",
        default="logs/bot_supervisor.weather_mimic.observe.json",
        help="Supervisor config output path (simple filename goes under logs/)",
    )
    p.add_argument("--no-supervisor-config", action="store_true", help="Do not write supervisor config JSON")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON outputs")
    args = p.parse_args()

    in_path = Path(args.cohort_json)
    if not in_path.is_absolute():
        in_path = repo_root() / in_path
    if not in_path.exists():
        print(f"Input JSON not found: {in_path}")
        return 2

    try:
        src = json.loads(in_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Failed to parse JSON: {exc}")
        return 2
    if not isinstance(src, dict):
        print("Input JSON root must be object.")
        return 2

    cohort = src.get("cohort") if isinstance(src.get("cohort"), dict) else {}
    if not cohort.get("ok", False):
        print("Input cohort summary is not ok. Re-run analyze_trader_cohort.py first.")
        return 2

    template = cohort.get("mimic_template") if isinstance(cohort.get("mimic_template"), dict) else {}
    market_filter = template.get("market_filter") if isinstance(template.get("market_filter"), dict) else {}
    include_regex = parse_include_regex(market_filter)

    thresholds = derive_thresholds(cohort)
    consensus_weights_override = {
        "overlap": args.consensus_weight_overlap,
        "net_yield": args.consensus_weight_net_yield,
        "max_profit": args.consensus_weight_max_profit,
        "liquidity": args.consensus_weight_liquidity,
        "volume": args.consensus_weight_volume,
    }
    consensus_weights_override = {k: float(v) for k, v in consensus_weights_override.items() if v is not None}

    commands = build_commands(
        profile_name=str(args.profile_name).strip() or "weather_mimic",
        include_regex=include_regex,
        thresholds=thresholds,
        scan_max_pages=int(args.scan_max_pages),
        scan_page_size=int(args.scan_page_size),
        min_liquidity=float(args.min_liquidity),
        min_volume_24h=float(args.min_volume_24h),
        top_n=int(args.top_n),
        consensus_score_mode=str(args.consensus_score_mode),
        consensus_weight_overlap=args.consensus_weight_overlap,
        consensus_weight_net_yield=args.consensus_weight_net_yield,
        consensus_weight_max_profit=args.consensus_weight_max_profit,
        consensus_weight_liquidity=args.consensus_weight_liquidity,
        consensus_weight_volume=args.consensus_weight_volume,
        consensus_require_overlap=bool(args.consensus_require_overlap),
        consensus_max_per_correlation_bucket=int(args.consensus_max_per_correlation_bucket),
        consensus_min_turnover_ratio=float(args.consensus_min_turnover_ratio),
        consensus_max_hours_to_end=float(args.consensus_max_hours_to_end),
        no_longshot_per_trade_cost=float(args.no_longshot_per_trade_cost),
        no_longshot_min_net_yield_per_day=float(args.no_longshot_min_net_yield_per_day),
        lateprob_per_trade_cost=float(args.lateprob_per_trade_cost),
        lateprob_max_active_stale_hours=float(args.lateprob_max_active_stale_hours),
        lateprob_disable_weather_filter=bool(args.lateprob_disable_weather_filter),
    )

    supervisor_cfg = build_supervisor_config(
        profile_name=str(args.profile_name).strip() or "weather_mimic",
        commands=commands,
        scan_interval_sec=float(args.scan_interval_sec),
    )

    supervisor_out_path: Optional[Path] = None
    if not args.no_supervisor_config:
        supervisor_out_path = resolve_path(
            args.out_supervisor_config,
            "bot_supervisor.weather_mimic.observe.json",
        )

    out_json = resolve_path(args.out_json, f"{args.profile_name}_profile_{utc_tag()}.json")
    payload = {
        "meta": {
            "generated_at_utc": now_utc().isoformat(),
            "observe_only": True,
            "source_cohort_json": str(in_path),
            "source_cohort_generated_at_utc": (src.get("meta") or {}).get("generated_at_utc", ""),
            "profile_name": args.profile_name,
        },
        "source_cohort_style": cohort.get("style", {}),
        "source_cohort_summary": {
            "weighted_weather_trade_share_pct": cohort.get("weighted_weather_trade_share_pct"),
            "weighted_low_price_buy_share_pct": cohort.get("weighted_low_price_buy_share_pct"),
            "weighted_high_price_buy_share_pct": cohort.get("weighted_high_price_buy_share_pct"),
            "weighted_avg_interval_sec": cohort.get("weighted_avg_interval_sec"),
        },
        "derived_thresholds": thresholds,
        "filters": {
            "include_regex": include_regex,
            "min_liquidity": float(args.min_liquidity),
            "min_volume_24h": float(args.min_volume_24h),
            "top_n": int(args.top_n),
            "scan_max_pages": int(args.scan_max_pages),
            "scan_page_size": int(args.scan_page_size),
            "scan_interval_sec": float(args.scan_interval_sec),
            "consensus_score_mode": str(args.consensus_score_mode),
            "consensus_require_overlap": bool(args.consensus_require_overlap),
            "consensus_max_per_correlation_bucket": int(args.consensus_max_per_correlation_bucket),
            "consensus_min_turnover_ratio": float(args.consensus_min_turnover_ratio),
            "consensus_max_hours_to_end": float(args.consensus_max_hours_to_end),
            "no_longshot_per_trade_cost": float(args.no_longshot_per_trade_cost),
            "no_longshot_min_net_yield_per_day": float(args.no_longshot_min_net_yield_per_day),
            "lateprob_per_trade_cost": float(args.lateprob_per_trade_cost),
            "lateprob_max_active_stale_hours": float(args.lateprob_max_active_stale_hours),
            "lateprob_disable_weather_filter": bool(args.lateprob_disable_weather_filter),
            **(
                {"consensus_weights_override": consensus_weights_override}
                if consensus_weights_override
                else {}
            ),
        },
        "commands": {
            "no_longshot_screen_args": commands["no_longshot_screen"],
            "lateprob_screen_args": commands["lateprob_screen"],
            "consensus_watchlist_args": commands["consensus_watchlist"],
            "no_longshot_screen_cmdline": to_cmdline(commands["no_longshot_screen"]),
            "lateprob_screen_cmdline": to_cmdline(commands["lateprob_screen"]),
            "consensus_watchlist_cmdline": to_cmdline(commands["consensus_watchlist"]),
        },
        "supervisor": {
            "config_path": "" if supervisor_out_path is None else str(supervisor_out_path),
            "run_cmdline": ""
            if supervisor_out_path is None
            else to_cmdline(
                [
                    "python",
                    "scripts/bot_supervisor.py",
                    "run",
                    "--config",
                    str(supervisor_out_path),
                ]
            ),
        },
        "note": (
            "Generated settings are observe-only scanner defaults from cohort statistics. "
            "Re-validate with paper/monitor results before any live execution."
        ),
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        if args.pretty:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        else:
            json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)

    if supervisor_out_path is not None:
        supervisor_out_path.parent.mkdir(parents=True, exist_ok=True)
        with supervisor_out_path.open("w", encoding="utf-8") as f:
            if args.pretty:
                json.dump(supervisor_cfg, f, indent=2, ensure_ascii=False)
            else:
                json.dump(supervisor_cfg, f, separators=(",", ":"), ensure_ascii=False)

    print("Weather mimic profile generated (observe-only).")
    print(f"Profile JSON: {out_json}")
    if supervisor_out_path is not None:
        print(f"Supervisor config: {supervisor_out_path}")
        print(
            "Run:",
            to_cmdline(["python", "scripts/bot_supervisor.py", "run", "--config", str(supervisor_out_path)]),
        )
    print("No-longshot scan:")
    print(to_cmdline(commands["no_longshot_screen"]))
    print("Lateprob scan:")
    print(to_cmdline(commands["lateprob_screen"]))
    print("Consensus watchlist:")
    print(to_cmdline(commands["consensus_watchlist"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
