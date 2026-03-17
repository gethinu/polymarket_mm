#!/usr/bin/env python3
"""
Polymarket BTC 15m lag observer (simulation only).

Purpose:
- Observe Polymarket BTC 15m up/down pricing vs external BTC spot.
- Estimate a hybrid fair probability for "UP" using GBM base probability plus
  a short-horizon momentum adjustment.
- Emit mispricing signals and simulate one taker-style paper entry per window.

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
USER_AGENT = "btc15m-lag-observe/1.0"

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_FILE = str(DEFAULT_REPO_ROOT / "logs" / "btc15m-lag-observe.log")
DEFAULT_STATE_FILE = str(DEFAULT_REPO_ROOT / "logs" / "btc15m_lag_observe_state.json")
DEFAULT_METRICS_FILE = str(DEFAULT_REPO_ROOT / "logs" / "btc15m-lag-observe-metrics.jsonl")
ENTRY_START_BUFFER_SEC = 30.0
MIN_FAIR_PROB = 0.02
MAX_FAIR_PROB = 0.98


def default_runtime_paths(window_minutes: int) -> Tuple[str, str, str]:
    _ = int(window_minutes)
    return DEFAULT_LOG_FILE, DEFAULT_STATE_FILE, DEFAULT_METRICS_FILE


def normalize_side_mode(value: str) -> str:
    s = str(value or "").strip().lower()
    if s in {"down", "down-only", "short"}:
        return "down"
    if s in {"up", "up-only", "long"}:
        return "up"
    return "both"


def is_side_allowed(side: str, allowed_side_mode: str) -> bool:
    mode = normalize_side_mode(allowed_side_mode)
    side_norm = str(side or "").strip().upper()
    if side_norm not in {"UP", "DOWN"}:
        return False
    if mode == "both":
        return True
    if mode == "up":
        return side_norm == "UP"
    return side_norm == "DOWN"


def pick_best_side(
    edge_up: float,
    edge_down: float,
    allow_up: bool = True,
    allow_down: bool = True,
) -> Tuple[str, float]:
    best_side = ""
    best_edge = -math.inf
    if allow_up and math.isfinite(edge_up) and edge_up > best_edge:
        best_side = "UP"
        best_edge = float(edge_up)
    if allow_down and math.isfinite(edge_down) and edge_down > best_edge:
        best_side = "DOWN"
        best_edge = float(edge_down)
    if not best_side:
        return "", math.nan
    return best_side, best_edge


def candidate_block_reason(
    side: str,
    entry_time_allowed: bool,
    configured_allow_up: bool,
    configured_allow_down: bool,
    regime_allow_up: bool,
    regime_allow_down: bool,
) -> str:
    if not entry_time_allowed:
        return "time"
    side_norm = str(side or "").strip().upper()
    if (side_norm == "UP" and not regime_allow_up) or (side_norm == "DOWN" and not regime_allow_down):
        return "regime"
    if (side_norm == "UP" and not configured_allow_up) or (side_norm == "DOWN" and not configured_allow_down):
        return "side"
    return ""


def normalize_regime_mode(value: str) -> str:
    s = str(value or "").strip().lower()
    if s in {"strict", "hard"}:
        return "strict"
    if s in {"off", "none", "disable", "disabled"}:
        return "off"
    return "prefer"


def normalize_fair_model(value: str) -> str:
    s = str(value or "").strip().lower()
    if s in {"drift", "trend", "trend-follow", "continuation"}:
        return "drift"
    return "hybrid"


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


def _clip_symmetric(x: float, limit: float) -> float:
    lim = max(0.0, float(limit))
    if not math.isfinite(x):
        return 0.0
    return max(-lim, min(lim, float(x)))


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


def _recent_history_points(
    history: Deque[Tuple[float, float]],
    lookback_sec: float,
) -> List[Tuple[float, float]]:
    if lookback_sec <= 0:
        return []
    cutoff = now_ts() - float(lookback_sec)
    return [
        (float(t), float(p))
        for (t, p) in history
        if t >= cutoff and math.isfinite(t) and math.isfinite(p) and p > 0
    ]


def compute_momentum_adjustment(
    history: Deque[Tuple[float, float]],
    lookback_sec: float = 180.0,
    max_adjustment: float = 0.05,
    reference_move_pct: float = 0.01,
) -> float:
    pts = _recent_history_points(history=history, lookback_sec=lookback_sec)
    if len(pts) < 2:
        return 0.0

    start_ts, start_px = pts[0]
    end_ts, end_px = pts[-1]
    if not (start_px > 0 and end_px > 0):
        return 0.0

    dt = max(1.0, end_ts - start_ts)
    pct_change = (end_px - start_px) / start_px
    speed_scaled_move = pct_change * (float(lookback_sec) / dt)
    scale = max(1e-9, float(reference_move_pct))
    raw = (speed_scaled_move / scale) * float(max_adjustment)
    return max(-float(max_adjustment), min(float(max_adjustment), raw))


def compute_continuation_adjustment(
    history: Deque[Tuple[float, float]],
    spot: float,
    open_price: float,
    remaining_sec: float,
    window_sec: float,
    trend_lookback_sec: float = 300.0,
    max_adjustment: float = 0.10,
    trend_reference_move_pct: float = 0.0010,
    open_gap_reference_pct: float = 0.0015,
) -> float:
    if not (math.isfinite(spot) and spot > 0 and math.isfinite(open_price) and open_price > 0):
        return 0.0

    recent_ret = compute_trend_return_pct(history=history, lookback_sec=trend_lookback_sec)
    open_gap_pct = (float(spot) - float(open_price)) / float(open_price)
    rem_frac = _clamp01(float(remaining_sec) / max(1.0, float(window_sec)))
    late_weight = 1.0 - rem_frac

    trend_scale = max(1e-9, float(trend_reference_move_pct))
    gap_scale = max(1e-9, float(open_gap_reference_pct))
    trend_component = 0.0
    if math.isfinite(recent_ret):
        trend_component = _clip_symmetric(recent_ret / trend_scale, 1.0) * float(max_adjustment) * 0.60
    gap_component = _clip_symmetric(open_gap_pct / gap_scale, 1.0) * float(max_adjustment) * 0.40 * (
        0.25 + (0.75 * late_weight)
    )
    return _clip_symmetric(trend_component + gap_component, float(max_adjustment))


def compute_fair_up_components(
    spot: float,
    open_price: float,
    remaining_sec: float,
    sigma_per_s: float,
    history: Deque[Tuple[float, float]],
    fair_model: str = "hybrid",
    window_sec: float = 900.0,
    drift_trend_lookback_sec: float = 300.0,
    drift_max_adjustment: float = 0.10,
    drift_trend_reference_move_pct: float = 0.0010,
    drift_open_gap_reference_pct: float = 0.0015,
) -> Tuple[float, float, float, float]:
    base_prob = fair_up_probability(
        spot=spot,
        open_price=open_price,
        remaining_sec=remaining_sec,
        sigma_per_s=sigma_per_s,
    )
    momentum_adj = compute_momentum_adjustment(history=history, lookback_sec=180.0)
    continuation_adj = 0.0
    if normalize_fair_model(fair_model) == "drift":
        continuation_adj = compute_continuation_adjustment(
            history=history,
            spot=spot,
            open_price=open_price,
            remaining_sec=remaining_sec,
            window_sec=window_sec,
            trend_lookback_sec=drift_trend_lookback_sec,
            max_adjustment=drift_max_adjustment,
            trend_reference_move_pct=drift_trend_reference_move_pct,
            open_gap_reference_pct=drift_open_gap_reference_pct,
        )
    fair = max(MIN_FAIR_PROB, min(MAX_FAIR_PROB, base_prob + momentum_adj + continuation_adj))
    return base_prob, momentum_adj, continuation_adj, fair


def fair_up_probability_v2(
    spot: float,
    open_price: float,
    remaining_sec: float,
    sigma_per_s: float,
    history: Deque[Tuple[float, float]],
) -> float:
    _base, _mom, _cont, fair = compute_fair_up_components(
        spot=spot,
        open_price=open_price,
        remaining_sec=remaining_sec,
        sigma_per_s=sigma_per_s,
        history=history,
    )
    return fair


def is_entry_time_allowed(
    elapsed_sec: float,
    remaining_sec: float,
    start_buffer_sec: float = ENTRY_START_BUFFER_SEC,
    min_remaining_sec: float = 60.0,
    max_remaining_sec: float = 0.0,
) -> bool:
    if elapsed_sec < max(0.0, float(start_buffer_sec)):
        return False
    if remaining_sec < max(0.0, float(min_remaining_sec)):
        return False
    if max_remaining_sec > 0 and remaining_sec > float(max_remaining_sec):
        return False
    return True


def compute_trend_return_pct(
    history: Deque[Tuple[float, float]],
    lookback_sec: float,
) -> float:
    pts = _recent_history_points(history=history, lookback_sec=lookback_sec)
    if len(pts) < 2:
        return math.nan
    start_px = float(pts[0][1])
    end_px = float(pts[-1][1])
    if not (math.isfinite(start_px) and start_px > 0 and math.isfinite(end_px) and end_px > 0):
        return math.nan
    return (end_px - start_px) / start_px


def classify_trend_regime(
    history: Deque[Tuple[float, float]],
    short_lookback_sec: float = 1800.0,
    long_lookback_sec: float = 7200.0,
    short_threshold_pct: float = 0.0015,
    long_threshold_pct: float = 0.0030,
) -> Tuple[str, float, float]:
    short_ret = compute_trend_return_pct(history=history, lookback_sec=short_lookback_sec)
    long_ret = compute_trend_return_pct(history=history, lookback_sec=long_lookback_sec)
    short_thr = max(0.0, float(short_threshold_pct))
    long_thr = max(0.0, float(long_threshold_pct))

    if not math.isfinite(short_ret) or not math.isfinite(long_ret):
        return "FLAT", short_ret, long_ret
    if short_ret >= short_thr and long_ret >= long_thr:
        return "UP", short_ret, long_ret
    if short_ret <= -short_thr and long_ret <= -long_thr:
        return "DOWN", short_ret, long_ret
    if abs(short_ret) < short_thr and abs(long_ret) < long_thr:
        return "FLAT", short_ret, long_ret
    return "MIXED", short_ret, long_ret


def apply_regime_edge_adjustment(
    edge_up: float,
    edge_down: float,
    regime: str,
    regime_mode: str = "prefer",
    opposite_edge_penalty_cents: float = 4.0,
) -> Tuple[float, float, bool, bool]:
    mode = normalize_regime_mode(regime_mode)
    adjusted_up = float(edge_up) if math.isfinite(edge_up) else math.nan
    adjusted_down = float(edge_down) if math.isfinite(edge_down) else math.nan
    allow_up = True
    allow_down = True
    penalty = max(0.0, float(opposite_edge_penalty_cents)) / 100.0
    regime_norm = str(regime or "").strip().upper()

    if mode == "off":
        return adjusted_up, adjusted_down, allow_up, allow_down

    if regime_norm == "UP":
        if mode == "strict":
            allow_down = False
        elif math.isfinite(adjusted_down):
            adjusted_down -= penalty
    elif regime_norm == "DOWN":
        if mode == "strict":
            allow_up = False
        elif math.isfinite(adjusted_up):
            adjusted_up -= penalty
    elif regime_norm in {"FLAT", "MIXED"} and mode == "strict":
        allow_up = False
        allow_down = False

    return adjusted_up, adjusted_down, allow_up, allow_down


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


def normalize_optional_price_bound(value: float) -> float:
    try:
        x = float(value)
    except Exception:
        return -1.0
    if not math.isfinite(x) or x < 0:
        return -1.0
    return max(0.0, min(1.0, x))


def resolve_side_entry_price_band(
    side: str,
    base_min: float,
    base_max: float,
    up_min: float = -1.0,
    up_max: float = -1.0,
    down_min: float = -1.0,
    down_max: float = -1.0,
) -> Tuple[float, float]:
    min_px = max(0.0, min(1.0, float(base_min)))
    max_px = max(0.0, min(1.0, float(base_max)))
    side_norm = str(side or "").strip().upper()
    if side_norm == "UP":
        if float(up_min) >= 0:
            min_px = max(0.0, min(1.0, float(up_min)))
        if float(up_max) >= 0:
            max_px = max(0.0, min(1.0, float(up_max)))
    elif side_norm == "DOWN":
        if float(down_min) >= 0:
            min_px = max(0.0, min(1.0, float(down_min)))
        if float(down_max) >= 0:
            max_px = max(0.0, min(1.0, float(down_max)))
    if max_px < min_px:
        min_px, max_px = max_px, min_px
    return min_px, max_px


def has_aligned_entry_momentum(side: str, momentum_adj: float, continuation_adj: float) -> bool:
    side_norm = str(side or "").strip().upper()
    if side_norm == "UP":
        return (float(momentum_adj) > 0.0) and (float(continuation_adj) > 0.0)
    if side_norm == "DOWN":
        return (float(momentum_adj) < 0.0) and (float(continuation_adj) < 0.0)
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
    peak_pnl_usd: float = 0.0
    max_drawdown_usd: float = 0.0
    filter_skip_time: int = 0
    filter_skip_side: int = 0
    filter_skip_regime: int = 0
    filter_skip_price_band: int = 0
    filter_skip_momentum: int = 0
    filter_skip_reversal: int = 0
    filter_skip_spread: int = 0
    filter_skip_depth: int = 0
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
            peak_pnl_usd=float(raw.get("peak_pnl_usd") or 0.0),
            max_drawdown_usd=float(raw.get("max_drawdown_usd") or 0.0),
            filter_skip_time=int(raw.get("filter_skip_time") or 0),
            filter_skip_side=int(raw.get("filter_skip_side") or 0),
            filter_skip_regime=int(raw.get("filter_skip_regime") or 0),
            filter_skip_price_band=int(raw.get("filter_skip_price_band") or 0),
            filter_skip_momentum=int(raw.get("filter_skip_momentum") or 0),
            filter_skip_reversal=int(raw.get("filter_skip_reversal") or 0),
            filter_skip_spread=int(raw.get("filter_skip_spread") or 0),
            filter_skip_depth=int(raw.get("filter_skip_depth") or 0),
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
    slippage_cost = shares * max(0.0, as_float(pos.get("slippage_cents"), 0.0)) / 100.0
    pnl = gross - fee_cost - slippage_cost
    state.pnl_total_usd += pnl
    if state.pnl_total_usd > state.peak_pnl_usd:
        state.peak_pnl_usd = state.pnl_total_usd
    dd = state.peak_pnl_usd - state.pnl_total_usd
    if dd > state.max_drawdown_usd:
        state.max_drawdown_usd = dd
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


def settle_stale_active_position_if_needed(
    state: RuntimeState,
    current_window_start: int,
    window_sec: int,
    spot: Optional[float],
    settle_epsilon: float,
    taker_fee_rate: float,
    logger: Logger,
) -> bool:
    pos = state.active_position
    if not isinstance(pos, dict):
        return False

    pos_window_start = int(pos.get("window_start_ts") or 0)
    if pos_window_start <= 0 or pos_window_start >= int(current_window_start):
        return False

    close_px = None
    close_source = ""
    _op, candle_close = fetch_coinbase_candle_open_close(pos_window_start, window_sec)
    if candle_close is not None and math.isfinite(candle_close) and candle_close > 0:
        close_px = float(candle_close)
        close_source = "coinbase_candle"
    elif spot is not None and math.isfinite(spot) and spot > 0:
        close_px = float(spot)
        close_source = "spot_fallback"
    else:
        return False

    logger.info(
        f"[{iso_now_local()}] stale settle window={pos_window_start} "
        f"close_source={close_source} close={close_px:.2f}"
    )
    settle_active_position(
        state=state,
        close_price=close_px,
        settle_epsilon=settle_epsilon,
        taker_fee_rate=taker_fee_rate,
        logger=logger,
    )
    return True


def _apply_env_overrides(args):
    prefix = "BTC15MLAG_"
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


def _has_ask_depth(book: Optional[dict], min_size: float) -> bool:
    """Check if the book has at least min_size shares on ask side."""
    if not isinstance(book, dict):
        return False
    asks = book.get("asks")
    if not isinstance(asks, list) or not asks:
        return False
    total = 0.0
    for level in asks:
        if isinstance(level, dict):
            try:
                s = float(level.get("size", 0))
                if math.isfinite(s) and s > 0:
                    total += s
            except (TypeError, ValueError):
                continue
    return total >= min_size


def parse_args():
    p = argparse.ArgumentParser(description="Polymarket BTC 15m lag observer (simulation only)")
    p.add_argument(
        "--window-minutes",
        type=int,
        choices=[15],
        default=15,
        help="Target market window size in minutes (fixed at 15)",
    )
    p.add_argument("--poll-sec", type=float, default=1.0, help="Polling interval seconds")
    p.add_argument("--summary-every-sec", type=float, default=15.0, help="Summary cadence seconds (0=disabled)")
    p.add_argument("--metrics-sample-sec", type=float, default=5.0, help="Metrics cadence seconds (0=disabled)")
    p.add_argument("--run-seconds", type=int, default=0, help="Auto-exit after N seconds (0=run forever)")

    p.add_argument("--shares", type=float, default=25.0, help="Paper entry size in shares")
    p.add_argument("--entry-edge-cents", type=float, default=4.0, help="Paper entry threshold in cents")
    p.add_argument("--alert-edge-cents", type=float, default=0.8, help="Signal alert threshold in cents")
    p.add_argument(
        "--allowed-side-mode",
        choices=("both", "down", "up"),
        default="both",
        help="Restrict paper entries/signals to one side",
    )
    p.add_argument(
        "--regime-mode",
        choices=("prefer", "strict", "off"),
        default="prefer",
        help="Trend-regime side selection mode",
    )
    p.add_argument(
        "--regime-short-lookback-sec",
        type=float,
        default=1800.0,
        help="Short trend lookback for regime classification",
    )
    p.add_argument(
        "--regime-long-lookback-sec",
        type=float,
        default=7200.0,
        help="Long trend lookback for regime classification",
    )
    p.add_argument(
        "--regime-short-threshold-pct",
        type=float,
        default=0.0015,
        help="Short lookback return threshold for UP/DOWN regime",
    )
    p.add_argument(
        "--regime-long-threshold-pct",
        type=float,
        default=0.0030,
        help="Long lookback return threshold for UP/DOWN regime",
    )
    p.add_argument(
        "--regime-opposite-edge-penalty-cents",
        type=float,
        default=4.0,
        help="Penalty applied to the side opposite the detected regime in prefer mode",
    )
    p.add_argument(
        "--fair-model",
        choices=("hybrid", "drift"),
        default="hybrid",
        help="Fair-value model: legacy hybrid or drift-biased continuation model",
    )
    p.add_argument(
        "--drift-trend-lookback-sec",
        type=float,
        default=300.0,
        help="Trend lookback used by --fair-model drift",
    )
    p.add_argument(
        "--drift-max-adjustment",
        type=float,
        default=0.10,
        help="Max probability adjustment applied by --fair-model drift",
    )
    p.add_argument(
        "--drift-trend-reference-move-pct",
        type=float,
        default=0.0010,
        help="Trend return scale used by --fair-model drift",
    )
    p.add_argument(
        "--drift-open-gap-reference-pct",
        type=float,
        default=0.0015,
        help="Open-gap scale used by --fair-model drift",
    )
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
    p.add_argument("--up-entry-price-min", type=float, default=-1.0, help="Optional UP-only lower bound; negative=inherit global band")
    p.add_argument("--up-entry-price-max", type=float, default=-1.0, help="Optional UP-only upper bound; negative=inherit global band")
    p.add_argument("--down-entry-price-min", type=float, default=-1.0, help="Optional DOWN-only lower bound; negative=inherit global band")
    p.add_argument("--down-entry-price-max", type=float, default=-1.0, help="Optional DOWN-only upper bound; negative=inherit global band")
    p.add_argument(
        "--require-aligned-momentum",
        action="store_true",
        help="Require short momentum and drift continuation bias to align with entry side",
    )
    p.add_argument(
        "--require-reversal",
        action="store_true",
        help="Require a two-leg spot reversal pattern before paper entry",
    )
    p.add_argument(
        "--reversal-lookback-sec",
        type=float,
        default=180.0,
        help="Lookback window seconds for --require-reversal",
    )
    p.add_argument(
        "--reversal-min-move-usd",
        type=float,
        default=15.0,
        help="Minimum USD move per half-leg for reversal confirmation",
    )
    p.add_argument("--min-remaining-sec", type=float, default=60.0, help="Do not enter if less than this many sec remain")
    p.add_argument(
        "--max-remaining-sec",
        type=float,
        default=0.0,
        help="Optional upper bound for remaining seconds at entry (0=disabled)",
    )
    p.add_argument(
        "--no-max-one-entry-per-window",
        dest="max_one_entry_per_window",
        action="store_false",
        help="Allow multiple paper entries in one window",
    )
    p.set_defaults(max_one_entry_per_window=True)
    p.add_argument("--settle-epsilon-usd", type=float, default=0.5, help="Treat close-open within epsilon as PUSH")

    p.add_argument("--taker-fee-rate", type=float, default=0.02, help="Assumed taker fee rate (default 2%%)")
    p.add_argument("--slippage-cents", type=float, default=0.5, help="Conservative slippage per share in cents")
    p.add_argument("--max-spread-cents", type=float, default=6.0, help="Skip entry if spread exceeds this (cents)")
    p.add_argument("--min-ask-depth", type=float, default=15.0, help="Skip entry if ask depth < this shares")
    p.add_argument("--daily-loss-limit-usd", type=float, default=0.0, help="Halt new paper entries if day pnl <= -limit")
    p.add_argument("--vol-lookback-sec", type=float, default=1800.0, help="Spot return lookback for sigma estimate")
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
    args.window_minutes = 15
    args.allowed_side_mode = normalize_side_mode(str(args.allowed_side_mode))
    args.regime_mode = normalize_regime_mode(str(args.regime_mode))
    args.fair_model = normalize_fair_model(str(args.fair_model))
    args.entry_edge_cents = max(1.0, float(args.entry_edge_cents))
    args.taker_fee_rate = max(0.02, float(args.taker_fee_rate))
    args.slippage_cents = max(0.0, float(args.slippage_cents))
    args.entry_price_min = max(0.0, min(1.0, float(args.entry_price_min)))
    args.entry_price_max = max(0.0, min(1.0, float(args.entry_price_max)))
    if args.entry_price_max < args.entry_price_min:
        args.entry_price_min, args.entry_price_max = args.entry_price_max, args.entry_price_min
    args.up_entry_price_min = normalize_optional_price_bound(args.up_entry_price_min)
    args.up_entry_price_max = normalize_optional_price_bound(args.up_entry_price_max)
    if args.up_entry_price_min >= 0 and args.up_entry_price_max >= 0 and args.up_entry_price_max < args.up_entry_price_min:
        args.up_entry_price_min, args.up_entry_price_max = args.up_entry_price_max, args.up_entry_price_min
    args.down_entry_price_min = normalize_optional_price_bound(args.down_entry_price_min)
    args.down_entry_price_max = normalize_optional_price_bound(args.down_entry_price_max)
    if args.down_entry_price_min >= 0 and args.down_entry_price_max >= 0 and args.down_entry_price_max < args.down_entry_price_min:
        args.down_entry_price_min, args.down_entry_price_max = args.down_entry_price_max, args.down_entry_price_min
    args.reversal_lookback_sec = max(10.0, float(args.reversal_lookback_sec))
    args.reversal_min_move_usd = max(0.0, float(args.reversal_min_move_usd))
    args.max_remaining_sec = max(0.0, float(args.max_remaining_sec))
    args.regime_short_lookback_sec = max(60.0, float(args.regime_short_lookback_sec))
    args.regime_long_lookback_sec = max(args.regime_short_lookback_sec, float(args.regime_long_lookback_sec))
    args.regime_short_threshold_pct = max(0.0, float(args.regime_short_threshold_pct))
    args.regime_long_threshold_pct = max(0.0, float(args.regime_long_threshold_pct))
    args.regime_opposite_edge_penalty_cents = max(0.0, float(args.regime_opposite_edge_penalty_cents))
    args.drift_trend_lookback_sec = max(30.0, float(args.drift_trend_lookback_sec))
    args.drift_max_adjustment = max(0.0, min(0.25, float(args.drift_max_adjustment)))
    args.drift_trend_reference_move_pct = max(1e-6, float(args.drift_trend_reference_move_pct))
    args.drift_open_gap_reference_pct = max(1e-6, float(args.drift_open_gap_reference_pct))
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
    logger = Logger(args.log_file)
    state_path = Path(args.state_file)
    state = _load_state(state_path)
    history: Deque[Tuple[float, float]] = deque(maxlen=2400)
    entered_windows: Dict[int, bool] = {}

    logger.info("Polymarket BTC 15m Lag Observer")
    logger.info("=" * 64)
    logger.info("Mode: observe-only (no live orders)")
    logger.info(
        f"Config: window={int(args.window_minutes)}m poll={args.poll_sec:.2f}s summary={args.summary_every_sec:.1f}s "
        f"metrics={args.metrics_sample_sec:.1f}s shares={args.shares:.2f} "
        f"entry_edge={args.entry_edge_cents:.2f}c alert_edge={args.alert_edge_cents:.2f}c "
        f"taker_fee={args.taker_fee_rate:.4f} allowed_side={args.allowed_side_mode}"
    )
    logger.info(
        f"Entry filters: start_buffer={ENTRY_START_BUFFER_SEC:.0f}s "
        f"min_remaining={float(args.min_remaining_sec):.0f}s "
        f"max_remaining={float(args.max_remaining_sec):.0f}s "
        f"price_band=[{float(args.entry_price_min):.3f},{float(args.entry_price_max):.3f}] "
        f"reversal={'on' if args.require_reversal else 'off'} "
        f"aligned_momentum={'on' if args.require_aligned_momentum else 'off'} "
        f"lb={float(args.reversal_lookback_sec):.0f}s move={float(args.reversal_min_move_usd):.1f} "
        f"max_spread={float(args.max_spread_cents):.1f}c min_ask_depth={float(args.min_ask_depth):.1f}"
    )
    logger.info(
        f"Fair model: {args.fair_model} drift_lb={float(args.drift_trend_lookback_sec):.0f}s "
        f"drift_max={float(args.drift_max_adjustment):.3f} "
        f"trend_ref={float(args.drift_trend_reference_move_pct):.4f} "
        f"gap_ref={float(args.drift_open_gap_reference_pct):.4f}"
    )
    logger.info(
        f"Side price bands: up=["
        f"{(float(args.up_entry_price_min) if float(args.up_entry_price_min) >= 0 else float(args.entry_price_min)):.3f},"
        f"{(float(args.up_entry_price_max) if float(args.up_entry_price_max) >= 0 else float(args.entry_price_max)):.3f}] "
        f"down=["
        f"{(float(args.down_entry_price_min) if float(args.down_entry_price_min) >= 0 else float(args.entry_price_min)):.3f},"
        f"{(float(args.down_entry_price_max) if float(args.down_entry_price_max) >= 0 else float(args.entry_price_max)):.3f}]"
    )
    logger.info(
        f"Regime: mode={args.regime_mode} short_lb={args.regime_short_lookback_sec:.0f}s "
        f"long_lb={args.regime_long_lookback_sec:.0f}s short_thr={args.regime_short_threshold_pct:.4f} "
        f"long_thr={args.regime_long_threshold_pct:.4f} opp_penalty={args.regime_opposite_edge_penalty_cents:.1f}c"
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
            settle_stale_active_position_if_needed(
                state=state,
                current_window_start=window_start,
                window_sec=window_sec,
                spot=spot,
                settle_epsilon=float(args.settle_epsilon_usd),
                taker_fee_rate=float(args.taker_fee_rate),
                logger=logger,
            )
            if (current_window is None) or (window_start != current_window.start_ts):
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
            base_fair_up = 0.5
            fair_up = 0.5
            momentum_adj = 0.0
            continuation_adj = 0.0
            sigma = float(args.sigma_floor_per_sqrt_sec)
            elapsed_sec = 0.0
            remaining_sec = 0.0
            entry_time_allowed = False
            regime = "FLAT"
            short_trend_return_pct = math.nan
            long_trend_return_pct = math.nan
            edge_up = edge_down = math.nan
            adjusted_edge_up = adjusted_edge_down = math.nan
            raw_best_side = ""
            raw_best_edge = math.nan
            best_side = ""
            best_edge = math.nan
            regime_allow_up = True
            regime_allow_down = True
            configured_allow_up = is_side_allowed("UP", args.allowed_side_mode)
            configured_allow_down = is_side_allowed("DOWN", args.allowed_side_mode)
            best_side_allowed = False

            if current_window is not None and state.current_window_open_price > 0:
                up_book = fetch_clob_book(current_window.up_token_id)
                down_book = fetch_clob_book(current_window.down_token_id)
                up_ask = best_ask(up_book)
                down_ask = best_ask(down_book)
                up_bid = best_bid(up_book)
                down_bid = best_bid(down_book)
                remaining_sec = max(0.0, float(current_window.end_ts - ts_now))
                elapsed_sec = max(0.0, float(ts_now - current_window.start_ts))
                entry_time_allowed = is_entry_time_allowed(
                    elapsed_sec=elapsed_sec,
                    remaining_sec=remaining_sec,
                    min_remaining_sec=float(args.min_remaining_sec),
                    max_remaining_sec=float(args.max_remaining_sec),
                )

                sigma = estimate_sigma_per_s(
                    history=history,
                    lookback_sec=float(args.vol_lookback_sec),
                    floor_sigma=float(args.sigma_floor_per_sqrt_sec),
                )
                (
                    base_fair_up,
                    momentum_adj,
                    continuation_adj,
                    fair_up,
                ) = compute_fair_up_components(
                    spot=float(spot),
                    open_price=float(state.current_window_open_price),
                    remaining_sec=remaining_sec,
                    sigma_per_s=sigma,
                    history=history,
                    fair_model=args.fair_model,
                    window_sec=float(window_sec),
                    drift_trend_lookback_sec=float(args.drift_trend_lookback_sec),
                    drift_max_adjustment=float(args.drift_max_adjustment),
                    drift_trend_reference_move_pct=float(args.drift_trend_reference_move_pct),
                    drift_open_gap_reference_pct=float(args.drift_open_gap_reference_pct),
                )

                fee = float(args.taker_fee_rate)
                if math.isfinite(up_ask) and up_ask > 0:
                    edge_up = fair_up - up_ask - (up_ask * fee)
                if math.isfinite(down_ask) and down_ask > 0:
                    p_down = 1.0 - fair_up
                    edge_down = p_down - down_ask - (down_ask * fee)

                regime, short_trend_return_pct, long_trend_return_pct = classify_trend_regime(
                    history=history,
                    short_lookback_sec=float(args.regime_short_lookback_sec),
                    long_lookback_sec=float(args.regime_long_lookback_sec),
                    short_threshold_pct=float(args.regime_short_threshold_pct),
                    long_threshold_pct=float(args.regime_long_threshold_pct),
                )
                (
                    adjusted_edge_up,
                    adjusted_edge_down,
                    regime_allow_up,
                    regime_allow_down,
                ) = apply_regime_edge_adjustment(
                    edge_up=edge_up,
                    edge_down=edge_down,
                    regime=regime,
                    regime_mode=args.regime_mode,
                    opposite_edge_penalty_cents=float(args.regime_opposite_edge_penalty_cents),
                )
                raw_best_side, raw_best_edge = pick_best_side(
                    edge_up=adjusted_edge_up,
                    edge_down=adjusted_edge_down,
                )
                best_side, best_edge = pick_best_side(
                    edge_up=adjusted_edge_up,
                    edge_down=adjusted_edge_down,
                    allow_up=(configured_allow_up and regime_allow_up),
                    allow_down=(configured_allow_down and regime_allow_down),
                )
                best_side_allowed = bool(best_side)

                alert_edge = float(args.alert_edge_cents) / 100.0
                if (
                    best_side
                    and best_side_allowed
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

                entry_edge_threshold = float(args.entry_edge_cents) / 100.0
                candidate_side = ""
                candidate_edge = math.nan
                if best_side and math.isfinite(best_edge) and best_edge >= entry_edge_threshold:
                    candidate_side = best_side
                    candidate_edge = best_edge
                elif raw_best_side and math.isfinite(raw_best_edge) and raw_best_edge >= entry_edge_threshold:
                    candidate_side = raw_best_side
                    candidate_edge = raw_best_edge

                if (
                    not state.halted
                    and state.active_position is None
                    and candidate_side
                    and math.isfinite(candidate_edge)
                ):
                    if not (args.max_one_entry_per_window and entered_windows.get(current_window.start_ts, False)):
                        block_reason = candidate_block_reason(
                            side=candidate_side,
                            entry_time_allowed=entry_time_allowed,
                            configured_allow_up=configured_allow_up,
                            configured_allow_down=configured_allow_down,
                            regime_allow_up=regime_allow_up,
                            regime_allow_down=regime_allow_down,
                        )
                        if block_reason == "time":
                            state.filter_skip_time += 1
                        elif block_reason == "regime":
                            state.filter_skip_regime += 1
                        elif block_reason == "side":
                            state.filter_skip_side += 1
                        else:
                            if candidate_side == "UP" and math.isfinite(up_ask) and up_ask > 0:
                                entry_px = up_ask
                            elif candidate_side == "DOWN" and math.isfinite(down_ask) and down_ask > 0:
                                entry_px = down_ask
                            else:
                                entry_px = math.nan
                            if math.isfinite(entry_px) and entry_px > 0:
                                side_min_px, side_max_px = resolve_side_entry_price_band(
                                    side=candidate_side,
                                    base_min=float(args.entry_price_min),
                                    base_max=float(args.entry_price_max),
                                    up_min=float(args.up_entry_price_min),
                                    up_max=float(args.up_entry_price_max),
                                    down_min=float(args.down_entry_price_min),
                                    down_max=float(args.down_entry_price_max),
                                )
                                if (entry_px < side_min_px) or (entry_px > side_max_px):
                                    state.filter_skip_price_band += 1
                                elif args.require_aligned_momentum and not has_aligned_entry_momentum(
                                    side=candidate_side,
                                    momentum_adj=momentum_adj,
                                    continuation_adj=continuation_adj,
                                ):
                                    state.filter_skip_momentum += 1
                                elif args.require_reversal and not has_two_leg_reversal(
                                    history=history,
                                    side=candidate_side,
                                    lookback_sec=float(args.reversal_lookback_sec),
                                    min_move_usd=float(args.reversal_min_move_usd),
                                ):
                                    state.filter_skip_reversal += 1
                                else:
                                    chosen_book = up_book if candidate_side == "UP" else down_book
                                    chosen_bid = best_bid(chosen_book)
                                    spread_val = (entry_px - chosen_bid) if (math.isfinite(chosen_bid) and chosen_bid > 0) else math.nan
                                    if math.isfinite(spread_val) and spread_val > (float(args.max_spread_cents) / 100.0):
                                        state.filter_skip_spread += 1
                                    elif not _has_ask_depth(chosen_book, float(args.min_ask_depth)):
                                        state.filter_skip_depth += 1
                                    else:
                                        state.active_position = {
                                            "window_start_ts": current_window.start_ts,
                                            "window_slug": current_window.slug,
                                            "market_id": current_window.market_id,
                                            "side": candidate_side,
                                            "shares": float(args.shares),
                                            "entry_price": float(entry_px),
                                            "entry_ts": int(ts_now),
                                            "fair_up_entry": float(fair_up),
                                            "base_fair_up_entry": float(base_fair_up),
                                            "momentum_adjustment": float(momentum_adj),
                                            "continuation_adjustment": float(continuation_adj),
                                            "fair_model": str(args.fair_model),
                                            "raw_edge_entry": float(edge_up if candidate_side == "UP" else edge_down),
                                            "adjusted_edge_entry": float(candidate_edge),
                                            "regime_at_entry": regime,
                                            "short_trend_return_pct": float(short_trend_return_pct),
                                            "long_trend_return_pct": float(long_trend_return_pct),
                                            "window_open_price": float(state.current_window_open_price),
                                            "slippage_cents": float(args.slippage_cents),
                                        }
                                        entered_windows[current_window.start_ts] = True
                                        logger.info(
                                            f"[{iso_now_local()}] paper ENTER side={candidate_side} shares={args.shares:.2f} "
                                            f"entry={entry_px:.4f} edge_raw={(edge_up if candidate_side == 'UP' else edge_down):+.4f} "
                                            f"edge_adj={candidate_edge:+.4f} regime={regime} "
                                            f"trend30={short_trend_return_pct:+.4%} trend120={long_trend_return_pct:+.4%} fair_up={fair_up:.4f} "
                                            f"base={base_fair_up:.4f} mom={momentum_adj:+.4f} drift={continuation_adj:+.4f} "
                                            f"rem={remaining_sec:.1f}s elapsed={elapsed_sec:.1f}s window={current_window.slug}"
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
                    f"window={state.current_window_slug or '-'} rem={remaining_sec:.1f}s elapsed={elapsed_sec:.1f}s "
                    f"spot={float(spot):.2f} open={state.current_window_open_price:.2f} sigma={sigma:.6f} "
                    f"regime={regime} trend30={short_trend_return_pct:+.4%} trend120={long_trend_return_pct:+.4%} "
                    f"base_up={base_fair_up:.4f} mom={momentum_adj:+.4f} drift={continuation_adj:+.4f} fair_up={fair_up:.4f} "
                    f"ask_up={up_ask:.4f} ask_dn={down_ask:.4f} "
                    f"edge_up={edge_up:+.4f}/{adjusted_edge_up:+.4f} edge_dn={edge_down:+.4f}/{adjusted_edge_down:+.4f} "
                    f"raw_best={raw_best_side or '-'}:{(raw_best_edge if math.isfinite(raw_best_edge) else math.nan):+.4f} "
                    f"best={best_side or '-'}:{(best_edge if math.isfinite(best_edge) else math.nan):+.4f} "
                    f"active={active_txt} day={d_pnl:+.4f} total={state.pnl_total_usd:+.4f} "
                    f"W/L/P={state.wins}/{state.losses}/{state.pushes} "
                    f"dd={state.max_drawdown_usd:.4f} "
                    f"entry_time_allowed={entry_time_allowed} "
                    f"fskip_time={state.filter_skip_time} "
                    f"fskip_side={state.filter_skip_side} "
                    f"fskip_regime={state.filter_skip_regime} "
                    f"fskip_price={state.filter_skip_price_band} fskip_mom={state.filter_skip_momentum} "
                    f"fskip_rev={state.filter_skip_reversal} "
                    f"fskip_spread={state.filter_skip_spread} fskip_depth={state.filter_skip_depth} "
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
                        "elapsed_sec": elapsed_sec,
                        "remaining_sec": remaining_sec,
                        "sigma_per_sqrt_sec": sigma,
                        "regime": regime,
                        "regime_mode": args.regime_mode,
                        "fair_model": args.fair_model,
                        "short_trend_return_pct": short_trend_return_pct,
                        "long_trend_return_pct": long_trend_return_pct,
                        "base_fair_up": base_fair_up,
                        "momentum_adjustment": momentum_adj,
                        "continuation_adjustment": continuation_adj,
                        "fair_up": fair_up,
                        "ask_up": up_ask,
                        "bid_up": up_bid,
                        "ask_down": down_ask,
                        "bid_down": down_bid,
                        "edge_up": edge_up,
                        "edge_down": edge_down,
                        "adjusted_edge_up": adjusted_edge_up,
                        "adjusted_edge_down": adjusted_edge_down,
                        "raw_best_side": raw_best_side,
                        "raw_best_edge": raw_best_edge if math.isfinite(raw_best_edge) else None,
                        "best_side": best_side,
                        "best_edge": best_edge if math.isfinite(best_edge) else None,
                        "active_position": state.active_position,
                        "pnl_total_usd": state.pnl_total_usd,
                        "day_pnl_usd": day_pnl_usd(state),
                        "trades_closed": state.trades_closed,
                        "wins": state.wins,
                        "losses": state.losses,
                        "pushes": state.pushes,
                        "allowed_side_mode": args.allowed_side_mode,
                        "entry_price_min": float(args.entry_price_min),
                        "entry_price_max": float(args.entry_price_max),
                        "up_entry_price_min": (float(args.up_entry_price_min) if float(args.up_entry_price_min) >= 0 else None),
                        "up_entry_price_max": (float(args.up_entry_price_max) if float(args.up_entry_price_max) >= 0 else None),
                        "down_entry_price_min": (float(args.down_entry_price_min) if float(args.down_entry_price_min) >= 0 else None),
                        "down_entry_price_max": (float(args.down_entry_price_max) if float(args.down_entry_price_max) >= 0 else None),
                        "require_aligned_momentum": bool(args.require_aligned_momentum),
                        "require_reversal": bool(args.require_reversal),
                        "reversal_lookback_sec": float(args.reversal_lookback_sec),
                        "reversal_min_move_usd": float(args.reversal_min_move_usd),
                        "best_side_allowed": best_side_allowed,
                        "regime_allow_up": regime_allow_up,
                        "regime_allow_down": regime_allow_down,
                        "entry_time_allowed": entry_time_allowed,
                        "filter_skip_time": int(state.filter_skip_time),
                        "filter_skip_side": int(state.filter_skip_side),
                        "filter_skip_regime": int(state.filter_skip_regime),
                        "filter_skip_price_band": int(state.filter_skip_price_band),
                        "filter_skip_momentum": int(state.filter_skip_momentum),
                        "filter_skip_reversal": int(state.filter_skip_reversal),
                        "filter_skip_spread": int(state.filter_skip_spread),
                        "filter_skip_depth": int(state.filter_skip_depth),
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
