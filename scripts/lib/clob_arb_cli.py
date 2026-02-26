from __future__ import annotations

import argparse
import sys
from typing import Sequence

from lib.runtime_common import env_bool as _env_bool
from lib.runtime_common import env_str as _env_str


def _apply_env_overrides(args, cli_tokens: Sequence[str], default_espn_paths: Sequence[str]):
    def _flag_explicit(attr: str) -> bool:
        flag = f"--{attr.replace('_', '-')}"
        for tok in cli_tokens:
            if tok == flag or tok.startswith(flag + "="):
                return True
        return False

    def maybe(attr: str, env: str, default, cast):
        raw = _env_str(env)
        if not raw:
            return
        if _flag_explicit(attr):
            return
        if getattr(args, attr) != default:
            return
        try:
            setattr(args, attr, cast(raw))
        except Exception:
            return

    maybe("run_seconds", "CLOBBOT_RUN_SECONDS", 0, lambda s: int(float(s)))
    maybe("summary_every_sec", "CLOBBOT_SUMMARY_EVERY_SEC", 0.0, float)
    maybe("universe", "CLOBBOT_UNIVERSE", "weather", str)
    maybe("shares", "CLOBBOT_SHARES", 5.0, float)
    maybe("min_edge_cents", "CLOBBOT_MIN_EDGE_CENTS", 1.0, float)
    maybe("winner_fee_rate", "CLOBBOT_WINNER_FEE_RATE", 0.0, float)
    maybe("fixed_cost", "CLOBBOT_FIXED_COST", 0.0, float)
    maybe("alert_cooldown_sec", "CLOBBOT_ALERT_COOLDOWN_SEC", 10.0, float)

    maybe("gamma_limit", "CLOBBOT_GAMMA_LIMIT", 500, lambda s: int(float(s)))
    maybe("gamma_offset", "CLOBBOT_GAMMA_OFFSET", 0, lambda s: int(float(s)))
    maybe("gamma_min_liquidity", "CLOBBOT_GAMMA_MIN_LIQUIDITY", 0.0, float)
    maybe("gamma_min_volume24hr", "CLOBBOT_GAMMA_MIN_VOLUME24HR", 0.0, float)
    maybe("gamma_scan_max_markets", "CLOBBOT_GAMMA_SCAN_MAX", 5000, lambda s: int(float(s)))
    maybe("gamma_max_days_to_end", "CLOBBOT_GAMMA_MAX_DAYS_TO_END", 0.0, float)
    maybe("gamma_score_halflife_days", "CLOBBOT_GAMMA_SCORE_HALFLIFE_DAYS", 30.0, float)
    maybe("gamma_include_regex", "CLOBBOT_GAMMA_INCLUDE_REGEX", "", str)
    maybe("gamma_exclude_regex", "CLOBBOT_GAMMA_EXCLUDE_REGEX", "", str)
    maybe("sports_live_prestart_min", "CLOBBOT_SPORTS_LIVE_PRESTART_MIN", 10.0, float)
    maybe("sports_live_postend_min", "CLOBBOT_SPORTS_LIVE_POSTEND_MIN", 30.0, float)
    maybe("sports_market_types", "CLOBBOT_SPORTS_MARKET_TYPES", "", str)
    maybe("sports_market_types_exclude", "CLOBBOT_SPORTS_MARKET_TYPES_EXCLUDE", "", str)
    maybe("sports_feed_provider", "CLOBBOT_SPORTS_FEED_PROVIDER", "none", str)
    maybe("sports_feed_timeout_sec", "CLOBBOT_SPORTS_FEED_TIMEOUT_SEC", 5.0, float)
    maybe("sports_feed_live_buffer_sec", "CLOBBOT_SPORTS_FEED_LIVE_BUFFER_SEC", 90.0, float)
    maybe("sports_feed_espn_paths", "CLOBBOT_SPORTS_FEED_ESPN_PATHS", ",".join(default_espn_paths), str)

    maybe("btc_5m_windows_back", "CLOBBOT_BTC_5M_WINDOWS_BACK", 1, lambda s: int(float(s)))
    maybe("btc_5m_windows_forward", "CLOBBOT_BTC_5M_WINDOWS_FORWARD", 1, lambda s: int(float(s)))
    maybe("btc_updown_window_minutes", "CLOBBOT_BTC_UPDOWN_WINDOW_MINUTES", "5", str)

    maybe("max_exec_per_day", "CLOBBOT_MAX_EXEC_PER_DAY", 20, lambda s: int(float(s)))
    maybe("max_notional_per_day", "CLOBBOT_MAX_NOTIONAL_PER_DAY", 200.0, float)
    maybe("max_open_orders", "CLOBBOT_MAX_OPEN_ORDERS", 0, lambda s: int(float(s)))
    maybe("max_consecutive_failures", "CLOBBOT_MAX_CONSEC_FAILURES", 3, lambda s: int(float(s)))
    maybe("daily_loss_limit_usd", "CLOBBOT_DAILY_LOSS_LIMIT_USD", 0.0, float)
    maybe("pnl_check_interval_sec", "CLOBBOT_PNL_CHECK_INTERVAL_SEC", 60.0, float)

    maybe("exec_slippage_bps", "CLOBBOT_EXEC_SLIPPAGE_BPS", 50.0, float)
    maybe("unwind_slippage_bps", "CLOBBOT_UNWIND_SLIPPAGE_BPS", 150.0, float)
    maybe("exec_cooldown_sec", "CLOBBOT_EXEC_COOLDOWN_SEC", 30.0, float)
    maybe("exec_max_attempts", "CLOBBOT_EXEC_MAX_ATTEMPTS", 2, lambda s: int(float(s)))
    maybe("exec_book_stale_sec", "CLOBBOT_EXEC_BOOK_STALE_SEC", 5.0, float)
    maybe("exec_backend", "CLOBBOT_EXEC_BACKEND", "auto", str)
    maybe("strategy", "CLOBBOT_STRATEGY", "both", str)
    maybe("max_legs", "CLOBBOT_MAX_LEGS", 0, lambda s: int(float(s)))

    maybe("simmer_venue", "CLOBBOT_SIMMER_VENUE", "polymarket", str)
    maybe("simmer_source", "CLOBBOT_SIMMER_SOURCE", "sdk:clob-arb", str)
    maybe("simmer_min_amount", "CLOBBOT_SIMMER_MIN_AMOUNT", 1.0, float)

    maybe("max_subscribe_tokens", "CLOBBOT_MAX_SUBSCRIBE_TOKENS", 0, lambda s: int(float(s)))
    maybe("min_eval_interval_ms", "CLOBBOT_MIN_EVAL_INTERVAL_MS", 0, lambda s: int(float(s)))
    maybe("max_markets_per_event", "CLOBBOT_MAX_MARKETS_PER_EVENT", 0, lambda s: int(float(s)))
    maybe("observe_notify_min_interval_sec", "CLOBBOT_OBSERVE_NOTIFY_MIN_INTERVAL_SEC", 30.0, float)
    maybe("observe_exec_edge_min_usd", "CLOBBOT_OBSERVE_EXEC_EDGE_MIN_USD", 0.0, float)
    maybe("observe_exec_edge_strike_limit", "CLOBBOT_OBSERVE_EXEC_EDGE_STRIKE_LIMIT", 3, lambda s: int(float(s)))
    maybe("observe_exec_edge_cooldown_sec", "CLOBBOT_OBSERVE_EXEC_EDGE_COOLDOWN_SEC", 300.0, float)
    maybe(
        "observe_exec_edge_filter_strategies",
        "CLOBBOT_OBSERVE_EXEC_EDGE_FILTER_STRATEGIES",
        "event-yes,event-no,event-pair",
        str,
    )
    maybe("metrics_file", "CLOBBOT_METRICS_FILE", "", str)
    maybe("wallet_signal_weight", "CLOBBOT_WALLET_SIGNAL_WEIGHT", 0.25, float)
    maybe("wallet_signal_max_baskets", "CLOBBOT_WALLET_SIGNAL_MAX_BASKETS", 80, lambda s: int(float(s)))
    maybe("wallet_signal_holders_limit", "CLOBBOT_WALLET_SIGNAL_HOLDERS_LIMIT", 16, lambda s: int(float(s)))
    maybe("wallet_signal_top_wallets", "CLOBBOT_WALLET_SIGNAL_TOP_WALLETS", 8, lambda s: int(float(s)))
    maybe("wallet_signal_min_trades", "CLOBBOT_WALLET_SIGNAL_MIN_TRADES", 8, lambda s: int(float(s)))
    maybe("wallet_signal_max_trades", "CLOBBOT_WALLET_SIGNAL_MAX_TRADES", 600, lambda s: int(float(s)))
    maybe("wallet_signal_page_size", "CLOBBOT_WALLET_SIGNAL_PAGE_SIZE", 200, lambda s: int(float(s)))

    maybe("log_file", "CLOBBOT_LOG_FILE", "", str)
    maybe("state_file", "CLOBBOT_STATE_FILE", "", str)

    execute = _env_bool("CLOBBOT_EXECUTE")
    if execute is True and not args.execute:
        args.execute = True
        if not args.confirm_live:
            args.confirm_live = "YES"

    allow_best_only = _env_bool("CLOBBOT_ALLOW_BEST_ONLY")
    if allow_best_only is True and not getattr(args, "allow_best_only", False):
        args.allow_best_only = True

    sports_live_only = _env_bool("CLOBBOT_SPORTS_LIVE_ONLY")
    if sports_live_only is True and not getattr(args, "sports_live_only", False):
        args.sports_live_only = True

    sports_require_matchup = _env_bool("CLOBBOT_SPORTS_REQUIRE_MATCHUP")
    if sports_require_matchup is True and not getattr(args, "sports_require_matchup", False):
        args.sports_require_matchup = True

    sports_feed_strict = _env_bool("CLOBBOT_SPORTS_FEED_STRICT")
    if sports_feed_strict is True and not getattr(args, "sports_feed_strict", False):
        args.sports_feed_strict = True

    notify_observe_signals = _env_bool("CLOBBOT_NOTIFY_OBSERVE_SIGNALS")
    if notify_observe_signals is True and not getattr(args, "notify_observe_signals", False):
        args.notify_observe_signals = True

    observe_exec_edge_filter = _env_bool("CLOBBOT_OBSERVE_EXEC_EDGE_FILTER")
    if observe_exec_edge_filter is True and not getattr(args, "observe_exec_edge_filter", False):
        args.observe_exec_edge_filter = True

    metrics_log_all_candidates = _env_bool("CLOBBOT_METRICS_LOG_ALL_CANDIDATES")
    if metrics_log_all_candidates is True and not getattr(args, "metrics_log_all_candidates", False):
        args.metrics_log_all_candidates = True

    wallet_signal_enable = _env_bool("CLOBBOT_WALLET_SIGNAL_ENABLE")
    if wallet_signal_enable is True and not getattr(args, "wallet_signal_enable", False):
        args.wallet_signal_enable = True

    if args.exec_backend not in {"auto", "clob", "simmer"}:
        args.exec_backend = "auto"
    if getattr(args, "universe", "weather") not in {"weather", "gamma-active", "btc-5m", "btc-updown"}:
        args.universe = "weather"
    if args.strategy not in {"buckets", "yes-no", "event-pair", "both", "all"}:
        args.strategy = "both"
    args.sports_feed_provider = str(getattr(args, "sports_feed_provider", "none") or "none").strip().lower()
    if args.sports_feed_provider not in {"none", "espn"}:
        args.sports_feed_provider = "none"

    return args


