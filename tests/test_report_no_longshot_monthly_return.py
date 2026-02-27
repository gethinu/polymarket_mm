from __future__ import annotations

import report_no_longshot_monthly_return as mod


def test_build_kpi_prefers_text_keys():
    payload = {
        "generated_utc": "2026-02-26T15:22:39+00:00",
        "no_longshot_status": {
            "monthly_return_now_text": "+5.43%",
            "monthly_return_now_source": "realized_rolling_30d_new_condition",
            "monthly_return_now_new_condition_text": "+5.43%",
            "monthly_return_now_new_condition_source": "realized_rolling_30d_new_condition",
            "monthly_return_now_all_text": "-19.90%",
            "monthly_return_now_all_source": "realized_rolling_30d_all",
            "rolling_30d_monthly_return_text": "+5.43%",
            "rolling_30d_monthly_return_source": "realized_rolling_30d_new_condition",
            "rolling_30d_resolved_trades": 15,
        },
        "realized_30d_gate": {
            "decision": "PENDING_30D",
            "decision_3stage": "PENDING_TENTATIVE",
            "decision_3stage_label_ja": "7日暫定判定待ち",
        },
    }

    kpi = mod.build_kpi(payload)

    assert kpi["monthly_return_now_text"] == "+5.43%"
    assert kpi["monthly_return_now_source"] == "realized_rolling_30d_new_condition"
    assert kpi["monthly_return_now_all_text"] == "-19.90%"
    assert kpi["rolling_30d_resolved_trades"] == 15
    assert kpi["realized_30d_gate_decision"] == "PENDING_30D"


def test_build_kpi_falls_back_to_ratio_when_text_missing():
    payload = {
        "no_longshot_status": {
            "monthly_return_now_ratio": 0.012345,
            "rolling_30d_monthly_return_ratio": -0.05,
            "rolling_30d_resolved_trades": "7",
        },
        "realized_30d_gate": {},
    }

    kpi = mod.build_kpi(payload)

    assert kpi["monthly_return_now_text"] == "+1.23%"
    assert kpi["rolling_30d_monthly_return_text"] == "-5.00%"
    assert kpi["rolling_30d_resolved_trades"] == 7


def test_render_pretty_contains_core_lines():
    lines = mod.render_pretty(
        {
            "generated_utc": "2026-02-26T15:22:39+00:00",
            "monthly_return_now_text": "+5.43%",
            "monthly_return_now_source": "realized_rolling_30d_new_condition",
            "monthly_return_now_new_condition_text": "+5.43%",
            "monthly_return_now_new_condition_source": "realized_rolling_30d_new_condition",
            "monthly_return_now_all_text": "-19.90%",
            "monthly_return_now_all_source": "realized_rolling_30d_all",
            "rolling_30d_monthly_return_text": "+5.43%",
            "rolling_30d_monthly_return_source": "realized_rolling_30d_new_condition",
            "rolling_30d_resolved_trades": 15,
            "realized_30d_gate_decision": "PENDING_30D",
            "realized_30d_gate_decision_3stage": "PENDING_TENTATIVE",
            "realized_30d_gate_decision_3stage_label_ja": "7日暫定判定待ち",
        }
    )

    assert "monthly_return_now: +5.43%" in lines
    assert "rolling_30d_resolved_trades: 15" in lines
    assert "realized_30d_gate: PENDING_30D" in lines
