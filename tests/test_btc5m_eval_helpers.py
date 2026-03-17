"""Tests for btc5m_eval_helpers shared helpers."""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lib.btc5m_eval_helpers import (
    classify_entry_bucket,
    compute_fee_adjusted_pnl,
    compute_max_drawdown,
    compute_spread,
    compute_stale_gap_seconds,
    compute_trade_stats,
    is_book_deep_enough,
)


# --- compute_fee_adjusted_pnl ---

def test_pnl_win_no_fees():
    pnl = compute_fee_adjusted_pnl(0.05, 25.0, "WIN", 0.0, 0.0)
    assert abs(pnl - 23.75) < 0.001


def test_pnl_loss_no_fees():
    pnl = compute_fee_adjusted_pnl(0.05, 25.0, "LOSS", 0.0, 0.0)
    assert abs(pnl - (-1.25)) < 0.001


def test_pnl_win_with_fee():
    pnl = compute_fee_adjusted_pnl(0.05, 25.0, "WIN", 0.02, 0.0)
    gross = 25.0 * (1.0 - 0.05)
    fee = 25.0 * 0.05 * 0.02
    assert abs(pnl - (gross - fee)) < 0.001


def test_pnl_win_with_fee_and_slippage():
    pnl = compute_fee_adjusted_pnl(0.05, 25.0, "WIN", 0.02, 0.5)
    eff = 0.05 + 0.005  # 0.055
    gross = 25.0 * (1.0 - eff)
    fee = 25.0 * eff * 0.02
    assert abs(pnl - (gross - fee)) < 0.001


def test_pnl_push():
    pnl = compute_fee_adjusted_pnl(0.50, 10.0, "PUSH", 0.02, 0.5)
    assert abs(pnl) < 0.15  # near zero minus small fee


def test_pnl_invalid_price():
    assert compute_fee_adjusted_pnl(0.0, 25.0, "WIN", 0.02, 0.0) == 0.0
    assert compute_fee_adjusted_pnl(-1.0, 25.0, "WIN", 0.02, 0.0) == 0.0


# --- compute_max_drawdown ---

def test_max_drawdown_simple():
    series = [0.0, 10.0, 5.0, 8.0, 2.0, 6.0]
    dd = compute_max_drawdown(series)
    assert abs(dd - 8.0) < 0.001  # peak 10 -> trough 2


def test_max_drawdown_monotonic_up():
    dd = compute_max_drawdown([1.0, 2.0, 3.0, 4.0])
    assert dd == 0.0


def test_max_drawdown_single():
    assert compute_max_drawdown([5.0]) == 0.0


def test_max_drawdown_empty():
    assert compute_max_drawdown([]) == 0.0


# --- classify_entry_bucket ---

def test_bucket_low():
    assert classify_entry_bucket(0.05) == "0-10c"


def test_bucket_mid():
    assert classify_entry_bucket(0.55) == "50-60c"


def test_bucket_high():
    assert classify_entry_bucket(0.95) == "90-100c"


def test_bucket_invalid():
    assert classify_entry_bucket(-0.1) == "invalid"


# --- compute_spread ---

def test_spread_normal():
    s = compute_spread(0.10, 0.05)
    assert abs(s - 0.05) < 0.001


def test_spread_nan_ask():
    assert math.isnan(compute_spread(math.nan, 0.05))


def test_spread_zero_bid():
    assert math.isnan(compute_spread(0.10, 0.0))


# --- is_book_deep_enough ---

def test_deep_enough_yes():
    asks = [{"price": 0.05, "size": 15.0}]
    assert is_book_deep_enough(asks, 10.0) is True


def test_deep_enough_no():
    asks = [{"price": 0.05, "size": 5.0}]
    assert is_book_deep_enough(asks, 10.0) is False


def test_deep_enough_empty():
    assert is_book_deep_enough([], 10.0) is False
    assert is_book_deep_enough(None, 10.0) is False


# --- compute_stale_gap_seconds ---

def test_stale_gaps():
    ts = [1000, 2000, 3000, 100000, 101000]  # ms
    result = compute_stale_gap_seconds(ts)
    assert result["stale_count"] == 1  # 97s gap
    assert result["max_gap_sec"] > 90.0


def test_stale_no_gaps():
    ts = [1000, 2000, 3000]
    result = compute_stale_gap_seconds(ts)
    assert result["stale_count"] == 0


def test_stale_single():
    result = compute_stale_gap_seconds([1000])
    assert result["max_gap_sec"] == 0.0


# --- compute_trade_stats ---

def test_trade_stats_basic():
    trades = [
        {"entry_price": 0.05, "shares": 25.0, "outcome": "WIN", "side": "UP"},
        {"entry_price": 0.05, "shares": 25.0, "outcome": "LOSS", "side": "UP"},
        {"entry_price": 0.05, "shares": 25.0, "outcome": "LOSS", "side": "DOWN"},
    ]
    stats = compute_trade_stats(trades, taker_fee_rate=0.02, slippage_cents=0.0)
    assert stats["count"] == 3
    assert stats["wins"] == 1
    assert stats["losses"] == 2
    assert stats["win_rate"] > 0.3  # 1/3
    assert "UP" in stats["by_side"]
    assert "0-10c" in stats["by_bucket"]


def test_trade_stats_empty():
    stats = compute_trade_stats([])
    assert stats["count"] == 0
    assert stats["net_pnl"] == 0.0
