"""Tests for polymarket_btc15m_lag_observe core functions."""
from __future__ import annotations

import math
import sys
import time
from collections import deque
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_btc15m_lag_observe as lag15_mod


def test_parse_args_defaults_match_15m_design(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", ["polymarket_btc15m_lag_observe.py"])
    args = lag15_mod.parse_args()
    assert args.window_minutes == 15
    assert args.vol_lookback_sec == 1800.0
    assert args.entry_edge_cents == 4.0
    assert args.taker_fee_rate == 0.02
    assert args.slippage_cents == 0.5
    assert args.min_remaining_sec == 60.0
    assert args.max_remaining_sec == 0.0
    assert args.max_spread_cents == 6.0
    assert args.min_ask_depth == 15.0
    assert args.allowed_side_mode == "both"
    assert args.regime_mode == "prefer"
    assert args.fair_model == "hybrid"
    assert args.entry_price_min == 0.0
    assert args.entry_price_max == 1.0
    assert args.up_entry_price_max == -1.0
    assert args.down_entry_price_max == -1.0
    assert args.require_aligned_momentum is False
    assert args.require_reversal is False
    assert args.regime_short_lookback_sec == 1800.0
    assert args.regime_long_lookback_sec == 7200.0
    assert args.log_file.endswith("logs\\btc15m-lag-observe.log") or args.log_file.endswith("logs/btc15m-lag-observe.log")


def test_parse_args_clamps_entry_edge_floor(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["polymarket_btc15m_lag_observe.py", "--entry-edge-cents", "0.5"],
    )
    args = lag15_mod.parse_args()
    assert args.entry_edge_cents == 1.0


def test_parse_args_normalizes_down_only_side_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["polymarket_btc15m_lag_observe.py", "--allowed-side-mode", "down"],
    )
    args = lag15_mod.parse_args()
    assert args.allowed_side_mode == "down"


def test_parse_args_normalizes_price_band_and_remaining_window(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "polymarket_btc15m_lag_observe.py",
            "--entry-price-min",
            "0.40",
            "--entry-price-max",
            "0.10",
            "--max-remaining-sec",
            "-1",
        ],
    )
    args = lag15_mod.parse_args()
    assert args.entry_price_min == 0.10
    assert args.entry_price_max == 0.40
    assert args.max_remaining_sec == 0.0


def test_is_side_allowed_matches_side_mode():
    assert lag15_mod.is_side_allowed("DOWN", "down") is True
    assert lag15_mod.is_side_allowed("UP", "down") is False
    assert lag15_mod.is_side_allowed("UP", "up") is True
    assert lag15_mod.is_side_allowed("DOWN", "both") is True


def test_compute_trend_return_pct_positive():
    now = time.time()
    history = deque(
        [
            (now - 7200.0, 100.0),
            (now - 1800.0, 101.0),
            (now, 102.0),
        ]
    )
    ret = lag15_mod.compute_trend_return_pct(history=history, lookback_sec=7200.0)
    assert math.isclose(ret, 0.02, rel_tol=0.0, abs_tol=1e-6)


def test_classify_trend_regime_down():
    now = time.time()
    history = deque(
        [
            (now - 7200.0, 100.0),
            (now - 1800.0, 99.6),
            (now, 99.4),
        ]
    )
    regime, short_ret, long_ret = lag15_mod.classify_trend_regime(
        history=history,
        short_lookback_sec=1800.0,
        long_lookback_sec=7200.0,
        short_threshold_pct=0.0015,
        long_threshold_pct=0.0030,
    )
    assert regime == "DOWN"
    assert short_ret < 0
    assert long_ret < 0


def test_classify_trend_regime_mixed():
    now = time.time()
    history = deque(
        [
            (now - 7200.0, 100.0),
            (now - 1800.0, 101.0),
            (now, 100.7),
        ]
    )
    regime, _, _ = lag15_mod.classify_trend_regime(
        history=history,
        short_lookback_sec=1800.0,
        long_lookback_sec=7200.0,
        short_threshold_pct=0.0015,
        long_threshold_pct=0.0030,
    )
    assert regime == "MIXED"


