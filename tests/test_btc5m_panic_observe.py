"""Tests for polymarket_btc5m_panic_observe core functions."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_btc5m_panic_observe as panic_mod


# --- args / side mode helpers ---

def test_parse_args_defaults_include_both_side_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", ["polymarket_btc5m_panic_observe.py"])
    args = panic_mod.parse_args()
    assert args.window_minutes == 5
    assert args.allowed_side_mode == "both"


def test_parse_args_normalizes_down_side_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["polymarket_btc5m_panic_observe.py", "--allowed-side-mode", "short"],
    )
    args = panic_mod.parse_args()
    assert args.allowed_side_mode == "down"


def test_is_side_allowed_matches_side_mode():
    assert panic_mod.is_side_allowed("DOWN", "down") is True
    assert panic_mod.is_side_allowed("UP", "down") is False
    assert panic_mod.is_side_allowed("UP", "up") is True
    assert panic_mod.is_side_allowed("DOWN", "both") is True


# --- detect_panic_signal ---

def test_detect_panic_signal_up():
    """Expensive DOWN + cheap UP → trigger UP."""
    side, entry, expensive, reason = panic_mod.detect_panic_signal(
        up_ask=0.05,
        down_ask=0.95,
        cheap_ask_max=0.10,
        expensive_ask_min=0.90,
    )
    assert side == "UP"
    assert math.isfinite(entry) and entry > 0
    assert reason != ""


def test_detect_panic_signal_down():
    """Expensive UP + cheap DOWN → trigger DOWN."""
    side, entry, expensive, reason = panic_mod.detect_panic_signal(
        up_ask=0.95,
        down_ask=0.05,
        cheap_ask_max=0.10,
        expensive_ask_min=0.90,
    )
    assert side == "DOWN"
    assert math.isfinite(entry) and entry > 0


def test_detect_panic_signal_none():
    """Both sides near 50c → no panic."""
    side, entry, expensive, reason = panic_mod.detect_panic_signal(
        up_ask=0.50,
        down_ask=0.50,
        cheap_ask_max=0.10,
        expensive_ask_min=0.90,
    )
    assert side == ""
    assert reason == ""


def test_detect_panic_nan_inputs():
    """NaN inputs → no signal."""
    side, entry, expensive, reason = panic_mod.detect_panic_signal(
        up_ask=math.nan,
        down_ask=0.95,
        cheap_ask_max=0.10,
        expensive_ask_min=0.90,
    )
    assert side == ""


# --- settle_active_position ---

def test_settle_win_fees():
    """Win with 2% taker fee."""
    state = panic_mod.RuntimeState(day_key="2026-03-07")
    state.active_position = {
        "side": "UP",
        "shares": 25.0,
        "entry_price": 0.05,
        "window_open_price": 100.0,
        "slippage_cents": 0.0,
    }
    panic_mod.settle_active_position(
        state=state,
        close_price=101.0,
        settle_epsilon=0.5,
        taker_fee_rate=0.02,
        logger=type("L", (), {"info": lambda self, m: None})(),
    )
    assert state.wins == 1
    # gross = 25 * (1 - 0.05) = 23.75
    # fee = 25 * 0.05 * 0.02 = 0.025
    expected = 23.75 - 0.025
    assert abs(state.pnl_total_usd - expected) < 0.01


def test_settle_loss_fees():
    """Loss with 2% taker fee."""
    state = panic_mod.RuntimeState(day_key="2026-03-07")
    state.active_position = {
        "side": "UP",
        "shares": 25.0,
        "entry_price": 0.05,
        "window_open_price": 100.0,
        "slippage_cents": 0.0,
    }
    panic_mod.settle_active_position(
        state=state,
        close_price=99.0,
        settle_epsilon=0.5,
        taker_fee_rate=0.02,
        logger=type("L", (), {"info": lambda self, m: None})(),
    )
    assert state.losses == 1
    # gross = -(25 * 0.05) = -1.25
    # fee = 25 * 0.05 * 0.02 = 0.025
    expected = -1.25 - 0.025
    assert abs(state.pnl_total_usd - expected) < 0.01


def test_settle_drawdown():
    """Drawdown tracking on loss."""
    state = panic_mod.RuntimeState(day_key="2026-03-07")
    state.pnl_total_usd = 50.0
    state.peak_pnl_usd = 50.0
    state.active_position = {
        "side": "UP",
        "shares": 25.0,
        "entry_price": 0.05,
        "window_open_price": 100.0,
        "slippage_cents": 0.0,
    }
    panic_mod.settle_active_position(
        state=state,
        close_price=99.0,
        settle_epsilon=0.5,
        taker_fee_rate=0.02,
        logger=type("L", (), {"info": lambda self, m: None})(),
    )
    assert state.max_drawdown_usd > 0
    assert state.peak_pnl_usd == 50.0


def test_settle_with_slippage():
    """Slippage cost deducted on settle."""
    state = panic_mod.RuntimeState(day_key="2026-03-07")
    state.active_position = {
        "side": "UP",
        "shares": 25.0,
        "entry_price": 0.05,
        "window_open_price": 100.0,
        "slippage_cents": 0.5,
    }
    panic_mod.settle_active_position(
        state=state,
        close_price=101.0,
        settle_epsilon=0.5,
        taker_fee_rate=0.02,
        logger=type("L", (), {"info": lambda self, m: None})(),
    )
    # slippage_cost = 25 * 0.005 = 0.125
    assert state.wins == 1
    expected = 23.75 - 0.025 - 0.125
    assert abs(state.pnl_total_usd - expected) < 0.01


# --- _has_depth ---

def test_has_depth_true():
    book = {"asks": [{"price": 0.05, "size": 20.0}]}
    assert panic_mod._has_depth(book, 10.0) is True


def test_has_depth_false():
    book = {"asks": [{"price": 0.05, "size": 5.0}]}
    assert panic_mod._has_depth(book, 10.0) is False


def test_has_depth_empty():
    assert panic_mod._has_depth({}, 10.0) is False
    assert panic_mod._has_depth(None, 10.0) is False


def test_backup_and_reset_state_file(tmp_path):
    state_path = tmp_path / "panic_state.json"
    original = {
        "day_key": "2026-03-07",
        "pnl_total_usd": 12.5,
        "trades_closed": 3,
    }
    state_path.write_text(json.dumps(original), encoding="utf-8")
    logger = type("L", (), {"info": lambda self, m: None})()

    fresh = panic_mod.backup_and_reset_state_file(state_path, logger)

    backup_path = tmp_path / "panic_state_backup_pre_oos.json"
    assert backup_path.exists()
    backed_up = json.loads(backup_path.read_text(encoding="utf-8"))
    assert backed_up["pnl_total_usd"] == 12.5
    reset_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert reset_state["pnl_total_usd"] == 0.0
    assert fresh.pnl_total_usd == 0.0


def test_reset_oos_runtime_artifacts_rotates_log_and_metrics(tmp_path):
    state_path = tmp_path / "panic_state.json"
    log_path = tmp_path / "panic.log"
    metrics_path = tmp_path / "panic-metrics.jsonl"
    state_path.write_text(json.dumps({"pnl_total_usd": 7.5}), encoding="utf-8")
    log_path.write_text("old-log\n", encoding="utf-8")
    metrics_path.write_text('{"ts_ms":1}\n', encoding="utf-8")

    fresh, messages = panic_mod.reset_oos_runtime_artifacts(
        state_path=state_path,
        log_path=log_path,
        metrics_path=metrics_path,
    )

    assert fresh.pnl_total_usd == 0.0
    assert state_path.exists()
    assert not log_path.exists()
    assert not metrics_path.exists()
    assert (tmp_path / "panic_state_backup_pre_oos.json").exists()
    assert (tmp_path / "panic_backup_pre_oos.log").read_text(encoding="utf-8") == "old-log\n"
    assert (tmp_path / "panic-metrics_backup_pre_oos.jsonl").read_text(encoding="utf-8") == '{"ts_ms":1}\n'
    assert any("rotated log" in msg for msg in messages)
    assert any("rotated metrics" in msg for msg in messages)


def test_replace_with_retries_recovers_after_permission_error(monkeypatch):
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("locked")
        return None

    slept = []
    monkeypatch.setattr(panic_mod.time, "sleep", lambda sec: slept.append(sec))
    panic_mod._replace_with_retries(
        src=Path("a"),
        dst=Path("b"),
        attempts=4,
        sleep_sec=0.01,
        replace_func=flaky_replace,
    )
    assert calls["n"] == 3
    assert slept == [0.01, 0.01]


# --- infer_up_down_tokens ---

def test_infer_up_down_tokens_basic():
    """Should identify UP and DOWN token ids from labels."""
    result = panic_mod.infer_up_down_tokens(
        question="Will BTC go up or down?",
        token_ids=["t1", "t2"],
        outcomes=["Up", "Down"],
    )
    assert result is not None
    up_id, down_id, up_lbl, down_lbl = result
    assert up_id == "t1"
    assert down_id == "t2"


def test_infer_up_down_tokens_reversed():
    """Order doesn't matter - labels drive assignment."""
    result = panic_mod.infer_up_down_tokens(
        question="Will BTC go up or down?",
        token_ids=["t2", "t1"],
        outcomes=["Down", "Up"],
    )
    assert result is not None
    up_id, down_id, up_lbl, down_lbl = result
    assert up_id == "t1"
    assert down_id == "t2"
