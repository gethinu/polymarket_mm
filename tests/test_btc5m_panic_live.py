"""Tests for polymarket_btc5m_panic_live execution wrapper."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from polymarket_btc5m_panic_live import (
    LiveRiskState,
    check_risk_limits,
    check_alarms,
    collect_alarm_messages,
    compute_maker_limit_price,
    summarize_matching_trades,
    submit_live_order,
)
from polymarket_btc5m_panic_observe import RuntimeState


# --- check_risk_limits ---

def _make_args(**overrides):
    defaults = {
        "max_entries_per_day": 30,
        "max_notional_per_trade": 2.50,
        "daily_loss_limit_usd": 50.0,
    }
    defaults.update(overrides)
    return type("Args", (), defaults)()


def test_risk_allows_normal_entry():
    risk = LiveRiskState(entries_today=5)
    state = RuntimeState(day_key="2026-03-07")
    allowed, reason = check_risk_limits(
        risk, state, notional=2.00, args=_make_args()
    )
    assert allowed is True
    assert reason == ""


def test_risk_blocks_daily_entry_cap():
    risk = LiveRiskState(entries_today=30)
    state = RuntimeState(day_key="2026-03-07")
    allowed, reason = check_risk_limits(
        risk, state, notional=1.00, args=_make_args()
    )
    assert allowed is False
    assert "entry cap" in reason


def test_risk_blocks_notional_cap():
    risk = LiveRiskState(entries_today=0)
    state = RuntimeState(day_key="2026-03-07")
    allowed, reason = check_risk_limits(
        risk, state, notional=5.00, args=_make_args()
    )
    assert allowed is False
    assert "notional" in reason


def test_risk_blocks_daily_loss():
    risk = LiveRiskState(entries_today=0)
    state = RuntimeState(day_key="2026-03-07")
    state.pnl_total_usd = -60.0
    state.day_anchor_pnl_usd = 0.0
    allowed, reason = check_risk_limits(
        risk, state, notional=1.00, args=_make_args()
    )
    assert allowed is False
    assert "loss limit" in reason


def test_risk_blocks_halted():
    risk = LiveRiskState()
    state = RuntimeState(day_key="2026-03-07")
    state.halted = True
    state.halt_reason = "test halt"
    allowed, reason = check_risk_limits(
        risk, state, notional=1.00, args=_make_args()
    )
    assert allowed is False
    assert "halted" in reason


# --- check_alarms ---

def test_alarms_none_when_healthy():
    state = RuntimeState(day_key="2026-03-07")
    state.pnl_total_usd = 10.0
    state.day_anchor_pnl_usd = 0.0
    state.max_drawdown_usd = 5.0
    state.losses = 3
    logger = type("L", (), {"info": lambda self, m: None})()
    alarms = check_alarms(state, logger)
    assert len(alarms) == 0


def test_alarms_daily_pnl():
    state = RuntimeState(day_key="2026-03-07")
    state.pnl_total_usd = -35.0
    state.day_anchor_pnl_usd = 0.0
    state.max_drawdown_usd = 5.0
    state.losses = 3
    logger = type("L", (), {"info": lambda self, m: None})()
    alarms = check_alarms(state, logger)
    assert any("daily PnL" in a for a in alarms)


def test_alarms_drawdown():
    state = RuntimeState(day_key="2026-03-07")
    state.pnl_total_usd = 0.0
    state.day_anchor_pnl_usd = 0.0
    state.max_drawdown_usd = 60.0
    state.losses = 3
    logger = type("L", (), {"info": lambda self, m: None})()
    alarms = check_alarms(state, logger)
    assert any("drawdown" in a for a in alarms)


def test_collect_alarm_messages_loss_streak_and_metrics_stale():
    state = RuntimeState(day_key="2026-03-07")
    alarm_map = collect_alarm_messages(
        state=state,
        current_loss_streak=11,
        last_metrics_age_sec=3.5 * 3600.0,
    )
    assert "loss_streak" in alarm_map
    assert "metrics_stale" in alarm_map


def test_compute_maker_limit_price_stays_inside_spread():
    price = compute_maker_limit_price(entry_ask=0.05, current_bid=0.03)
    assert price == 0.04


def test_compute_maker_limit_price_rejects_no_safe_tick():
    price = compute_maker_limit_price(entry_ask=0.01, current_bid=0.0)
    assert price != price  # NaN


# --- submit_live_order (mocked) ---

def test_submit_user_declines():
    logger = type("L", (), {"info": lambda self, m: None})()
    with patch("builtins.input", return_value="n"):
        result = submit_live_order(
            client=None,
            token_id="test_token_123",
            price=0.05,
            size=25.0,
            logger=logger,
            require_confirm=True,
        )
    assert result["success"] is False
    assert result["reason"] == "user_declined"


def test_submit_mock_success():
    logger = type("L", (), {"info": lambda self, m: None})()
    mock_client = MagicMock()
    mock_client.create_order.return_value = {"mock": True}
    mock_client.post_order.return_value = {
        "orderID": "ord_123"
    }
    result = submit_live_order(
        client=mock_client,
        token_id="test_token_123",
        price=0.05,
        size=25.0,
        logger=logger,
        require_confirm=False,
    )
    assert result["success"] is True
    assert result["order_id"] == "ord_123"
    mock_client.create_order.assert_called_once()


def test_submit_mock_error():
    logger = type("L", (), {"info": lambda self, m: None})()
    mock_client = MagicMock()
    mock_client.create_order.side_effect = Exception("network error")
    result = submit_live_order(
        client=mock_client,
        token_id="test_token_123",
        price=0.05,
        size=25.0,
        logger=logger,
        require_confirm=False,
    )
    assert result["success"] is False
    assert "network error" in result["reason"]


def test_summarize_matching_trades_aggregates_fill_details():
    summary = summarize_matching_trades(
        [
            {"id": "t1", "size": 5.0, "price": 0.04, "transactionHash": "0xabc"},
            {"id": "t2", "size": 3.0, "price": 0.05},
        ],
        fallback_price=0.04,
    )
    assert summary["actual_fill_shares"] == 8.0
    assert abs(summary["actual_fill_price"] - 0.04375) < 1e-9
    assert summary["trade_count"] == 2
    assert summary["tx_hash"] == "0xabc"
