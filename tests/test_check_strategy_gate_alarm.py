from __future__ import annotations

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
