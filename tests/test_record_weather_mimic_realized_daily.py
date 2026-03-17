from __future__ import annotations

import datetime as dt

import pytest

import record_weather_mimic_realized_daily as mod


def test_ingest_and_resolve_no_side(monkeypatch):
    fixed_now = dt.datetime(2026, 3, 7, 12, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(mod, "now_utc", lambda: fixed_now)

    positions = []
    consensus_rows = [
        {
            "market_id": "123",
            "question": "Will it rain?",
            "side_hint": "no",
            "entry_price": 0.70,
            "yes_price": 0.30,
            "no_price": 0.70,
            "rank": 1,
            "score_total": 92.5,
            "end_iso": "2026-03-08T12:00:00Z",
        }
    ]

    added = mod.ingest_entries(
        positions=positions,
        consensus_rows=consensus_rows,
        top_n=1,
        per_trade_cost=0.002,
        shares_per_entry=1.0,
    )

    assert added == 1
    assert positions[0]["entry_day"] == "2026-03-07"
    assert positions[0]["side"] == "no"

    def _fake_fetch_market_by_id(_market_id: str, timeout_sec: float):
        assert timeout_sec == pytest.approx(2.0)
        return {
            "endDate": "2026-03-08T12:00:00Z",
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.00", "1.00"],
        }

    monkeypatch.setattr(mod, "fetch_market_by_id", _fake_fetch_market_by_id)
    monkeypatch.setattr(
        mod,
        "now_utc",
        lambda: dt.datetime(2026, 3, 8, 13, 0, tzinfo=dt.timezone.utc),
    )

    resolved = mod.try_resolve_position(
        positions[0],
        timeout_sec=2.0,
        win_threshold=0.99,
        lose_threshold=0.01,
    )

    assert resolved is True
    assert positions[0]["resolution"] == "WIN"
    assert positions[0]["winning_outcome"] == "NO"
    assert positions[0]["pnl_usd"] == pytest.approx(0.298)

    rows_by_day = mod.aggregate_daily(
        positions=positions,
        profile_name="weather_7acct_auto",
        strategy_id="weather_7acct_auto",
        assumed_bankroll_usd=60.0,
    )

    rec = rows_by_day["2026-03-08"]
    assert rec["realized_pnl_usd"] == pytest.approx(0.298)
    assert rec["realized_cost_basis_usd"] == pytest.approx(0.702)
    assert rec["bankroll_return_pct"] == pytest.approx(0.298 / 60.0)


def test_build_gate_and_monthly_return_for_tentative_stage():
    rows_by_day = {}
    for i in range(1, 8):
        day = f"2026-03-{i:02d}"
        rows_by_day[day] = {
            "day": day,
            "realized_pnl_usd": 1.0,
            "realized_cost_basis_usd": 2.0,
            "resolved_trades": 1,
            "bankroll_usd": 60.0,
        }

    gate = mod.build_realized_30d_gate(
        rows_by_day=rows_by_day,
        strategy_id="weather_7acct_auto",
        min_days=30,
    )
    monthly = mod.summarize_realized_monthly_return(
        rows_by_day=rows_by_day,
        strategy_id="weather_7acct_auto",
        min_days=30,
    )

    assert gate["decision"] == "PENDING_30D"
    assert gate["decision_3stage"] == "READY_TENTATIVE"
    assert gate["observed_realized_days"] == 7
    assert gate["next_stage"]["remaining_days"] == 7
    assert monthly["observed_realized_days"] == 7
    assert monthly["latest_day"] == "2026-03-07"
    assert monthly["daily_realized_pnl_usd_latest"] == pytest.approx(1.0)
    assert monthly["trailing_window_return_ratio"] == pytest.approx(7.0 / 60.0)
    assert monthly["rolling_30d_return_ratio"] is None
