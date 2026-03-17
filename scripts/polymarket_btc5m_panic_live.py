#!/usr/bin/env python3
"""
BTC short-window panic strategy live wrapper.

Default behavior is observe-only. Live order submission is enabled only with:
  --execute --confirm-live YES

The wrapper reuses the panic observe loop and adds:
- guarded maker-order submission
- per-day entry and notional caps
- fill reconciliation against CLOB trade history
- alarm logging for drawdown / loss-streak / stale metrics
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from polymarket_btc5m_panic_observe import (
    Logger,
    RuntimeState,
    _append_metrics,
    _has_depth,
    _load_state,
    as_float,
    backup_and_reset_state_file,
    best_ask,
    best_bid,
    build_market_window,
    day_pnl_usd,
    detect_panic_signal,
    fetch_clob_book,
    fetch_coinbase_candle_open_close,
    fetch_coinbase_price,
    iso_now_local,
    now_ts,
    settle_active_position,
)

DEFAULT_REPO_ROOT = REPO_ROOT
DEFAULT_LOG_FILE = str(DEFAULT_REPO_ROOT / "logs" / "btc5m-panic-live.log")
DEFAULT_STATE_FILE = str(DEFAULT_REPO_ROOT / "logs" / "btc5m_panic_live_state.json")
DEFAULT_METRICS_FILE = str(DEFAULT_REPO_ROOT / "logs" / "btc5m-panic-live-metrics.jsonl")
DEFAULT_EXEC_LOG_FILE = str(DEFAULT_REPO_ROOT / "logs" / "btc5m-panic-live-exec.jsonl")
METRICS_STALE_ALARM_SEC = 3.0 * 3600.0
DEFAULT_FILL_RETRIES = 4
DEFAULT_FILL_SLEEP_SEC = 0.35


def default_live_runtime_paths(window_minutes: int) -> Tuple[str, str, str, str]:
    m = int(window_minutes)
    if m == 5:
        return (
            DEFAULT_LOG_FILE,
            DEFAULT_STATE_FILE,
            DEFAULT_METRICS_FILE,
            DEFAULT_EXEC_LOG_FILE,
        )
    logs_dir = DEFAULT_REPO_ROOT / "logs"
    return (
        str(logs_dir / f"btc{m}m-panic-live.log"),
        str(logs_dir / f"btc{m}m_panic_live_state.json"),
        str(logs_dir / f"btc{m}m-panic-live-metrics.jsonl"),
        str(logs_dir / f"btc{m}m-panic-live-exec.jsonl"),
    )


def q_cent_down(value: float) -> float:
    return float(
        Decimal(str(max(0.0, value))).quantize(
            Decimal("0.01"),
            rounding=ROUND_DOWN,
        )
    )


def q_size2_down(value: float) -> float:
    return float(
        Decimal(str(max(0.0, value))).quantize(
            Decimal("0.01"),
            rounding=ROUND_DOWN,
        )
    )


def append_jsonl(path: str, payload: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=True) + "\n")


@dataclass
class LiveRiskState:
    entries_today: int = 0
    notional_today_usd: float = 0.0
    live_orders_submitted: int = 0
    live_orders_filled: int = 0
    live_orders_failed: int = 0


def _default_live_meta() -> dict:
    return {
        "current_loss_streak": 0,
        "max_loss_streak": 0,
        "last_metrics_ts_ms": 0,
    }


def _load_live_state(path: Path) -> Tuple[RuntimeState, LiveRiskState, dict]:
    state = _load_state(path)
    raw = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raw = {}
        except Exception:
            raw = {}
    risk = LiveRiskState(
        entries_today=int(raw.get("entries_today") or 0),
        notional_today_usd=float(raw.get("notional_today_usd") or 0.0),
        live_orders_submitted=int(raw.get("live_orders_submitted") or 0),
        live_orders_filled=int(raw.get("live_orders_filled") or 0),
        live_orders_failed=int(raw.get("live_orders_failed") or 0),
    )
    meta = _default_live_meta()
    meta["current_loss_streak"] = int(raw.get("current_loss_streak") or 0)
    meta["max_loss_streak"] = int(raw.get("max_loss_streak") or 0)
    meta["last_metrics_ts_ms"] = int(raw.get("last_metrics_ts_ms") or 0)
    return state, risk, meta


def _save_live_state(path: Path, state: RuntimeState, risk: LiveRiskState, meta: dict) -> None:
    payload = asdict(state)
    payload.update(asdict(risk))
    payload.update(
        {
            "current_loss_streak": int(meta.get("current_loss_streak") or 0),
            "max_loss_streak": int(meta.get("max_loss_streak") or 0),
            "last_metrics_ts_ms": int(meta.get("last_metrics_ts_ms") or 0),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    tmp.replace(path)


def maybe_roll_day(state: RuntimeState, risk: LiveRiskState) -> float:
    prev_day_key = state.day_key
    pnl = day_pnl_usd(state)
    if state.day_key != prev_day_key:
        risk.entries_today = 0
        risk.notional_today_usd = 0.0
    return pnl


def clip_shares_to_notional(requested_shares: float, limit_price: float, max_notional_usd: float) -> float:
    if not (math.isfinite(requested_shares) and requested_shares > 0):
        return 0.0
    if not (math.isfinite(limit_price) and limit_price > 0):
        return 0.0
    shares = float(requested_shares)
    if math.isfinite(max_notional_usd) and max_notional_usd > 0:
        shares = min(shares, float(max_notional_usd) / float(limit_price))
    shares = q_size2_down(shares)
    if shares < 1.0:
        return 0.0
    return shares


def compute_maker_limit_price(entry_ask: float, current_bid: float) -> float:
    if not (math.isfinite(entry_ask) and entry_ask > 0):
        return math.nan
    inside_cap = float(entry_ask) - 0.01
    if math.isfinite(current_bid) and current_bid > 0:
        target = min(inside_cap, float(current_bid) + 0.01)
    else:
        target = inside_cap
    if target < 0.01:
        if math.isfinite(current_bid) and current_bid >= 0.01 and current_bid < entry_ask:
            target = float(current_bid)
        else:
            return math.nan
    price = q_cent_down(min(0.99, max(0.01, target)))
    if price <= 0.0 or price >= float(entry_ask):
        return math.nan
    return price


def extract_order_id(response) -> str:
    if not isinstance(response, dict):
        return ""
    for key in ("orderID", "order_id", "id"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def response_has_error(response) -> bool:
    if not isinstance(response, dict):
        return False
    for key in ("error", "errorMsg", "error_message"):
        if response.get(key):
            return True
    status = str(response.get("status") or "").strip().lower()
    return status in {"rejected", "failed", "error"}


def parse_iso_or_epoch_to_ms(value) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw <= 0:
            return None
        if raw > 10_000_000_000:
            return int(raw)
        return int(raw * 1000.0)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            raw = float(text)
            return parse_iso_or_epoch_to_ms(raw)
        except Exception:
            pass
        try:
            ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return int(ts.astimezone(timezone.utc).timestamp() * 1000.0)
        except Exception:
            return None
    return None


def _trade_size(trade: dict) -> float:
    for key in (
        "size",
        "amount",
        "filled_size",
        "matched_size",
        "maker_amount",
        "taker_amount",
    ):
        value = as_float(trade.get(key), math.nan)
        if math.isfinite(value) and value > 0:
            return float(value)
    return 0.0


def _trade_price(trade: dict, fallback_price: float) -> float:
    for key in ("price", "maker_price", "taker_price", "matched_price"):
        value = as_float(trade.get(key), math.nan)
        if math.isfinite(value) and value > 0:
            return float(value)
    return float(fallback_price)


def _trade_tx_hash(trade: dict) -> str:
    for key in ("transaction_hash", "transactionHash", "tx_hash", "txHash"):
        value = trade.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def trade_matches_order_id(trade: dict, order_id: str) -> bool:
    oid = str(order_id or "").strip()
    if not oid or not isinstance(trade, dict):
        return False
    if str(trade.get("taker_order_id") or "").strip() == oid:
        return True
    maker_orders = trade.get("maker_orders")
    if isinstance(maker_orders, list):
        for row in maker_orders:
            if not isinstance(row, dict):
                continue
            if str(row.get("order_id") or "").strip() == oid:
                return True
    return False


def fetch_matching_trades(
    client,
    order_id: str,
    asset_id: str,
    retries: int = DEFAULT_FILL_RETRIES,
    sleep_sec: float = DEFAULT_FILL_SLEEP_SEC,
) -> List[dict]:
    oid = str(order_id or "").strip()
    aid = str(asset_id or "").strip()
    if not oid or not aid:
        return []
    from py_clob_client.clob_types import TradeParams

    last: List[dict] = []
    for idx in range(max(1, int(retries))):
        try:
            trades = client.get_trades(TradeParams(asset_id=aid))
        except Exception:
            trades = []
        matched = [row for row in trades if trade_matches_order_id(row, oid)]
        if matched or idx >= int(retries) - 1:
            return matched
        last = matched
        if sleep_sec > 0 and idx < int(retries) - 1:
            time.sleep(float(sleep_sec))
    return last


def summarize_matching_trades(trades: List[dict], fallback_price: float) -> dict:
    total_shares = 0.0
    total_notional = 0.0
    trade_ids: List[str] = []
    tx_hash = ""
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        size = _trade_size(trade)
        if size <= 0:
            continue
        price = _trade_price(trade, fallback_price=fallback_price)
        total_shares += size
        total_notional += size * price
        trade_id = trade.get("id")
        if isinstance(trade_id, str) and trade_id.strip():
            trade_ids.append(trade_id.strip())
        if not tx_hash:
            tx_hash = _trade_tx_hash(trade)
    avg_fill_price = (
        float(total_notional / total_shares)
        if total_shares > 0
        else float(fallback_price)
    )
    return {
        "actual_fill_shares": float(total_shares),
        "actual_fill_price": float(avg_fill_price),
        "actual_fill_notional_usd": float(total_notional),
        "trade_count": len(trade_ids),
        "trade_ids": trade_ids,
        "tx_hash": tx_hash,
    }


def cancel_open_order_guard(client, order_id: str, logger: Logger) -> None:
    oid = str(order_id or "").strip()
    if not oid:
        return
    try:
        client.cancel(oid)
        logger.info(f"[{iso_now_local()}] cancel order_id={oid[:24]}")
    except Exception as exc:
        logger.info(
            f"[{iso_now_local()}] warn: cancel failed order_id={oid[:24]} "
            f"{type(exc).__name__}: {exc}"
        )


def collect_alarm_messages(
    state: RuntimeState,
    current_loss_streak: int = 0,
    last_metrics_age_sec: Optional[float] = None,
) -> Dict[str, str]:
    alarms: Dict[str, str] = {}
    day_pnl = state.pnl_total_usd - state.day_anchor_pnl_usd
    if day_pnl < -30.0:
        alarms["daily_pnl"] = f"ALARM: daily PnL ${day_pnl:+.2f} < -$30"
    if state.max_drawdown_usd > 50.0:
        alarms["drawdown"] = f"ALARM: max drawdown ${state.max_drawdown_usd:.2f} > $50"
    if int(current_loss_streak) > 10:
        alarms["loss_streak"] = f"ALARM: consecutive losses {int(current_loss_streak)} > 10"
    if last_metrics_age_sec is not None and last_metrics_age_sec > METRICS_STALE_ALARM_SEC:
        alarms["metrics_stale"] = (
            f"ALARM: metrics stale for {float(last_metrics_age_sec) / 3600.0:.1f}h > 3.0h"
        )
    return alarms


def check_alarms(
    state: RuntimeState,
    logger,
    current_loss_streak: int = 0,
    last_metrics_age_sec: Optional[float] = None,
) -> List[str]:
    alarms = list(
        collect_alarm_messages(
            state=state,
            current_loss_streak=current_loss_streak,
            last_metrics_age_sec=last_metrics_age_sec,
        ).values()
    )
    if logger is not None:
        for msg in alarms:
            logger.info(f"[{iso_now_local()}] {msg}")
    return alarms


def check_risk_limits(
    risk: LiveRiskState,
    state: RuntimeState,
    notional: float,
    args,
) -> Tuple[bool, str]:
    if state.halted:
        return False, f"halted: {state.halt_reason}"

    max_entries = int(getattr(args, "max_entries_per_day", 0))
    if max_entries > 0 and risk.entries_today >= max_entries:
        return False, f"daily entry cap ({risk.entries_today}/{max_entries})"

    max_notional = float(getattr(args, "max_notional_per_trade", 2.50))
    if max_notional > 0 and float(notional) > max_notional:
        return False, f"notional ${float(notional):.2f} > cap ${max_notional:.2f}"

    loss_limit = float(getattr(args, "daily_loss_limit_usd", 0.0))
    if loss_limit > 0:
        day_pnl = state.pnl_total_usd - state.day_anchor_pnl_usd
        if day_pnl <= -loss_limit:
            return False, f"daily loss limit (${day_pnl:+.2f} <= -${loss_limit:.2f})"

    return True, ""


def submit_live_order(
    client,
    token_id: str,
    price: float,
    size: float,
    logger,
    require_confirm: bool = False,
) -> dict:
    from py_clob_client.clob_types import OrderArgs, OrderType

    px = q_cent_down(min(0.99, max(0.01, float(price))))
    sz = q_size2_down(max(1.0, float(size)))

    if require_confirm:
        prompt = (
            f"LIVE ORDER: BUY {sz} shares @ ${px:.2f} "
            f"(token={str(token_id)[:16]}...) [y/N]: "
        )
        answer = input(prompt).strip().lower()
        if answer != "y":
            return {
                "success": False,
                "order_id": "",
                "reason": "user_declined",
            }

    logger.info(
        f"[{iso_now_local()}] LIVE submit BUY size={sz:.2f} "
        f"price={px:.2f} token={str(token_id)[:20]}"
    )
    try:
        order = client.create_order(
            OrderArgs(
                token_id=str(token_id),
                price=float(px),
                size=float(sz),
                side="BUY",
            )
        )
        response = client.post_order(order, orderType=OrderType.GTC, post_only=True)
        order_id = extract_order_id(response)
        if response_has_error(response) and not order_id:
            return {
                "success": False,
                "order_id": "",
                "reason": str(response)[:300],
            }
        return {
            "success": True,
            "order_id": order_id,
            "response": response,
        }
    except Exception as exc:
        return {
            "success": False,
            "order_id": "",
            "reason": f"{type(exc).__name__}: {exc}",
        }


def _update_loss_streak(meta: dict, before_counts: Tuple[int, int, int], state: RuntimeState) -> None:
    before_wins, before_losses, before_pushes = before_counts
    if state.losses > before_losses:
        meta["current_loss_streak"] = int(meta.get("current_loss_streak") or 0) + 1
        meta["max_loss_streak"] = max(
            int(meta.get("max_loss_streak") or 0),
            int(meta["current_loss_streak"]),
        )
        return
    if state.wins > before_wins or state.pushes > before_pushes:
        meta["current_loss_streak"] = 0


def build_live_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="BTC short-window panic strategy live wrapper"
    )
    parser.add_argument(
        "--window-minutes",
        type=int,
        choices=[5, 15],
        default=5,
        help="Target market window size in minutes (5 or 15)",
    )
    parser.add_argument("--poll-sec", type=float, default=1.0, help="Polling interval seconds")
    parser.add_argument("--summary-every-sec", type=float, default=15.0, help="Summary cadence seconds (0=disabled)")
    parser.add_argument("--metrics-sample-sec", type=float, default=5.0, help="Metrics cadence seconds (0=disabled)")
    parser.add_argument("--run-seconds", type=int, default=0, help="Auto-exit after N seconds (0=run forever)")

    parser.add_argument("--shares", type=float, default=25.0, help="Target entry size in shares")
    parser.add_argument("--cheap-ask-max-cents", type=float, default=10.0, help="Cheap-side ask threshold in cents")
    parser.add_argument("--expensive-ask-min-cents", type=float, default=90.0, help="Expensive-side ask threshold in cents")
    parser.add_argument("--min-remaining-sec", type=float, default=20.0, help="Do not enter if less than this many sec remain")
    parser.add_argument(
        "--max-remaining-sec",
        type=float,
        default=120.0,
        help="Do not enter if more than this many sec remain (0=disabled)",
    )
    parser.add_argument(
        "--no-max-one-entry-per-window",
        dest="max_one_entry_per_window",
        action="store_false",
        help="Allow multiple entries in one window",
    )
    parser.set_defaults(max_one_entry_per_window=True)
    parser.add_argument("--settle-epsilon-usd", type=float, default=0.5, help="Treat close-open within epsilon as PUSH")
    parser.add_argument("--taker-fee-rate", type=float, default=0.02, help="Assumed taker fee rate for PnL accounting")
    parser.add_argument("--slippage-cents", type=float, default=0.5, help="Conservative slippage per share in cents")
    parser.add_argument("--max-spread-cents", type=float, default=8.0, help="Skip entry if cheap-side spread exceeds this (cents)")
    parser.add_argument("--min-ask-depth", type=float, default=10.0, help="Skip entry if cheap-side ask depth < this shares")
    parser.add_argument("--daily-loss-limit-usd", type=float, default=50.0, help="Halt entries if day pnl <= -limit")
    parser.add_argument("--max-consecutive-errors", type=int, default=10, help="Halt after N consecutive loop errors")
    parser.add_argument("--reset-state", action="store_true", help="Backup and reset state file for a fresh OOS/live dry-run")

    parser.add_argument("--execute", action="store_true", help="Enable live order submission")
    parser.add_argument("--confirm-live", default="", help="Must be YES to confirm live mode")
    parser.add_argument("--require-confirm", action="store_true", help="Prompt before each live order")
    parser.add_argument("--max-notional-per-trade", type=float, default=2.50, help="Max USD notional per entry")
    parser.add_argument("--max-entries-per-day", type=int, default=30, help="Max paper/live entries per day")

    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE, help="Runtime log path")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="State JSON path")
    parser.add_argument("--metrics-file", default=DEFAULT_METRICS_FILE, help="Metrics JSONL path")
    parser.add_argument("--exec-log-file", default=DEFAULT_EXEC_LOG_FILE, help="Execution audit JSONL path")

    parser.add_argument("--clob-host", default="https://clob.polymarket.com")
    parser.add_argument("--chain-id", type=int, default=137)
    return parser


def _resolve_default_paths(args) -> None:
    d_log, d_state, d_metrics, d_exec = default_live_runtime_paths(int(args.window_minutes))
    if args.log_file == DEFAULT_LOG_FILE:
        args.log_file = d_log
    if args.state_file == DEFAULT_STATE_FILE:
        args.state_file = d_state
    if args.metrics_file == DEFAULT_METRICS_FILE:
        args.metrics_file = d_metrics
    if args.exec_log_file == DEFAULT_EXEC_LOG_FILE:
        args.exec_log_file = d_exec


def main() -> int:
    args = build_live_parser().parse_args()
    _resolve_default_paths(args)

    if args.execute and str(args.confirm_live).strip() != "YES":
        print("ERROR: live execution requires --execute --confirm-live YES")
        return 2

    mode_label = "LIVE" if args.execute else "OBSERVE"
    logger = Logger(args.log_file)
    state_path = Path(args.state_file)

    if bool(args.reset_state):
        state = backup_and_reset_state_file(state_path, logger)
        risk = LiveRiskState()
        meta = _default_live_meta()
        _save_live_state(state_path, state, risk, meta)
    else:
        state, risk, meta = _load_live_state(state_path)

    client = None
    if args.execute:
        try:
            from lib.clob_auth import build_clob_client_from_env

            client = build_clob_client_from_env(
                clob_host=str(args.clob_host),
                chain_id=int(args.chain_id),
                missing_env_message=(
                    "Missing env for CLOB auth. Need PM_PRIVATE_KEY and PM_FUNDER."
                ),
                invalid_key_message="Invalid PM_PRIVATE_KEY format.",
            )
        except Exception as exc:
            print(f"ERROR: could not initialize CLOB client: {exc}")
            return 2

    window_sec = int(args.window_minutes) * 60
    cheap_ask_max = float(args.cheap_ask_max_cents) / 100.0
    expensive_ask_min = float(args.expensive_ask_min_cents) / 100.0

    logger.info(f"Polymarket BTC {int(args.window_minutes)}m Panic {mode_label} Wrapper")
    logger.info("=" * 64)
    logger.info(
        f"Config: poll={args.poll_sec:.2f}s summary={args.summary_every_sec:.1f}s "
        f"metrics={args.metrics_sample_sec:.1f}s shares={args.shares:.2f} "
        f"cheap_ask<={cheap_ask_max:.3f} expensive_ask>={expensive_ask_min:.3f} "
        f"rem=[{args.min_remaining_sec:.1f}s,{args.max_remaining_sec:.1f}s] "
        f"live={bool(args.execute)}"
    )
    logger.info(
        f"Risk: max_notional=${float(args.max_notional_per_trade):.2f} "
        f"daily_loss=${float(args.daily_loss_limit_usd):.2f} "
        f"max_entries={int(args.max_entries_per_day)}"
    )
    logger.info(f"Log: {args.log_file}")
    logger.info(f"State: {args.state_file}")
    logger.info(f"Metrics: {args.metrics_file}")
    logger.info(f"Exec log: {args.exec_log_file}")
    if state.halted:
        logger.info(f"Resume state: HALTED ({state.halt_reason})")

    start_ts = now_ts()
    last_summary_ts = 0.0
    last_metrics_ts = (
        float(int(meta.get("last_metrics_ts_ms") or 0)) / 1000.0
        if int(meta.get("last_metrics_ts_ms") or 0) > 0
        else 0.0
    )
    last_alert_key = ""
    last_alert_ts = 0.0
    alarm_latches: set[str] = set()
    current_window = None
    entered_windows: Dict[int, bool] = {}
    spot: Optional[float] = None

    while True:
        if args.run_seconds > 0 and (now_ts() - start_ts) >= float(args.run_seconds):
            logger.info(f"[{iso_now_local()}] run-seconds reached; exiting.")
            _save_live_state(state_path, state, risk, meta)
            return 0

        try:
            ts_now = now_ts()
            state.consecutive_errors = 0
            maybe_roll_day(state, risk)
            spot = fetch_coinbase_price()

            window_start = (int(ts_now) // window_sec) * window_sec
            if (current_window is None) or (window_start != current_window.start_ts):
                prev_window = current_window
                prev_start = prev_window.start_ts if prev_window is not None else 0

                if prev_window is not None and state.active_position is not None:
                    close_px = None
                    _open_px, close_px = fetch_coinbase_candle_open_close(prev_start, window_sec)
                    if close_px is None and spot is not None and math.isfinite(spot) and spot > 0:
                        close_px = float(spot)
                    if close_px is not None and math.isfinite(close_px) and close_px > 0:
                        before_counts = (state.wins, state.losses, state.pushes)
                        settle_active_position(
                            state=state,
                            close_price=float(close_px),
                            settle_epsilon=float(args.settle_epsilon_usd),
                            taker_fee_rate=float(args.taker_fee_rate),
                            logger=logger,
                        )
                        _update_loss_streak(meta, before_counts, state)

                current_window = build_market_window(window_start, int(args.window_minutes))
                if current_window is None:
                    state.current_window_start_ts = window_start
                    state.current_window_slug = f"btc-updown-{int(args.window_minutes)}m-{window_start}"
                    state.current_window_open_price = 0.0
                    logger.info(
                        f"[{iso_now_local()}] window {state.current_window_slug}: market fetch failed; waiting."
                    )
                else:
                    open_px, _close_px = fetch_coinbase_candle_open_close(window_start, window_sec)
                    if open_px is None and spot is not None and math.isfinite(spot) and spot > 0:
                        open_px = float(spot)
                    state.current_window_start_ts = current_window.start_ts
                    state.current_window_slug = current_window.slug
                    state.current_window_open_price = float(open_px) if open_px else 0.0
                    logger.info(
                        f"[{iso_now_local()}] window {current_window.slug} | market={current_window.market_id} "
                        f"| open={state.current_window_open_price:.2f} | labels={current_window.up_label}/{current_window.down_label}"
                    )

                for key in list(entered_windows.keys()):
                    if key < window_start - (3 * window_sec):
                        entered_windows.pop(key, None)

            up_ask = down_ask = up_bid = down_bid = math.nan
            remaining_sec = 0.0
            trigger_side = ""
            trigger_entry_px = math.nan
            trigger_expensive_px = math.nan
            trigger_reason = ""

            if current_window is not None:
                up_book = fetch_clob_book(current_window.up_token_id)
                down_book = fetch_clob_book(current_window.down_token_id)
                up_ask = best_ask(up_book)
                down_ask = best_ask(down_book)
                up_bid = best_bid(up_book)
                down_bid = best_bid(down_book)
                remaining_sec = max(0.0, float(current_window.end_ts - ts_now))

                trigger_side, trigger_entry_px, trigger_expensive_px, trigger_reason = detect_panic_signal(
                    up_ask=up_ask,
                    down_ask=down_ask,
                    cheap_ask_max=cheap_ask_max,
                    expensive_ask_min=expensive_ask_min,
                )

                if trigger_side:
                    alert_key = f"{current_window.start_ts}:{trigger_side}"
                    if alert_key != last_alert_key or (ts_now - last_alert_ts) >= 5.0:
                        state.signals_total += 1
                        logger.info(
                            f"[{iso_now_local()}] signal side={trigger_side} reason={trigger_reason} "
                            f"entry={trigger_entry_px:.4f} expensive={trigger_expensive_px:.4f} "
                            f"ask_up={up_ask:.4f} ask_down={down_ask:.4f} rem={remaining_sec:.1f}s"
                        )
                        last_alert_key = alert_key
                        last_alert_ts = ts_now

                if args.daily_loss_limit_usd > 0 and not state.halted:
                    day_pnl = maybe_roll_day(state, risk)
                    if day_pnl <= -float(args.daily_loss_limit_usd):
                        state.halted = True
                        state.halt_reason = (
                            f"Daily loss limit reached ({day_pnl:+.4f} <= -{float(args.daily_loss_limit_usd):.4f})"
                        )
                        logger.info(f"[{iso_now_local()}] HALT: {state.halt_reason}")

                allow_entry = remaining_sec >= float(args.min_remaining_sec)
                if float(args.max_remaining_sec) > 0 and remaining_sec > float(args.max_remaining_sec):
                    allow_entry = False

                if (
                    allow_entry
                    and not state.halted
                    and state.active_position is None
                    and trigger_side
                    and math.isfinite(trigger_entry_px)
                    and trigger_entry_px > 0
                ):
                    if not (args.max_one_entry_per_window and entered_windows.get(current_window.start_ts, False)):
                        cheap_book = up_book if trigger_side == "UP" else down_book
                        cheap_bid = best_bid(cheap_book)
                        spread = (
                            trigger_entry_px - cheap_bid
                            if (math.isfinite(cheap_bid) and cheap_bid > 0)
                            else math.nan
                        )
                        if math.isfinite(spread) and spread > (float(args.max_spread_cents) / 100.0):
                            state.filter_skip_spread += 1
                        elif not _has_depth(cheap_book, float(args.min_ask_depth)):
                            state.filter_skip_depth += 1
                        elif args.execute:
                            live_limit_px = compute_maker_limit_price(trigger_entry_px, cheap_bid)
                            live_shares = clip_shares_to_notional(
                                requested_shares=float(args.shares),
                                limit_price=live_limit_px,
                                max_notional_usd=float(args.max_notional_per_trade),
                            )
                            if not (math.isfinite(live_limit_px) and live_limit_px > 0 and live_shares >= 1.0):
                                logger.info(
                                    f"[{iso_now_local()}] skip live entry side={trigger_side}: no safe maker price/size "
                                    f"(ask={trigger_entry_px:.4f} bid={cheap_bid:.4f})"
                                )
                                entered_windows[current_window.start_ts] = True
                            else:
                                notional = live_limit_px * live_shares
                                allowed, reason = check_risk_limits(
                                    risk=risk,
                                    state=state,
                                    notional=notional,
                                    args=args,
                                )
                                if not allowed:
                                    logger.info(
                                        f"[{iso_now_local()}] skip live entry side={trigger_side}: {reason}"
                                    )
                                else:
                                    token_id = (
                                        current_window.up_token_id
                                        if trigger_side == "UP"
                                        else current_window.down_token_id
                                    )
                                    risk.live_orders_submitted += 1
                                    submit = submit_live_order(
                                        client=client,
                                        token_id=token_id,
                                        price=live_limit_px,
                                        size=live_shares,
                                        logger=logger,
                                        require_confirm=bool(args.require_confirm),
                                    )
                                    event = {
                                        "ts": iso_now_local(),
                                        "mode": "LIVE",
                                        "window_slug": current_window.slug,
                                        "market_id": current_window.market_id,
                                        "token_id": token_id,
                                        "side": trigger_side,
                                        "screen_entry_price": float(trigger_entry_px),
                                        "limit_price": float(live_limit_px),
                                        "shares_requested": float(live_shares),
                                        "remaining_sec": float(remaining_sec),
                                    }
                                    if not submit.get("success"):
                                        if submit.get("reason") != "user_declined":
                                            risk.live_orders_failed += 1
                                        event["status"] = "order_failed"
                                        event["reason"] = str(submit.get("reason") or "")
                                        append_jsonl(args.exec_log_file, event)
                                        logger.info(
                                            f"[{iso_now_local()}] live order failed side={trigger_side}: {event['reason']}"
                                        )
                                        if submit.get("reason") == "user_declined":
                                            entered_windows[current_window.start_ts] = True
                                    else:
                                        order_id = str(submit.get("order_id") or "")
                                        event["status"] = "submitted"
                                        event["order_id"] = order_id
                                        append_jsonl(args.exec_log_file, event)
                                        entered_windows[current_window.start_ts] = True
                                        fills = fetch_matching_trades(
                                            client=client,
                                            order_id=order_id,
                                            asset_id=token_id,
                                        )
                                        fill_summary = summarize_matching_trades(
                                            fills,
                                            fallback_price=live_limit_px,
                                        )
                                        if fill_summary["actual_fill_shares"] <= 0:
                                            cancel_open_order_guard(client, order_id, logger)
                                            event["status"] = "cancelled_unfilled"
                                            append_jsonl(args.exec_log_file, event)
                                            logger.info(
                                                f"[{iso_now_local()}] live order no fill side={trigger_side} "
                                                f"order_id={order_id[:24]}"
                                            )
                                        else:
                                            risk.entries_today += 1
                                            risk.notional_today_usd += float(fill_summary["actual_fill_notional_usd"])
                                            risk.live_orders_filled += 1
                                            state.entries_total += 1
                                            fill_price = float(fill_summary["actual_fill_price"])
                                            fill_shares = float(fill_summary["actual_fill_shares"])
                                            state.active_position = {
                                                "window_start_ts": current_window.start_ts,
                                                "window_slug": current_window.slug,
                                                "market_id": current_window.market_id,
                                                "token_id": token_id,
                                                "side": trigger_side,
                                                "shares": fill_shares,
                                                "entry_price": fill_price,
                                                "entry_ts": int(ts_now),
                                                "trigger_reason": trigger_reason,
                                                "expensive_price": float(trigger_expensive_px),
                                                "window_open_price": float(state.current_window_open_price),
                                                "slippage_cents": float(args.slippage_cents),
                                                "order_id": order_id,
                                                "tx_hash": str(fill_summary.get("tx_hash") or ""),
                                                "actual_fill_price": fill_price,
                                                "actual_fill_shares": fill_shares,
                                                "actual_fill_notional_usd": float(fill_summary["actual_fill_notional_usd"]),
                                                "screen_entry_price": float(trigger_entry_px),
                                                "maker_limit_price": float(live_limit_px),
                                                "trade_ids": list(fill_summary.get("trade_ids") or []),
                                            }
                                            event.update(
                                                {
                                                    "status": "filled",
                                                    "actual_fill_price": fill_price,
                                                    "actual_fill_shares": fill_shares,
                                                    "actual_fill_notional_usd": float(fill_summary["actual_fill_notional_usd"]),
                                                    "tx_hash": str(fill_summary.get("tx_hash") or ""),
                                                }
                                            )
                                            append_jsonl(args.exec_log_file, event)
                                            logger.info(
                                                f"[{iso_now_local()}] LIVE ENTER side={trigger_side} shares={fill_shares:.2f} "
                                                f"fill={fill_price:.4f} screen={trigger_entry_px:.4f} "
                                                f"rem={remaining_sec:.1f}s window={current_window.slug}"
                                            )
                        else:
                            notional = float(args.shares) * float(trigger_entry_px)
                            allowed, reason = check_risk_limits(
                                risk=risk,
                                state=state,
                                notional=notional,
                                args=args,
                            )
                            if not allowed:
                                logger.info(
                                    f"[{iso_now_local()}] skip paper entry side={trigger_side}: {reason}"
                                )
                            else:
                                state.active_position = {
                                    "window_start_ts": current_window.start_ts,
                                    "window_slug": current_window.slug,
                                    "market_id": current_window.market_id,
                                    "side": trigger_side,
                                    "shares": float(args.shares),
                                    "entry_price": float(trigger_entry_px),
                                    "entry_ts": int(ts_now),
                                    "trigger_reason": trigger_reason,
                                    "expensive_price": float(trigger_expensive_px),
                                    "window_open_price": float(state.current_window_open_price),
                                    "slippage_cents": float(args.slippage_cents),
                                }
                                risk.entries_today += 1
                                risk.notional_today_usd += float(notional)
                                state.entries_total += 1
                                entered_windows[current_window.start_ts] = True
                                logger.info(
                                    f"[{iso_now_local()}] paper ENTER side={trigger_side} shares={args.shares:.2f} "
                                    f"entry={trigger_entry_px:.4f} expensive={trigger_expensive_px:.4f} "
                                    f"rem={remaining_sec:.1f}s window={current_window.slug}"
                                )

            if args.summary_every_sec > 0 and (ts_now - last_summary_ts) >= float(args.summary_every_sec):
                active = state.active_position
                active_txt = "none"
                if isinstance(active, dict):
                    active_txt = (
                        f"{str(active.get('side') or '')}@{as_float(active.get('entry_price'), 0.0):.4f}"
                        f"x{as_float(active.get('shares'), 0.0):.2f}"
                    )
                day_pnl = maybe_roll_day(state, risk)
                logger.info(
                    f"[{iso_now_local()}] summary mode={mode_label} "
                    f"window={state.current_window_slug or '-'} rem={remaining_sec:.1f}s "
                    f"ask_up={up_ask:.4f} ask_down={down_ask:.4f} "
                    f"trigger={trigger_side or '-'} entry={trigger_entry_px:.4f} active={active_txt} "
                    f"day={day_pnl:+.4f} total={state.pnl_total_usd:+.4f} "
                    f"W/L/P={state.wins}/{state.losses}/{state.pushes} "
                    f"loss_streak={int(meta.get('current_loss_streak') or 0)} "
                    f"signals={state.signals_total} entries={state.entries_total} "
                    f"entries_today={risk.entries_today} submitted={risk.live_orders_submitted} "
                    f"filled={risk.live_orders_filled} failed={risk.live_orders_failed} "
                    f"dd={state.max_drawdown_usd:.4f} "
                    f"fskip_spread={state.filter_skip_spread} fskip_depth={state.filter_skip_depth} "
                    f"halted={state.halted}"
                )
                last_summary_ts = ts_now

            if args.metrics_sample_sec > 0 and (ts_now - last_metrics_ts) >= float(args.metrics_sample_sec):
                _append_metrics(
                    args.metrics_file,
                    {
                        "ts": iso_now_local(),
                        "ts_ms": int(ts_now * 1000.0),
                        "mode": mode_label,
                        "window_start_ts": int(state.current_window_start_ts or 0),
                        "window_slug": state.current_window_slug,
                        "spot_btc_usd": float(spot) if spot is not None and math.isfinite(spot) else None,
                        "window_open_btc_usd": float(state.current_window_open_price or 0.0),
                        "remaining_sec": remaining_sec,
                        "ask_up": up_ask,
                        "bid_up": up_bid,
                        "ask_down": down_ask,
                        "bid_down": down_bid,
                        "trigger_side": trigger_side,
                        "trigger_entry_px": trigger_entry_px if math.isfinite(trigger_entry_px) else None,
                        "active_position": state.active_position,
                        "pnl_total_usd": state.pnl_total_usd,
                        "day_pnl_usd": maybe_roll_day(state, risk),
                        "trades_closed": state.trades_closed,
                        "wins": state.wins,
                        "losses": state.losses,
                        "pushes": state.pushes,
                        "entries_today": risk.entries_today,
                        "live_orders_submitted": risk.live_orders_submitted,
                        "live_orders_filled": risk.live_orders_filled,
                        "live_orders_failed": risk.live_orders_failed,
                        "current_loss_streak": int(meta.get("current_loss_streak") or 0),
                        "max_loss_streak": int(meta.get("max_loss_streak") or 0),
                        "filter_skip_spread": int(state.filter_skip_spread),
                        "filter_skip_depth": int(state.filter_skip_depth),
                        "halted": state.halted,
                    },
                )
                last_metrics_ts = ts_now
                meta["last_metrics_ts_ms"] = int(ts_now * 1000.0)

            metrics_age_sec = (
                max(0.0, ts_now - last_metrics_ts)
                if args.metrics_sample_sec > 0 and last_metrics_ts > 0
                else None
            )
            alarm_map = collect_alarm_messages(
                state=state,
                current_loss_streak=int(meta.get("current_loss_streak") or 0),
                last_metrics_age_sec=metrics_age_sec,
            )
            for key, msg in alarm_map.items():
                if key not in alarm_latches:
                    logger.info(f"[{iso_now_local()}] {msg}")
            alarm_latches = set(alarm_map.keys())

            _save_live_state(state_path, state, risk, meta)
            time.sleep(max(0.1, float(args.poll_sec)))

        except Exception as exc:
            state.consecutive_errors += 1
            logger.info(f"[{iso_now_local()}] error: loop exception: {type(exc).__name__}: {exc}")
            if state.consecutive_errors >= int(args.max_consecutive_errors):
                state.halted = True
                state.halt_reason = (
                    f"Consecutive errors cap reached ({state.consecutive_errors}/{args.max_consecutive_errors})"
                )
                logger.info(f"[{iso_now_local()}] HALT: {state.halt_reason}")
            _save_live_state(state_path, state, risk, meta)
            time.sleep(1.5)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
