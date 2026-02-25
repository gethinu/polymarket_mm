#!/usr/bin/env python3
"""
Observe-only Polymarket CLOB fade monitor with multi-bot consensus.

What it does:
- Monitors a selected universe of YES tokens (public order books only).
- Runs multiple independent signal bots per token:
  - z-score mean reversion
  - velocity exhaustion
  - order book imbalance confirmation
  - extreme probability fade
- Requires weighted consensus and regime filters before simulated entries.
- Simulates entry/exit with TP/SL/time-stop and daily loss guard.

This script never places live orders.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from polymarket_clob_arb_scanner import as_float, extract_yes_token_id, fetch_active_markets, fetch_book


def now_ts() -> float:
    return time.time()


def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def local_day_key() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def parse_iso_datetime(value: object) -> Optional[datetime]:
    s = str(value or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def sign(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def safe_stdev(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    try:
        return float(statistics.pstdev(xs))
    except Exception:
        return 0.0


def append_limited(xs: List[float], x: float, max_len: int) -> None:
    xs.append(float(x))
    if len(xs) > max_len:
        del xs[:-max_len]


class Logger:
    def __init__(self, log_file: Optional[str]):
        self.log_file = Path(log_file) if log_file else None
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, msg: str) -> None:
        if not self.log_file:
            return
        with self.log_file.open("a", encoding="utf-8", newline="\n") as f:
            f.write(msg + "\n")

    def info(self, msg: str) -> None:
        try:
            print(msg)
        except UnicodeEncodeError:
            try:
                enc = getattr(sys.stdout, "encoding", None) or "utf-8"
                print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))
            except Exception:
                pass
        self._append(msg)


@dataclass
class TokenState:
    token_id: str = ""
    market_id: str = ""
    label: str = ""
    active: bool = True
    tick_size: float = 0.01
    min_order_size: float = 1.0
    liquidity_num: float = 0.0
    volume24hr: float = 0.0
    spread_hint: float = 0.0
    score: float = 0.0

    last_bid: float = 0.0
    last_ask: float = 0.0
    last_mid: float = 0.0
    last_spread: float = 0.0
    last_depth_bid: float = 0.0
    last_depth_ask: float = 0.0
    last_imbalance: float = 0.0
    last_update_ts: float = 0.0

    mids: List[float] = field(default_factory=list)
    spreads: List[float] = field(default_factory=list)
    imbalances: List[float] = field(default_factory=list)
    returns: List[float] = field(default_factory=list)

    zscore: float = 0.0
    velocity_move: float = 0.0
    bot_zscore: float = 0.0
    bot_velocity: float = 0.0
    bot_imbalance: float = 0.0
    bot_extreme: float = 0.0
    consensus_score: float = 0.0
    consensus_side: int = 0
    consensus_agree: int = 0
    last_signal_ts: float = 0.0

    position_side: int = 0
    position_size: float = 0.0
    entry_price: float = 0.0
    entry_mid: float = 0.0
    entry_tp_per_share: float = 0.0
    entry_sl_per_share: float = 0.0
    entry_cost_per_share: float = 0.0
    entry_expected_move_per_share: float = 0.0
    entry_ts: float = 0.0
    entry_peak_per_share: float = 0.0
    cooldown_until_ts: float = 0.0
    disabled_until_ts: float = 0.0
    disable_reason: str = ""
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    last_round_trip_cost_per_share: float = 0.0
    last_expected_move_per_share: float = 0.0
    last_expected_edge_per_share: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    tp_count: int = 0
    sl_count: int = 0
    timeout_count: int = 0
    guard_exit_count: int = 0
    consecutive_nonpositive_exits: int = 0
    last_exit_reason: str = ""
    last_exit_pnl: float = 0.0


@dataclass
class RuntimeState:
    token_states: Dict[str, TokenState] = field(default_factory=dict)
    active_token_ids: List[str] = field(default_factory=list)
    day_key: str = ""
    day_anchor_total_pnl: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    signals_seen: int = 0
    entries: int = 0
    exits: int = 0
    universe_refresh_count: int = 0
    last_universe_refresh_ts: float = 0.0
    long_trades: int = 0
    long_wins: int = 0
    long_realized: float = 0.0
    short_trades: int = 0
    short_wins: int = 0
    short_realized: float = 0.0
    disable_long_until_ts: float = 0.0
    disable_short_until_ts: float = 0.0
    disable_side_reason: str = ""


def _as_list_float(value) -> List[float]:
    if not isinstance(value, list):
        return []
    out: List[float] = []
    for x in value:
        try:
            out.append(float(x))
        except Exception:
            continue
    return out


def _token_from_raw(raw: dict, token_id_hint: str = "") -> Optional[TokenState]:
    if not isinstance(raw, dict):
        return None
    allowed = set(TokenState.__dataclass_fields__.keys())
    clean = {k: raw[k] for k in raw.keys() if k in allowed}
    try:
        t = TokenState(**clean)
    except Exception:
        return None
    if not t.token_id:
        t.token_id = token_id_hint
    t.mids = _as_list_float(t.mids)
    t.spreads = _as_list_float(t.spreads)
    t.imbalances = _as_list_float(t.imbalances)
    t.returns = _as_list_float(t.returns)
    return t


def load_state(path: Path) -> RuntimeState:
    if not path.exists():
        return RuntimeState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return RuntimeState()

    st = RuntimeState()
    st.active_token_ids = list(raw.get("active_token_ids") or [])
    st.day_key = str(raw.get("day_key") or "")
    st.day_anchor_total_pnl = as_float(raw.get("day_anchor_total_pnl"), 0.0)
    st.halted = bool(raw.get("halted") or False)
    st.halt_reason = str(raw.get("halt_reason") or "")
    st.signals_seen = int(raw.get("signals_seen") or 0)
    st.entries = int(raw.get("entries") or 0)
    st.exits = int(raw.get("exits") or 0)
    st.universe_refresh_count = int(raw.get("universe_refresh_count") or 0)
    st.last_universe_refresh_ts = as_float(raw.get("last_universe_refresh_ts"), 0.0)
    st.long_trades = int(raw.get("long_trades") or 0)
    st.long_wins = int(raw.get("long_wins") or 0)
    st.long_realized = as_float(raw.get("long_realized"), 0.0)
    st.short_trades = int(raw.get("short_trades") or 0)
    st.short_wins = int(raw.get("short_wins") or 0)
    st.short_realized = as_float(raw.get("short_realized"), 0.0)
    st.disable_long_until_ts = as_float(raw.get("disable_long_until_ts"), 0.0)
    st.disable_short_until_ts = as_float(raw.get("disable_short_until_ts"), 0.0)
    st.disable_side_reason = str(raw.get("disable_side_reason") or "")

    for tid, v in (raw.get("token_states") or {}).items():
        tok = _token_from_raw(v, token_id_hint=str(tid))
        if tok and tok.token_id:
            st.token_states[tok.token_id] = tok
    return st


def _is_windows_sharing_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        winerr = int(getattr(exc, "winerror", 0) or 0)
        if winerr in (5, 32):
            return True
    return False


def _replace_with_retry(tmp: Path, path: Path, retries: int = 10) -> None:
    delay = 0.01
    for i in range(max(1, int(retries))):
        try:
            tmp.replace(path)
            return
        except Exception as e:
            if (not _is_windows_sharing_error(e)) or i >= int(retries) - 1:
                raise
            time.sleep(delay)
            delay = min(0.2, delay * 2.0)


def save_state(path: Path, st: RuntimeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        "token_states": {k: asdict(v) for k, v in st.token_states.items()},
        "active_token_ids": list(st.active_token_ids),
        "day_key": st.day_key,
        "day_anchor_total_pnl": st.day_anchor_total_pnl,
        "halted": st.halted,
        "halt_reason": st.halt_reason,
        "signals_seen": st.signals_seen,
        "entries": st.entries,
        "exits": st.exits,
        "universe_refresh_count": st.universe_refresh_count,
        "last_universe_refresh_ts": st.last_universe_refresh_ts,
        "long_trades": st.long_trades,
        "long_wins": st.long_wins,
        "long_realized": st.long_realized,
        "short_trades": st.short_trades,
        "short_wins": st.short_wins,
        "short_realized": st.short_realized,
        "disable_long_until_ts": st.disable_long_until_ts,
        "disable_short_until_ts": st.disable_short_until_ts,
        "disable_side_reason": st.disable_side_reason,
    }
    # Atomic replace prevents readers (e.g., side router) from seeing partial JSON.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    _replace_with_retry(tmp, path)


def _pid_running(pid: int) -> bool:
    if int(pid or 0) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        # Process exists but permissions may differ.
        return True
    except OSError:
        return False
    except Exception:
        return False


def _read_lock_pid(lock_path: Path) -> int:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except Exception:
        return 0
    if not raw:
        return 0
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return int(obj.get("pid") or 0)
    except Exception:
        pass
    try:
        return int(raw)
    except Exception:
        return 0


def acquire_state_lock(state_file: Path) -> Callable[[], None]:
    lock_path = state_file.with_suffix(state_file.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    me = int(os.getpid())

    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(
                    json.dumps(
                        {
                            "pid": me,
                            "state_file": str(state_file),
                            "created_at": iso_now(),
                        },
                        ensure_ascii=True,
                    )
                )

            def _release() -> None:
                try:
                    owner = _read_lock_pid(lock_path)
                    if owner not in (0, me):
                        return
                    lock_path.unlink(missing_ok=True)
                except Exception:
                    pass

            return _release
        except FileExistsError:
            owner = _read_lock_pid(lock_path)
            if owner <= 0:
                # Another process may have just created the lock and not finished writing pid yet.
                raise RuntimeError(f"state lock busy: {lock_path} pid=unknown")
            if owner > 0 and _pid_running(owner):
                raise RuntimeError(f"state lock busy: {lock_path} pid={owner}")
            # Stale lock file from dead process; remove once and retry acquire.
            try:
                lock_path.unlink(missing_ok=True)
            except Exception:
                raise RuntimeError(f"state lock busy: {lock_path}")

    raise RuntimeError(f"state lock busy: {lock_path}")


def _best_price(levels: List[dict], choose_max: bool) -> Tuple[float, float]:
    best_price: Optional[float] = None
    best_size = 0.0
    for lv in levels or []:
        p = as_float((lv or {}).get("price"), math.nan)
        s = as_float((lv or {}).get("size"), 0.0)
        if not (math.isfinite(p) and p > 0 and s > 0):
            continue
        if best_price is None:
            best_price = p
            best_size = s
            continue
        if choose_max and p > best_price:
            best_price = p
            best_size = s
        elif (not choose_max) and p < best_price:
            best_price = p
            best_size = s
    return float(best_price or 0.0), float(best_size)


def _top_depth(levels: List[dict], choose_max: bool, top_n: int) -> float:
    arr: List[Tuple[float, float]] = []
    for lv in levels or []:
        p = as_float((lv or {}).get("price"), math.nan)
        s = as_float((lv or {}).get("size"), 0.0)
        if not (math.isfinite(p) and p > 0 and s > 0):
            continue
        arr.append((p, s))
    arr.sort(key=lambda x: x[0], reverse=choose_max)
    return float(sum(sz for _, sz in arr[: max(1, int(top_n))]))


def extract_book_features(book: dict, depth_levels: int) -> Optional[dict]:
    if not isinstance(book, dict):
        return None
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids and not asks:
        return None

    best_bid, _ = _best_price(bids, choose_max=True)
    best_ask, _ = _best_price(asks, choose_max=False)
    if best_bid <= 0 and best_ask <= 0:
        return None

    if best_bid > 0 and best_ask > 0:
        mid = (best_bid + best_ask) / 2.0
        spread = max(0.0, best_ask - best_bid)
    else:
        mid = best_bid if best_bid > 0 else best_ask
        spread = 0.0

    depth_bid = _top_depth(bids, choose_max=True, top_n=depth_levels)
    depth_ask = _top_depth(asks, choose_max=False, top_n=depth_levels)
    den = depth_bid + depth_ask
    imbalance = ((depth_bid - depth_ask) / den) if den > 0 else 0.0

    return {
        "best_bid": float(best_bid),
        "best_ask": float(best_ask),
        "mid": float(mid),
        "spread": float(spread),
        "depth_bid": float(depth_bid),
        "depth_ask": float(depth_ask),
        "imbalance": float(imbalance),
    }


def _safe_compile_regex(pattern: str) -> Optional[re.Pattern]:
    p = (pattern or "").strip()
    if not p:
        return None
    try:
        return re.compile(p, re.IGNORECASE)
    except re.error:
        return None


def choose_universe(args) -> List[TokenState]:
    include_re = _safe_compile_regex(args.include_regex)
    exclude_re = _safe_compile_regex(args.exclude_regex)
    now_utc = datetime.now(timezone.utc)
    min_days_to_end = max(0.0, float(args.min_days_to_end))
    max_days_to_end = max(0.0, float(args.max_days_to_end))
    max_load = max(int(args.gamma_limit), int(args.max_tokens) * 20)
    page_size = max(50, min(500, int(args.gamma_page_size)))
    max_pages = max(1, int(args.gamma_pages))
    raw_markets: List[dict] = []
    seen_market_ids = set()
    offset = 0
    for _ in range(max_pages):
        remaining = max_load - len(raw_markets)
        if remaining <= 0:
            break
        take = min(page_size, remaining)
        batch = fetch_active_markets(limit=take, offset=offset)
        if not batch:
            break
        for m in batch:
            mid = str(m.get("id") or "")
            if mid and mid in seen_market_ids:
                continue
            if mid:
                seen_market_ids.add(mid)
            raw_markets.append(m)
        if len(batch) < take:
            break
        offset += len(batch)

    rows: List[TokenState] = []
    for m in raw_markets:
        if m.get("enableOrderBook") is False:
            continue
        token_id = extract_yes_token_id(m)
        if not token_id:
            continue

        label = str(m.get("question") or "").strip()
        if not label:
            continue
        if include_re and not include_re.search(label):
            continue
        if exclude_re and exclude_re.search(label):
            continue
        if min_days_to_end > 0.0 or max_days_to_end > 0.0:
            end_dt = parse_iso_datetime(m.get("endDate") or m.get("endDateIso"))
            if not end_dt:
                continue
            days_to_end = (end_dt - now_utc).total_seconds() / 86400.0
            if days_to_end < min_days_to_end:
                continue
            if max_days_to_end > 0.0 and days_to_end > max_days_to_end:
                continue

        liq = as_float(m.get("liquidityNum", m.get("liquidity", 0.0)), 0.0)
        vol = as_float(m.get("volume24hr", 0.0), 0.0)
        spr = as_float(m.get("spread", 0.0), 0.0)
        if liq < float(args.min_liquidity):
            continue
        if vol < float(args.min_volume24hr):
            continue
        if spr < (float(args.min_spread_cents) / 100.0):
            continue

        tick = as_float(
            m.get("orderPriceMinTickSize", m.get("minimumTickSize", m.get("minimum_tick_size", 0.01))),
            0.01,
        )
        if tick <= 0:
            tick = 0.01
        min_size = as_float(
            m.get("orderMinSize", m.get("minimumOrderSize", m.get("minimum_order_size", 1.0))),
            1.0,
        )
        if min_size <= 0:
            min_size = 1.0

        score = math.log10(1.0 + vol) + 0.35 * math.log10(1.0 + liq) + (3.5 * spr)
        rows.append(
            TokenState(
                token_id=str(token_id),
                market_id=str(m.get("id") or ""),
                label=label,
                tick_size=tick,
                min_order_size=min_size,
                liquidity_num=liq,
                volume24hr=vol,
                spread_hint=spr,
                score=score,
            )
        )

    rows.sort(key=lambda x: x.score, reverse=True)
    return rows[: max(1, int(args.max_tokens))]


def merge_universe(st: RuntimeState, tokens: List[TokenState]) -> None:
    for t in st.token_states.values():
        t.active = False

    active_ids: List[str] = []
    for base in tokens:
        tid = base.token_id
        if not tid:
            continue
        active_ids.append(tid)
        if tid in st.token_states:
            t = st.token_states[tid]
            t.active = True
            t.label = base.label
            t.market_id = base.market_id
            t.tick_size = base.tick_size or t.tick_size or 0.01
            t.min_order_size = base.min_order_size or t.min_order_size or 1.0
            t.liquidity_num = base.liquidity_num
            t.volume24hr = base.volume24hr
            t.spread_hint = base.spread_hint
            t.score = base.score
        else:
            base.active = True
            st.token_states[tid] = base

    st.active_token_ids = active_ids


def fetch_books_parallel(token_ids: List[str], workers: int) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not token_ids:
        return out
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futures = {ex.submit(fetch_book, tid): tid for tid in token_ids}
        for fut in as_completed(futures):
            tid = futures[fut]
            try:
                book = fut.result()
                if isinstance(book, dict):
                    out[tid] = book
            except Exception:
                continue
    return out


def update_token_from_book(t: TokenState, feat: dict, history_size: int) -> None:
    prev_mid = t.last_mid
    t.last_bid = float(feat.get("best_bid") or 0.0)
    t.last_ask = float(feat.get("best_ask") or 0.0)
    t.last_mid = float(feat.get("mid") or 0.0)
    t.last_spread = float(feat.get("spread") or 0.0)
    t.last_depth_bid = float(feat.get("depth_bid") or 0.0)
    t.last_depth_ask = float(feat.get("depth_ask") or 0.0)
    t.last_imbalance = float(feat.get("imbalance") or 0.0)
    t.last_update_ts = now_ts()

    if t.last_mid > 0:
        append_limited(t.mids, t.last_mid, history_size)
    append_limited(t.spreads, t.last_spread, history_size)
    append_limited(t.imbalances, t.last_imbalance, history_size)
    if prev_mid > 0 and t.last_mid > 0:
        append_limited(t.returns, t.last_mid - prev_mid, history_size)

def zscore_signal(t: TokenState, args) -> Tuple[int, float]:
    lookback = max(5, int(args.zscore_lookback))
    if len(t.mids) < lookback:
        t.zscore = 0.0
        t.bot_zscore = 0.0
        return 0, 0.0
    window = t.mids[-lookback:]
    mu = statistics.mean(window)
    sd = safe_stdev(window)
    if sd <= 1e-9:
        t.zscore = 0.0
        t.bot_zscore = 0.0
        return 0, 0.0
    z = (window[-1] - mu) / sd
    t.zscore = float(z)
    side = 0
    thr = max(0.01, float(args.zscore_entry))
    if z >= thr:
        side = -1
    elif z <= -thr:
        side = 1
    conf = min(2.0, abs(z) / thr) if side != 0 else 0.0
    t.bot_zscore = side * conf
    return side, conf


def velocity_signal(t: TokenState, args) -> Tuple[int, float]:
    w = max(2, int(args.velocity_window))
    if len(t.mids) < (w + 2):
        t.velocity_move = 0.0
        t.bot_velocity = 0.0
        return 0, 0.0

    trend = t.mids[-1] - t.mids[-1 - w]
    last = t.mids[-1] - t.mids[-2]
    prev = t.mids[-2] - t.mids[-3]
    t.velocity_move = float(trend)

    thr = max(0.0001, float(args.velocity_threshold_cents) / 100.0)
    side = 0
    if trend >= thr and last < 0 and prev > 0:
        side = -1
    elif trend <= -thr and last > 0 and prev < 0:
        side = 1
    conf = min(2.0, abs(trend) / thr) if side != 0 else 0.0
    t.bot_velocity = side * conf
    return side, conf


def imbalance_signal(t: TokenState, args) -> Tuple[int, float]:
    thr = max(0.01, float(args.imbalance_threshold))
    z = float(t.zscore or 0.0)
    imb = float(t.last_imbalance or 0.0)
    side = 0
    if z > 0 and imb <= -thr:
        side = -1
    elif z < 0 and imb >= thr:
        side = 1
    conf = min(2.0, abs(imb) / thr) if side != 0 else 0.0
    t.bot_imbalance = side * conf
    return side, conf


def extreme_signal(t: TokenState, args) -> Tuple[int, float]:
    px = float(t.last_mid or 0.0)
    low = clamp(float(args.extreme_low), 0.001, 0.499)
    high = clamp(float(args.extreme_high), 0.501, 0.999)
    side = 0
    conf = 0.0
    if px >= high:
        side = -1
        conf = min(2.0, (px - high) / max(1e-9, (1.0 - high)))
    elif px <= low:
        side = 1
        conf = min(2.0, (low - px) / max(1e-9, low))
    t.bot_extreme = side * conf
    return side, conf


def regime_allows_entry(t: TokenState, args) -> bool:
    if t.last_mid <= 0:
        return False
    if t.last_mid < float(args.min_mid_prob) or t.last_mid > float(args.max_mid_prob):
        return False
    max_spread = float(args.max_spread_cents) / 100.0
    if max_spread > 0 and t.last_spread > max_spread:
        return False
    if min(t.last_depth_bid, t.last_depth_ask) < float(args.min_depth_shares):
        return False
    lb = max(5, int(args.vol_lookback))
    vol = safe_stdev(t.returns[-lb:]) if len(t.returns) >= lb else safe_stdev(t.returns)
    max_vol = max(0.0, float(args.max_volatility_cents)) / 100.0
    min_vol = max(0.0, float(args.min_volatility_cents)) / 100.0
    if max_vol > 0 and vol > max_vol:
        return False
    if min_vol > 0 and vol < min_vol:
        return False
    return True


def evaluate_consensus(t: TokenState, args) -> Tuple[int, float, int]:
    z_side, z_conf = zscore_signal(t, args)
    v_side, v_conf = velocity_signal(t, args)
    i_side, i_conf = imbalance_signal(t, args)
    e_side, e_conf = extreme_signal(t, args)

    score = (
        float(args.weight_zscore) * z_side * z_conf
        + float(args.weight_velocity) * v_side * v_conf
        + float(args.weight_imbalance) * i_side * i_conf
        + float(args.weight_extreme) * e_side * e_conf
    )
    side = sign(score)
    agree = 0
    if side != 0:
        for s in (z_side, v_side, i_side, e_side):
            if s == side:
                agree += 1

    t.consensus_score = float(score)
    t.consensus_side = int(side)
    t.consensus_agree = int(agree)

    if side == 0:
        return 0, score, agree
    req_score = float(args.consensus_min_score)
    if agree <= 1:
        req_score = max(req_score, float(args.consensus_min_score_agree1))
    elif agree == 2:
        req_score = max(req_score, float(args.consensus_min_score_agree2))
    if abs(score) < req_score:
        return 0, score, agree
    if agree < int(args.consensus_min_agree):
        return 0, score, agree
    non_extreme_agree = 0
    for s in (z_side, v_side, i_side):
        if s == side:
            non_extreme_agree += 1
    if non_extreme_agree < int(args.min_non_extreme_agree):
        return 0, score, agree
    if not regime_allows_entry(t, args):
        return 0, score, agree
    return side, score, agree


def entry_price(t: TokenState, side: int, args) -> float:
    tick = max(float(t.tick_size or 0.01), 0.0001)
    slip = max(0.0, float(args.slippage_ticks)) * tick
    if str(args.execution_mode) == "mid":
        base = t.last_mid if t.last_mid > 0 else (t.last_ask if side > 0 else t.last_bid)
        if side > 0:
            return max(0.001, base + (slip * 0.5))
        return max(0.001, base - (slip * 0.5))
    if side > 0:
        base = t.last_ask if t.last_ask > 0 else t.last_mid
        return max(0.001, base + slip)
    base = t.last_bid if t.last_bid > 0 else t.last_mid
    return max(0.001, base - slip)


def exit_price(t: TokenState, side: int, args) -> float:
    tick = max(float(t.tick_size or 0.01), 0.0001)
    slip = max(0.0, float(args.slippage_ticks)) * tick
    if str(args.execution_mode) == "mid":
        base = t.last_mid if t.last_mid > 0 else (t.last_bid if side > 0 else t.last_ask)
        if side > 0:
            return max(0.001, base - (slip * 0.5))
        return max(0.001, base + (slip * 0.5))
    if side > 0:
        base = t.last_bid if t.last_bid > 0 else t.last_mid
        return max(0.001, base - slip)
    base = t.last_ask if t.last_ask > 0 else t.last_mid
    return max(0.001, base + slip)


def _trade_fee_usd(entry_px: float, exit_px: float, size: float, fee_bps: float) -> float:
    bps = max(0.0, float(fee_bps))
    if bps <= 0:
        return 0.0
    notional = abs(entry_px * size) + abs(exit_px * size)
    return notional * (bps / 10000.0)


def _round_trip_cost_per_share(t: TokenState, args) -> float:
    tick = max(float(t.tick_size or 0.01), 0.0001)
    spread = max(float(t.last_spread or 0.0), tick)
    if str(args.execution_mode) == "mid":
        capture = clamp(float(args.maker_spread_capture), 0.0, 1.0)
        spread = spread * (1.0 - capture)
    slip = 2.0 * max(0.0, float(args.slippage_ticks)) * tick
    fee = 2.0 * max(0.0, float(t.last_mid or 0.0)) * (max(0.0, float(args.fee_bps)) / 10000.0)
    return spread + slip + fee


def _expected_move_per_share(t: TokenState, score: float, args) -> float:
    lb = max(5, int(args.vol_lookback))
    if len(t.returns) >= lb:
        vol = safe_stdev(t.returns[-lb:])
    else:
        vol = safe_stdev(t.returns)
    # If returns are flat, fall back to a spread-based proxy to avoid overfitting to zeros.
    if vol <= 1e-9:
        vol = max(float(t.last_spread or 0.0) * 0.5, max(float(t.tick_size or 0.01), 0.0001))
    return max(0.0, abs(float(score)) * vol * max(0.1, float(args.expected_move_vol_mult)))


def _entry_plan(t: TokenState, score: float, args) -> Tuple[bool, dict]:
    rt_cost = _round_trip_cost_per_share(t, args)
    exp_move = _expected_move_per_share(t, score, args)
    exp_edge = exp_move - rt_cost

    base_tp = max(0.0, float(args.take_profit_cents)) / 100.0
    base_sl = max(0.0, float(args.stop_loss_cents)) / 100.0
    tp_share = max(base_tp, rt_cost * max(0.1, float(args.tp_cost_mult)))
    sl_share = max(base_sl, rt_cost * max(0.1, float(args.sl_cost_mult)))

    min_edge = max(0.0, float(args.min_expected_edge_cents)) / 100.0
    min_ratio = max(0.0, float(args.expected_move_cost_ratio))
    if exp_move < (rt_cost * min_ratio):
        return False, {"reason": "expected_move_ratio", "rt_cost": rt_cost, "exp_move": exp_move, "exp_edge": exp_edge}
    if exp_edge < min_edge:
        return False, {"reason": "expected_edge", "rt_cost": rt_cost, "exp_move": exp_move, "exp_edge": exp_edge}

    return True, {
        "rt_cost": rt_cost,
        "exp_move": exp_move,
        "exp_edge": exp_edge,
        "tp_share": tp_share,
        "sl_share": sl_share,
    }


def maybe_disable_token_after_exit(t: TokenState, args, logger: Logger, reason: str, pnl: float) -> None:
    if now_ts() < float(t.disabled_until_ts or 0.0):
        return

    churn_streak = max(0, int(args.token_churn_streak))
    churn_disable_sec = max(0.0, float(args.token_churn_disable_sec))
    churn_max_pnl = max(0.0, float(args.token_churn_max_pnl_usd))
    if (
        churn_streak > 0
        and churn_disable_sec > 0
        and reason in {"timeout", "trail", "breakeven"}
        and int(t.consecutive_nonpositive_exits or 0) >= churn_streak
        and float(pnl) <= (churn_max_pnl + 1e-12)
    ):
        t.disabled_until_ts = now_ts() + churn_disable_sec
        t.disable_reason = (
            f"churn {int(t.consecutive_nonpositive_exits or 0)}x "
            f"{reason} pnl<={churn_max_pnl:.4f}"
        )
        logger.info(
            f"[{iso_now()}] token-disable {churn_disable_sec:.0f}s "
            f"| {t.disable_reason} | trades={t.trade_count} | {t.label[:120]}"
        )
        return

    min_trades = max(1, int(args.token_loss_min_trades))
    if int(t.trade_count or 0) < min_trades:
        return

    realized_cut = max(0.0, float(args.token_loss_cut_usd))
    min_winrate = clamp(float(args.token_min_winrate), 0.0, 1.0)
    win_rate = (float(t.win_count or 0.0) / max(1, int(t.trade_count or 0))) if int(t.trade_count or 0) > 0 else 0.0

    reason = ""
    if realized_cut > 0 and float(t.realized_pnl or 0.0) <= -realized_cut:
        reason = f"realized {float(t.realized_pnl):+.4f} <= -{realized_cut:.4f}"
    elif win_rate < min_winrate:
        reason = f"winrate {win_rate:.1%} < {min_winrate:.1%}"
    if not reason:
        return

    disable_sec = max(0.0, float(args.token_disable_sec))
    t.disabled_until_ts = now_ts() + disable_sec
    t.disable_reason = reason
    logger.info(
        f"[{iso_now()}] token-disable {disable_sec:.0f}s "
        f"| {reason} | trades={t.trade_count} | {t.label[:120]}"
    )


def _maybe_disable_side_after_exit(side: int, args, st: RuntimeState, logger: Logger) -> None:
    if side == 0:
        return
    now = now_ts()
    min_trades = max(1, int(args.side_loss_min_trades))
    cut = max(0.0, float(args.side_loss_cut_usd))
    min_wr = clamp(float(args.side_min_winrate), 0.0, 1.0)
    disable_sec = max(0.0, float(args.side_disable_sec))

    if side > 0:
        trades = int(st.long_trades or 0)
        wins = int(st.long_wins or 0)
        realized = float(st.long_realized or 0.0)
        if now < float(st.disable_long_until_ts or 0.0):
            return
    else:
        trades = int(st.short_trades or 0)
        wins = int(st.short_wins or 0)
        realized = float(st.short_realized or 0.0)
        if now < float(st.disable_short_until_ts or 0.0):
            return

    if trades < min_trades:
        return
    wr = (float(wins) / max(1, trades)) if trades > 0 else 0.0
    reason = ""
    if cut > 0 and realized <= -cut:
        reason = f"realized {realized:+.4f} <= -{cut:.4f}"
    elif wr < min_wr:
        reason = f"winrate {wr:.1%} < {min_wr:.1%}"
    if not reason:
        return

    if side > 0:
        st.disable_long_until_ts = now + disable_sec
    else:
        st.disable_short_until_ts = now + disable_sec
    st.disable_side_reason = reason
    logger.info(
        f"[{iso_now()}] side-disable {disable_sec:.0f}s | "
        f"{'LONG' if side > 0 else 'SHORT'} | {reason} | trades={trades}"
    )


def open_position(
    t: TokenState,
    side: int,
    args,
    st: RuntimeState,
    logger: Logger,
    plan: Optional[dict] = None,
) -> None:
    if side == 0 or t.position_side != 0:
        return
    size = max(float(args.position_size_shares), float(t.min_order_size or 1.0))
    px = entry_price(t, side=side, args=args)
    rt_cost = as_float((plan or {}).get("rt_cost"), _round_trip_cost_per_share(t, args))
    exp_move = as_float((plan or {}).get("exp_move"), 0.0)
    tp_share = as_float((plan or {}).get("tp_share"), max(0.0, float(args.take_profit_cents)) / 100.0)
    sl_share = as_float((plan or {}).get("sl_share"), max(0.0, float(args.stop_loss_cents)) / 100.0)
    exp_edge = as_float((plan or {}).get("exp_edge"), exp_move - rt_cost)

    t.position_side = side
    t.position_size = size
    t.entry_price = px
    t.entry_mid = float(t.last_mid or px)
    t.entry_tp_per_share = tp_share
    t.entry_sl_per_share = sl_share
    t.entry_cost_per_share = rt_cost
    t.entry_expected_move_per_share = exp_move
    t.entry_ts = now_ts()
    t.entry_peak_per_share = 0.0
    t.unrealized_pnl = 0.0
    t.last_signal_ts = t.entry_ts
    t.last_round_trip_cost_per_share = rt_cost
    t.last_expected_move_per_share = exp_move
    t.last_expected_edge_per_share = exp_edge
    st.entries += 1
    logger.info(
        f"[{iso_now()}] entry {'LONG' if side > 0 else 'SHORT'} "
        f"{size:g} @ {px:.4f} | score={t.consensus_score:+.2f} agree={t.consensus_agree} "
        f"| exp_edge={exp_edge:+.4f} tp/sl={tp_share:.4f}/{sl_share:.4f} | {t.label[:120]}"
    )


def close_position(
    t: TokenState,
    args,
    st: RuntimeState,
    logger: Logger,
    reason: str,
    force_now: bool = False,
) -> bool:
    if t.position_side == 0:
        return False
    side = int(t.position_side)
    size = float(t.position_size or 0.0)
    if size <= 0:
        t.position_side = 0
        t.position_size = 0.0
        return False

    if not force_now and t.last_mid <= 0:
        return False

    px_exit = exit_price(t, side=side, args=args)
    gross = side * (px_exit - float(t.entry_price)) * size
    fees = _trade_fee_usd(float(t.entry_price), px_exit, size, float(args.fee_bps))
    pnl = gross - fees
    t.realized_pnl += pnl
    t.unrealized_pnl = 0.0
    t.trade_count += 1
    if pnl > 1e-12:
        t.win_count += 1
        t.consecutive_nonpositive_exits = 0
    else:
        t.loss_count += 1
        t.consecutive_nonpositive_exits = int(t.consecutive_nonpositive_exits or 0) + 1
    t.last_exit_reason = str(reason or "")
    t.last_exit_pnl = float(pnl)
    if reason == "tp":
        t.tp_count += 1
    elif reason == "sl":
        t.sl_count += 1
    elif reason == "timeout":
        t.timeout_count += 1
    elif reason == "daily_loss_guard":
        t.guard_exit_count += 1

    if side > 0:
        st.long_trades += 1
        st.long_realized += pnl
        if pnl > 1e-12:
            st.long_wins += 1
    elif side < 0:
        st.short_trades += 1
        st.short_realized += pnl
        if pnl > 1e-12:
            st.short_wins += 1

    t.position_side = 0
    t.position_size = 0.0
    t.entry_price = 0.0
    t.entry_mid = 0.0
    t.entry_tp_per_share = 0.0
    t.entry_sl_per_share = 0.0
    t.entry_cost_per_share = 0.0
    t.entry_expected_move_per_share = 0.0
    t.entry_ts = 0.0
    t.entry_peak_per_share = 0.0
    cooldown = max(0.0, float(args.cooldown_sec))
    if pnl <= 1e-12:
        cooldown *= max(1.0, float(args.loss_cooldown_mult))
    t.cooldown_until_ts = now_ts() + cooldown
    st.exits += 1
    logger.info(
        f"[{iso_now()}] exit {reason} pnl={pnl:+.4f} "
        f"realized={t.realized_pnl:+.4f} trades={t.trade_count} | {t.label[:120]}"
    )
    if reason in {"tp", "sl", "timeout", "trail", "breakeven"}:
        maybe_disable_token_after_exit(t, args, logger, reason=reason, pnl=pnl)
        _maybe_disable_side_after_exit(side=side, args=args, st=st, logger=logger)
    return True


def maybe_close_by_rules(t: TokenState, args, st: RuntimeState, logger: Logger) -> bool:
    if t.position_side == 0:
        return False
    hold = now_ts() - float(t.entry_ts or 0.0)
    max_hold = float(args.max_hold_sec)
    # Inactive tokens can stop receiving book updates; still enforce max-hold timeout.
    if max_hold > 0 and hold >= max_hold and float(t.last_mid or 0.0) <= 0:
        return close_position(t, args, st, logger, reason="timeout", force_now=True)
    if t.last_mid <= 0:
        return False
    side = int(t.position_side)
    per_share = side * (float(t.last_mid) - float(t.entry_price))
    t.unrealized_pnl = per_share * float(t.position_size or 0.0)
    t.entry_peak_per_share = max(float(t.entry_peak_per_share or 0.0), float(per_share))

    base_tp = max(0.0, float(args.take_profit_cents)) / 100.0
    base_sl = max(0.0, float(args.stop_loss_cents)) / 100.0
    tp = max(base_tp, float(t.entry_tp_per_share or 0.0))
    sl = max(base_sl, float(t.entry_sl_per_share or 0.0))
    peak = float(t.entry_peak_per_share or 0.0)
    trail_arm = max(0.0, float(args.trail_arm_cents)) / 100.0
    trail_drop = max(0.0, float(args.trail_drop_cents)) / 100.0
    be_arm = max(0.0, float(args.breakeven_arm_cents)) / 100.0
    if tp > 0 and per_share >= tp:
        return close_position(t, args, st, logger, reason="tp")
    if sl > 0 and per_share <= -sl:
        return close_position(t, args, st, logger, reason="sl")
    if trail_arm > 0 and trail_drop > 0 and peak >= trail_arm and per_share <= (peak - trail_drop):
        return close_position(t, args, st, logger, reason="trail")
    if be_arm > 0 and peak >= be_arm and per_share <= 0.0:
        return close_position(t, args, st, logger, reason="breakeven")
    if max_hold > 0 and hold >= max_hold:
        return close_position(t, args, st, logger, reason="timeout")
    return False


def total_pnl(st: RuntimeState) -> Tuple[float, float, float]:
    realized = 0.0
    unreal = 0.0
    for t in st.token_states.values():
        realized += float(t.realized_pnl or 0.0)
        if t.position_side != 0 and t.position_size > 0 and t.last_mid > 0:
            unreal += float(t.position_side) * (float(t.last_mid) - float(t.entry_price)) * float(t.position_size)
        else:
            unreal += float(t.unrealized_pnl or 0.0)
    return realized + unreal, realized, unreal


def open_position_counts(st: RuntimeState, active_ids: List[str]) -> Tuple[int, int, int]:
    active = {str(tid) for tid in (active_ids or []) if str(tid)}
    open_all = 0
    open_active = 0
    for tid, t in st.token_states.items():
        if int(t.position_side or 0) == 0 or float(t.position_size or 0.0) <= 0:
            continue
        open_all += 1
        tok_id = str(t.token_id or tid)
        if tok_id in active:
            open_active += 1
    return open_all, open_active, max(0, open_all - open_active)


def maybe_append_metric(metrics_file: str, payload: dict) -> None:
    if not metrics_file:
        return
    p = Path(metrics_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _control_signature(path: Path) -> str:
    try:
        st = path.stat()
    except FileNotFoundError:
        return "missing"
    except Exception:
        return "error"
    return f"{int(st.st_mtime_ns)}:{int(st.st_size)}"


def _apply_runtime_control(args, logger: Logger, payload: dict, source: str) -> None:
    if not isinstance(payload, dict):
        logger.info(f"[{iso_now()}] control-ignore {source}: json is not an object")
        return
    cfg = payload.get("overrides") if isinstance(payload.get("overrides"), dict) else payload
    if not isinstance(cfg, dict):
        logger.info(f"[{iso_now()}] control-ignore {source}: overrides is not an object")
        return

    changes: List[str] = []

    def apply_float(key: str, lo: Optional[float] = None, hi: Optional[float] = None) -> None:
        if key not in cfg:
            return
        try:
            new_v = float(cfg.get(key))
        except Exception:
            return
        if lo is not None:
            new_v = max(float(lo), new_v)
        if hi is not None:
            new_v = min(float(hi), new_v)
        cur_v = float(getattr(args, key))
        if abs(cur_v - new_v) > 1e-12:
            setattr(args, key, new_v)
            changes.append(f"{key} {cur_v:g}->{new_v:g}")

    def apply_int(key: str, lo: Optional[int] = None, hi: Optional[int] = None) -> None:
        if key not in cfg:
            return
        try:
            new_v = int(cfg.get(key))
        except Exception:
            return
        if lo is not None:
            new_v = max(int(lo), new_v)
        if hi is not None:
            new_v = min(int(hi), new_v)
        cur_v = int(getattr(args, key))
        if cur_v != new_v:
            setattr(args, key, new_v)
            changes.append(f"{key} {cur_v}->{new_v}")

    allowed = cfg.get("allowed_sides")
    if isinstance(allowed, str):
        mode = allowed.strip().lower()
        if mode in ("both", "long", "short") and mode != str(args.allowed_sides):
            prev = str(args.allowed_sides)
            args.allowed_sides = mode
            changes.append(f"allowed_sides {prev}->{mode}")

    apply_float("consensus_min_score", lo=0.0)
    apply_int("consensus_min_agree", lo=1)
    apply_float("consensus_min_score_agree1", lo=0.0)
    apply_float("consensus_min_score_agree2", lo=0.0)
    apply_int("min_non_extreme_agree", lo=0)
    apply_float("min_expected_edge_cents", lo=0.0)
    apply_float("expected_move_cost_ratio", lo=0.0)
    apply_float("max_hold_sec", lo=0.0)
    apply_float("trail_arm_cents", lo=0.0)
    apply_float("trail_drop_cents", lo=0.0)
    apply_float("breakeven_arm_cents", lo=0.0)
    apply_int("token_churn_streak", lo=0)
    apply_float("token_churn_disable_sec", lo=0.0)
    apply_float("token_churn_max_pnl_usd", lo=0.0)

    note = str(payload.get("reason") or payload.get("note") or "").strip()
    if changes:
        msg = f"[{iso_now()}] control-apply {source} | " + ", ".join(changes)
        if note:
            msg += f" | {note[:160]}"
        logger.info(msg)


def maybe_reload_runtime_control(args, logger: Logger, control_file: Optional[Path], last_sig: str) -> str:
    if not control_file:
        return last_sig
    sig = _control_signature(control_file)
    if sig == last_sig:
        return last_sig
    if sig == "missing":
        if last_sig and last_sig != "missing":
            logger.info(f"[{iso_now()}] control-file missing; keep current params: {control_file}")
        return sig
    if sig == "error":
        logger.info(f"[{iso_now()}] control-file stat error: {control_file}")
        return sig
    try:
        payload = json.loads(control_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.info(f"[{iso_now()}] control-file parse error: {e}")
        return sig
    _apply_runtime_control(args, logger, payload, source=str(control_file))
    return sig

def parse_args():
    script_dir = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Observe-only Polymarket fade monitor (multi-bot consensus)")
    p.add_argument("--gamma-limit", type=int, default=900, help="Gamma active markets limit for universe selection")
    p.add_argument("--gamma-page-size", type=int, default=500, help="Gamma page size per request (max 500)")
    p.add_argument("--gamma-pages", type=int, default=12, help="Max Gamma pages to scan for universe selection")
    p.add_argument("--max-tokens", type=int, default=12, help="Max tokens to monitor")
    p.add_argument("--min-liquidity", type=float, default=20000.0, help="Universe filter: liquidityNum minimum")
    p.add_argument("--min-volume24hr", type=float, default=2500.0, help="Universe filter: volume24hr minimum")
    p.add_argument("--min-spread-cents", type=float, default=0.20, help="Universe filter: minimum spread in cents")
    p.add_argument("--include-regex", default="", help="Only include markets where question matches regex")
    p.add_argument("--exclude-regex", default="", help="Exclude markets where question matches regex")
    p.add_argument(
        "--min-days-to-end",
        type=float,
        default=0.0,
        help="Universe filter: minimum days until market endDate (0=disabled)",
    )
    p.add_argument(
        "--max-days-to-end",
        type=float,
        default=0.0,
        help="Universe filter: maximum days until market endDate (0=disabled)",
    )
    p.add_argument("--universe-refresh-sec", type=float, default=600.0, help="Universe refresh cadence")

    p.add_argument("--poll-sec", type=float, default=2.0, help="Order book polling interval")
    p.add_argument("--workers", type=int, default=12, help="Parallel workers for book fetch")
    p.add_argument("--book-depth-levels", type=int, default=5, help="Top N levels for depth/imbalance")
    p.add_argument("--history-size", type=int, default=300, help="Per-token feature history size")

    p.add_argument("--zscore-lookback", type=int, default=40, help="Lookback points for zscore bot")
    p.add_argument("--zscore-entry", type=float, default=1.8, help="Absolute zscore threshold for fade entry")
    p.add_argument("--velocity-window", type=int, default=6, help="Lookback points for velocity bot")
    p.add_argument("--velocity-threshold-cents", type=float, default=1.2, help="Trend threshold for velocity bot")
    p.add_argument("--imbalance-threshold", type=float, default=0.25, help="Depth imbalance threshold (0..1)")
    p.add_argument("--extreme-low", type=float, default=0.08, help="Extreme probability lower bound")
    p.add_argument("--extreme-high", type=float, default=0.92, help="Extreme probability upper bound")

    p.add_argument("--weight-zscore", type=float, default=1.00, help="Consensus weight for zscore bot")
    p.add_argument("--weight-velocity", type=float, default=0.85, help="Consensus weight for velocity bot")
    p.add_argument("--weight-imbalance", type=float, default=0.70, help="Consensus weight for imbalance bot")
    p.add_argument("--weight-extreme", type=float, default=0.90, help="Consensus weight for extreme bot")
    p.add_argument("--consensus-min-score", type=float, default=1.45, help="Min absolute weighted score to enter")
    p.add_argument("--consensus-min-agree", type=int, default=2, help="Min agreeing bots to enter")
    p.add_argument(
        "--consensus-min-score-agree1",
        type=float,
        default=0.0,
        help="Override min score when only 1 bot agrees (0=disabled)",
    )
    p.add_argument(
        "--consensus-min-score-agree2",
        type=float,
        default=0.0,
        help="Override min score when only 2 bots agree (0=disabled)",
    )
    p.add_argument(
        "--min-non-extreme-agree",
        type=int,
        default=1,
        help="Min agreeing bots among zscore/velocity/imbalance (exclude extreme-only entries)",
    )
    p.add_argument(
        "--allowed-sides",
        choices=("both", "long", "short"),
        default="both",
        help="Allow entries only on selected side(s)",
    )

    p.add_argument("--max-spread-cents", type=float, default=2.5, help="Regime gate: max spread for entry")
    p.add_argument("--min-depth-shares", type=float, default=100.0, help="Regime gate: min top-book depth side")
    p.add_argument("--vol-lookback", type=int, default=30, help="Volatility lookback points")
    p.add_argument("--max-volatility-cents", type=float, default=1.4, help="Regime gate: max stdev of returns")
    p.add_argument("--min-volatility-cents", type=float, default=0.0, help="Regime gate: min stdev of returns")
    p.add_argument("--min-mid-prob", type=float, default=0.05, help="Regime gate: minimum mid probability")
    p.add_argument("--max-mid-prob", type=float, default=0.95, help="Regime gate: maximum mid probability")

    p.add_argument("--position-size-shares", type=float, default=25.0, help="Simulated shares per entry")
    p.add_argument("--max-open-positions", type=int, default=3, help="Max simultaneous simulated positions")
    p.add_argument("--cooldown-sec", type=float, default=60.0, help="Per-token cooldown after exit")
    p.add_argument(
        "--loss-cooldown-mult",
        type=float,
        default=1.0,
        help="Multiply cooldown after non-positive exits (>=1.0)",
    )
    p.add_argument("--take-profit-cents", type=float, default=1.4, help="Per-share TP in cents")
    p.add_argument("--stop-loss-cents", type=float, default=1.2, help="Per-share SL in cents")
    p.add_argument("--max-hold-sec", type=float, default=240.0, help="Time stop per position")
    p.add_argument(
        "--trail-arm-cents",
        type=float,
        default=0.0,
        help="Activate trailing exit after this per-share gain in cents (0=disabled)",
    )
    p.add_argument(
        "--trail-drop-cents",
        type=float,
        default=0.0,
        help="Trailing exit distance from peak gain in cents (0=disabled)",
    )
    p.add_argument(
        "--breakeven-arm-cents",
        type=float,
        default=0.0,
        help="After this gain in cents, exit if gain falls back to <= 0 (0=disabled)",
    )
    p.add_argument(
        "--execution-mode",
        choices=("taker", "mid"),
        default="taker",
        help="Price model for simulated fills (taker crosses spread, mid assumes passive/mid fills)",
    )
    p.add_argument("--slippage-ticks", type=float, default=1.0, help="Execution slippage in ticks (sim)")
    p.add_argument(
        "--maker-spread-capture",
        type=float,
        default=0.0,
        help="When --execution-mode mid, fraction of spread captured (0..1) for cost model",
    )
    p.add_argument("--fee-bps", type=float, default=0.0, help="Round-trip fee model in bps (sim)")
    p.add_argument("--tp-cost-mult", type=float, default=1.8, help="Dynamic TP multiplier against round-trip cost")
    p.add_argument("--sl-cost-mult", type=float, default=1.2, help="Dynamic SL multiplier against round-trip cost")
    p.add_argument(
        "--expected-move-vol-mult",
        type=float,
        default=1.6,
        help="Scale from observed volatility to expected move (higher => more aggressive entries)",
    )
    p.add_argument(
        "--expected-move-cost-ratio",
        type=float,
        default=1.4,
        help="Require expected move >= ratio * round-trip cost",
    )
    p.add_argument(
        "--min-expected-edge-cents",
        type=float,
        default=0.10,
        help="Require expected move - round-trip cost >= this many cents",
    )
    p.add_argument("--daily-loss-limit-usd", type=float, default=8.0, help="Halt entries when day PnL <= -limit")
    p.add_argument(
        "--token-loss-cut-usd",
        type=float,
        default=0.60,
        help="Disable token temporarily when its realized pnl <= -value (after min trades)",
    )
    p.add_argument(
        "--token-loss-min-trades",
        type=int,
        default=5,
        help="Min closed trades before token loss/winrate disable logic activates",
    )
    p.add_argument(
        "--token-min-winrate",
        type=float,
        default=0.20,
        help="Disable token temporarily when winrate falls below this (0..1, after min trades)",
    )
    p.add_argument(
        "--token-disable-sec",
        type=float,
        default=1800.0,
        help="How long to disable a losing token before re-enabling",
    )
    p.add_argument(
        "--token-churn-streak",
        type=int,
        default=0,
        help="Disable token when non-positive timeout/trail/breakeven exits reach this streak (0=disabled)",
    )
    p.add_argument(
        "--token-churn-disable-sec",
        type=float,
        default=0.0,
        help="How long to disable a token when churn streak guard triggers",
    )
    p.add_argument(
        "--token-churn-max-pnl-usd",
        type=float,
        default=0.0,
        help="Count timeout/trail/breakeven exits as churn only when pnl <= this USD",
    )
    p.add_argument(
        "--side-loss-cut-usd",
        type=float,
        default=0.0,
        help="Disable LONG/SHORT side temporarily when side realized pnl <= -value (after min trades)",
    )
    p.add_argument(
        "--side-loss-min-trades",
        type=int,
        default=12,
        help="Min closed trades per side before side disable logic activates",
    )
    p.add_argument(
        "--side-min-winrate",
        type=float,
        default=0.0,
        help="Disable side temporarily when side winrate falls below this (0..1, after min trades)",
    )
    p.add_argument(
        "--side-disable-sec",
        type=float,
        default=1800.0,
        help="How long to disable a losing side before re-enabling",
    )

    p.add_argument("--metrics-sample-sec", type=float, default=30.0, help="Metrics JSONL cadence")
    p.add_argument("--summary-every-sec", type=float, default=60.0, help="Summary log cadence")
    p.add_argument("--run-seconds", type=int, default=0, help="Auto-exit after N seconds (0=forever)")
    p.add_argument("--log-signals", action="store_true", help="Log accepted entry signals")
    p.add_argument(
        "--control-file",
        default="",
        help="Optional JSON file for runtime overrides (hot reload)",
    )
    p.add_argument(
        "--control-reload-sec",
        type=float,
        default=15.0,
        help="Control file polling interval in seconds (0=disabled)",
    )

    p.add_argument(
        "--log-file",
        default=str(script_dir.parent / "logs" / "clob-fade-observe.log"),
        help="Event log file path",
    )
    p.add_argument(
        "--state-file",
        default=str(script_dir.parent / "logs" / "clob_fade_observe_state.json"),
        help="Runtime state JSON path",
    )
    p.add_argument(
        "--metrics-file",
        default=str(script_dir.parent / "logs" / "clob-fade-observe-metrics.jsonl"),
        help="Metrics JSONL path",
    )
    return p.parse_args()


def run(args) -> int:
    logger = Logger(args.log_file)
    state_file = Path(args.state_file)
    try:
        release_lock = acquire_state_lock(state_file)
    except Exception as e:
        logger.info(f"[{iso_now()}] startup aborted: {type(e).__name__}: {e}")
        return 2

    logger.info(f"[{iso_now()}] instance-lock acquired: {state_file}.lock")
    state = load_state(state_file)
    if not state.day_key:
        state.day_key = local_day_key()

    logger.info("Polymarket CLOB Fade Monitor (observe-only)")
    logger.info("=" * 60)
    logger.info(
        f"tokens={args.max_tokens} poll={args.poll_sec}s workers={args.workers} "
        f"consensus>={args.consensus_min_score} agree>={args.consensus_min_agree} "
        f"agree1_score>={args.consensus_min_score_agree1} agree2_score>={args.consensus_min_score_agree2} "
        f"sides={args.allowed_sides} "
        f"tp/sl={args.take_profit_cents}/{args.stop_loss_cents}c "
        f"exp_edge>={args.min_expected_edge_cents}c ratio>={args.expected_move_cost_ratio} "
        f"exec={args.execution_mode} loss_cd_x{args.loss_cooldown_mult} "
        f"side_cut={args.side_loss_cut_usd} side_wr>={args.side_min_winrate}"
    )
    control_file: Optional[Path] = Path(args.control_file) if str(args.control_file).strip() else None
    control_sig = ""
    last_control_check = 0.0
    if control_file:
        logger.info(
            f"control={control_file} reload={float(args.control_reload_sec):g}s"
        )
        control_sig = maybe_reload_runtime_control(args, logger, control_file, control_sig)

    t0 = now_ts()
    last_summary = now_ts()
    last_metrics = 0.0

    while True:
        if args.run_seconds and (now_ts() - t0) >= int(args.run_seconds):
            logger.info("run-seconds reached. stopping.")
            break

        if state.day_key != local_day_key():
            total, _, _ = total_pnl(state)
            state.day_key = local_day_key()
            state.day_anchor_total_pnl = total
            state.halted = False
            state.halt_reason = ""
            state.long_trades = 0
            state.long_wins = 0
            state.long_realized = 0.0
            state.short_trades = 0
            state.short_wins = 0
            state.short_realized = 0.0
            state.disable_long_until_ts = 0.0
            state.disable_short_until_ts = 0.0
            state.disable_side_reason = ""
            logger.info(f"[{iso_now()}] day rollover: anchor={total:+.4f}")

        if (
            control_file
            and float(args.control_reload_sec) > 0
            and (now_ts() - float(last_control_check or 0.0)) >= float(args.control_reload_sec)
        ):
            last_control_check = now_ts()
            control_sig = maybe_reload_runtime_control(args, logger, control_file, control_sig)

        if (now_ts() - float(state.last_universe_refresh_ts or 0.0)) >= float(args.universe_refresh_sec):
            uni = choose_universe(args)
            merge_universe(state, uni)
            state.last_universe_refresh_ts = now_ts()
            state.universe_refresh_count += 1
            logger.info(
                f"[{iso_now()}] universe refresh: active={len(state.active_token_ids)} "
                f"top={', '.join((state.token_states[t].label[:28] for t in state.active_token_ids[:3]))}"
            )

        active_ids = [tid for tid in state.active_token_ids if (state.token_states.get(tid) and state.token_states[tid].active)]
        books = fetch_books_parallel(active_ids, workers=int(args.workers))

        for tid in active_ids:
            t = state.token_states[tid]
            book = books.get(tid)
            if not book:
                continue
            feat = extract_book_features(book, depth_levels=int(args.book_depth_levels))
            if not feat:
                continue
            update_token_from_book(t, feat, history_size=int(args.history_size))

        # Exit checks first to keep risk bounded, including inactive open positions.
        for t in state.token_states.values():
            if int(t.position_side or 0) == 0:
                continue
            maybe_close_by_rules(t, args, state, logger)

        # Daily loss guard based on mark-to-mid total PnL.
        total, realized, unreal = total_pnl(state)
        day = total - float(state.day_anchor_total_pnl or 0.0)
        if float(args.daily_loss_limit_usd) > 0 and day <= -float(args.daily_loss_limit_usd):
            if not state.halted:
                state.halted = True
                state.halt_reason = (
                    f"daily loss guard hit: day_pnl {day:+.4f} <= -{float(args.daily_loss_limit_usd):.4f}"
                )
                logger.info(f"[{iso_now()}] HALT: {state.halt_reason}")
            for t in state.token_states.values():
                if t.position_side != 0:
                    close_position(t, args, state, logger, reason="daily_loss_guard", force_now=True)

        open_all_positions, open_active_positions, open_inactive_positions = open_position_counts(state, active_ids)
        slots = max(0, int(args.max_open_positions) - open_active_positions)
        candidates: List[Tuple[float, str, int, dict]] = []
        now_entry = now_ts()
        if state.disable_long_until_ts > 0 and now_entry >= float(state.disable_long_until_ts):
            state.disable_long_until_ts = 0.0
            state.disable_side_reason = ""
            logger.info(f"[{iso_now()}] side-enable LONG")
        if state.disable_short_until_ts > 0 and now_entry >= float(state.disable_short_until_ts):
            state.disable_short_until_ts = 0.0
            state.disable_side_reason = ""
            logger.info(f"[{iso_now()}] side-enable SHORT")

        for tid in active_ids:
            t = state.token_states[tid]
            now = now_ts()
            if t.disabled_until_ts > 0:
                if now < float(t.disabled_until_ts):
                    continue
                # Token cooldown expired; allow entries again.
                t.disabled_until_ts = 0.0
                t.disable_reason = ""
            if t.last_mid <= 0 or len(t.mids) < 5:
                continue
            side, score, agree = evaluate_consensus(t, args)
            if side == 0:
                continue
            allowed = str(args.allowed_sides)
            if (allowed == "long" and side < 0) or (allowed == "short" and side > 0):
                continue
            if side > 0 and now < float(state.disable_long_until_ts or 0.0):
                continue
            if side < 0 and now < float(state.disable_short_until_ts or 0.0):
                continue
            t.last_signal_ts = now_ts()
            state.signals_seen += 1
            if t.position_side != 0:
                continue
            if now < float(t.cooldown_until_ts or 0.0):
                continue

            ok_plan, plan = _entry_plan(t, score, args)
            t.last_round_trip_cost_per_share = as_float(plan.get("rt_cost"), 0.0)
            t.last_expected_move_per_share = as_float(plan.get("exp_move"), 0.0)
            t.last_expected_edge_per_share = as_float(plan.get("exp_edge"), 0.0)
            if not ok_plan:
                if args.log_signals:
                    logger.info(
                        f"[{iso_now()}] reject {'LONG' if side > 0 else 'SHORT'} "
                        f"score={score:+.2f} agree={agree} reason={plan.get('reason','?')} "
                        f"edge={as_float(plan.get('exp_edge'),0.0):+.4f} cost={as_float(plan.get('rt_cost'),0.0):.4f} "
                        f"| {t.label[:100]}"
                    )
                continue
            candidates.append((abs(score), tid, side, plan))
            if args.log_signals:
                logger.info(
                    f"[{iso_now()}] signal {'LONG' if side > 0 else 'SHORT'} "
                    f"score={score:+.2f} agree={agree} z={t.zscore:+.2f} "
                    f"imb={t.last_imbalance:+.2f} vel={t.velocity_move:+.4f} "
                    f"edge={as_float(plan.get('exp_edge'),0.0):+.4f} cost={as_float(plan.get('rt_cost'),0.0):.4f} "
                    f"| {t.label[:100]}"
                )

        if (not state.halted) and slots > 0 and candidates:
            for _, tid, side, plan in sorted(candidates, key=lambda x: x[0], reverse=True):
                if slots <= 0:
                    break
                t = state.token_states[tid]
                if t.position_side != 0:
                    continue
                open_position(t, side, args, state, logger, plan=plan)
                slots -= 1

        if float(args.metrics_sample_sec) > 0 and (now_ts() - last_metrics) >= float(args.metrics_sample_sec):
            last_metrics = now_ts()
            total, realized, unreal = total_pnl(state)
            day = total - float(state.day_anchor_total_pnl or 0.0)
            open_all_positions, open_active_positions, open_inactive_positions = open_position_counts(state, active_ids)
            for tid in active_ids:
                t = state.token_states[tid]
                maybe_append_metric(
                    args.metrics_file,
                    {
                        "ts": iso_now(),
                        "ts_ms": int(now_ts() * 1000.0),
                        "token_id": tid,
                        "label": t.label[:180],
                        "mid": float(t.last_mid or 0.0),
                        "best_bid": float(t.last_bid or 0.0),
                        "best_ask": float(t.last_ask or 0.0),
                        "spread": float(t.last_spread or 0.0),
                        "depth_bid": float(t.last_depth_bid or 0.0),
                        "depth_ask": float(t.last_depth_ask or 0.0),
                        "imbalance": float(t.last_imbalance or 0.0),
                        "zscore": float(t.zscore or 0.0),
                        "velocity_move": float(t.velocity_move or 0.0),
                        "bot_zscore": float(t.bot_zscore or 0.0),
                        "bot_velocity": float(t.bot_velocity or 0.0),
                        "bot_imbalance": float(t.bot_imbalance or 0.0),
                        "bot_extreme": float(t.bot_extreme or 0.0),
                        "consensus_score": float(t.consensus_score or 0.0),
                        "consensus_side": int(t.consensus_side or 0),
                        "consensus_agree": int(t.consensus_agree or 0),
                        "position_side": int(t.position_side or 0),
                        "position_size": float(t.position_size or 0.0),
                        "disabled_until_ts": float(t.disabled_until_ts or 0.0),
                        "disable_reason": str(t.disable_reason or "")[:120],
                        "entry_price": float(t.entry_price or 0.0),
                        "entry_tp_per_share": float(t.entry_tp_per_share or 0.0),
                        "entry_sl_per_share": float(t.entry_sl_per_share or 0.0),
                        "entry_cost_per_share": float(t.entry_cost_per_share or 0.0),
                        "entry_expected_move_per_share": float(t.entry_expected_move_per_share or 0.0),
                        "entry_peak_per_share": float(t.entry_peak_per_share or 0.0),
                        "last_round_trip_cost_per_share": float(t.last_round_trip_cost_per_share or 0.0),
                        "last_expected_move_per_share": float(t.last_expected_move_per_share or 0.0),
                        "last_expected_edge_per_share": float(t.last_expected_edge_per_share or 0.0),
                        "unrealized_pnl": float(t.unrealized_pnl or 0.0),
                        "realized_pnl": float(t.realized_pnl or 0.0),
                        "trade_count": int(t.trade_count or 0),
                        "win_count": int(t.win_count or 0),
                        "loss_count": int(t.loss_count or 0),
                        "consecutive_nonpositive_exits": int(t.consecutive_nonpositive_exits or 0),
                        "last_exit_reason": str(t.last_exit_reason or "")[:32],
                        "last_exit_pnl": float(t.last_exit_pnl or 0.0),
                        "halted": bool(state.halted),
                        "open_positions_total": int(open_all_positions),
                        "open_positions_active": int(open_active_positions),
                        "open_positions_inactive": int(open_inactive_positions),
                        "total_pnl": float(total),
                        "day_pnl": float(day),
                        "realized_total": float(realized),
                        "unrealized_total": float(unreal),
                    },
                )

        if float(args.summary_every_sec) > 0 and (now_ts() - last_summary) >= float(args.summary_every_sec):
            last_summary = now_ts()
            total, _, _ = total_pnl(state)
            day = total - float(state.day_anchor_total_pnl or 0.0)
            open_all_positions, open_active_positions, open_inactive_positions = open_position_counts(state, active_ids)
            entry_exit_gap = int(state.entries or 0) - int(state.exits or 0)
            consistency_ok = (entry_exit_gap == open_all_positions)
            disabled_tokens = sum(1 for tid in active_ids if state.token_states[tid].disabled_until_ts > now_ts())
            long_losses = max(0, int(state.long_trades or 0) - int(state.long_wins or 0))
            short_losses = max(0, int(state.short_trades or 0) - int(state.short_wins or 0))
            side_disabled = (
                f"L{int(float(state.disable_long_until_ts or 0.0) > now_ts())}"
                f"/S{int(float(state.disable_short_until_ts or 0.0) > now_ts())}"
            )
            side_stats = (
                f"L={float(state.long_realized or 0.0):+.4f}({int(state.long_wins or 0)}/{long_losses}) "
                f"S={float(state.short_realized or 0.0):+.4f}({int(state.short_wins or 0)}/{short_losses})"
            )
            warnings: List[str] = []
            if open_inactive_positions > 0:
                warnings.append(f"inactive_open={open_inactive_positions}")
            if not consistency_ok:
                warnings.append(f"entries_minus_exits={entry_exit_gap} open_total={open_all_positions}")
            warn_txt = f" warn={','.join(warnings)}" if warnings else ""
            best_sig = None
            for tid in active_ids:
                t = state.token_states[tid]
                if best_sig is None or abs(t.consensus_score) > abs(best_sig.consensus_score):
                    best_sig = t
            if best_sig:
                logger.info(
                    f"[{iso_now()}] summary({int(args.summary_every_sec)}s): "
                    f"active={len(active_ids)} open={open_all_positions} "
                    f"open_active={open_active_positions} open_inactive={open_inactive_positions} "
                    f"signals={state.signals_seen} "
                    f"entries={state.entries} exits={state.exits} disabled={disabled_tokens} "
                    f"mode={args.allowed_sides} side_dis={side_disabled} side={side_stats} "
                    f"day_pnl={day:+.4f} total={total:+.4f} "
                    f"best={best_sig.consensus_score:+.2f} {best_sig.label[:52]}"
                    f"{warn_txt}"
                )
            else:
                logger.info(
                    f"[{iso_now()}] summary({int(args.summary_every_sec)}s): "
                    f"active={len(active_ids)} open={open_all_positions} "
                    f"open_active={open_active_positions} open_inactive={open_inactive_positions} "
                    f"signals={state.signals_seen} "
                    f"entries={state.entries} exits={state.exits} disabled={disabled_tokens} "
                    f"mode={args.allowed_sides} side_dis={side_disabled} side={side_stats} "
                    f"day_pnl={day:+.4f} total={total:+.4f}"
                    f"{warn_txt}"
                )

        try:
            save_state(state_file, state)
        except Exception as e:
            logger.info(f"[{iso_now()}] state-save error: {type(e).__name__}: {e}")
        time.sleep(max(0.2, float(args.poll_sec)))

    total, realized, unreal = total_pnl(state)
    day = total - float(state.day_anchor_total_pnl or 0.0)
    logger.info(
        f"[{iso_now()}] stopped | total={total:+.4f} day={day:+.4f} "
        f"realized={realized:+.4f} unrealized={unreal:+.4f} entries={state.entries} exits={state.exits}"
    )
    try:
        save_state(state_file, state)
    except Exception as e:
        logger.info(f"[{iso_now()}] state-save error: {type(e).__name__}: {e}")
    try:
        release_lock()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