def build_clob_arb_parser(ws_url: str, default_espn_paths: Sequence[str]) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Realtime Polymarket CLOB basket arbitrage monitor")
    p.add_argument("--ws-url", default=ws_url, help="Market channel websocket URL")
    p.add_argument(
        "--universe",
        choices=("weather", "gamma-active", "btc-5m", "btc-updown"),
        default="weather",
        help=(
            "Universe to monitor "
            "(weather uses Simmer weather index; gamma-active uses Gamma active markets; "
            "btc-5m/btc-updown fetch rolling BTC up/down events by slug)"
        ),
    )
    p.add_argument("--limit", type=int, default=250, help="Max weather markets to build universe from")
    p.add_argument("--workers", type=int, default=24, help="Parallel workers for universe discovery")
    p.add_argument("--min-outcomes", type=int, default=4, help="Min bucket outcomes per event")
    p.add_argument("--gamma-limit", type=int, default=500, help="Max Gamma active markets to include (gamma-active)")
    p.add_argument("--gamma-offset", type=int, default=0, help="Gamma markets offset (pagination)")
    p.add_argument("--gamma-min-liquidity", type=float, default=0.0, help="Min Gamma liquidityNum filter (gamma-active)")
    p.add_argument(
        "--gamma-min-volume24hr", type=float, default=0.0, help="Min Gamma volume24hr filter (gamma-active)"
    )
    p.add_argument(
        "--gamma-scan-max-markets",
        type=int,
        default=5000,
        help="Max Gamma markets to scan for matches before giving up (gamma-active)",
    )
    p.add_argument(
        "--gamma-max-days-to-end",
        type=float,
        default=0.0,
        help="Hard filter: skip markets ending more than N days out (0=disabled, gamma-active)",
    )
    p.add_argument(
        "--gamma-score-halflife-days",
        type=float,
        default=30.0,
        help="Time decay half-life in days for gamma-active scoring (smaller => more short-dated markets)",
    )
    p.add_argument(
        "--gamma-include-regex",
        default="",
        help="Only include Gamma markets whose question/event slug matches this regex (case-insensitive)",
    )
    p.add_argument(
        "--gamma-exclude-regex",
        default="",
        help="Exclude Gamma markets whose question/event slug matches this regex (case-insensitive)",
    )
    p.add_argument(
        "--sports-live-only",
        action="store_true",
        help="For gamma yes-no baskets, only include likely sports markets that are live/near start-end window.",
    )
    p.add_argument(
        "--sports-live-prestart-min",
        type=float,
        default=10.0,
        help="With --sports-live-only: include markets this many minutes before scheduled start.",
    )
    p.add_argument(
        "--sports-live-postend-min",
        type=float,
        default=30.0,
        help="With --sports-live-only: keep markets this many minutes after finish/end timestamp.",
    )
    p.add_argument(
        "--sports-market-types",
        default="",
        help=(
            "Comma-separated sports market type allowlist for gamma yes-no "
            "(moneyline,spread,total,draw,btts,other). Empty=all."
        ),
    )
    p.add_argument(
        "--sports-market-types-exclude",
        default="",
        help=(
            "Comma-separated sports market type denylist for gamma yes-no "
            "(moneyline,spread,total,draw,btts,other)."
        ),
    )
    p.add_argument(
        "--sports-require-matchup",
        action="store_true",
        help="With --sports-live-only: require matchup text like 'A vs B' or 'A @ B' in question/event.",
    )
    p.add_argument(
        "--sports-feed-provider",
        choices=("none", "espn"),
        default="none",
        help="Optional external sports feed used to refine live-window detection (default none).",
    )
    p.add_argument(
        "--sports-feed-strict",
        action="store_true",
        help="With --sports-feed-provider: require a feed matchup hit for sports-live inclusion.",
    )
    p.add_argument(
        "--sports-feed-timeout-sec",
        type=float,
        default=5.0,
        help="HTTP timeout seconds for external sports feed fetch.",
    )
    p.add_argument(
        "--sports-feed-live-buffer-sec",
        type=float,
        default=90.0,
        help="Extra seconds around feed state transitions (pre/live/post) for window matching.",
    )
    p.add_argument(
        "--sports-feed-espn-paths",
        default=",".join(default_espn_paths),
        help=(
            "Comma-separated ESPN sport/league paths (without trailing /scoreboard), "
            "e.g. basketball/nba,soccer/eng.1"
        ),
    )
    p.add_argument(
        "--wallet-signal-enable",
        action="store_true",
        help="For gamma-active scored selection, blend holder-wallet quality into ranking (observe-only Data API).",
    )
    p.add_argument(
        "--wallet-signal-weight",
        type=float,
        default=0.25,
        help="Score blend weight for wallet signal in gamma-active ranking.",
    )
    p.add_argument(
        "--wallet-signal-max-baskets",
        type=int,
        default=80,
        help="Max top-ranked gamma baskets to enrich with wallet signal.",
    )
    p.add_argument(
        "--wallet-signal-holders-limit",
        type=int,
        default=16,
        help="Data API holder fetch limit per condition for wallet signal scoring.",
    )
    p.add_argument(
        "--wallet-signal-top-wallets",
        type=int,
        default=8,
        help="Top holders per condition used for wallet signal scoring.",
    )
    p.add_argument(
        "--wallet-signal-min-trades",
        type=int,
        default=8,
        help="Min market-specific trades required to score one holder wallet.",
    )
    p.add_argument(
        "--wallet-signal-max-trades",
        type=int,
        default=600,
        help="Max market-specific trades fetched per holder wallet.",
    )
    p.add_argument(
        "--wallet-signal-page-size",
        type=int,
        default=200,
        help="Page size for Data API trade fetch in wallet signal scoring.",
    )
    p.add_argument(
        "--btc-5m-windows-back",
        type=int,
        default=1,
        help="For universe=btc-5m/btc-updown: how many prior windows to include",
    )
    p.add_argument(
        "--btc-5m-windows-forward",
        type=int,
        default=1,
        help="For universe=btc-5m/btc-updown: how many future windows to include",
    )
    p.add_argument(
        "--btc-updown-window-minutes",
        default="5",
        help="For universe=btc-5m/btc-updown: comma-separated window sizes (supported: 5,15). Example: 5,15",
    )
    p.add_argument(
        "--strategy",
        choices=("buckets", "yes-no", "event-pair", "both", "all"),
        default="both",
        help="Arbitrage strategy to monitor (all=buckets+yes-no+event-pair, event-pair=YES+YES/NO+NO on binary negRisk events)",
    )

    p.add_argument("--shares", type=float, default=5.0, help="Shares per bucket leg")
    p.add_argument("--max-legs", type=int, default=0, help="Skip opportunities with more than N legs (0=unlimited)")
    p.add_argument("--min-edge-cents", type=float, default=1.0, help="Alert/execution threshold in cents")
    p.add_argument("--winner-fee-rate", type=float, default=0.0, help="Winner fee rate (default 0.0)")
    p.add_argument("--fixed-cost", type=float, default=0.0, help="Per-event fixed USD cost")
    p.add_argument("--alert-cooldown-sec", type=float, default=10.0, help="Suppress duplicate alerts")
    p.add_argument("--run-seconds", type=int, default=0, help="Auto-exit after N seconds (0=run forever)")
    p.add_argument("--summary-every-sec", type=float, default=0.0, help="Emit periodic summary line (0=disabled)")
    p.add_argument(
        "--notify-observe-signals",
        action="store_true",
        help="In observe-only mode, send Discord notification when threshold signal is detected.",
    )
    p.add_argument(
        "--observe-notify-min-interval-sec",
        type=float,
        default=30.0,
        help="With --notify-observe-signals: minimum seconds between observe signal notifications (global).",
    )
    p.add_argument(
        "--observe-exec-edge-filter",
        action="store_true",
        help="Observe-only: temporarily mute events whose exec-est edge stays below threshold for consecutive evaluations.",
    )
    p.add_argument(
        "--observe-exec-edge-min-usd",
        type=float,
        default=0.0,
        help="With --observe-exec-edge-filter: treat exec-est edge <= this USD value as a strike.",
    )
    p.add_argument(
        "--observe-exec-edge-strike-limit",
        type=int,
        default=3,
        help="With --observe-exec-edge-filter: strikes needed before muting an event.",
    )
    p.add_argument(
        "--observe-exec-edge-cooldown-sec",
        type=float,
        default=300.0,
        help="With --observe-exec-edge-filter: mute duration per event after strike limit is hit.",
    )
    p.add_argument(
        "--observe-exec-edge-filter-strategies",
        default="event-yes,event-no,event-pair",
        help="With --observe-exec-edge-filter: comma-separated strategy names to apply filter to (empty=all).",
    )
    p.add_argument(
        "--metrics-file",
        default="",
        help="Path to candidate metrics JSONL (default logs/clob-arb-monitor-metrics.jsonl, use 'off' to disable).",
    )
    p.add_argument(
        "--metrics-log-all-candidates",
        action="store_true",
        help="Write metrics for every evaluated candidate (default writes threshold-pass candidates only).",
    )
    p.add_argument(
        "--max-subscribe-tokens",
        type=int,
        default=0,
        help="Cap total subscribed token IDs (0=unlimited). Helps keep local PC stable.",
    )
    p.add_argument(
        "--min-eval-interval-ms",
        type=int,
        default=0,
        help="Debounce per-event evaluation (0=disabled). Helps reduce CPU on large universes.",
    )
    p.add_argument(
        "--max-markets-per-event",
        type=int,
        default=0,
        help="Limit subscribed markets per Gamma event id (0=unlimited). Helps diversify gamma-active universe.",
    )

    p.add_argument("--max-exec-per-day", type=int, default=20, help="Max successful executions per day")
    p.add_argument("--max-notional-per-day", type=float, default=200.0, help="Max basket cost deployed per day")
    p.add_argument("--max-open-orders", type=int, default=0, help="Max open orders allowed before blocking")
    p.add_argument("--max-consecutive-failures", type=int, default=3, help="Stop after N execution failures")
    p.add_argument("--daily-loss-limit-usd", type=float, default=0.0, help="Halt on daily pnl drawdown")
    p.add_argument("--pnl-check-interval-sec", type=float, default=60.0, help="PnL guard polling interval")
    p.add_argument(
        "--exec-book-stale-sec",
        type=float,
        default=5.0,
        help="Skip live execution if any leg book is older than N seconds (0=disabled).",
    )
    p.add_argument(
        "--allow-best-only",
        action="store_true",
        help="Allow execution using synthetic books derived from best_ask/best_bid-only updates (less reliable).",
    )

    p.add_argument("--execute", action="store_true", help="Enable live order submission")
    p.add_argument("--confirm-live", default="", help='Must be "YES" when --execute is enabled')
    p.add_argument(
        "--exec-backend",
        choices=("auto", "clob", "simmer"),
        default="auto",
        help="Live execution backend (auto detects credentials).",
    )

    p.add_argument("--simmer-venue", default="polymarket", help="Venue used for simmer batch execution")
    p.add_argument("--simmer-source", default="sdk:clob-arb", help="Source tag for simmer batch execution")
    p.add_argument("--simmer-min-amount", type=float, default=1.0, help="Min USD amount per leg in simmer mode")
    p.add_argument(
        "--no-simmer-unwind-partial",
        dest="simmer_unwind_partial",
        action="store_false",
        help="Do not auto-unwind successful legs after partial batch failures",
    )

    p.add_argument("--clob-host", default="https://clob.polymarket.com", help="CLOB host for execution")
    p.add_argument("--chain-id", type=int, default=137, help="EVM chain id for signing")
    p.add_argument("--exec-slippage-bps", type=float, default=50.0, help="Limit price cushion in bps")
    p.add_argument("--unwind-slippage-bps", type=float, default=150.0, help="Unwind price cushion in bps (clob)")
    p.add_argument("--exec-cooldown-sec", type=float, default=30.0, help="Per-event execution cooldown")
    p.add_argument("--exec-max-attempts", type=int, default=2, help="Max attempts for one candidate")
    p.add_argument("--exec-retry-delay-sec", type=float, default=2.0, help="Delay between retries")

    p.add_argument("--reconcile-polls", type=int, default=4, help="Number of fill reconciliation polls")
    p.add_argument("--reconcile-interval-sec", type=float, default=1.0, help="Seconds between reconciliation polls")
    p.add_argument("--min-fill-ratio", type=float, default=0.98, help="Required fill ratio per leg")
    p.add_argument(
        "--no-cancel-unfilled-on-fail",
        dest="cancel_unfilled_on_fail",
        action="store_false",
        help="Do not cancel lingering orders after failed reconciliation",
    )
    p.add_argument(
        "--no-clob-unwind-partial",
        dest="clob_unwind_partial",
        action="store_false",
        help="Do not attempt to unwind filled legs after partial clob fills",
    )
    p.set_defaults(cancel_unfilled_on_fail=True, clob_unwind_partial=True, simmer_unwind_partial=True)

    p.add_argument("--log-file", default="", help="Path to log file (optional)")
    p.add_argument("--state-file", default="", help="Path to runtime state json (optional)")
    return p


def parse_clob_arb_args(ws_url: str, default_espn_paths: Sequence[str], argv=None):
    parser = build_clob_arb_parser(ws_url=ws_url, default_espn_paths=default_espn_paths)
    args = parser.parse_args(argv)
    cli_tokens = list(sys.argv[1:] if argv is None else argv)
    return _apply_env_overrides(args, cli_tokens=cli_tokens, default_espn_paths=default_espn_paths)

