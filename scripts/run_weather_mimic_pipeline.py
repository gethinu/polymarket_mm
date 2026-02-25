#!/usr/bin/env python3
"""
Run an observe-only weather mimic pipeline end-to-end.

Pipeline:
  1) Analyze all input users as one cohort.
  2) Select winners by configurable filters.
  3) Re-analyze winners-only cohort.
  4) Build weather mimic profile/supervisor config.
  5) Optionally run no_longshot, lateprob, and consensus scans once.

All artifacts are written under logs/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_tag() -> str:
    return now_utc().strftime("%Y%m%d_%H%M%S")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def logs_dir() -> Path:
    p = repo_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_weather_token(value: str) -> str:
    s = str(value or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_required_tokens(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in values:
        tok = normalize_weather_token(raw)
        if not tok:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def to_cmdline(args: Iterable[str]) -> str:
    return subprocess.list2cmdline([str(a) for a in args])


def run_checked(args: List[str], cwd: Path) -> None:
    print(f"[pipeline] run: {to_cmdline(args)}")
    code = subprocess.call(args, cwd=str(cwd))
    if code != 0:
        raise RuntimeError(f"command failed ({code}): {to_cmdline(args)}")


def read_user_file(path: Path) -> List[str]:
    out: List[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#"):
            continue
        out.append(s)
    return out


def unique_keep_order(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def json_load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def arg_value(args: List[str], key: str) -> str:
    try:
        i = args.index(key)
        if i + 1 < len(args):
            return str(args[i + 1])
    except ValueError:
        pass
    return ""


def resolve_repo_path(root: Path, logs: Path, raw: str) -> Path:
    p = Path(str(raw or "").strip())
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs / p.name
    return root / p


def select_winners(
    all_cohort_json: Path,
    min_weather_share_pct: float,
    min_trades: int,
    min_realized_pnl: float,
    required_weather_tokens: List[str],
    min_weather_token_hits: int,
    min_roundtrip_key_share_pct: float,
    min_close_leg_count: int,
    max_avg_interval_sec: float,
    min_sell_to_buy_notional_ratio: float,
) -> Dict[str, object]:
    src = json_load(all_cohort_json)
    users = src.get("users") if isinstance(src.get("users"), list) else []
    req_tokens = parse_required_tokens(required_weather_tokens)
    req_hits = max(0, int(min_weather_token_hits))

    scored: List[dict] = []
    winners: List[str] = []
    for user in users:
        if not isinstance(user, dict):
            continue
        analysis = user.get("analysis") if isinstance(user.get("analysis"), dict) else {}
        weather = analysis.get("weather_focus") if isinstance(analysis.get("weather_focus"), dict) else {}
        pnl = analysis.get("pnl_approx") if isinstance(analysis.get("pnl_approx"), dict) else {}
        activity = analysis.get("activity") if isinstance(analysis.get("activity"), dict) else {}
        roundtrip = analysis.get("roundtrip") if isinstance(analysis.get("roundtrip"), dict) else {}

        trade_count = as_int(analysis.get("trade_count"), 0)
        weather_pct = as_float(weather.get("weather_trade_share_pct"), 0.0)
        realized_pnl = as_float(pnl.get("realized_pnl"), 0.0)
        roundtrip_share_pct = as_float(roundtrip.get("roundtrip_key_share_pct"), 0.0)
        close_leg_count = as_int(pnl.get("close_leg_count"), 0)
        avg_interval = as_float(activity.get("avg_interval_sec"), 0.0)
        buy_notional = as_float(activity.get("buy_notional"), 0.0)
        sell_notional = as_float(activity.get("sell_notional"), 0.0)
        sell_buy_ratio = (sell_notional / buy_notional) if buy_notional > 0 else 0.0
        token_rows = weather.get("top_weather_tokens") if isinstance(weather.get("top_weather_tokens"), list) else []
        token_set = set()
        for t in token_rows:
            if not isinstance(t, dict):
                continue
            tok = normalize_weather_token(t.get("token"))
            if not tok:
                continue
            if as_int(t.get("count"), 0) <= 0:
                continue
            token_set.add(tok)
        matched_tokens = [tok for tok in req_tokens if tok in token_set]
        station_passed = True
        if req_tokens:
            station_passed = len(matched_tokens) >= req_hits
        roundtrip_passed = roundtrip_share_pct >= float(min_roundtrip_key_share_pct)
        close_leg_passed = close_leg_count >= int(min_close_leg_count)
        interval_passed = True if float(max_avg_interval_sec) <= 0.0 else avg_interval <= float(max_avg_interval_sec)
        sell_buy_passed = True if float(min_sell_to_buy_notional_ratio) <= 0.0 else sell_buy_ratio >= float(min_sell_to_buy_notional_ratio)

        passed = (
            trade_count >= int(min_trades)
            and weather_pct >= float(min_weather_share_pct)
            and realized_pnl >= float(min_realized_pnl)
            and station_passed
            and roundtrip_passed
            and close_leg_passed
            and interval_passed
            and sell_buy_passed
        )
        input_user = str(user.get("input_user") or user.get("display_user") or "").strip()
        if passed and input_user:
            winners.append(input_user)

        scored.append(
            {
                "input_user": input_user,
                "display_user": str(user.get("display_user") or ""),
                "resolved_wallet": str(user.get("resolved_wallet") or ""),
                "trade_count": trade_count,
                "weather_trade_share_pct": weather_pct,
                "realized_pnl": realized_pnl,
                "matched_weather_tokens": matched_tokens,
                "matched_weather_token_count": len(matched_tokens),
                "station_filter_passed": bool(station_passed),
                "roundtrip_key_share_pct": roundtrip_share_pct,
                "close_leg_count": close_leg_count,
                "avg_interval_sec": avg_interval,
                "sell_to_buy_notional_ratio": sell_buy_ratio,
                "roundtrip_filter_passed": bool(roundtrip_passed),
                "close_leg_filter_passed": bool(close_leg_passed),
                "interval_filter_passed": bool(interval_passed),
                "sell_to_buy_filter_passed": bool(sell_buy_passed),
                "passed_filter": bool(passed),
            }
        )
    winners = unique_keep_order(winners)
    return {
        "winners": winners,
        "scored_users": scored,
        "required_weather_tokens": req_tokens,
        "min_weather_token_hits": req_hits,
        "min_roundtrip_key_share_pct": float(min_roundtrip_key_share_pct),
        "min_close_leg_count": int(min_close_leg_count),
        "max_avg_interval_sec": float(max_avg_interval_sec),
        "min_sell_to_buy_notional_ratio": float(min_sell_to_buy_notional_ratio),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Run observe-only weather mimic pipeline from profile links/wallets")
    p.add_argument("--user", action="append", default=[], help="Repeatable wallet / @handle / profile URL")
    p.add_argument("--user-file", default="", help="Newline-separated user identifiers")
    p.add_argument("--profile-name", default="weather_mimic_auto", help="Output profile prefix")
    p.add_argument("--limit", type=int, default=500, help="Fetch page limit for analyze_trader_cohort.py")
    p.add_argument("--max-trades", type=int, default=4000, help="Max trades per user")
    p.add_argument("--sleep-sec", type=float, default=0.2, help="Fetch sleep seconds")
    p.add_argument("--top-markets", type=int, default=10, help="Top markets per user in cohort report")
    p.add_argument("--min-weather-share-pct", type=float, default=60.0, help="Winner filter minimum weather share")
    p.add_argument("--min-trades", type=int, default=500, help="Winner filter minimum trade count")
    p.add_argument("--min-realized-pnl", type=float, default=0.0, help="Winner filter minimum realized pnl")
    p.add_argument(
        "--winner-require-weather-token",
        action="append",
        default=[],
        help="Require winners to include this token in top_weather_tokens (repeatable)",
    )
    p.add_argument(
        "--winner-min-weather-token-hits",
        type=int,
        default=1,
        help="Minimum distinct required weather tokens matched",
    )
    p.add_argument(
        "--aleiah-weather-pack",
        action="store_true",
        help="Apply Link01 defaults: require NYC/London token coverage and consensus overlap",
    )
    p.add_argument(
        "--browomo-small-edge-pack",
        action="store_true",
        help="Apply Link02 defaults: many positions + low-correlation cap + consensus overlap",
    )
    p.add_argument(
        "--c0-micro-moves-pack",
        action="store_true",
        help="Apply Link03 defaults: wallet micro-move style filters + overlap/diversification",
    )
    p.add_argument(
        "--velon-micro-moves-pack",
        action="store_true",
        help="Apply Link04 defaults: exit-focused micro-move filters + faster watch cadence",
    )
    p.add_argument(
        "--roan-roadmap-pack",
        action="store_true",
        help="Apply Link05 defaults: uncertainty-aware filters + cost-adjusted edge floors",
    )
    p.add_argument(
        "--winner-min-roundtrip-key-share-pct",
        type=float,
        default=0.0,
        help="Minimum roundtrip key share percent required for winner selection",
    )
    p.add_argument(
        "--winner-min-close-leg-count",
        type=int,
        default=0,
        help="Minimum close-leg count required for winner selection",
    )
    p.add_argument(
        "--winner-max-avg-interval-sec",
        type=float,
        default=0.0,
        help="Maximum avg interval sec allowed for winner selection (<=0 disables)",
    )
    p.add_argument(
        "--winner-min-sell-to-buy-notional-ratio",
        type=float,
        default=0.0,
        help="Minimum sell_notional/buy_notional ratio required for winner selection (<=0 disables)",
    )
    p.add_argument("--scan-max-pages", type=int, default=80)
    p.add_argument("--scan-page-size", type=int, default=500)
    p.add_argument("--scan-interval-sec", type=float, default=300.0)
    p.add_argument("--min-liquidity", type=float, default=500.0)
    p.add_argument("--min-volume-24h", type=float, default=100.0)
    p.add_argument("--top-n", type=int, default=30)
    p.add_argument(
        "--lateprob-disable-weather-filter",
        action="store_true",
        help="Pass through to build_weather_mimic_profile.py and disable lateprob weather include-regex.",
    )
    p.add_argument("--consensus-score-mode", choices=("balanced", "liquidity", "edge"), default="balanced")
    p.add_argument("--consensus-weight-overlap", type=float, default=None)
    p.add_argument("--consensus-weight-net-yield", type=float, default=None)
    p.add_argument("--consensus-weight-max-profit", type=float, default=None)
    p.add_argument("--consensus-weight-liquidity", type=float, default=None)
    p.add_argument("--consensus-weight-volume", type=float, default=None)
    p.add_argument(
        "--consensus-require-overlap",
        action="store_true",
        help="Require consensus rows to exist in both no_longshot and lateprob outputs",
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
        help="Minimum volume_24h/liquidity ratio filter for consensus output (0 disables)",
    )
    p.add_argument(
        "--consensus-max-hours-to-end",
        type=float,
        default=0.0,
        help="Maximum hours_to_end filter for consensus output (0 disables)",
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
    p.add_argument("--no-run-scans", action="store_true", help="Build profile only; do not run scanner commands")
    p.add_argument("--pretty", action="store_true", help="Pretty-print pipeline summary JSON")
    args = p.parse_args()

    root = repo_root()
    logs = logs_dir()
    tag = utc_tag()
    profile_name = str(args.profile_name).strip() or "weather_mimic_auto"
    required_weather_tokens = parse_required_tokens(args.winner_require_weather_token or [])
    min_roundtrip_key_share_pct = float(args.winner_min_roundtrip_key_share_pct)
    min_close_leg_count = int(args.winner_min_close_leg_count)
    max_avg_interval_sec = float(args.winner_max_avg_interval_sec)
    min_sell_to_buy_notional_ratio = float(args.winner_min_sell_to_buy_notional_ratio)
    scan_max_pages = int(args.scan_max_pages)
    scan_interval_sec = float(args.scan_interval_sec)
    min_liquidity = float(args.min_liquidity)
    min_volume_24h = float(args.min_volume_24h)
    top_n = int(args.top_n)
    lateprob_disable_weather_filter = bool(args.lateprob_disable_weather_filter)
    consensus_score_mode = str(args.consensus_score_mode)
    consensus_require_overlap = bool(args.consensus_require_overlap)
    consensus_max_per_correlation_bucket = int(args.consensus_max_per_correlation_bucket)
    consensus_min_turnover_ratio = float(args.consensus_min_turnover_ratio)
    consensus_max_hours_to_end = float(args.consensus_max_hours_to_end)
    no_longshot_per_trade_cost = float(args.no_longshot_per_trade_cost)
    no_longshot_min_net_yield_per_day = float(args.no_longshot_min_net_yield_per_day)
    lateprob_per_trade_cost = float(args.lateprob_per_trade_cost)
    lateprob_max_active_stale_hours = float(args.lateprob_max_active_stale_hours)
    if bool(args.aleiah_weather_pack):
        if not required_weather_tokens:
            required_weather_tokens = ["nyc", "london"]
        consensus_require_overlap = True
    if bool(args.browomo_small_edge_pack):
        consensus_require_overlap = True
        scan_max_pages = max(scan_max_pages, 120)
        top_n = max(top_n, 50)
        if consensus_max_per_correlation_bucket <= 0:
            consensus_max_per_correlation_bucket = 2
        if consensus_score_mode == "balanced":
            consensus_score_mode = "edge"
    if bool(args.c0_micro_moves_pack):
        consensus_require_overlap = True
        scan_max_pages = max(scan_max_pages, 140)
        top_n = max(top_n, 60)
        min_liquidity = max(min_liquidity, 700.0)
        min_volume_24h = max(min_volume_24h, 200.0)
        if consensus_max_per_correlation_bucket <= 0:
            consensus_max_per_correlation_bucket = 2
        if consensus_score_mode == "balanced":
            consensus_score_mode = "edge"
        if min_roundtrip_key_share_pct <= 0.0:
            min_roundtrip_key_share_pct = 8.0
        if min_close_leg_count <= 0:
            min_close_leg_count = 30
        if max_avg_interval_sec <= 0.0:
            max_avg_interval_sec = 25000.0
        if min_sell_to_buy_notional_ratio <= 0.0:
            min_sell_to_buy_notional_ratio = 0.35
    if bool(args.velon_micro_moves_pack):
        consensus_require_overlap = True
        scan_max_pages = max(scan_max_pages, 160)
        top_n = max(top_n, 80)
        scan_interval_sec = min(scan_interval_sec, 120.0)
        min_liquidity = max(min_liquidity, 1000.0)
        min_volume_24h = max(min_volume_24h, 300.0)
        if consensus_max_per_correlation_bucket <= 0:
            consensus_max_per_correlation_bucket = 2
        if consensus_min_turnover_ratio <= 0.0:
            consensus_min_turnover_ratio = 0.30
        if consensus_max_hours_to_end <= 0.0:
            consensus_max_hours_to_end = 48.0
        if consensus_score_mode == "balanced":
            consensus_score_mode = "edge"
        if min_roundtrip_key_share_pct <= 0.0:
            min_roundtrip_key_share_pct = 10.0
        if min_close_leg_count <= 0:
            min_close_leg_count = 50
        if max_avg_interval_sec <= 0.0:
            max_avg_interval_sec = 22000.0
        if min_sell_to_buy_notional_ratio <= 0.0:
            min_sell_to_buy_notional_ratio = 0.60
    if bool(args.roan_roadmap_pack):
        consensus_require_overlap = True
        scan_max_pages = max(scan_max_pages, 180)
        top_n = max(top_n, 100)
        scan_interval_sec = min(scan_interval_sec, 90.0)
        min_liquidity = max(min_liquidity, 1200.0)
        min_volume_24h = max(min_volume_24h, 400.0)
        if consensus_max_per_correlation_bucket <= 0:
            consensus_max_per_correlation_bucket = 1
        if consensus_min_turnover_ratio <= 0.0:
            consensus_min_turnover_ratio = 0.50
        if consensus_max_hours_to_end <= 0.0:
            consensus_max_hours_to_end = 36.0
        if consensus_score_mode == "balanced":
            consensus_score_mode = "edge"
        if min_roundtrip_key_share_pct <= 0.0:
            min_roundtrip_key_share_pct = 10.0
        if min_close_leg_count <= 0:
            min_close_leg_count = 70
        if max_avg_interval_sec <= 0.0:
            max_avg_interval_sec = 20000.0
        if min_sell_to_buy_notional_ratio <= 0.0:
            min_sell_to_buy_notional_ratio = 0.65
        if no_longshot_per_trade_cost <= 0.0:
            no_longshot_per_trade_cost = 0.003
        if no_longshot_min_net_yield_per_day <= 0.0:
            no_longshot_min_net_yield_per_day = 0.02
        if lateprob_per_trade_cost <= 0.0:
            lateprob_per_trade_cost = 0.003
        if lateprob_max_active_stale_hours <= 0.0:
            lateprob_max_active_stale_hours = 4.0

    users: List[str] = [str(x).strip() for x in (args.user or []) if str(x).strip()]
    if args.user_file.strip():
        user_file_path = Path(args.user_file)
        if not user_file_path.is_absolute():
            user_file_path = root / user_file_path
        if not user_file_path.exists():
            print(f"User file not found: {user_file_path}")
            return 2
        users.extend(read_user_file(user_file_path))
    users = unique_keep_order(users)
    if not users:
        print("No users provided. Use --user and/or --user-file.")
        return 2

    inputs_path = logs / f"{profile_name}_inputs_{tag}.txt"
    inputs_path.write_text("\n".join(users) + "\n", encoding="utf-8")

    all_cohort_path = logs / f"{profile_name}_cohort_all_{tag}.json"
    all_cmd = [
        sys.executable,
        "scripts/analyze_trader_cohort.py",
        "--user-file",
        str(inputs_path),
        "--limit",
        str(int(args.limit)),
        "--max-trades",
        str(int(args.max_trades)),
        "--sleep-sec",
        str(float(args.sleep_sec)),
        "--top-markets",
        str(int(args.top_markets)),
        "--out",
        str(all_cohort_path),
        "--pretty",
    ]

    try:
        run_checked(all_cmd, cwd=root)
    except RuntimeError as exc:
        print(str(exc))
        return 1

    selected = select_winners(
        all_cohort_json=all_cohort_path,
        min_weather_share_pct=float(args.min_weather_share_pct),
        min_trades=int(args.min_trades),
        min_realized_pnl=float(args.min_realized_pnl),
        required_weather_tokens=required_weather_tokens,
        min_weather_token_hits=int(args.winner_min_weather_token_hits),
        min_roundtrip_key_share_pct=min_roundtrip_key_share_pct,
        min_close_leg_count=min_close_leg_count,
        max_avg_interval_sec=max_avg_interval_sec,
        min_sell_to_buy_notional_ratio=min_sell_to_buy_notional_ratio,
    )
    winners = selected["winners"] if isinstance(selected.get("winners"), list) else []
    if not winners:
        print("Winner filter selected zero users. Adjust thresholds and retry.")
        print(
            f"Current filter: min_weather_share_pct={args.min_weather_share_pct} "
            f"min_trades={args.min_trades} min_realized_pnl={args.min_realized_pnl} "
            f"required_weather_tokens={required_weather_tokens} "
            f"min_weather_token_hits={args.winner_min_weather_token_hits} "
            f"min_roundtrip_key_share_pct={min_roundtrip_key_share_pct} "
            f"min_close_leg_count={min_close_leg_count} "
            f"max_avg_interval_sec={max_avg_interval_sec} "
            f"min_sell_to_buy_notional_ratio={min_sell_to_buy_notional_ratio} "
            f"consensus_min_turnover_ratio={consensus_min_turnover_ratio} "
            f"consensus_max_hours_to_end={consensus_max_hours_to_end} "
            f"no_longshot_per_trade_cost={no_longshot_per_trade_cost} "
            f"no_longshot_min_net_yield_per_day={no_longshot_min_net_yield_per_day} "
            f"lateprob_per_trade_cost={lateprob_per_trade_cost} "
            f"lateprob_max_active_stale_hours={lateprob_max_active_stale_hours}"
        )
        return 2

    winner_inputs_path = logs / f"{profile_name}_winner_inputs_{tag}.txt"
    winner_inputs_path.write_text("\n".join(str(x) for x in winners) + "\n", encoding="utf-8")

    winners_cohort_path = logs / f"{profile_name}_cohort_winners_{tag}.json"
    winners_cmd = [
        sys.executable,
        "scripts/analyze_trader_cohort.py",
        "--user-file",
        str(winner_inputs_path),
        "--limit",
        str(int(args.limit)),
        "--max-trades",
        str(int(args.max_trades)),
        "--sleep-sec",
        str(float(args.sleep_sec)),
        "--top-markets",
        str(int(args.top_markets)),
        "--out",
        str(winners_cohort_path),
        "--pretty",
    ]

    try:
        run_checked(winners_cmd, cwd=root)
    except RuntimeError as exc:
        print(str(exc))
        return 1

    profile_json_path = logs / f"{profile_name}_profile_latest.json"
    supervisor_cfg_path = logs / f"bot_supervisor.{profile_name}.observe.json"
    mimic_cmd = [
        sys.executable,
        "scripts/build_weather_mimic_profile.py",
        str(winners_cohort_path),
        "--profile-name",
        profile_name,
        "--scan-max-pages",
        str(int(scan_max_pages)),
        "--scan-page-size",
        str(int(args.scan_page_size)),
        "--scan-interval-sec",
        str(float(scan_interval_sec)),
        "--min-liquidity",
        str(float(min_liquidity)),
        "--min-volume-24h",
        str(float(min_volume_24h)),
        "--top-n",
        str(int(top_n)),
        "--consensus-score-mode",
        str(consensus_score_mode),
        "--out-json",
        str(profile_json_path),
        "--out-supervisor-config",
        str(supervisor_cfg_path),
        "--pretty",
    ]
    if args.consensus_weight_overlap is not None:
        mimic_cmd.extend(["--consensus-weight-overlap", str(float(args.consensus_weight_overlap))])
    if args.consensus_weight_net_yield is not None:
        mimic_cmd.extend(["--consensus-weight-net-yield", str(float(args.consensus_weight_net_yield))])
    if args.consensus_weight_max_profit is not None:
        mimic_cmd.extend(["--consensus-weight-max-profit", str(float(args.consensus_weight_max_profit))])
    if args.consensus_weight_liquidity is not None:
        mimic_cmd.extend(["--consensus-weight-liquidity", str(float(args.consensus_weight_liquidity))])
    if args.consensus_weight_volume is not None:
        mimic_cmd.extend(["--consensus-weight-volume", str(float(args.consensus_weight_volume))])
    if consensus_require_overlap:
        mimic_cmd.extend(["--consensus-require-overlap"])
    if consensus_max_per_correlation_bucket > 0:
        mimic_cmd.extend(["--consensus-max-per-correlation-bucket", str(int(consensus_max_per_correlation_bucket))])
    if consensus_min_turnover_ratio > 0:
        mimic_cmd.extend(["--consensus-min-turnover-ratio", str(float(consensus_min_turnover_ratio))])
    if consensus_max_hours_to_end > 0:
        mimic_cmd.extend(["--consensus-max-hours-to-end", str(float(consensus_max_hours_to_end))])
    if no_longshot_per_trade_cost > 0:
        mimic_cmd.extend(["--no-longshot-per-trade-cost", str(float(no_longshot_per_trade_cost))])
    if no_longshot_min_net_yield_per_day > 0:
        mimic_cmd.extend(["--no-longshot-min-net-yield-per-day", str(float(no_longshot_min_net_yield_per_day))])
    if lateprob_per_trade_cost > 0:
        mimic_cmd.extend(["--lateprob-per-trade-cost", str(float(lateprob_per_trade_cost))])
    if lateprob_max_active_stale_hours > 0:
        mimic_cmd.extend(["--lateprob-max-active-stale-hours", str(float(lateprob_max_active_stale_hours))])
    if lateprob_disable_weather_filter:
        mimic_cmd.extend(["--lateprob-disable-weather-filter"])

    try:
        run_checked(mimic_cmd, cwd=root)
    except RuntimeError as exc:
        print(str(exc))
        return 1

    profile_obj = json_load(profile_json_path)
    commands = profile_obj.get("commands") if isinstance(profile_obj.get("commands"), dict) else {}
    no_longshot_args = commands.get("no_longshot_screen_args") if isinstance(commands.get("no_longshot_screen_args"), list) else []
    lateprob_args = commands.get("lateprob_screen_args") if isinstance(commands.get("lateprob_screen_args"), list) else []
    consensus_args = commands.get("consensus_watchlist_args") if isinstance(commands.get("consensus_watchlist_args"), list) else []
    no_longshot_csv = arg_value([str(x) for x in no_longshot_args], "--out-csv")
    no_longshot_json = arg_value([str(x) for x in no_longshot_args], "--out-json")
    lateprob_csv = arg_value([str(x) for x in lateprob_args], "--out-csv")
    lateprob_json = arg_value([str(x) for x in lateprob_args], "--out-json")
    consensus_csv = arg_value([str(x) for x in consensus_args], "--out-csv")
    consensus_json = arg_value([str(x) for x in consensus_args], "--out-json")

    if not args.no_run_scans:
        try:
            run_checked([str(x) for x in no_longshot_args], cwd=root)
            run_checked([str(x) for x in lateprob_args], cwd=root)
            run_checked([str(x) for x in consensus_args], cwd=root)
        except RuntimeError as exc:
            print(str(exc))
            return 1

    ab_vs_no_json = str(Path("logs") / f"{profile_name}_ab_vs_no_longshot_latest.json")
    ab_vs_no_md = str(Path("logs") / f"{profile_name}_ab_vs_no_longshot_latest.md")
    ab_vs_late_json = str(Path("logs") / f"{profile_name}_ab_vs_lateprob_latest.json")
    ab_vs_late_md = str(Path("logs") / f"{profile_name}_ab_vs_lateprob_latest.md")
    ab_compare: Dict[str, object] = {}

    if not args.no_run_scans:
        compare_tool = root / "scripts" / "compare_weather_watchlists.py"
        if not compare_tool.exists():
            ab_compare["status"] = "skipped"
            ab_compare["reason"] = f"missing_tool:{compare_tool}"
        else:
            comparisons = [
                ("no_longshot", no_longshot_json, ab_vs_no_json, ab_vs_no_md),
                ("lateprob", lateprob_json, ab_vs_late_json, ab_vs_late_md),
            ]
            consensus_abs = resolve_repo_path(root=root, logs=logs, raw=consensus_json) if consensus_json else None
            for baseline_name, baseline_json, out_json_rel, out_md_rel in comparisons:
                row: Dict[str, object] = {
                    "baseline_name": baseline_name,
                    "status": "skipped",
                    "consensus_json": str(consensus_json or ""),
                    "baseline_json": str(baseline_json or ""),
                    "out_json": out_json_rel,
                    "out_md": out_md_rel,
                }
                if consensus_abs is None or not baseline_json:
                    row["reason"] = "missing_input_path"
                    ab_compare[baseline_name] = row
                    continue
                baseline_abs = resolve_repo_path(root=root, logs=logs, raw=baseline_json)
                if not consensus_abs.exists() or not baseline_abs.exists():
                    row["reason"] = "missing_input_file"
                    row["consensus_json_resolved"] = str(consensus_abs)
                    row["baseline_json_resolved"] = str(baseline_abs)
                    ab_compare[baseline_name] = row
                    continue
                compare_cmd = [
                    sys.executable,
                    "scripts/compare_weather_watchlists.py",
                    "--consensus-json",
                    str(consensus_abs),
                    "--baseline-json",
                    str(baseline_abs),
                    "--consensus-name",
                    "consensus",
                    "--baseline-name",
                    baseline_name,
                    "--top-n",
                    str(int(top_n)),
                    "--out-json",
                    out_json_rel,
                    "--out-md",
                    out_md_rel,
                    "--pretty",
                ]
                try:
                    run_checked(compare_cmd, cwd=root)
                    row["status"] = "ok"
                except RuntimeError as exc:
                    row["status"] = "error"
                    row["error"] = str(exc)
                    print(f"[pipeline] warning: ab compare failed baseline={baseline_name}: {exc}")
                ab_compare[baseline_name] = row

    summary_path = logs / f"{profile_name}_pipeline_summary_{tag}.json"
    summary = {
        "meta": {
            "generated_at_utc": now_utc().isoformat(),
            "observe_only": True,
            "profile_name": profile_name,
            "source": "scripts/run_weather_mimic_pipeline.py",
        },
        "inputs": {
            "count": len(users),
            "inputs_file": str(inputs_path),
            "users": users,
        },
        "winner_filter": {
            "min_weather_share_pct": float(args.min_weather_share_pct),
            "min_trades": int(args.min_trades),
            "min_realized_pnl": float(args.min_realized_pnl),
            "required_weather_tokens": selected.get("required_weather_tokens", []),
            "min_weather_token_hits": selected.get("min_weather_token_hits", 0),
            "aleiah_weather_pack": bool(args.aleiah_weather_pack),
            "browomo_small_edge_pack": bool(args.browomo_small_edge_pack),
            "c0_micro_moves_pack": bool(args.c0_micro_moves_pack),
            "velon_micro_moves_pack": bool(args.velon_micro_moves_pack),
            "roan_roadmap_pack": bool(args.roan_roadmap_pack),
            "min_roundtrip_key_share_pct": selected.get("min_roundtrip_key_share_pct", 0.0),
            "min_close_leg_count": selected.get("min_close_leg_count", 0),
            "max_avg_interval_sec": selected.get("max_avg_interval_sec", 0.0),
            "min_sell_to_buy_notional_ratio": selected.get("min_sell_to_buy_notional_ratio", 0.0),
            "winner_count": len(winners),
            "winner_inputs_file": str(winner_inputs_path),
            "scored_users": selected.get("scored_users", []),
        },
        "artifacts": {
            "all_cohort_json": str(all_cohort_path),
            "winners_cohort_json": str(winners_cohort_path),
            "profile_json": str(profile_json_path),
            "supervisor_config_json": str(supervisor_cfg_path),
            "scans_executed": not bool(args.no_run_scans),
            "no_longshot_csv": no_longshot_csv,
            "no_longshot_json": no_longshot_json,
            "lateprob_csv": lateprob_csv,
            "lateprob_json": lateprob_json,
            "consensus_csv": consensus_csv,
            "consensus_json": consensus_json,
            "ab_vs_no_longshot_json": ab_vs_no_json,
            "ab_vs_no_longshot_md": ab_vs_no_md,
            "ab_vs_lateprob_json": ab_vs_late_json,
            "ab_vs_lateprob_md": ab_vs_late_md,
            "ab_compare": ab_compare,
            "consensus_require_overlap": bool(consensus_require_overlap),
            "consensus_max_per_correlation_bucket": int(consensus_max_per_correlation_bucket),
            "consensus_min_turnover_ratio": float(consensus_min_turnover_ratio),
            "consensus_max_hours_to_end": float(consensus_max_hours_to_end),
            "no_longshot_per_trade_cost": float(no_longshot_per_trade_cost),
            "no_longshot_min_net_yield_per_day": float(no_longshot_min_net_yield_per_day),
            "lateprob_per_trade_cost": float(lateprob_per_trade_cost),
            "lateprob_max_active_stale_hours": float(lateprob_max_active_stale_hours),
            "consensus_score_mode": str(consensus_score_mode),
            "lateprob_disable_weather_filter": bool(lateprob_disable_weather_filter),
            "top_n": int(top_n),
            "scan_max_pages": int(scan_max_pages),
            "scan_interval_sec": float(scan_interval_sec),
            "min_liquidity": float(min_liquidity),
            "min_volume_24h": float(min_volume_24h),
        },
    }

    with summary_path.open("w", encoding="utf-8") as f:
        if args.pretty:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        else:
            json.dump(summary, f, ensure_ascii=False, separators=(",", ":"))

    print("[pipeline] completed (observe-only)")
    print(f"[pipeline] summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
