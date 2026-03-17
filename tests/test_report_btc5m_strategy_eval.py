"""Tests for report_btc5m_strategy_eval lag15 mode support."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import report_btc5m_strategy_eval as eval_mod


def test_default_paths_support_lag15():
    log_path, state_path, metrics_path = eval_mod.default_paths("lag15")
    assert log_path.name == "btc15m-lag-observe.log"
    assert state_path.name == "btc15m_lag_observe_state.json"
    assert metrics_path.name == "btc15m-lag-observe-metrics.jsonl"


def test_format_text_report_mentions_lag15():
    report = eval_mod.build_report(
        mode="lag15",
        state={
            "pnl_total_usd": 12.5,
            "trades_closed": 2,
            "wins": 1,
            "losses": 1,
            "pushes": 0,
        },
        metrics=[
            {"ts_ms": 1_000, "pnl_total_usd": 0.0},
            {"ts_ms": 3_601_000, "pnl_total_usd": 12.5},
        ],
        trades=[
            {"side": "UP", "shares": 25.0, "entry_price": 0.20, "outcome": "WIN"},
            {"side": "DOWN", "shares": 25.0, "entry_price": 0.15, "outcome": "LOSS"},
        ],
        taker_fee_rate=0.02,
        slippage_cents=0.5,
    )
    text = eval_mod.format_text_report(report)
    assert "BTC LAG15 Strategy Evaluation" in text
    assert report["mode"] == "lag15"


def test_build_report_supports_down_side_filter():
    report = eval_mod.build_report(
        mode="lag15",
        state={
            "pnl_total_usd": 12.5,
            "trades_closed": 2,
            "wins": 1,
            "losses": 1,
            "pushes": 0,
        },
        metrics=[
            {"ts_ms": 1_000, "pnl_total_usd": 0.0},
            {"ts_ms": 3_601_000, "pnl_total_usd": 12.5},
        ],
        trades=[
            {"side": "UP", "shares": 25.0, "entry_price": 0.20, "outcome": "WIN"},
            {"side": "DOWN", "shares": 25.0, "entry_price": 0.15, "outcome": "LOSS"},
        ],
        taker_fee_rate=0.02,
        slippage_cents=0.5,
        trade_side="down",
    )
    assert report["trade_side_filter"] == "down"
    assert report["fee_adjusted"]["trade_count"] == 1
    assert "UP" not in report["by_side"]
    assert "DOWN" in report["by_side"]
    text = eval_mod.format_text_report(report)
    assert "Trade filter: DOWN only" in text