def test_apply_regime_edge_adjustment_prefer_penalizes_opposite():
    adj_up, adj_down, allow_up, allow_down = lag15_mod.apply_regime_edge_adjustment(
        edge_up=0.06,
        edge_down=0.04,
        regime="DOWN",
        regime_mode="prefer",
        opposite_edge_penalty_cents=4.0,
    )
    assert math.isclose(adj_up, 0.02, rel_tol=0.0, abs_tol=1e-9)
    assert math.isclose(adj_down, 0.04, rel_tol=0.0, abs_tol=1e-9)
    assert allow_up is True
    assert allow_down is True


def test_apply_regime_edge_adjustment_strict_blocks_opposite():
    adj_up, adj_down, allow_up, allow_down = lag15_mod.apply_regime_edge_adjustment(
        edge_up=0.06,
        edge_down=0.04,
        regime="DOWN",
        regime_mode="strict",
        opposite_edge_penalty_cents=4.0,
    )
    assert math.isclose(adj_up, 0.06, rel_tol=0.0, abs_tol=1e-9)
    assert math.isclose(adj_down, 0.04, rel_tol=0.0, abs_tol=1e-9)
    assert allow_up is False
    assert allow_down is True


def test_compute_momentum_adjustment_positive():
    now = time.time()
    history = deque(
        [
            (now - 179.0, 100.0),
            (now - 90.0, 100.2),
            (now, 100.6),
        ]
    )
    adj = lag15_mod.compute_momentum_adjustment(history=history, lookback_sec=180.0)
    assert adj > 0.0
    assert abs(adj - 0.0302) < 0.003


def test_compute_momentum_adjustment_negative_clipped():
    now = time.time()
    history = deque(
        [
            (now - 60.0, 100.0),
            (now - 30.0, 95.0),
            (now, 90.0),
        ]
    )
    adj = lag15_mod.compute_momentum_adjustment(history=history, lookback_sec=180.0)
    assert adj == -0.05


def test_compute_continuation_adjustment_positive_late_window():
    now = time.time()
    history = deque(
        [
            (now - 300.0, 100.0),
            (now - 120.0, 100.4),
            (now, 100.8),
        ]
    )
    adj = lag15_mod.compute_continuation_adjustment(
        history=history,
        spot=100.8,
        open_price=100.0,
        remaining_sec=180.0,
        window_sec=900.0,
        trend_lookback_sec=300.0,
        max_adjustment=0.10,
        trend_reference_move_pct=0.0020,
        open_gap_reference_pct=0.0040,
    )
    assert adj > 0.0


def test_compute_fair_up_components_drift_pushes_with_trend():
    now = time.time()
    history = deque(
        [
            (now - 300.0, 100.0),
            (now - 120.0, 100.3),
            (now, 100.7),
        ]
    )
    base, mom, cont, fair = lag15_mod.compute_fair_up_components(
        spot=100.7,
        open_price=100.0,
        remaining_sec=240.0,
        sigma_per_s=0.001,
        history=history,
        fair_model="drift",
        window_sec=900.0,
    )
    assert fair >= base + mom
    assert cont > 0.0


