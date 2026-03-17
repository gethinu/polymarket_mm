"""
Shared testable helpers for BTC short-window strategy evaluation.

All functions are pure (no I/O, no network) so they can be unit-tested easily.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional


def compute_fee_adjusted_pnl(
    entry_price: float,
    shares: float,
    outcome: str,
    taker_fee_rate: float = 0.02,
    slippage_cents: float = 0.5,
) -> float:
    """Compute PnL for a single binary-option paper trade with fees + slippage.

    Args:
        entry_price: price paid per share (0-1 scale)
        shares: number of shares
        outcome: "WIN", "LOSS", or "PUSH"
        taker_fee_rate: fractional taker fee (e.g. 0.02 for 2%)
        slippage_cents: additional cost in cents per share

    Returns:
        Net PnL in USD after fees and slippage.
    """
    if not math.isfinite(entry_price) or entry_price <= 0:
        return 0.0
    if not math.isfinite(shares) or shares <= 0:
        return 0.0

    slip = max(0.0, slippage_cents) / 100.0
    effective_entry = min(entry_price + slip, 0.99)
    entry_notional = shares * effective_entry
    fee_cost = entry_notional * max(0.0, taker_fee_rate)

    outcome_upper = (outcome or "").strip().upper()
    if outcome_upper == "WIN":
        gross = shares * (1.0 - effective_entry)
    elif outcome_upper == "LOSS":
        gross = -shares * effective_entry
    else:  # PUSH
        gross = 0.0

    return gross - fee_cost


def compute_max_drawdown(pnl_series: List[float]) -> float:
    """Compute maximum peak-to-trough drawdown from a PnL time series.

    Args:
        pnl_series: list of cumulative PnL values over time

    Returns:
        Maximum drawdown as a positive number (0.0 if no drawdown).
    """
    if len(pnl_series) < 2:
        return 0.0

    peak = pnl_series[0]
    max_dd = 0.0
    for v in pnl_series:
        if not math.isfinite(v):
            continue
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    return max_dd


def classify_entry_bucket(price: float, bucket_width: float = 0.10) -> str:
    """Classify an entry price into a human-readable bucket string.

    Args:
        price: entry price (0-1 scale)
        bucket_width: width of each bucket (default 0.10 = 10c)

    Returns:
        Bucket label like "0-10c", "10-20c", etc.
    """
    if not math.isfinite(price) or price < 0:
        return "invalid"
    if bucket_width <= 0:
        bucket_width = 0.10
    bucket_idx = int(price / bucket_width)
    lo = int(bucket_idx * bucket_width * 100)
    hi = int((bucket_idx + 1) * bucket_width * 100)
    return f"{lo}-{hi}c"


def compute_spread(best_ask: float, best_bid: float) -> float:
    """Compute bid-ask spread.

    Returns NaN if either side is missing/invalid.
    """
    if (
        not math.isfinite(best_ask)
        or best_ask <= 0
        or not math.isfinite(best_bid)
        or best_bid <= 0
    ):
        return math.nan
    return best_ask - best_bid


def is_book_deep_enough(
    asks: Optional[list],
    min_size: float = 10.0,
) -> bool:
    """Check whether the best ask has at least min_size shares available.

    Args:
        asks: list of ask levels [{"price": ..., "size": ...}, ...]
        min_size: minimum aggregate size required at best ask
    """
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


def compute_stale_gap_seconds(timestamps_ms: List[int]) -> Dict[str, float]:
    """Analyse gaps between consecutive metrics timestamps.

    Args:
        timestamps_ms: list of ts_ms values from metrics JSONL

    Returns:
        dict with max_gap_sec, mean_gap_sec, stale_count (gaps > 60s)
    """
    if len(timestamps_ms) < 2:
        return {"max_gap_sec": 0.0, "mean_gap_sec": 0.0, "stale_count": 0}

    sorted_ts = sorted(timestamps_ms)
    gaps = []
    stale_count = 0
    for i in range(1, len(sorted_ts)):
        gap_sec = (sorted_ts[i] - sorted_ts[i - 1]) / 1000.0
        if math.isfinite(gap_sec) and gap_sec >= 0:
            gaps.append(gap_sec)
            if gap_sec > 60.0:
                stale_count += 1

    if not gaps:
        return {"max_gap_sec": 0.0, "mean_gap_sec": 0.0, "stale_count": 0}

    return {
        "max_gap_sec": max(gaps),
        "mean_gap_sec": sum(gaps) / len(gaps),
        "stale_count": stale_count,
    }


def compute_trade_stats(
    trades: List[dict],
    taker_fee_rate: float = 0.02,
    slippage_cents: float = 0.5,
) -> dict:
    """Compute aggregate statistics from a list of trade dicts.

    Each trade dict should have: entry_price, shares, outcome, side.

    Returns a dict with comprehensive stats.
    """
    if not trades:
        return {
            "count": 0,
            "wins": 0,
            "losses": 0,
            "pushes": 0,
            "win_rate": 0.0,
            "gross_pnl": 0.0,
            "net_pnl": 0.0,
            "avg_pnl_per_trade": 0.0,
            "max_drawdown": 0.0,
            "by_side": {},
            "by_bucket": {},
        }

    wins = losses = pushes = 0
    gross_total = 0.0
    net_total = 0.0
    cumulative_pnl: List[float] = [0.0]
    by_side: Dict[str, dict] = {}
    by_bucket: Dict[str, dict] = {}

    for t in trades:
        entry_price = float(t.get("entry_price", 0))
        shares = float(t.get("shares", 0))
        outcome = str(t.get("outcome", "PUSH")).upper()
        side = str(t.get("side", "?")).upper()

        # Gross (no fees)
        gross = compute_fee_adjusted_pnl(entry_price, shares, outcome, 0.0, 0.0)
        # Net (with fees + slippage)
        net = compute_fee_adjusted_pnl(entry_price, shares, outcome, taker_fee_rate, slippage_cents)

        gross_total += gross
        net_total += net
        cumulative_pnl.append(cumulative_pnl[-1] + net)

        if outcome == "WIN":
            wins += 1
        elif outcome == "LOSS":
            losses += 1
        else:
            pushes += 1

        # by side
        if side not in by_side:
            by_side[side] = {"count": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
        by_side[side]["count"] += 1
        if outcome == "WIN":
            by_side[side]["wins"] += 1
        elif outcome == "LOSS":
            by_side[side]["losses"] += 1
        by_side[side]["net_pnl"] += net

        # by bucket
        bucket = classify_entry_bucket(entry_price)
        if bucket not in by_bucket:
            by_bucket[bucket] = {"count": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
        by_bucket[bucket]["count"] += 1
        if outcome == "WIN":
            by_bucket[bucket]["wins"] += 1
        elif outcome == "LOSS":
            by_bucket[bucket]["losses"] += 1
        by_bucket[bucket]["net_pnl"] += net

    count = wins + losses + pushes
    win_rate = wins / max(1, wins + losses) if (wins + losses) > 0 else 0.0

    # Side win rates
    for side_data in by_side.values():
        sw = side_data["wins"]
        sl = side_data["losses"]
        side_data["win_rate"] = sw / max(1, sw + sl) if (sw + sl) > 0 else 0.0

    # Bucket win rates
    for bucket_data in by_bucket.values():
        bw = bucket_data["wins"]
        bl = bucket_data["losses"]
        bucket_data["win_rate"] = bw / max(1, bw + bl) if (bw + bl) > 0 else 0.0

    return {
        "count": count,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate": win_rate,
        "gross_pnl": gross_total,
        "net_pnl": net_total,
        "avg_pnl_per_trade": net_total / max(1, count),
        "max_drawdown": compute_max_drawdown(cumulative_pnl),
        "by_side": by_side,
        "by_bucket": by_bucket,
    }
