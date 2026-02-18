#!/usr/bin/env python3
"""
bitFlyer public-board market-making observer (simulation only).

Purpose:
- Observe BTC/JPY order book and simulate maker-style quoting.
- Never sends orders. This script is always observe-only.
- Keep runtime outputs under logs/ (gitignored).

Simulation model (simple):
- Quote around current mid:
  bid_quote = mid - quote_half_spread_yen
  ask_quote = mid + quote_half_spread_yen
- A BUY fill is simulated if market ask <= bid_quote.
- A SELL fill is simulated if market bid >= ask_quote.

This is a rough approximation for strategy tuning only.
It does not represent real fill probability, queue position, or slippage.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BITFLYER_API_BASE = "https://api.bitflyer.com"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_FILE = str(DEFAULT_REPO_ROOT / "logs" / "bitflyer-mm-observe.log")
DEFAULT_STATE_FILE = str(DEFAULT_REPO_ROOT / "logs" / "bitflyer_mm_observe_state.json")
DEFAULT_METRICS_FILE = str(DEFAULT_REPO_ROOT / "logs" / "bitflyer-mm-observe-metrics.jsonl")


def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_ts() -> float:
    return time.time()


def local_day_key() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _env_str(name: str) -> str:
    return str(os.environ.get(name, "") or "").strip()


def _env_int(name: str) -> Optional[int]:
    v = _env_str(name)
    if not v:
        return None
    try:
        return int(float(v))
    except Exception:
        return None


def _env_float(name: str) -> Optional[float]:
    v = _env_str(name)
    if not v:
        return None
    try:
        return float(v)
    except Exception:
        return None


class Logger:
    def __init__(self, log_file: str):
        self.log_file = Path(log_file)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, msg: str) -> None:
        with self.log_file.open("a", encoding="utf-8", newline="\n") as f:
            f.write(msg + "\n")

    def info(self, msg: str) -> None:
        # Avoid crashing on Windows consoles with legacy encodings (cp932, etc.).
        try:
            print(msg)
        except UnicodeEncodeError:
            try:
                enc = getattr(sys.stdout, "encoding", None) or "utf-8"
                print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))
            except Exception:
                pass
        self._append(msg)


def _http_get_json(url: str, timeout_sec: float = 7.0) -> dict:
    req = Request(
        url,
        headers={
            "User-Agent": "bitflyer-mm-observe/1.0",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urlopen(req, timeout=timeout_sec) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def fetch_board(product_code: str) -> dict:
    url = f"{BITFLYER_API_BASE}/v1/board?product_code={product_code}"
    return _http_get_json(url, timeout_sec=7.0)


def _extract_best(book: dict) -> Tuple[float, float]:
    bids = book.get("bids") if isinstance(book, dict) else None
    asks = book.get("asks") if isinstance(book, dict) else None
    if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
        return math.nan, math.nan
    try:
        best_bid = float((bids[0] or {}).get("price"))
        best_ask = float((asks[0] or {}).get("price"))
        return best_bid, best_ask
    except Exception:
        return math.nan, math.nan


def _q_down(x: float, tick: float) -> float:
    if tick <= 0:
        return x
    return math.floor(x / tick + 1e-12) * tick


def _q_up(x: float, tick: float) -> float:
    if tick <= 0:
        return x
    return math.ceil(x / tick - 1e-12) * tick


@dataclass
class RuntimeState:
    inventory_btc: float = 0.0
    avg_entry_jpy: float = 0.0
    realized_pnl_jpy: float = 0.0
    last_mid_jpy: float = 0.0
    quote_bid_jpy: float = 0.0
    quote_ask_jpy: float = 0.0
    total_buy_fills: int = 0
    total_sell_fills: int = 0
    day_key: str = ""
    day_anchor_total_pnl_jpy: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    consecutive_errors: int = 0


def _load_state(path: Path) -> RuntimeState:
    if not path.exists():
        return RuntimeState(day_key=local_day_key())
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return RuntimeState(day_key=local_day_key())
        s = RuntimeState(
            inventory_btc=float(raw.get("inventory_btc") or 0.0),
            avg_entry_jpy=float(raw.get("avg_entry_jpy") or 0.0),
            realized_pnl_jpy=float(raw.get("realized_pnl_jpy") or 0.0),
            last_mid_jpy=float(raw.get("last_mid_jpy") or 0.0),
            quote_bid_jpy=float(raw.get("quote_bid_jpy") or 0.0),
            quote_ask_jpy=float(raw.get("quote_ask_jpy") or 0.0),
            total_buy_fills=int(raw.get("total_buy_fills") or 0),
            total_sell_fills=int(raw.get("total_sell_fills") or 0),
            day_key=str(raw.get("day_key") or local_day_key()),
            day_anchor_total_pnl_jpy=float(raw.get("day_anchor_total_pnl_jpy") or 0.0),
            halted=bool(raw.get("halted") or False),
            halt_reason=str(raw.get("halt_reason") or ""),
            consecutive_errors=int(raw.get("consecutive_errors") or 0),
        )
        return s
    except Exception:
        return RuntimeState(day_key=local_day_key())


def _save_state(path: Path, state: RuntimeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), ensure_ascii=True, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_metrics(path: str, payload: dict) -> None:
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _total_pnl_jpy(state: RuntimeState) -> float:
    unrealized = 0.0
    if state.inventory_btc > 0 and state.avg_entry_jpy > 0 and state.last_mid_jpy > 0:
        unrealized = (state.last_mid_jpy - state.avg_entry_jpy) * state.inventory_btc
    return float(state.realized_pnl_jpy + unrealized)


def _day_pnl_jpy(state: RuntimeState) -> float:
    day = local_day_key()
    cur_total = _total_pnl_jpy(state)
    if state.day_key != day:
        state.day_key = day
        state.day_anchor_total_pnl_jpy = cur_total
    return float(cur_total - state.day_anchor_total_pnl_jpy)


def _buy_fill(state: RuntimeState, fill_price: float, size_btc: float, maker_fee_bps: float) -> None:
    if size_btc <= 0 or fill_price <= 0:
        return
    fee = fill_price * size_btc * (maker_fee_bps / 10000.0)
    total_cost_before = state.avg_entry_jpy * state.inventory_btc
    total_cost_after = total_cost_before + (fill_price * size_btc) + fee
    inv_after = state.inventory_btc + size_btc
    state.inventory_btc = inv_after
    state.avg_entry_jpy = (total_cost_after / inv_after) if inv_after > 0 else 0.0
    state.total_buy_fills += 1


def _sell_fill(state: RuntimeState, fill_price: float, size_btc: float, maker_fee_bps: float) -> None:
    if size_btc <= 0 or fill_price <= 0:
        return
    size = min(size_btc, state.inventory_btc)
    if size <= 0:
        return
    fee = fill_price * size * (maker_fee_bps / 10000.0)
    proceeds = (fill_price * size) - fee
    cost = state.avg_entry_jpy * size
    state.realized_pnl_jpy += proceeds - cost
    state.inventory_btc = max(0.0, state.inventory_btc - size)
    if state.inventory_btc <= 0:
        state.avg_entry_jpy = 0.0
    state.total_sell_fills += 1


def _apply_env_overrides(args):
    prefix = "BITFLYERMM_"
    for k, v in vars(args).items():
        env_name = prefix + k.upper()
        if isinstance(v, int):
            x = _env_int(env_name)
            if x is not None:
                setattr(args, k, x)
        elif isinstance(v, float):
            x = _env_float(env_name)
            if x is not None:
                setattr(args, k, x)
        elif isinstance(v, str):
            s = _env_str(env_name)
            if s:
                setattr(args, k, s)
    return args


def parse_args():
    p = argparse.ArgumentParser(description="bitFlyer public-board MM observer (simulation only)")
    p.add_argument("--product-code", default="BTC_JPY", help="bitFlyer product code")
    p.add_argument("--poll-sec", type=float, default=2.0, help="Board polling interval seconds")
    p.add_argument("--tick-size-jpy", type=float, default=1.0, help="Quote price tick in JPY")
    p.add_argument("--quote-half-spread-yen", type=float, default=250.0, help="Half spread from mid for quoted bid/ask")
    p.add_argument("--order-size-btc", type=float, default=0.001, help="Simulated order size (BTC)")
    p.add_argument("--max-inventory-btc", type=float, default=0.01, help="Max simulated inventory (BTC)")
    p.add_argument("--maker-fee-bps", type=float, default=0.0, help="Assumed maker fee in bps")
    p.add_argument("--daily-loss-limit-jpy", type=float, default=0.0, help="Halt simulated fills when day pnl <= -limit (0=disabled)")
    p.add_argument("--summary-every-sec", type=float, default=60.0, help="Summary log cadence seconds (0=disabled)")
    p.add_argument("--metrics-file", default=DEFAULT_METRICS_FILE, help="JSONL metrics path")
    p.add_argument("--metrics-sample-sec", type=float, default=10.0, help="Metrics sample cadence seconds (0=disabled)")
    p.add_argument("--log-file", default=DEFAULT_LOG_FILE, help="Log file path")
    p.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="State file path")
    p.add_argument("--run-seconds", type=int, default=0, help="Auto-exit after N seconds (0=run forever)")
    p.add_argument("--max-consecutive-errors", type=int, default=10, help="Halt after N consecutive polling errors")
    args = p.parse_args()
    return _apply_env_overrides(args)


def main() -> int:
    args = parse_args()
    logger = Logger(args.log_file)
    state_path = Path(args.state_file)
    state = _load_state(state_path)

    logger.info("bitFlyer MM Observe Simulator")
    logger.info("=" * 56)
    logger.info("Mode: observe-only (simulation; no real orders)")
    logger.info(f"Market: {args.product_code}")
    logger.info(
        f"Quote: half_spread={args.quote_half_spread_yen:.1f} JPY | size={args.order_size_btc:.6f} BTC "
        f"| max_inventory={args.max_inventory_btc:.6f} BTC | maker_fee={args.maker_fee_bps:.4f} bps"
    )
    logger.info(
        f"Poll: {args.poll_sec:.2f}s | summary_every={args.summary_every_sec:.1f}s | "
        f"metrics_every={args.metrics_sample_sec:.1f}s"
    )
    logger.info(f"Log: {args.log_file}")
    logger.info(f"State: {args.state_file}")
    logger.info(f"Metrics: {args.metrics_file}")

    start_ts = now_ts()
    last_summary_ts = 0.0
    last_metrics_ts = 0.0

    while True:
        if args.run_seconds > 0 and (now_ts() - start_ts) >= float(args.run_seconds):
            logger.info(f"[{iso_now()}] run-seconds reached; exiting.")
            _save_state(state_path, state)
            return 0

        try:
            book = fetch_board(args.product_code)
            best_bid, best_ask = _extract_best(book)
            if not (math.isfinite(best_bid) and math.isfinite(best_ask) and best_bid > 0 and best_ask > 0 and best_ask >= best_bid):
                raise RuntimeError("invalid top-of-book")

            state.consecutive_errors = 0
            mid = (best_bid + best_ask) / 2.0
            spread = max(0.0, best_ask - best_bid)

            half = max(float(args.quote_half_spread_yen), float(args.tick_size_jpy))
            bid_quote = _q_down(mid - half, float(args.tick_size_jpy))
            ask_quote = _q_up(mid + half, float(args.tick_size_jpy))
            if ask_quote <= bid_quote:
                ask_quote = bid_quote + max(float(args.tick_size_jpy), 1.0)

            state.last_mid_jpy = mid
            state.quote_bid_jpy = bid_quote
            state.quote_ask_jpy = ask_quote

            if not state.halted:
                if best_ask <= bid_quote and state.inventory_btc + args.order_size_btc <= args.max_inventory_btc + 1e-12:
                    _buy_fill(state, bid_quote, args.order_size_btc, args.maker_fee_bps)
                    logger.info(
                        f"[{iso_now()}] fill BUY  px={bid_quote:.0f} size={args.order_size_btc:.6f} "
                        f"inv={state.inventory_btc:.6f} avg={state.avg_entry_jpy:.1f}"
                    )

                if best_bid >= ask_quote and state.inventory_btc >= args.order_size_btc - 1e-12:
                    _sell_fill(state, ask_quote, args.order_size_btc, args.maker_fee_bps)
                    logger.info(
                        f"[{iso_now()}] fill SELL px={ask_quote:.0f} size={args.order_size_btc:.6f} "
                        f"inv={state.inventory_btc:.6f} realized={state.realized_pnl_jpy:+.1f}"
                    )

            day_pnl = _day_pnl_jpy(state)
            total_pnl = _total_pnl_jpy(state)

            if args.daily_loss_limit_jpy > 0 and not state.halted and day_pnl <= -float(args.daily_loss_limit_jpy):
                state.halted = True
                state.halt_reason = f"Daily loss limit reached ({day_pnl:+.1f} JPY <= -{args.daily_loss_limit_jpy:.1f} JPY)"
                logger.info(f"[{iso_now()}] HALT: {state.halt_reason}")

            ts_now = now_ts()
            if args.summary_every_sec > 0 and (ts_now - last_summary_ts) >= args.summary_every_sec:
                logger.info(
                    f"[{iso_now()}] summary "
                    f"bid/ask={best_bid:.0f}/{best_ask:.0f} mid={mid:.1f} spread={spread:.1f} "
                    f"q={bid_quote:.0f}/{ask_quote:.0f} inv={state.inventory_btc:.6f} "
                    f"realized={state.realized_pnl_jpy:+.1f} total={total_pnl:+.1f} day={day_pnl:+.1f} "
                    f"fills={state.total_buy_fills + state.total_sell_fills} halted={state.halted}"
                )
                last_summary_ts = ts_now

            if args.metrics_sample_sec > 0 and (ts_now - last_metrics_ts) >= args.metrics_sample_sec:
                _append_metrics(
                    args.metrics_file,
                    {
                        "ts": iso_now(),
                        "ts_ms": int(ts_now * 1000.0),
                        "product_code": args.product_code,
                        "best_bid_jpy": best_bid,
                        "best_ask_jpy": best_ask,
                        "mid_jpy": mid,
                        "spread_jpy": spread,
                        "quote_bid_jpy": bid_quote,
                        "quote_ask_jpy": ask_quote,
                        "inventory_btc": state.inventory_btc,
                        "avg_entry_jpy": state.avg_entry_jpy,
                        "realized_pnl_jpy": state.realized_pnl_jpy,
                        "total_pnl_jpy": total_pnl,
                        "day_pnl_jpy": day_pnl,
                        "fills_buy": state.total_buy_fills,
                        "fills_sell": state.total_sell_fills,
                        "halted": state.halted,
                    },
                )
                last_metrics_ts = ts_now

            _save_state(state_path, state)
            time.sleep(max(0.2, float(args.poll_sec)))

        except (HTTPError, URLError, TimeoutError) as e:
            state.consecutive_errors += 1
            logger.info(f"[{iso_now()}] warn: board fetch failed: {type(e).__name__}")
            if state.consecutive_errors >= int(args.max_consecutive_errors):
                state.halted = True
                state.halt_reason = f"Consecutive errors cap reached ({state.consecutive_errors}/{args.max_consecutive_errors})"
                logger.info(f"[{iso_now()}] HALT: {state.halt_reason}")
            _save_state(state_path, state)
            time.sleep(2.0)
        except Exception as e:
            state.consecutive_errors += 1
            logger.info(f"[{iso_now()}] error: loop exception: {e}")
            if state.consecutive_errors >= int(args.max_consecutive_errors):
                state.halted = True
                state.halt_reason = f"Consecutive errors cap reached ({state.consecutive_errors}/{args.max_consecutive_errors})"
                logger.info(f"[{iso_now()}] HALT: {state.halt_reason}")
            _save_state(state_path, state)
            time.sleep(2.0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