def test_fair_up_probability_v2_applies_momentum_and_clips(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(lag15_mod, "compute_momentum_adjustment", lambda history, lookback_sec=180.0: 0.05)
    fair = lag15_mod.fair_up_probability_v2(
        spot=101.0,
        open_price=100.0,
        remaining_sec=1.0,
        sigma_per_s=0.001,
        history=deque(),
    )
    assert fair == 0.98


def test_pick_best_side_prefers_larger_edge_and_respects_flags():
    assert lag15_mod.pick_best_side(0.03, 0.05) == ("DOWN", 0.05)
    assert lag15_mod.pick_best_side(0.03, 0.05, allow_up=True, allow_down=False) == ("UP", 0.03)
    side, edge = lag15_mod.pick_best_side(math.nan, math.nan)
    assert side == ""
    assert math.isnan(edge)


def test_candidate_block_reason_prefers_time_then_regime_then_side():
    assert lag15_mod.candidate_block_reason("DOWN", False, False, True, False, False) == "time"
    assert lag15_mod.candidate_block_reason("DOWN", True, True, True, True, False) == "regime"
    assert lag15_mod.candidate_block_reason("UP", True, False, True, True, True) == "side"
    assert lag15_mod.candidate_block_reason("DOWN", True, True, True, True, True) == ""


def test_resolve_side_entry_price_band_uses_side_override():
    assert lag15_mod.resolve_side_entry_price_band(
        side="UP",
        base_min=0.05,
        base_max=0.35,
        up_min=-1.0,
        up_max=0.70,
        down_min=-1.0,
        down_max=0.25,
    ) == (0.05, 0.70)
    assert lag15_mod.resolve_side_entry_price_band(
        side="DOWN",
        base_min=0.05,
        base_max=0.35,
        up_min=-1.0,
        up_max=0.70,
        down_min=-1.0,
        down_max=0.25,
    ) == (0.05, 0.25)


def test_has_aligned_entry_momentum_matches_side_direction():
    assert lag15_mod.has_aligned_entry_momentum("UP", momentum_adj=0.01, continuation_adj=0.02) is True
    assert lag15_mod.has_aligned_entry_momentum("UP", momentum_adj=-0.01, continuation_adj=0.02) is False
    assert lag15_mod.has_aligned_entry_momentum("DOWN", momentum_adj=-0.01, continuation_adj=-0.02) is True
    assert lag15_mod.has_aligned_entry_momentum("DOWN", momentum_adj=-0.01, continuation_adj=0.02) is False


def test_is_entry_time_allowed_blocks_start_and_end():
    assert lag15_mod.is_entry_time_allowed(elapsed_sec=10.0, remaining_sec=700.0) is False
    assert lag15_mod.is_entry_time_allowed(elapsed_sec=35.0, remaining_sec=59.0) is False
    assert lag15_mod.is_entry_time_allowed(elapsed_sec=35.0, remaining_sec=120.0) is True


def test_is_entry_time_allowed_blocks_when_too_early_by_max_remaining():
    assert lag15_mod.is_entry_time_allowed(
        elapsed_sec=120.0,
        remaining_sec=700.0,
        min_remaining_sec=60.0,
        max_remaining_sec=540.0,
    ) is False


def test_settle_active_position_win_with_fee_and_slippage():
    state = lag15_mod.RuntimeState(day_key="2026-03-07")
    state.active_position = {
        "side": "UP",
        "shares": 25.0,
        "entry_price": 0.20,
        "window_open_price": 100.0,
        "slippage_cents": 0.5,
    }
    lag15_mod.settle_active_position(
        state=state,
        close_price=101.0,
        settle_epsilon=0.5,
        taker_fee_rate=0.02,
        logger=type("L", (), {"info": lambda self, msg: None})(),
    )
    assert state.active_position is None
    assert state.wins == 1
    assert math.isclose(state.pnl_total_usd, 19.775, rel_tol=0.0, abs_tol=0.01)
    assert math.isclose(state.max_drawdown_usd, 0.0, rel_tol=0.0, abs_tol=1e-9)


def test_settle_stale_active_position_uses_position_window_close(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        lag15_mod,
        "fetch_coinbase_candle_open_close",
        lambda start_ts, window_sec: (100.0, 99.0),
    )
    state = lag15_mod.RuntimeState(day_key="2026-03-07")
    state.active_position = {
        "window_start_ts": 1_000,
        "side": "DOWN",
        "shares": 25.0,
        "entry_price": 0.20,
        "window_open_price": 100.0,
        "slippage_cents": 0.0,
    }
    settled = lag15_mod.settle_stale_active_position_if_needed(
        state=state,
        current_window_start=1_900,
        window_sec=900,
        spot=150.0,
        settle_epsilon=0.5,
        taker_fee_rate=0.02,
        logger=type("L", (), {"info": lambda self, msg: None})(),
    )
    assert settled is True
    assert state.active_position is None
    assert state.wins == 1
    assert math.isclose(state.pnl_total_usd, 19.9, rel_tol=0.0, abs_tol=0.01)


def test_settle_stale_active_position_ignores_current_window(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        lag15_mod,
        "fetch_coinbase_candle_open_close",
        lambda start_ts, window_sec: (_ for _ in ()).throw(AssertionError("should not fetch")),
    )
    state = lag15_mod.RuntimeState(day_key="2026-03-07")
    state.active_position = {
        "window_start_ts": 1_900,
        "side": "DOWN",
        "shares": 25.0,
        "entry_price": 0.20,
        "window_open_price": 100.0,
        "slippage_cents": 0.0,
    }
    settled = lag15_mod.settle_stale_active_position_if_needed(
        state=state,
        current_window_start=1_900,
        window_sec=900,
        spot=150.0,
        settle_epsilon=0.5,
        taker_fee_rate=0.02,
        logger=type("L", (), {"info": lambda self, msg: None})(),
    )
    assert settled is False
    assert state.active_position is not None
