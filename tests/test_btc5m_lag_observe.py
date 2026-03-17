"""Tests for polymarket_btc5m_lag_observe core functions."""
from __future__ import annotations

import math
import sys
from pathlib import Path

# Add scripts/ to path so we can import the module directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_btc5m_lag_observe as lag_mod


# --- fair_up_probability ---

def test_fair_up_at_the_money():
    """When spot == open, fair_up should be ~0.5."""
    p = lag_mod.fair_up_probability(
        spot=100.0,
        open_price=100.0,
        remaining_sec=60.0,
        sigma_per_s=0.001,
    )
    assert abs(p - 0.5) < 0.05


def test_fair_up_spot_above():
    """When spot > open, fair_up should be > 0.5."""
    p = lag_mod.fair_up_probability(
        spot=102.0,
        open_price=100.0,
        remaining_sec=60.0,
        sigma_per_s=0.001,
    )
    assert p > 0.5


def test_fair_up_spot_below():
    """When spot < open, fair_up should be < 0.5."""
    p = lag_mod.fair_up_probability(
        spot=98.0,
        open_price=100.0,
        remaining_sec=60.0,
        sigma_per_s=0.001,
    )
    assert p < 0.5


def test_fair_up_expired_above():
    """When remaining_sec=0 and spot > open, fair_up → ~1.0."""
    p = lag_mod.fair_up_probability(
        spot=101.0,
        open_price=100.0,
        remaining_sec=0.0,
        sigma_per_s=0.001,
    )
    assert p > 0.95


def test_fair_up_expired_below():
    """When remaining_sec=0 and spot < open, fair_up → ~0.0."""
    p = lag_mod.fair_up_probability(
        spot=99.0,
        open_price=100.0,
        remaining_sec=0.0,
        sigma_per_s=0.001,
    )
    assert p < 0.05


# --- estimate_sigma_per_s ---

def test_sigma_floor_with_few_points():
    """With too few data, sigma should return floor."""
    import time
    now = time.time()
    history = [(now - 1, 100.0), (now, 100.01)]
    sigma = lag_mod.estimate_sigma_per_s(
        history=history,
        lookback_sec=60.0,
        floor_sigma=0.005,
    )
    assert sigma >= 0.005


def test_sigma_with_volatile_data():
    """With significant moves, sigma should be above floor."""
    import time
    now = time.time()
    # Create history with significant price jumps
    history = []
    for i in range(100):
        t = now - 100 + i
        price = 100.0 + (1.0 if i % 2 == 0 else -1.0)
        history.append((t, price))
    sigma = lag_mod.estimate_sigma_per_s(
        history=history,
        lookback_sec=120.0,
        floor_sigma=0.0001,
    )
    assert sigma > 0.0001


# --- settle_active_position ---

def test_settle_win_with_fee():
    """Win: BTC went UP, bought UP side."""
    state = lag_mod.RuntimeState(day_key="2026-03-07")
    state.active_position = {
        "side": "UP",
        "shares": 25.0,
        "entry_price": 0.20,
        "window_open_price": 100.0,
        "slippage_cents": 0.0,
    }
    lag_mod.settle_active_position(
        state=state,
        close_price=101.0,
        settle_epsilon=0.5,
        taker_fee_rate=0.02,
        logger=type("L", (), {"info": lambda self, m: None})(),
    )
    assert state.active_position is None
    assert state.wins == 1
    # PnL = 25 * (1-0.20) - 25*0.20*0.02 = 20.0 - 0.1 = 19.9
    assert abs(state.pnl_total_usd - 19.9) < 0.01


def test_settle_loss_with_fee():
    """Loss: BTC went DOWN, bought UP side."""
    state = lag_mod.RuntimeState(day_key="2026-03-07")
    state.active_position = {
        "side": "UP",
        "shares": 25.0,
        "entry_price": 0.20,
        "window_open_price": 100.0,
        "slippage_cents": 0.0,
    }
    lag_mod.settle_active_position(
        state=state,
        close_price=99.0,
        settle_epsilon=0.5,
        taker_fee_rate=0.02,
        logger=type("L", (), {"info": lambda self, m: None})(),
    )
    assert state.active_position is None
    assert state.losses == 1
    # PnL = -(25 * 0.20) - 25*0.20*0.02 = -5.0 - 0.1 = -5.1
    assert abs(state.pnl_total_usd - (-5.1)) < 0.01


def test_settle_push():
    """Push: close price ~= open price."""
    state = lag_mod.RuntimeState(day_key="2026-03-07")
    state.active_position = {
        "side": "UP",
        "shares": 25.0,
        "entry_price": 0.50,
        "window_open_price": 100.0,
        "slippage_cents": 0.0,
    }
    lag_mod.settle_active_position(
        state=state,
        close_price=100.0,
        settle_epsilon=0.5,
        taker_fee_rate=0.02,
        logger=type("L", (), {"info": lambda self, m: None})(),
    )
    assert state.active_position is None
    assert state.pushes == 1


def test_settle_with_slippage():
    """Win with slippage cost deducted."""
    state = lag_mod.RuntimeState(day_key="2026-03-07")
    state.active_position = {
        "side": "UP",
        "shares": 25.0,
        "entry_price": 0.20,
        "window_open_price": 100.0,
        "slippage_cents": 0.5,
    }
    lag_mod.settle_active_position(
        state=state,
        close_price=101.0,
        settle_epsilon=0.5,
        taker_fee_rate=0.02,
        logger=type("L", (), {"info": lambda self, m: None})(),
    )
    # slippage_cost = 25 * 0.005 = 0.125
    # PnL = 20.0 - 0.1 - 0.125 = 19.775
    assert state.wins == 1
    assert abs(state.pnl_total_usd - 19.775) < 0.01


def test_drawdown_tracking():
    """max_drawdown_usd is updated on losses."""
    state = lag_mod.RuntimeState(day_key="2026-03-07")
    state.pnl_total_usd = 10.0
    state.peak_pnl_usd = 10.0
    state.active_position = {
        "side": "UP",
        "shares": 25.0,
        "entry_price": 0.20,
        "window_open_price": 100.0,
        "slippage_cents": 0.0,
    }
    lag_mod.settle_active_position(
        state=state,
        close_price=99.0,
        settle_epsilon=0.5,
        taker_fee_rate=0.02,
        logger=type("L", (), {"info": lambda self, m: None})(),
    )
    assert state.max_drawdown_usd > 0
    assert state.peak_pnl_usd == 10.0
