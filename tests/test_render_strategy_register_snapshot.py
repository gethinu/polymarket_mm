from __future__ import annotations

import pytest

import render_strategy_register_snapshot as mod


def test_summarize_realized_monthly_return_includes_latest_and_drawdown():
    series = {
        "strategy_id": "weather_clob_arb_buckets_observe",
        "bankroll_usd": 100.0,
        "per_day": {
            "2026-02-01": 1.0,
            "2026-02-02": -2.0,
            "2026-02-03": 1.0,
        },
        "source_files": [],
        "source_modes": {},
    }
    out = mod.summarize_realized_monthly_return(min_days=1, series=series)
    assert out["latest_day"] == "2026-02-03"
    assert out["daily_realized_pnl_usd_latest"] == pytest.approx(1.0)
    assert out["max_drawdown_30d_ratio"] == pytest.approx(-0.02, abs=1e-6)
    assert out["max_drawdown_30d_text"] == "-2.00%"


def test_build_kpi_core_uses_primary_fields():
    no_longshot = {
        "monthly_return_now_text": "+9.89%",
        "monthly_return_now_source": "realized_rolling_30d_new_condition",
    }
    realized_monthly = {
        "latest_day": "2026-02-27",
        "daily_realized_pnl_usd_latest": 1.25,
        "max_drawdown_30d_ratio": -0.031,
        "max_drawdown_30d_text": "-3.10%",
    }
    out = mod.build_kpi_core(no_longshot, realized_monthly)
    assert out["daily_realized_pnl_usd"] == pytest.approx(1.25)
    assert out["daily_realized_pnl_day"] == "2026-02-27"
    assert out["monthly_return_now_text"] == "+9.89%"
    assert out["monthly_return_now_source"] == "realized_rolling_30d_new_condition"
    assert out["max_drawdown_30d_ratio"] == pytest.approx(-0.031)
    assert out["max_drawdown_30d_text"] == "-3.10%"

