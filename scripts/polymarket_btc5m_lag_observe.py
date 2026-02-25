#!/usr/bin/env python3
"""
Polymarket BTC lag observer (simulation only).

Purpose:
- Observe Polymarket BTC up/down short-window market pricing vs external BTC spot.
- Estimate a simple fair probability for "UP" using a diffusion model.
- Emit mispricing signals and simulate one taker-style paper entry per window.
- Optional entry filters can emulate "low-price panic + reversal" style setups.

Supported window sizes:
- 5 minutes (default)
- 15 minutes

This script never places real orders. It is always observe-only.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
USER_AGENT = "btc5m-lag-observe/1.0"

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_FILE = str(DEFAULT_REPO_ROOT / "logs" / "btc5m-lag-observe.log")
DEFAULT_STATE_FILE = str(DEFAULT_REPO_ROOT / "logs" / "btc5m_lag_observe_state.json")
DEFAULT_METRICS_FILE = str(DEFAULT_REPO_ROOT / "logs" / "btc5m-lag-observe-metrics.jsonl")


def default_runtime_paths(window_minutes: int) -> Tuple[str, str, str]:
    m = int(window_minutes)
    if m == 5:
        return DEFAULT_LOG_FILE, DEFAULT_STATE_FILE, DEFAULT_METRICS_FILE
    logs_dir = DEFAULT_REPO_ROOT / "logs"
    return (
        str(logs_dir / f"btc{m}m-lag-observe.log"),
        str(logs_dir / f"btc{m}m_lag_observe_state.json"),
        str(logs_dir / f"btc{m}m-lag-observe-metrics.jsonl"),
    )


def iso_now_local() -> str:
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


def _env_bool(name: str) -> Optional[bool]:
    v = _env_str(name).lower()
    if not v:
        return None
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return None


class Logger:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, msg: str) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(msg + "\n")

    def info(self, msg: str) -> None:
        print(msg)
        self._append(msg)


def _append_metrics(path: str, payload: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")


def as_float(x, default: float = math.nan) -> float:
    try:
        return float(x)
    except Exception:
        return default


def parse_json_string_field(value) -> List[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            arr = json.loads(value)
            if isinstance(arr, list):
                return [str(v) for v in arr]
        except Exception:
            return []
    return []


def _http_get_json(url: str, timeout_sec: float = 4.0) -> object:
    req = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    with urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def fetch_gamma_event_by_slug(slug: str) -> Optional[dict]:
    url = f"{GAMMA_API_BASE}/events/slug/{slug}"
    try:
        obj = _http_get_json(url, timeout_sec=6.0)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def fetch_clob_book(token_id: str) -> Optional[dict]:
    url = f"{CLOB_API_BASE}/book?token_id={token_id}"
    try:
        obj = _http_get_json(url, timeout_sec=4.0)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def best_ask(book: Optional[dict]) -> float:
    if not isinstance(book, dict):
        return math.nan
    asks = book.get("asks")
    if not isinstance(asks, list) or not asks:
        return math.nan
    vals = [as_float(x.get("price"), math.nan) for x in asks if isinstance(x, dict)]
    vals = [x for x in vals if math.isfinite(x) and x > 0]
    return min(vals) if vals else math.nan


def best_bid(book: Optional[dict]) -> float:
    if not isinstance(book, dict):
        return math.nan
    bids = book.get("bids")
    if not isinstance(bids, list) or not bids:
        return math.nan
    vals = [as_float(x.get("price"), math.nan) for x in bids if isinstance(x, dict)]
    vals = [x for x in vals if math.isfinite(x) and x > 0]
    return max(vals) if vals else math.nan


def _iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clamp01(x: float) -> float:
    if not math.isfinite(x):
        return 0.5
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    return x


def _norm_cdf(x: float) -> float:
    if not math.isfinite(x):
        return 0.5
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def estimate_sigma_per_s(history: Deque[Tuple[float, float]], lookback_sec: float, floor_sigma: float) -> float:
    if lookback_sec <= 0:
        return max(1e-9, floor_sigma)
    cutoff = now_ts() - float(lookback_sec)
    pts = [(t, p) for (t, p) in history if t >= cutoff and p > 0]
    if len(pts) < 4:
        return max(1e-9, floor_sigma)

    var_rate_sum = 0.0
    n = 0
    for i in range(1, len(pts)):
        t0, p0 = pts[i - 1]
        t1, p1 = pts[i]
        dt = max(1e-6, t1 - t0)
        if p0 <= 0 or p1 <= 0:
            continue
        r = math.log(p1 / p0)
        var_rate_sum += (r * r) / dt
        n += 1
    if n < 3:
        return max(1e-9, floor_sigma)
    sig = math.sqrt(max(var_rate_sum / float(n), 0.0))
    return max(sig, floor_sigma, 1e-9)


def fair_up_probability(spot: float, open_price: float, remaining_sec: float, sigma_per_s: float) -> float:
    if not (math.isfinite(spot) and spot > 0 and math.isfinite(open_price) and open_price > 0):
        return 0.5
    if remaining_sec <= 0.5:
        if spot > open_price:
            return 1.0
        if spot < open_price:
            return 0.0
        return 0.5

    log_m = math.log(spot / open_price)
    denom = max(1e-9, sigma_per_s * math.sqrt(max(remaining_sec, 1e-6)))
    z = log_m / denom
    return _clamp01(_norm_cdf(z))


def _first_price_at_or_after(history: Deque[Tuple[float, float]], ts_cutoff: float) -> Optional[float]:
    for t, p in history:
        if t >= ts_cutoff and math.isfinite(p) and p > 0:
            return float(p)
    return None


def has_two_leg_reversal(
    history: Deque[Tuple[float, float]],
    side: str,
    lookback_sec: float,
    min_move_usd: float,
) -> bool:
    """
    Reversal check over one lookback window:
      - UP: older half down by >= min_move, newer half up by >= min_move
      - DOWN: older half up by >= min_move, newer half down by >= min_move
    """
    lb = max(1.0, float(lookback_sec))
    move = max(0.0, float(min_move_usd))
    if len(history) < 6:
        return False

    t_now = now_ts()
    t0 = t_now - lb
    tm = t_now - (0.5 * lb)
    p0 = _first_price_at_or_after(history, t0)
    pm = _first_price_at_or_after(history, tm)
    p2 = float(history[-1][1]) if history and math.isfinite(history[-1][1]) and history[-1][1] > 0 else None
    if p0 is None or pm is None or p2 is None:
        return False

    first_leg = pm - p0
    second_leg = p2 - pm
    s = str(side or "").strip().upper()
    if s == "UP":
        return (first_leg <= -move) and (second_leg >= move)
    if s == "DOWN":
        return (first_leg >= move) and (second_leg <= -move)
    return False


def fetch_coinbase_price() -> Optional[float]:
    url = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
    try:
        obj = _http_get_json(url, timeout_sec=3.0)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    px = as_float(obj.get("price"), math.nan)
    return px if math.isfinite(px) and px > 0 else None


def fetch_kraken_price() -> Optional[float]:
    url = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
    try:
        obj = _http_get_json(url, timeout_sec=3.0)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    result = obj.get("result")
    if not isinstance(result, dict) or not result:
        return None
    first_pair = next(iter(result.values()))
    if not isinstance(first_pair, dict):
        return None
    c = first_pair.get("c")
    if not isinstance(c, list) or not c:
        return None
    px = as_float(c[0], math.nan)
    return px if math.isfinite(px) and px > 0 else None


def fetch_bitstamp_price() -> Optional[float]:
    url = "https://www.bitstamp.net/api/v2/ticker/btcusd/"
    try:
        obj = _http_get_json(url, timeout_sec=3.0)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    px = as_float(obj.get("last"), math.nan)
    return px if math.isfinite(px) and px > 0 else None


def fetch_external_spot() -> Tuple[Optional[float], Dict[str, float]]:
    prices: Dict[str, float] = {}
    for name, fn in (
        ("coinbase", fetch_coinbase_price),
        ("kraken", fetch_kraken_price),
        ("bitstamp", fetch_bitstamp_price),
    ):
        p = fn()
        if p is not None and math.isfinite(p) and p > 0:
            prices[name] = float(p)
    if not prices:
        return None, {}
    vals = sorted(prices.values())
    med = vals[len(vals) // 2]
    return med, prices


def fetch_coinbase_candle_open_close(start_ts: int, window_sec: int) -> Tuple[Optional[float], Optional[float]]:
    """
    Return (open, close) for the candle starting at start_ts (UTC).
    """
    start_ts = int(start_ts)
    granularity = max(60, int(window_sec))
    end_ts = start_ts + granularity
    q = urlencode(
        {
            "granularity": str(granularity),
            "start": _iso_utc(start_ts),
            "end": _iso_utc(end_ts),
        }
    )
    url = f"https://api.exchange.coinbase.com/products/BTC-USD/candles?{q}"
    try:
        obj = _http_get_json(url, timeout_sec=5.0)
    except Exception:
        return None, None
    if not isinstance(obj, list):
        return None, None
    for row in obj:
        if not isinstance(row, list) or len(row) < 5:
            continue
        ts = int(as_float(row[0], -1))
        if ts != start_ts:
            continue
        # Coinbase format: [time, low, high, open, close, volume]
        op = as_float(row[3], math.nan)
        cl = as_float(row[4], math.nan)
        open_px = op if math.isfinite(op) and op > 0 else None
        close_px = cl if math.isfinite(cl) and cl > 0 else None
        return open_px, close_px
    return None, None


@dataclass
class MarketWindow:
    start_ts: int
    end_ts: int
    slug: str
    event_title: str
    market_id: str
    question: str
    up_token_id: str
    down_token_id: str
    up_label: str
    down_label: str


def _looks_up(label: str) -> bool:
    s = (label or "").strip().lower()
    return any(x in s for x in ("up", "higher", "rise", "rises", "increase", "above", "yes"))


def _looks_down(label: str) -> bool:
    s = (label or "").strip().lower()
    return any(x in s for x in ("down", "lower", "fall", "falls", "decrease", "below", "no"))


def infer_up_down_tokens(question: str, token_ids: List[str], outcomes: List[str]) -> Optional[Tuple[str, str, str, str]]:
    if len(token_ids) < 2:
        return None
    labels = outcomes[:] if outcomes else [f"out{i+1}" for i in range(len(token_ids))]
    labels = (labels + [f"out{i+1}" for i in range(len(labels), len(token_ids))])[: len(token_ids)]

    up_idx = None
    down_idx = None

    for i, lbl in enumerate(labels):
        if up_idx is None and _looks_up(lbl):
            up_idx = i
        if down_idx is None and _looks_down(lbl):
            down_idx = i

    if up_idx is None or down_idx is None:
        q = (question or "").strip().lower()
        yes_idx = None
        no_idx = None
        for i, lbl in enumerate(labels):
            x = (lbl or "").strip().lower()
            if x == "yes":
                yes_idx = i
            elif x == "no":
                no_idx = i
        if yes_idx is not None and no_idx is not None:
            if any(k in q for k in ("up", "higher", "increase", "above")):
                up_idx, down_idx = yes_idx, no_idx
            elif any(k in q for k in ("down", "lower", "decrease", "below")):
                up_idx, down_idx = no_idx, yes_idx

    if up_idx is None or down_idx is None:
        up_idx, down_idx = 0, 1

    if up_idx == down_idx:
        return None
    return token_ids[up_idx], token_ids[down_idx], labels[up_idx], labels[down_idx]


def build_market_window(start_ts: int, window_minutes: int) -> Optional[MarketWindow]:
    start_ts = int(start_ts)
    window_min = int(window_minutes)
    slug = f"btc-updown-{window_min}m-{start_ts}"
    ev = fetch_gamma_event_by_slug(slug)
    if not isinstance(ev, dict):
        return None

    markets = ev.get("markets")
    if not isinstance(markets, list) or not markets:
        return None

    candidates: List[Tuple[float, dict]] = []
    for market in markets:
        if not isinstance(market, dict):
            continue
        if market.get("enableOrderBook") is False:
            continue
        if bool(market.get("closed", False)):
            continue
        tids = parse_json_string_field(market.get("clobTokenIds"))
        if len(tids) < 2:
            continue
        liq = as_float(market.get("liquidityNum", market.get("liquidity", 0.0)), 0.0)
        candidates.append((liq, market))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    chosen = candidates[0][1]

    tids = parse_json_string_field(chosen.get("clobTokenIds"))
    outs = parse_json_string_field(chosen.get("outcomes"))
    question = str(chosen.get("question") or ev.get("title") or slug).strip()
    mapping = infer_up_down_tokens(question=question, token_ids=tids, outcomes=outs)
    if mapping is None:
        return None
    up_tid, down_tid, up_label, down_label = mapping
    return MarketWindow(
        start_ts=start_ts,
        end_ts=start_ts + (window_min * 60),
        slug=slug,
        event_title=str(ev.get("title") or question or slug).strip(),
        market_id=str(chosen.get("id") or "").strip(),
        question=question,
        up_token_id=up_tid,
        down_token_id=down_tid,
        up_label=up_label,
        down_label=down_label,
    )


@dataclass
class RuntimeState:
    day_key: str
    day_anchor_pnl_usd: float = 0.0
    pnl_total_usd: float = 0.0
    trades_closed: int = 0
    wins: int = 0
    losses: int = 0
    pushes: int = 0
    active_position: Optional[dict] = None
    current_window_start_ts: int = 0
    current_window_slug: str = ""
    current_window_open_price: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    consecutive_errors: int = 0


def _save_state(path: Path, state: RuntimeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), ensure_ascii=True, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_state(path: Path) -> RuntimeState:
    if not path.exists():
        return RuntimeState(day_key=local_day_key())
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return RuntimeState(day_key=local_day_key())
        return RuntimeState(
            day_key=str(raw.get("day_key") or local_day_key()),
            day_anchor_pnl_usd=float(raw.get("day_anchor_pnl_usd") or 0.0),
            pnl_total_usd=float(raw.get("pnl_total_usd") or 0.0),
            trades_closed=int(raw.get("trades_closed") or 0),
            wins=int(raw.get("wins") or 0),
            losses=int(raw.get("losses") or 0),
            pushes=int(raw.get("pushes") or 0),
            active_position=raw.get("active_position") if isinstance(raw.get("active_position"), dict) else None,
            current_window_start_ts=int(raw.get("current_window_start_ts") or 0),
            current_window_slug=str(raw.get("current_window_slug") or ""),
            current_window_open_price=float(raw.get("current_window_open_price") or 0.0),
            halted=bool(raw.get("halted") or False),
            halt_reason=str(raw.get("halt_reason") or ""),
            consecutive_errors=int(raw.get("consecutive_errors") or 0),
        )
    except Exception:
        return RuntimeState(day_key=local_day_key())


def day_pnl_usd(state: RuntimeState) -> float:
    today = local_day_key()
    if state.day_key != today:
        state.day_key = today
        state.day_anchor_pnl_usd = state.pnl_total_usd
    return float(state.pnl_total_usd - state.day_anchor_pnl_usd)


def _entry_fee_cost(entry_notional: float, taker_fee_rate: float) -> float:
    if entry_notional <= 0 or taker_fee_rate <= 0:
        return 0.0
    return float(entry_notional * taker_fee_rate)


def settle_active_position(
    state: RuntimeState,
    close_price: float,
    settle_epsilon: float,
    taker_fee_rate: float,
    logger: Logger,
) -> None:
    pos = state.active_position
    if not isinstance(pos, dict):
        return
    entry_price = as_float(pos.get("entry_price"), math.nan)
    shares = as_float(pos.get("shares"), math.nan)
    open_px = as_float(pos.get("window_open_price"), math.nan)
    side = str(pos.get("side") or "").strip().upper()
    if (
        not math.isfinite(entry_price)
        or entry_price <= 0
        or not math.isfinite(shares)
        or shares <= 0
        or not math.isfinite(open_px)
        or open_px <= 0
        or not math.isfinite(close_price)
        or close_price <= 0
        or side not in {"UP", "DOWN"}
    ):
        state.active_position = None
        return

    diff = close_price - open_px
    if abs(diff) <= float(settle_epsilon):
        outcome = "PUSH"
        gross = 0.0
    elif diff > 0:
        outcome = "WIN" if side == "UP" else "LOSS"
        gross = shares * (1.0 - entry_price) if side == "UP" else (-shares * entry_price)
    else:
        outcome = "WIN" if side == "DOWN" else "LOSS"
        gross = shares * (1.0 - entry_price) if side == "DOWN" else (-shares * entry_price)

    fee_cost = _entry_fee_cost(entry_notional=shares * entry_price, taker_fee_rate=taker_fee_rate)
    pnl = gross - fee_cost
    state.pnl_total_usd += pnl
    state.trades_closed += 1
    if outcome == "WIN":
        state.wins += 1
    elif outcome == "LOSS":
        state.losses += 1
    else:
        state.pushes += 1

    logger.info(
        f"[{iso_now_local()}] settle {outcome} side={side} "
        f"entry={entry_price:.4f} shares={shares:.2f} open={open_px:.2f} close={close_price:.2f} "
        f"gross={gross:+.4f} fee={fee_cost:+.4f} pnl={pnl:+.4f} total={state.pnl_total_usd:+.4f}"
    )
    state.active_position = None


def _apply_env_overrides(args):
    prefix = "BTC5MLAG_"
    for k, v in vars(args).items():
        env_name = prefix + k.upper().replace("-", "_")
        if isinstance(v, bool):
            b = _env_bool(env_name)
            if b is not None:
                setattr(args, k, b)
            continue
        if isinstance(v, int):
            x = _env_int(env_name)
            if x is not None:
                setattr(args, k, x)
            continue
        if isinstance(v, float):
            x = _env_float(env_name)
            if x is not None:
                setattr(args, k, x)
            continue
        if isinstance(v, str):
            s = _env_str(env_name)
            if s:
                setattr(args, k, s)
            continue
    return args


def parse_args():
    p = argparse.ArgumentParser(description="Polymarket BTC lag observer (simulation only)")
    p.add_argument(
        "--window-minutes",
        type=int,
        choices=[5, 15],
        default=5,
        help="Target market window size in minutes (5 or 15)",
    )
    p.add_argument("--poll-sec", type=float, default=1.0, help="Polling interval seconds")
    p.add_argument("--summary-every-sec", type=float, default=15.0, help="Summary cadence seconds (0=disabled)")
    p.add_argument("--metrics-sample-sec", type=float, default=5.0, help="Metrics cadence seconds (0=disabled)")
    p.add_argument("--run-seconds", type=int, default=0, help="Auto-exit after N seconds (0=run forever)")

    p.add_argument("--shares", type=float, default=25.0, help="Paper entry size in shares")
    p.add_argument("--entry-edge-cents", type=float, default=1.5, help="Paper entry threshold in cents")
    p.add_argument("--alert-edge-cents", type=float, default=0.8, help="Signal alert threshold in cents")
    p.add_argument(
        "--entry-price-min",
        type=float,
        default=0.0,
        help="Optional lower bound for entry price (0-1; default no lower bound)",
    )
    p.add_argument(
        "--entry-price-max",
        type=float,
        default=1.0,
        help="Optional upper bound for entry price (0-1; default no upper bound)",
    )
    p.add_argument(
        "--require-reversal",
        action="store_true",
        help="Require a two-leg spot reversal pattern before paper entry (panic/reversal style filter)",
    )
    p.add_argument(
        "--reversal-lookback-sec",
        type=float,
        default=30.0,
        help="Lookback window seconds for --require-reversal",
    )
    p.add_argument(
        "--reversal-min-move-usd",
        type=float,
        default=5.0,
        help="Minimum USD move per half-leg for reversal confirmation",
    )
    p.add_argument("--min-remaining-sec", type=float, default=20.0, help="Do not enter if less than this many sec remain")
    p.add_argument(
        "--no-max-one-entry-per-window",
        dest="max_one_entry_per_window",
        action="store_false",
        help="Allow multiple paper entries in one window",
    )
    p.set_defaults(max_one_entry_per_window=True)
    p.add_argument("--settle-epsilon-usd", type=float, default=0.5, help="Treat close-open within epsilon as PUSH")

    p.add_argument("--taker-fee-rate", type=float, default=0.0, help="Assumed taker fee rate (e.g. 0.002)")
    p.add_argument("--daily-loss-limit-usd", type=float, default=0.0, help="Halt new paper entries if day pnl <= -limit")
    p.add_argument("--vol-lookback-sec", type=float, default=120.0, help="Spot return lookback for sigma estimate")
    p.add_argument(
        "--sigma-floor-per-sqrt-sec",
        type=float,
        default=0.00008,
        help="Floor sigma per sqrt(second) for fair probability model",
    )
    p.add_argument("--max-consecutive-errors", type=int, default=10, help="Halt after N consecutive loop errors")

    p.add_argument("--log-file", default=DEFAULT_LOG_FILE, help="Log file path")
    p.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="State file path")
    p.add_argument("--metrics-file", default=DEFAULT_METRICS_FILE, help="Metrics JSONL path")

    args = p.parse_args()
    args = _apply_env_overrides(args)
    d_log, d_state, d_metrics = default_runtime_paths(int(args.window_minutes))
    if args.log_file == DEFAULT_LOG_FILE:
        args.log_file = d_log
    if args.state_file == DEFAULT_STATE_FILE:
        args.state_file = d_state
    if args.metrics_file == DEFAULT_METRICS_FILE:
        args.metrics_file = d_metrics
    return args


def main() -> int:
    args = parse_args()
    window_sec = int(args.window_minutes) * 60
    args.entry_price_min = max(0.0, min(1.0, float(args.entry_price_min)))
    args.entry_price_max = max(0.0, min(1.0, float(args.entry_price_max)))
    if args.entry_price_max < args.entry_price_min:
        args.entry_price_min, args.entry_price_max = args.entry_price_max, args.entry_price_min
    logger = Logger(args.log_file)
    state_path = Path(args.state_file)
    state = _load_state(state_path)
    history: Deque[Tuple[float, float]] = deque(maxlen=1200)
    entered_windows: Dict[int, bool] = {}
    filter_skip_price_band = 0
    filter_skip_reversal = 0

    logger.info(f"Polymarket BTC {int(args.window_minutes)}m Lag Observer")
    logger.info("=" * 64)
    logger.info("Mode: observe-only (no live orders)")
    logger.info(
        f"Config: window={int(args.window_minutes)}m poll={args.poll_sec:.2f}s summary={args.summary_every_sec:.1f}s "
        f"metrics={args.metrics_sample_sec:.1f}s shares={args.shares:.2f} "
        f"entry_edge={args.entry_edge_cents:.2f}c alert_edge={args.alert_edge_cents:.2f}c "
        f"taker_fee={args.taker_fee_rate:.4f}"
    )
    logger.info(
        f"Entry filters: price_band=[{args.entry_price_min:.3f},{args.entry_price_max:.3f}] "
        f"reversal={'on' if args.require_reversal else 'off'} "
        f"lb={float(args.reversal_lookback_sec):.1f}s move={float(args.reversal_min_move_usd):.2f}"
    )
    logger.info(f"Log: {args.log_file}")
    logger.info(f"State: {args.state_file}")
    logger.info(f"Metrics: {args.metrics_file}")
    if state.halted:
        logger.info(f"Resume state: HALTED ({state.halt_reason})")

    start_ts = now_ts()
    last_summary_ts = 0.0
    last_metrics_ts = 0.0
    last_alert_side = ""
    last_alert_ts = 0.0
    current_window: Optional[MarketWindow] = None
    spot = None
    spot_sources: Dict[str, float] = {}

    while True:
        if args.run_seconds > 0 and (now_ts() - start_ts) >= float(args.run_seconds):
            logger.info(f"[{iso_now_local()}] run-seconds reached; exiting.")
            _save_state(state_path, state)
            return 0

        try:
            ts_now = now_ts()
            state.consecutive_errors = 0

            spot, spot_sources = fetch_external_spot()
            if spot is None or not math.isfinite(spot) or spot <= 0:
                raise RuntimeError("external BTC spot unavailable")
            history.append((ts_now, float(spot)))

            window_start = (int(ts_now) // window_sec) * window_sec
            if (current_window is None) or (window_start != current_window.start_ts):
                prev_window = current_window
                prev_start = prev_window.start_ts if prev_window is not None else 0

                if prev_window is not None and state.active_position is not None:
                    close_px = None
                    _op, cl = fetch_coinbase_candle_open_close(prev_start, window_sec)
                    if cl is not None and math.isfinite(cl) and cl > 0:
                        close_px = cl
                    elif spot is not None:
                        close_px = float(spot)
                    if close_px is not None and math.isfinite(close_px) and close_px > 0:
                        settle_active_position(
                            state=state,
                            close_price=close_px,
                            settle_epsilon=float(args.settle_epsilon_usd),
                            taker_fee_rate=float(args.taker_fee_rate),
                            logger=logger,
                        )

                current_window = build_market_window(window_start, int(args.window_minutes))
                if current_window is None:
                    state.current_window_start_ts = window_start
                    state.current_window_slug = f"btc-updown-{int(args.window_minutes)}m-{window_start}"
                    state.current_window_open_price = 0.0
                    logger.info(
                        f"[{iso_now_local()}] window {state.current_window_slug}: market fetch failed; waiting."
                    )
                else:
                    op, _cl = fetch_coinbase_candle_open_close(window_start, window_sec)
                    if op is None or not math.isfinite(op) or op <= 0:
                        op = float(spot)
                    state.current_window_start_ts = current_window.start_ts
                    state.current_window_slug = current_window.slug
                    state.current_window_open_price = float(op)
                    logger.info(
                        f"[{iso_now_local()}] window {current_window.slug} | market={current_window.market_id} "
                        f"| open={state.current_window_open_price:.2f} | labels={current_window.up_label}/{current_window.down_label}"
                    )

                # Keep only recent window markers to avoid unbounded memory growth.
                for k in list(entered_windows.keys()):
                    if k < window_start - (3 * window_sec):
                        entered_windows.pop(k, None)

            up_ask = down_ask = up_bid = down_bid = math.nan
            fair_up = 0.5
            sigma = float(args.sigma_floor_per_sqrt_sec)
            remaining_sec = 0.0
            edge_up = edge_down = math.nan
            best_side = ""
            best_edge = -math.inf

            if current_window is not None and state.current_window_open_price > 0:
                up_book = fetch_clob_book(current_window.up_token_id)
                down_book = fetch_clob_book(current_window.down_token_id)
                up_ask = best_ask(up_book)
                down_ask = best_ask(down_book)
                up_bid = best_bid(up_book)
                down_bid = best_bid(down_book)
                remaining_sec = max(0.0, float(current_window.end_ts - ts_now))

                sigma = estimate_sigma_per_s(
                    history=history,
                    lookback_sec=float(args.vol_lookback_sec),
                    floor_sigma=float(args.sigma_floor_per_sqrt_sec),
                )
                fair_up = fair_up_probability(
                    spot=float(spot),
                    open_price=float(state.current_window_open_price),
                    remaining_sec=remaining_sec,
                    sigma_per_s=sigma,
                )

                fee = float(args.taker_fee_rate)
                if math.isfinite(up_ask) and up_ask > 0:
                    edge_up = fair_up - up_ask - (up_ask * fee)
                if math.isfinite(down_ask) and down_ask > 0:
                    p_down = 1.0 - fair_up
                    edge_down = p_down - down_ask - (down_ask * fee)

                if math.isfinite(edge_up) and edge_up > best_edge:
                    best_edge = edge_up
                    best_side = "UP"
                if math.isfinite(edge_down) and edge_down > best_edge:
                    best_edge = edge_down
                    best_side = "DOWN"

                alert_edge = float(args.alert_edge_cents) / 100.0
                if (
                    best_side
                    and math.isfinite(best_edge)
                    and best_edge >= alert_edge
                    and ((best_side != last_alert_side) or (ts_now - last_alert_ts) >= 5.0)
                ):
                    logger.info(
                        f"[{iso_now_local()}] signal side={best_side} edge={best_edge:+.4f} "
                        f"fair_up={fair_up:.4f} ask_up={up_ask:.4f} ask_down={down_ask:.4f} "
                        f"spot={spot:.2f} open={state.current_window_open_price:.2f} rem={remaining_sec:.1f}s"
                    )
                    last_alert_side = best_side
                    last_alert_ts = ts_now

                if args.daily_loss_limit_usd > 0 and not state.halted:
                    d = day_pnl_usd(state)
                    if d <= -float(args.daily_loss_limit_usd):
                        state.halted = True
                        state.halt_reason = (
                            f"Daily loss limit reached ({d:+.4f} <= -{float(args.daily_loss_limit_usd):.4f})"
                        )
                        logger.info(f"[{iso_now_local()}] HALT: {state.halt_reason}")

                if (
                    not state.halted
                    and state.active_position is None
                    and best_side
                    and math.isfinite(best_edge)
                    and best_edge >= (float(args.entry_edge_cents) / 100.0)
                    and remaining_sec >= float(args.min_remaining_sec)
                ):
                    if not (args.max_one_entry_per_window and entered_windows.get(current_window.start_ts, False)):
                        if best_side == "UP" and math.isfinite(up_ask) and up_ask > 0:
                            entry_px = up_ask
                        elif best_side == "DOWN" and math.isfinite(down_ask) and down_ask > 0:
                            entry_px = down_ask
                        else:
                            entry_px = math.nan
                        if math.isfinite(entry_px) and entry_px > 0:
                            if (entry_px < float(args.entry_price_min)) or (entry_px > float(args.entry_price_max)):
                                filter_skip_price_band += 1
                                continue
                            if args.require_reversal and not has_two_leg_reversal(
                                history=history,
                                side=best_side,
                                lookback_sec=float(args.reversal_lookback_sec),
                                min_move_usd=float(args.reversal_min_move_usd),
                            ):
                                filter_skip_reversal += 1
                                continue
                            state.active_position = {
                                "window_start_ts": current_window.start_ts,
                                "window_slug": current_window.slug,
                                "market_id": current_window.market_id,
                                "side": best_side,
                                "shares": float(args.shares),
                                "entry_price": float(entry_px),
                                "entry_ts": int(ts_now),
                                "fair_up_entry": float(fair_up),
                                "edge_entry": float(best_edge),
                                "window_open_price": float(state.current_window_open_price),
                            }
                            entered_windows[current_window.start_ts] = True
                            logger.info(
                                f"[{iso_now_local()}] paper ENTER side={best_side} shares={args.shares:.2f} "
                                f"entry={entry_px:.4f} edge={best_edge:+.4f} fair_up={fair_up:.4f} "
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
                d_pnl = day_pnl_usd(state)
                src = ",".join(f"{k}:{v:.2f}" for k, v in sorted(spot_sources.items()))
                logger.info(
                    f"[{iso_now_local()}] summary "
                    f"window={state.current_window_slug or '-'} rem={remaining_sec:.1f}s "
                    f"spot={float(spot):.2f} open={state.current_window_open_price:.2f} sigma={sigma:.6f} "
                    f"fair_up={fair_up:.4f} ask_up={up_ask:.4f} ask_dn={down_ask:.4f} "
                    f"edge_up={edge_up:+.4f} edge_dn={edge_down:+.4f} "
                    f"active={active_txt} day={d_pnl:+.4f} total={state.pnl_total_usd:+.4f} "
                    f"W/L/P={state.wins}/{state.losses}/{state.pushes} "
                    f"fskip_price={filter_skip_price_band} fskip_rev={filter_skip_reversal} "
                    f"src=[{src}] halted={state.halted}"
                )
                last_summary_ts = ts_now

            if args.metrics_sample_sec > 0 and (ts_now - last_metrics_ts) >= float(args.metrics_sample_sec):
                _append_metrics(
                    args.metrics_file,
                    {
                        "ts": iso_now_local(),
                        "ts_ms": int(ts_now * 1000.0),
                        "window_start_ts": int(state.current_window_start_ts or 0),
                        "window_slug": state.current_window_slug,
                        "spot_btc_usd": float(spot),
                        "window_open_btc_usd": float(state.current_window_open_price or 0.0),
                        "remaining_sec": remaining_sec,
                        "sigma_per_sqrt_sec": sigma,
                        "fair_up": fair_up,
                        "ask_up": up_ask,
                        "bid_up": up_bid,
                        "ask_down": down_ask,
                        "bid_down": down_bid,
                        "edge_up": edge_up,
                        "edge_down": edge_down,
                        "best_side": best_side,
                        "best_edge": best_edge if math.isfinite(best_edge) else None,
                        "active_position": state.active_position,
                        "pnl_total_usd": state.pnl_total_usd,
                        "day_pnl_usd": day_pnl_usd(state),
                        "trades_closed": state.trades_closed,
                        "wins": state.wins,
                        "losses": state.losses,
                        "pushes": state.pushes,
                        "filter_skip_price_band": int(filter_skip_price_band),
                        "filter_skip_reversal": int(filter_skip_reversal),
                        "halted": state.halted,
                    },
                )
                last_metrics_ts = ts_now

            _save_state(state_path, state)
            time.sleep(max(0.1, float(args.poll_sec)))

        except (HTTPError, URLError, TimeoutError):
            state.consecutive_errors += 1
            logger.info(f"[{iso_now_local()}] warn: network error")
            if state.consecutive_errors >= int(args.max_consecutive_errors):
                state.halted = True
                state.halt_reason = (
                    f"Consecutive errors cap reached ({state.consecutive_errors}/{args.max_consecutive_errors})"
                )
                logger.info(f"[{iso_now_local()}] HALT: {state.halt_reason}")
            _save_state(state_path, state)
            time.sleep(1.5)
        except Exception as e:
            state.consecutive_errors += 1
            logger.info(f"[{iso_now_local()}] error: loop exception: {e}")
            if state.consecutive_errors >= int(args.max_consecutive_errors):
                state.halted = True
                state.halt_reason = (
                    f"Consecutive errors cap reached ({state.consecutive_errors}/{args.max_consecutive_errors})"
                )
                logger.info(f"[{iso_now_local()}] HALT: {state.halt_reason}")
            _save_state(state_path, state)
            time.sleep(1.5)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
