from __future__ import annotations

import json
from pathlib import Path

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


def test_build_kpi_core_prefers_no_longshot_fields():
    no_longshot = {
        "daily_realized_pnl_usd_latest": 0.26,
        "daily_realized_pnl_day": "2026-02-26",
        "monthly_return_now_text": "+9.89%",
        "monthly_return_now_source": "realized_rolling_30d_new_condition",
        "rolling_30d_max_drawdown_ratio": -0.211,
        "rolling_30d_max_drawdown_text": "-21.10%",
    }
    realized_monthly = {
        "latest_day": "2026-02-27",
        "daily_realized_pnl_usd_latest": 1.25,
        "max_drawdown_30d_ratio": -0.031,
        "max_drawdown_30d_text": "-3.10%",
    }
    out = mod.build_kpi_core(no_longshot, realized_monthly)
    assert out["daily_realized_pnl_usd"] == pytest.approx(0.26)
    assert out["daily_realized_pnl_day"] == "2026-02-26"
    assert out["monthly_return_now_text"] == "+9.89%"
    assert out["monthly_return_now_source"] == "realized_rolling_30d_new_condition"
    assert out["max_drawdown_30d_ratio"] == pytest.approx(-0.211)
    assert out["max_drawdown_30d_text"] == "-21.10%"


def test_build_kpi_core_falls_back_to_realized_monthly_when_no_longshot_missing():
    no_longshot = {
        "monthly_return_now_text": "n/a",
        "monthly_return_now_source": "",
    }
    realized_monthly = {
        "latest_day": "2026-02-27",
        "daily_realized_pnl_usd_latest": 1.25,
        "projected_monthly_return_text": "+2.50%",
        "max_drawdown_30d_ratio": -0.031,
        "max_drawdown_30d_text": "-3.10%",
    }
    out = mod.build_kpi_core(no_longshot, realized_monthly)
    assert out["daily_realized_pnl_usd"] == pytest.approx(1.25)
    assert out["daily_realized_pnl_day"] == "2026-02-27"
    assert out["monthly_return_now_text"] == "+2.50%"
    assert out["monthly_return_now_source"] == "realized_monthly_return.projected_monthly_return_text"
    assert out["max_drawdown_30d_ratio"] == pytest.approx(-0.031)
    assert out["max_drawdown_30d_text"] == "-3.10%"


def test_load_realized_daily_series_nondefault_skips_clob_fallback(monkeypatch, tmp_path: Path):
    clob_file = tmp_path / "clob_arb_realized_daily.jsonl"
    clob_file.write_text(
        json.dumps({"day": "2026-03-07", "realized_pnl_usd": 5.0}) + "\n",
        encoding="utf-8",
    )
    strategy_file = tmp_path / "strategy_realized_pnl_daily.jsonl"
    strategy_file.write_text("", encoding="utf-8")

    monkeypatch.setattr(mod, "logs_dir", lambda: tmp_path)

    series = mod.load_realized_daily_series(strategy_id="weather_7acct_auto")

    assert series["per_day"] == {}
    assert series["source_files"] == []


def test_summarize_weather_profile_realized_uses_profile_series(monkeypatch, tmp_path: Path):
    strategy_file = tmp_path / "strategy_realized_pnl_daily.jsonl"
    strategy_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "day": "2026-03-07",
                        "strategy_id": "weather_7acct_auto",
                        "realized_pnl_usd": 1.5,
                        "bankroll_usd": 60.0,
                    }
                ),
                json.dumps(
                    {
                        "day": "2026-03-08",
                        "strategy_id": "weather_7acct_auto",
                        "realized_pnl_usd": -0.5,
                        "bankroll_usd": 60.0,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    latest_path = tmp_path / "weather_7acct_auto_realized_latest.json"
    latest_path.write_text(
        json.dumps(
            {
                "counts": {"positions_total": 4},
                "metrics": {
                    "open_positions": 2,
                    "resolved_positions": 2,
                    "total_resolved_trades": 2,
                    "total_realized_pnl_usd": 1.0,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(mod, "logs_dir", lambda: tmp_path)

    out = mod.summarize_weather_profile_realized(
        records=[{"profile_name": "weather_7acct_auto"}],
        min_days=30,
    )

    assert out["count"] == 1
    row = out["profiles"][0]
    assert row["profile_name"] == "weather_7acct_auto"
    assert row["observed_realized_days"] == 2
    assert row["latest_day"] == "2026-03-08"
    assert row["daily_realized_pnl_usd_latest"] == pytest.approx(-0.5)
    assert row["open_positions"] == 2
    assert row["resolved_positions"] == 2
    assert row["positions_total"] == 4
