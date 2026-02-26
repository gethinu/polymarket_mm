#!/usr/bin/env python3
"""
Polymarket CLOB maker-only market-making bot (minimal, inventory-aware).

Goals:
- Keep local PC stable: small universe (default auto-select 3 tokens), REST polling, no WS firehose.
- Maker-only quotes (post_only=True) so we do not cross the spread.
- Inventory-aware: only place SELL when we have inventory (tracked from our own fills).
- Safe by default: observe-only unless --execute AND --confirm-live YES.
- Quiet by default: no quote spam logs/Discord. Only important events + periodic summaries.
- Daily loss guard: halt on (realized + unrealized) daily PnL <= -limit.

This is *not* risk-free arbitrage. Market-making can lose money via adverse selection.

Config:
- CLI args, with env overrides supported via CLOBMM_* (highest priority).
  Example: CLOBMM_SPREAD_CENTS=3, CLOBMM_ORDER_SIZE_SHARES=5

Discord:
- Uses CLOBBOT_DISCORD_WEBHOOK_URL / DISCORD_WEBHOOK_URL (same as clob-arb monitor).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from lib.clob_auth import build_clob_client_from_env
from lib.runtime_common import (
    day_key_local,
    env_bool as _env_bool,
    env_float as _env_float,
    env_int as _env_int,
    env_str as _env_str,
    iso_now,
    maybe_notify_discord as _maybe_notify_discord,
    now_ts,
)


# Local dependency (same folder)
from polymarket_clob_arb_scanner import fetch_active_markets, extract_yes_token_id, parse_json_string_field, as_float


DEFAULT_CLOB_HOST = "https://clob.polymarket.com"
# Repo-local defaults (gitignored) to avoid mixing with other projects.
_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_METRICS_FILE = str(_SCRIPT_DIR.parent / "logs" / "clob-mm-metrics.jsonl")


def local_day_key() -> str:
    # Keep existing naming in this script.
    return day_key_local()


def maybe_notify_discord(logger: "Logger", message: str) -> None:
    _maybe_notify_discord(
        logger,
        message,
        timeout_sec=7.0,
        user_agent="clob-mm-bot/1.0",
    )


class Logger:
    def __init__(self, log_file: Optional[str] = None):
        self.log_file = Path(log_file) if log_file else None
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, msg: str) -> None:
        if not self.log_file:
            return
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
                # As a last resort, skip console output (file logging still happens).
                pass
        self._append(msg)


def _q_down(x: float, tick: float) -> float:
    if tick <= 0:
        return x
    return math.floor(x / tick + 1e-12) * tick


def _q_up(x: float, tick: float) -> float:
    if tick <= 0:
        return x
    return math.ceil(x / tick - 1e-12) * tick


def build_clob_client(clob_host: str, chain_id: int):
    return build_clob_client_from_env(
        clob_host=clob_host,
        chain_id=int(chain_id),
        missing_env_message="Missing env for CLOB auth. Need PM_PRIVATE_KEY(_FILE/_DPAPI_FILE) and PM_FUNDER.",
        invalid_key_message="Invalid PM_PRIVATE_KEY format. Expected 64 hex chars (optionally prefixed with 0x).",
    )


def _apply_env_overrides(args):
    """
    Apply CLOBMM_* environment variables onto argparse args.
    """
    prefix = "CLOBMM_"
    for k, v in vars(args).items():
        env_name = prefix + k.upper()
        if isinstance(v, bool):
            b = _env_bool(env_name)
            if b is not None:
                setattr(args, k, b)
        elif isinstance(v, int):
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


def _extract_best(book) -> Tuple[Optional[float], Optional[float]]:
    """
    Return (best_bid, best_ask) from OrderBookSummary-like object or dict.
    """
    bids = None
    asks = None
    if isinstance(book, dict):
        bids = book.get("bids") or []
        asks = book.get("asks") or []
    else:
        bids = getattr(book, "bids", None) or []
        asks = getattr(book, "asks", None) or []

    def _best(levels, best_fn):
        out = None
        for x in levels or []:
            if isinstance(x, dict):
                p = as_float(x.get("price"), math.nan)
            else:
                p = as_float(getattr(x, "price", None), math.nan)
            if not math.isfinite(p) or p <= 0:
                continue
            out = p if out is None else best_fn(out, p)
        return out

    best_bid = _best(bids, max)
    best_ask = _best(asks, min)
    return best_bid, best_ask


def _choose_tokens_auto(
    gamma_limit: int,
    min_liquidity: float,
    min_volume24hr: float,
    min_spread_cents: float,
    count: int,
) -> List[Tuple[str, str]]:
    """
    Auto-select tokens for MM from Gamma active markets.
    Returns list of (token_id, label).
    """
    markets = fetch_active_markets(limit=max(50, int(gamma_limit or 500)), offset=0)
    scored: List[Tuple[float, str, str]] = []
    for m in markets or []:
        if m.get("enableOrderBook") is False:
            continue
        if m.get("feesEnabled") is True:
            continue
        liq = as_float(m.get("liquidityNum", m.get("liquidity", 0.0)), 0.0)
        vol = as_float(m.get("volume24hr", 0.0), 0.0)
        spr = as_float(m.get("spread", 0.0), 0.0)
        if liq < float(min_liquidity or 0.0) or vol < float(min_volume24hr or 0.0):
            continue
        if spr < (float(min_spread_cents or 0.0) / 100.0):
            continue
        token_id = extract_yes_token_id(m)
        if not token_id:
            continue
        q = str(m.get("question") or "").strip()
        if not q:
            continue
        # Score: prefer high volume, decent liquidity, and non-trivial spreads.
        score = math.log10(1.0 + vol) + 0.3 * math.log10(1.0 + liq) + (3.0 * spr)
        scored.append((score, str(token_id), q))

    scored.sort(key=lambda x: x[0], reverse=True)
    out: List[Tuple[str, str]] = []
    for _, tid, q in scored[: max(0, int(count or 0))]:
        out.append((tid, q))
    return out


@dataclass
class TradeFill:
    trade_id: str
    ts: float
    created_at_raw: int
    token_id: str
    side: str  # BUY/SELL
    price: float
    size: float


@dataclass
class TokenMMState:
    token_id: str
    label: str
    active: bool = True
    tick_size: float = 0.01
    min_order_size: float = 1.0
    inventory_shares: float = 0.0
    avg_cost: float = 0.0  # average cost basis for inventory
    realized_pnl: float = 0.0
    last_mid: float = 0.0
    last_best_bid: float = 0.0
    last_best_ask: float = 0.0
    last_trade_after: int = 0  # ms epoch passed to TradeParams(after=...)
    last_reconcile_ts: float = 0.0
    buy_quote_updates: int = 0
    sell_quote_updates: int = 0
    buy_order_id: str = ""
    buy_price: float = 0.0
    buy_size: float = 0.0
    buy_ts: float = 0.0
    sell_order_id: str = ""
    sell_price: float = 0.0
    sell_size: float = 0.0
    sell_ts: float = 0.0
    last_quote_ts: float = 0.0
    last_logged_buy_price: float = 0.0
    last_logged_sell_price: float = 0.0
    seen_trade_ids: List[str] = field(default_factory=list)


@dataclass
class RuntimeState:
    token_states: Dict[str, TokenMMState] = field(default_factory=dict)
    active_token_ids: List[str] = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""
    consecutive_errors: int = 0
    day_key: str = ""
    day_pnl_anchor: float = 0.0


def load_state(path: Path) -> RuntimeState:
    if not path.exists():
        return RuntimeState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        ts = {}
        for tid, s in (raw.get("token_states") or {}).items():
            ts[tid] = TokenMMState(**s)
        return RuntimeState(
            token_states=ts,
            active_token_ids=list(raw.get("active_token_ids") or []),
            halted=bool(raw.get("halted", False)),
            halt_reason=str(raw.get("halt_reason", "")),
            consecutive_errors=int(raw.get("consecutive_errors", 0)),
            day_key=str(raw.get("day_key") or ""),
            day_pnl_anchor=as_float(raw.get("day_pnl_anchor"), 0.0),
        )
    except Exception:
        return RuntimeState()


def save_state(path: Path, state: RuntimeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        "token_states": {k: asdict(v) for k, v in state.token_states.items()},
        "active_token_ids": list(state.active_token_ids or []),
        "halted": state.halted,
        "halt_reason": state.halt_reason,
        "consecutive_errors": state.consecutive_errors,
        "day_key": state.day_key,
        "day_pnl_anchor": state.day_pnl_anchor,
    }
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def _extract_trade_fill(t: dict) -> Optional[TradeFill]:
    if not isinstance(t, dict):
        return None
    tid = str(t.get("asset_id") or t.get("token_id") or "").strip()
    if not tid:
        return None
    trade_id = str(t.get("id") or t.get("trade_id") or "").strip()
    if not trade_id:
        return None
    side = str(t.get("side") or "").upper().strip()
    if side not in {"BUY", "SELL"}:
        # Some APIs use "buy"/"sell"
        side = str(t.get("side") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            return None
    price = as_float(t.get("price"), math.nan)
    size = as_float(t.get("size"), math.nan)
    if not (math.isfinite(price) and math.isfinite(size) and price > 0 and size > 0):
        return None
    # created_at might be ms or seconds; fallback to now.
    raw_ts = as_float(t.get("created_at") or t.get("timestamp") or 0.0, 0.0)
    created_at_raw = int(raw_ts) if raw_ts > 0 else 0
    # Normalize for internal sorting/logging.
    if raw_ts > 10_000_000_000:
        ts = raw_ts / 1000.0
    elif raw_ts > 0:
        ts = float(raw_ts)
    else:
        ts = now_ts()
    return TradeFill(
        trade_id=trade_id,
        ts=float(ts),
        created_at_raw=int(created_at_raw),
        token_id=tid,
        side=side,
        price=price,
        size=size,
    )


def _update_inventory_from_fill(s: TokenMMState, fill: TradeFill) -> None:
    if fill.side == "BUY":
        total_cost = s.avg_cost * s.inventory_shares + fill.price * fill.size
        s.inventory_shares += fill.size
        s.avg_cost = (total_cost / s.inventory_shares) if s.inventory_shares > 0 else 0.0
    else:
        # SELL
        size = min(fill.size, s.inventory_shares) if s.inventory_shares > 0 else 0.0
        # If we can't reconcile (unknown starting inventory), still compute pnl vs avg_cost.
        pnl = (fill.price - s.avg_cost) * fill.size
        s.realized_pnl += pnl
        s.inventory_shares = max(0.0, s.inventory_shares - size)
        if s.inventory_shares <= 0:
            s.avg_cost = 0.0


def _extract_order_id(o: dict) -> str:
    if not isinstance(o, dict):
        return ""
    return str(o.get("order_id") or o.get("orderID") or o.get("id") or "").strip()


def _extract_order_side(o: dict) -> str:
    if not isinstance(o, dict):
        return ""
    return str(o.get("side") or "").upper().strip()


def _extract_order_price(o: dict) -> float:
    if not isinstance(o, dict):
        return math.nan
    return as_float(o.get("price"), math.nan)


def _compute_total_pnl(state: RuntimeState) -> float:
    total = 0.0
    for s in (state.token_states or {}).values():
        mid = float(s.last_mid or 0.0)
        inv = float(s.inventory_shares or 0.0)
        avg = float(s.avg_cost or 0.0)
        realized = float(s.realized_pnl or 0.0)
        unreal = (mid - avg) * inv if (inv > 0 and mid > 0 and avg > 0) else 0.0
        total += realized + unreal
    return float(total)


def _maybe_append_metrics(metrics_file: str, payload: dict) -> None:
    if not metrics_file:
        return
    p = Path(metrics_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    # JSONL append.
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")


async def run(args) -> int:
    script_dir = Path(__file__).resolve().parent
    log_file = args.log_file or str(script_dir.parent / "logs" / "clob-mm.log")
    state_file = Path(args.state_file or (script_dir.parent / "logs" / "clob_mm_state.json"))
    logger = Logger(log_file)

    if args.execute and args.confirm_live != "YES":
        raise SystemExit('Refusing live mode: pass --confirm-live YES with --execute')

    state = load_state(state_file)

    logger.info("Polymarket CLOB MM Bot")
    logger.info("=" * 56)
    logger.info(f"Mode: {'LIVE' if args.execute else 'observe-only'} (post-only maker)")
    logger.info(f"Universe: {'manual' if args.token_ids else 'auto'} | tokens_target={args.auto_select_count if not args.token_ids else len(args.token_ids.split(','))}")
    logger.info(f"Spread: {args.spread_cents:.2f}c | order_size_shares={args.order_size_shares:.2f} | max_inventory_shares={args.max_inventory_shares:.2f}")
    logger.info(f"Poll: {args.poll_sec:.2f}s | refresh: {args.quote_refresh_sec:.1f}s | trade_poll: {args.trade_poll_sec:.1f}s")
    logger.info(f"Loss guard: daily_limit=${args.daily_loss_limit_usd:.2f} (0=disabled)")
    logger.info(f"Metrics: {args.metrics_file} | sample_every={args.metrics_sample_sec:.0f}s")
    logger.info(f"Log: {log_file}")
    logger.info(f"State: {state_file}")

    maybe_notify_discord(logger, f"CLOBMM started ({'LIVE' if args.execute else 'observe'}) | spread={args.spread_cents:.2f}c size={args.order_size_shares:g}")

    client = None
    try:
        client = build_clob_client(args.clob_host, args.chain_id)
    except Exception as e:
        logger.info(f"[{iso_now()}] fatal: could not init clob client: {e}")
        maybe_notify_discord(logger, f"CLOBMM HALT: cannot init client: {e}")
        return 2

    # Select tokens.
    tokens: List[Tuple[str, str]] = []
    if args.token_ids:
        for tid in [x.strip() for x in args.token_ids.split(",") if x.strip()]:
            tokens.append((tid, f"token:{tid}"))
    else:
        tokens = _choose_tokens_auto(
            gamma_limit=args.gamma_limit,
            min_liquidity=args.gamma_min_liquidity,
            min_volume24hr=args.gamma_min_volume24hr,
            min_spread_cents=args.gamma_min_spread_cents,
            count=args.auto_select_count,
        )

    if not tokens:
        logger.info(f"[{iso_now()}] fatal: no tokens selected (check gamma filters or --token-ids).")
        maybe_notify_discord(logger, "CLOBMM HALT: no tokens selected (check filters).")
        return 2

    # Mark active tokens and prune stale state to avoid state bloat.
    active_ids = [tid for tid, _ in tokens]
    active_set = set(active_ids)
    state.active_token_ids = list(active_ids)
    stale_ids = [tid for tid in list(state.token_states.keys()) if tid not in active_set]
    if stale_ids:
        if args.execute:
            # Best-effort cancel any stale orders we tracked.
            cancel_ids = []
            for tid in stale_ids:
                s0 = state.token_states.get(tid)
                if not s0:
                    continue
                for oid in [s0.buy_order_id, s0.sell_order_id]:
                    if oid:
                        cancel_ids.append(oid)
            if cancel_ids:
                try:
                    client.cancel_orders(cancel_ids)
                except Exception as e:
                    logger.info(f"[{iso_now()}] warn: cancel stale orders failed: {e}")
        for tid in stale_ids:
            del state.token_states[tid]

    logger.info("Selected tokens:")
    for tid, label in tokens:
        logger.info(f"  - {tid} | {label[:120]}")

    # Initialize per-token state and metadata.
    for tid, label in tokens:
        if tid not in state.token_states:
            state.token_states[tid] = TokenMMState(token_id=tid, label=label)
        else:
            state.token_states[tid].label = label
        state.token_states[tid].active = True

        # Best-effort: read tick size from server; fallback to 0.01.
        try:
            tick = as_float(client.get_tick_size(tid), 0.01)
            if tick > 0:
                state.token_states[tid].tick_size = tick
        except Exception:
            state.token_states[tid].tick_size = 0.01

        # Best-effort: Gamma provides min order size; derive from selected market if possible.
        # We only have token_id here, so we cannot reliably map back; use configured order_size as floor.
        state.token_states[tid].min_order_size = max(1.0, float(args.order_size_shares or 1.0))

    save_state(state_file, state)

    # Main loop.
    start_ts = now_ts()
    last_trade_poll = 0.0
    last_summary = 0.0
    last_metrics = 0.0
    while True:
        if int(args.run_seconds or 0) > 0 and (now_ts() - start_ts) >= int(args.run_seconds):
            logger.info(f"[{iso_now()}] run_seconds reached -> exiting")
            break
        if state.halted:
            await asyncio.sleep(2.0)
            continue

        try:
            # Daily anchor update (for daily loss guard).
            today = local_day_key()
            if not state.day_key:
                state.day_key = today
                state.day_pnl_anchor = _compute_total_pnl(state)
            elif state.day_key != today and not state.halted:
                state.day_key = today
                state.day_pnl_anchor = _compute_total_pnl(state)

            # Poll fills/trades (infrequent).
            if (now_ts() - last_trade_poll) >= float(args.trade_poll_sec or 5.0):
                last_trade_poll = now_ts()
                for tid in list(state.token_states.keys()):
                    s = state.token_states[tid]
                    # Pull full trade list for the user filtered by asset_id (small accounts OK).
                    try:
                        from py_clob_client.clob_types import TradeParams

                        after_ms = int(s.last_trade_after or 0)
                        # Overlap by 1ms to avoid missing trades with identical timestamps.
                        q_after = max(0, after_ms - 1) if after_ms > 0 else None
                        params = TradeParams(asset_id=tid, after=int(q_after) if q_after is not None else None)
                        trades = client.get_trades(params)
                        fills: List[TradeFill] = []
                        for t in trades or []:
                            fill = _extract_trade_fill(t)
                            if not fill:
                                continue
                            if fill.trade_id in s.seen_trade_ids:
                                continue
                            s.seen_trade_ids.append(fill.trade_id)
                            # Keep memory bounded.
                            if len(s.seen_trade_ids) > 500:
                                s.seen_trade_ids = s.seen_trade_ids[-250:]
                            fills.append(fill)

                        # Process in chronological order for sane avg_cost / realized_pnl.
                        fills.sort(key=lambda x: x.ts)
                        for fill in fills:
                            _update_inventory_from_fill(s, fill)
                            logger.info(
                                f"[{iso_now()}] fill {s.label[:60]} | {fill.side} {fill.size:g} @ {fill.price:.3f} | "
                                f"inv={s.inventory_shares:g} avg={s.avg_cost:.3f} pnl={s.realized_pnl:+.4f}"
                            )
                            maybe_notify_discord(
                                logger,
                                (
                                    f"CLOBMM FILL {fill.side} {fill.size:g}@{fill.price:.3f} | "
                                    f"inv={s.inventory_shares:g} avg={s.avg_cost:.3f} pnl={s.realized_pnl:+.4f} | {s.label[:120]}"
                                ),
                            )
                            if int(fill.created_at_raw or 0) > 0:
                                s.last_trade_after = max(int(s.last_trade_after or 0), int(fill.created_at_raw))
                    except Exception as e:
                        logger.info(f"[{iso_now()}] warn: get_trades failed token={tid}: {e}")

            # Quote update (frequent).
            for tid in list(state.token_states.keys()):
                s = state.token_states[tid]
                if not s.active:
                    continue
                tick = float(s.tick_size or 0.01)
                size = max(float(args.order_size_shares or 1.0), float(s.min_order_size or 1.0))

                # Inventory cap: stop bidding if too long.
                allow_buy = s.inventory_shares < float(args.max_inventory_shares or 0.0) if float(args.max_inventory_shares or 0.0) > 0 else True
                allow_sell = s.inventory_shares > 0.0
                sell_size = min(size, s.inventory_shares) if allow_sell else 0.0

                # Fetch book.
                book = client.get_order_book(tid)
                best_bid, best_ask = _extract_best(book)
                if best_bid is None and best_ask is None:
                    continue

                # Mid estimate.
                if best_bid is not None and best_ask is not None:
                    mid = (best_bid + best_ask) / 2.0
                else:
                    mid = best_bid if best_bid is not None else best_ask
                if mid is None or not math.isfinite(mid):
                    continue
                s.last_mid = float(mid)
                s.last_best_bid = float(best_bid or 0.0)
                s.last_best_ask = float(best_ask or 0.0)

                half = max(tick, float(args.spread_cents or 2.0) / 200.0)  # cents -> dollars, half spread
                desired_buy = _q_down(max(0.001, mid - half), tick)
                desired_sell = _q_up(min(0.999, mid + half), tick)

                # Enforce post-only by staying strictly inside the spread when possible.
                if best_ask is not None:
                    desired_buy = min(desired_buy, _q_down(max(0.001, best_ask - tick), tick))
                if best_bid is not None:
                    desired_sell = max(desired_sell, _q_up(min(0.999, best_bid + tick), tick))

                # If spread is too tight, skip (avoid postOnly rejections and churn).
                if best_bid is not None and best_ask is not None and (best_ask - best_bid) < (2 * tick):
                    continue
                if desired_buy >= desired_sell:
                    continue

                now = now_ts()
                if (now - s.last_quote_ts) < 0.05:
                    continue

                # Live mode: reconcile open orders occasionally (state self-heal).
                if args.execute and (now - float(s.last_reconcile_ts or 0.0)) >= float(args.order_reconcile_sec or 30.0):
                    s.last_reconcile_ts = now
                    try:
                        from py_clob_client.clob_types import OpenOrderParams

                        oo = client.get_orders(OpenOrderParams(asset_id=tid))
                        open_ids = set()
                        for o in oo or []:
                            oid = _extract_order_id(o)
                            if oid:
                                open_ids.add(oid)
                        if s.buy_order_id and s.buy_order_id not in open_ids:
                            s.buy_order_id = ""
                        if s.sell_order_id and s.sell_order_id not in open_ids:
                            s.sell_order_id = ""
                    except Exception as e:
                        logger.info(f"[{iso_now()}] warn: reconcile failed token={tid}: {e}")
                        s.buy_order_id = ""
                        s.sell_order_id = ""

                # BUY quote management.
                if allow_buy:
                    have_buy = bool(s.buy_order_id) if args.execute else (s.buy_ts > 0.0)
                    need_new_buy = (not have_buy) or (abs(s.buy_price - desired_buy) >= tick) or (now - s.buy_ts) >= float(args.quote_refresh_sec or 15.0)
                    if need_new_buy:
                        if s.buy_order_id and args.execute:
                            try:
                                client.cancel_orders([s.buy_order_id])
                            except Exception as e:
                                logger.info(f"[{iso_now()}] warn: cancel BUY failed token={tid} oid={s.buy_order_id[:10]}: {e}")
                                # Skip placing a replacement this cycle to avoid duplicate orders.
                                continue
                        s.buy_order_id = ""
                        s.buy_price = desired_buy
                        s.buy_size = size
                        s.buy_ts = now

                        if args.execute:
                            from py_clob_client.clob_types import OrderArgs, OrderType

                            order = client.create_order(
                                OrderArgs(token_id=tid, price=float(desired_buy), size=float(round(size, 2)), side="BUY")
                            )
                            resp = client.post_order(order, orderType=OrderType.GTC, post_only=True)
                            oid = ""
                            if isinstance(resp, dict):
                                oid = str(resp.get("orderID") or resp.get("id") or "").strip()
                            s.buy_order_id = oid
                            s.buy_quote_updates += 1
                            if args.log_quotes:
                                ticks = abs(desired_buy - float(s.last_logged_buy_price or 0.0)) / tick if tick > 0 else 999
                                if ticks >= float(args.quote_log_min_change_ticks or 1) or s.last_logged_buy_price <= 0:
                                    logger.info(f"[{iso_now()}] quote BUY {size:g} @ {desired_buy:.3f} token={tid} oid={oid[:10]}")
                                    s.last_logged_buy_price = float(desired_buy)
                        else:
                            s.buy_quote_updates += 1
                            if args.log_quotes:
                                ticks = abs(desired_buy - float(s.last_logged_buy_price or 0.0)) / tick if tick > 0 else 999
                                if ticks >= float(args.quote_log_min_change_ticks or 1) or s.last_logged_buy_price <= 0:
                                    logger.info(f"[{iso_now()}] quote(sim) BUY {size:g} @ {desired_buy:.3f} token={tid}")
                                    s.last_logged_buy_price = float(desired_buy)
                else:
                    # Too much inventory: cancel bid.
                    if s.buy_order_id and args.execute:
                        try:
                            client.cancel_orders([s.buy_order_id])
                        except Exception:
                            pass
                    s.buy_order_id = ""

                # SELL quote management (only if we have inventory).
                if allow_sell and sell_size >= float(s.min_order_size or 1.0):
                    have_sell = bool(s.sell_order_id) if args.execute else (s.sell_ts > 0.0)
                    need_new_sell = (not have_sell) or (abs(s.sell_price - desired_sell) >= tick) or (now - s.sell_ts) >= float(args.quote_refresh_sec or 15.0) or (abs(s.sell_size - sell_size) >= 1e-6)
                    if need_new_sell:
                        if s.sell_order_id and args.execute:
                            try:
                                client.cancel_orders([s.sell_order_id])
                            except Exception as e:
                                logger.info(f"[{iso_now()}] warn: cancel SELL failed token={tid} oid={s.sell_order_id[:10]}: {e}")
                                continue
                        s.sell_order_id = ""
                        s.sell_price = desired_sell
                        s.sell_size = sell_size
                        s.sell_ts = now

                        if args.execute:
                            from py_clob_client.clob_types import OrderArgs, OrderType

                            order = client.create_order(
                                OrderArgs(token_id=tid, price=float(desired_sell), size=float(round(sell_size, 2)), side="SELL")
                            )
                            resp = client.post_order(order, orderType=OrderType.GTC, post_only=True)
                            oid = ""
                            if isinstance(resp, dict):
                                oid = str(resp.get("orderID") or resp.get("id") or "").strip()
                            s.sell_order_id = oid
                            s.sell_quote_updates += 1
                            if args.log_quotes:
                                ticks = abs(desired_sell - float(s.last_logged_sell_price or 0.0)) / tick if tick > 0 else 999
                                if ticks >= float(args.quote_log_min_change_ticks or 1) or s.last_logged_sell_price <= 0:
                                    logger.info(f"[{iso_now()}] quote SELL {sell_size:g} @ {desired_sell:.3f} token={tid} oid={oid[:10]}")
                                    s.last_logged_sell_price = float(desired_sell)
                        else:
                            s.sell_quote_updates += 1
                            if args.log_quotes:
                                ticks = abs(desired_sell - float(s.last_logged_sell_price or 0.0)) / tick if tick > 0 else 999
                                if ticks >= float(args.quote_log_min_change_ticks or 1) or s.last_logged_sell_price <= 0:
                                    logger.info(f"[{iso_now()}] quote(sim) SELL {sell_size:g} @ {desired_sell:.3f} token={tid}")
                                    s.last_logged_sell_price = float(desired_sell)
                else:
                    # No inventory -> no ask.
                    if s.sell_order_id and args.execute:
                        try:
                            client.cancel_orders([s.sell_order_id])
                        except Exception:
                            pass
                    s.sell_order_id = ""

                s.last_quote_ts = now

            # Metrics sampling (write to JSONL, separate from main event log).
            if float(args.metrics_sample_sec or 0.0) > 0 and (now_ts() - last_metrics) >= float(args.metrics_sample_sec):
                last_metrics = now_ts()
                for tid, s in state.token_states.items():
                    if not s.active:
                        continue
                    bb = float(s.last_best_bid or 0.0)
                    ba = float(s.last_best_ask or 0.0)
                    spr = (ba - bb) if (bb > 0 and ba > 0) else 0.0
                    _maybe_append_metrics(
                        args.metrics_file,
                        {
                            "ts": iso_now(),
                            "ts_ms": int(now_ts() * 1000.0),
                            "token_id": tid,
                            "label": s.label[:180],
                            "best_bid": bb,
                            "best_ask": ba,
                            "mid": float(s.last_mid or 0.0),
                            "spread": float(spr),
                            "inv": float(s.inventory_shares or 0.0),
                        },
                    )

            # Periodic summary to Discord (low frequency).
            if float(args.summary_every_sec or 0.0) > 0 and (now_ts() - last_summary) >= float(args.summary_every_sec):
                last_summary = now_ts()
                total_pnl = _compute_total_pnl(state)
                pnl_today = total_pnl - float(state.day_pnl_anchor or 0.0)
                parts = []
                for tid, s in state.token_states.items():
                    mid = float(s.last_mid or 0.0)
                    inv = float(s.inventory_shares or 0.0)
                    unreal = (mid - float(s.avg_cost or 0.0)) * inv if inv > 0 and mid > 0 else 0.0
                    parts.append(f"inv={inv:g} pnl={float(s.realized_pnl or 0.0)+unreal:+.3f} {s.label[:36]}")
                maybe_notify_discord(logger, f"CLOBMM summary: pnl_today={pnl_today:+.2f} total={total_pnl:+.2f} | " + " | ".join(parts)[:1600])

            # Daily loss guard (realized + unrealized) relative to daily anchor.
            if float(args.daily_loss_limit_usd or 0.0) > 0:
                total_pnl = _compute_total_pnl(state)
                pnl_today = total_pnl - float(state.day_pnl_anchor or 0.0)
                if pnl_today <= -float(args.daily_loss_limit_usd):
                    state.halted = True
                    state.halt_reason = f"Daily loss guard hit: pnl_today {pnl_today:+.2f} <= -{float(args.daily_loss_limit_usd):.2f}"
                    logger.info(f"[{iso_now()}] HALT: {state.halt_reason}")
                    maybe_notify_discord(logger, f"CLOBMM HALT: {state.halt_reason}")
                    if args.execute:
                        # Cancel open orders best-effort.
                        ids = []
                        for s in state.token_states.values():
                            for oid in [s.buy_order_id, s.sell_order_id]:
                                if oid:
                                    ids.append(oid)
                        if ids:
                            try:
                                client.cancel_orders(ids)
                            except Exception:
                                pass
                    save_state(state_file, state)
                    await asyncio.sleep(2.0)
                    continue

            state.consecutive_errors = 0
            save_state(state_file, state)
            await asyncio.sleep(float(args.poll_sec or 1.0))

        except Exception as e:
            state.consecutive_errors += 1
            logger.info(f"[{iso_now()}] error: loop exception: {e}")
            if state.consecutive_errors >= int(args.max_consecutive_errors or 5):
                state.halted = True
                state.halt_reason = f"Consecutive errors cap reached ({state.consecutive_errors}/{args.max_consecutive_errors})"
                logger.info(f"[{iso_now()}] HALT: {state.halt_reason}")
                maybe_notify_discord(logger, f"CLOBMM HALT: {state.halt_reason}")
            save_state(state_file, state)
            await asyncio.sleep(2.0)

    # Best-effort cleanup.
    if args.execute:
        for tid, s in state.token_states.items():
            ids = [x for x in [s.buy_order_id, s.sell_order_id] if x]
            if ids:
                try:
                    client.cancel_orders(ids)
                except Exception:
                    pass
    maybe_notify_discord(logger, "CLOBMM stopped")
    save_state(state_file, state)
    return 0


def parse_args():
    p = argparse.ArgumentParser(description="Polymarket CLOB market maker (maker-only)")
    p.add_argument("--clob-host", default=DEFAULT_CLOB_HOST, help="CLOB host")
    p.add_argument("--chain-id", type=int, default=137, help="EVM chain id")

    p.add_argument("--token-ids", default="", help="Comma-separated token_ids to quote (manual universe)")
    p.add_argument("--auto-select-count", type=int, default=3, help="Auto-select N tokens from Gamma if --token-ids empty")
    p.add_argument("--gamma-limit", type=int, default=500, help="Gamma /markets limit for auto-selection")
    p.add_argument("--gamma-min-liquidity", type=float, default=25000.0, help="Auto-select filter: min liquidityNum")
    p.add_argument("--gamma-min-volume24hr", type=float, default=2500.0, help="Auto-select filter: min volume24hr")
    p.add_argument("--gamma-min-spread-cents", type=float, default=2.0, help="Auto-select filter: min spread (cents)")

    p.add_argument("--spread-cents", type=float, default=3.0, help="Total target spread (cents)")
    p.add_argument("--order-size-shares", type=float, default=5.0, help="Order size in shares (must meet min order size)")
    p.add_argument("--max-inventory-shares", type=float, default=10.0, help="Stop bidding when inventory exceeds this (0=disabled)")
    p.add_argument("--poll-sec", type=float, default=2.0, help="Book polling interval")
    p.add_argument("--trade-poll-sec", type=float, default=15.0, help="User trades polling interval")
    p.add_argument("--quote-refresh-sec", type=float, default=30.0, help="Cancel/replace quotes at most every N sec")
    p.add_argument("--summary-every-sec", type=float, default=0.0, help="Discord summary interval (0=disabled)")
    p.add_argument("--run-seconds", type=int, default=0, help="Auto-exit after N seconds (0=run forever)")

    p.add_argument("--daily-loss-limit-usd", type=float, default=5.0, help="Halt if daily PnL <= -limit (0=disabled)")
    p.add_argument("--order-reconcile-sec", type=float, default=30.0, help="Live only: reconcile open order IDs every N sec")

    p.add_argument("--metrics-file", default=DEFAULT_METRICS_FILE, help="JSONL metrics file path (separate from event log)")
    p.add_argument("--metrics-sample-sec", type=float, default=60.0, help="Write one metrics sample per token every N sec (0=disabled)")

    p.add_argument("--log-quotes", action="store_true", help="Log quote updates (debug). Default is quiet.")
    p.add_argument("--quote-log-min-change-ticks", type=float, default=1.0, help="When --log-quotes, only log if price changed by >= N ticks")

    p.add_argument("--max-consecutive-errors", type=int, default=7, help="Halt after N consecutive loop errors")

    p.add_argument("--execute", action="store_true", help="Enable live order submission")
    p.add_argument("--confirm-live", default="", help='Must be "YES" when --execute is enabled')

    p.add_argument("--log-file", default="", help="Log file path (optional)")
    p.add_argument("--state-file", default="", help="State file path (optional)")

    args = p.parse_args()
    return _apply_env_overrides(args)


if __name__ == "__main__":
    args = parse_args()
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("\nStopped by user.")
