from __future__ import annotations

import datetime as dt

import check_strategy_gate_alarm as mod


def test_load_no_longshot_prefers_builder(monkeypatch):
    snapshot = {
        "no_longshot_status": {
            "rolling_30d_resolved_trades": 3,
            "monthly_return_now_text": "+1.00%",
            "monthly_return_now_source": "fallback_src",
        }
    }

    monkeypatch.setattr(
        mod,
        "_NO_LONGSHOT_KPI_BUILDER",
        lambda _snapshot: {
            "rolling_30d_resolved_trades": 15,
            "monthly_return_now_text": "+5.43%",
            "monthly_return_now_source": "realized_rolling_30d_new_condition",
        },
    )

    out = mod.load_no_longshot(snapshot)
    assert out["rolling_30d_resolved_trades"] == 15
    assert out["monthly_return_now_text"] == "+5.43%"
    assert out["monthly_return_now_source"] == "realized_rolling_30d_new_condition"


def test_load_no_longshot_fallback_without_builder(monkeypatch):
    snapshot = {
        "no_longshot_status": {
            "rolling_30d_resolved_trades": "8",
            "monthly_return_now_text": "+2.22%",
            "monthly_return_now_source": "snapshot_src",
        }
    }

    monkeypatch.setattr(mod, "_NO_LONGSHOT_KPI_BUILDER", None)
    out = mod.load_no_longshot(snapshot)
    assert out["rolling_30d_resolved_trades"] == 8
    assert out["monthly_return_now_text"] == "+2.22%"
    assert out["monthly_return_now_source"] == "snapshot_src"


def test_evaluate_no_longshot_practical_gate_threshold_reached():
    out = mod.evaluate_no_longshot_practical_gate(
        today_local=dt.date(2026, 2, 27),
        resolved_trades=30,
        min_resolved_trades=30,
        initial_decision_date="2026-03-02",
        slide_days=3,
        prev_state={"no_longshot_practical_threshold_met": False},
    )
    assert out["status"] == "THRESHOLD_MET"
    assert out["threshold_met"] is True
    assert out["threshold_reached_now"] is True
    assert out["rollover_triggered"] is False
    assert out["active_decision_date"] == "2026-03-02"


def test_evaluate_no_longshot_practical_gate_rolls_when_due_and_unmet():
    out = mod.evaluate_no_longshot_practical_gate(
        today_local=dt.date(2026, 3, 2),
        resolved_trades=20,
        min_resolved_trades=30,
        initial_decision_date="2026-03-02",
        slide_days=3,
        prev_state={},
    )
    assert out["threshold_met"] is False
    assert out["rollover_triggered"] is True
    assert out["rollover_from_date"] == "2026-03-02"
    assert out["rollover_to_date"] == "2026-03-05"
    assert out["active_decision_date"] == "2026-03-05"
    assert out["remaining_days"] == 3
