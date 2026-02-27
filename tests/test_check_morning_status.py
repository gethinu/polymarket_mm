from __future__ import annotations

import check_morning_status as mod


def test_extract_no_longshot_monthly_prefers_builder(monkeypatch):
    payload = {
        "no_longshot_status": {
            "monthly_return_now_text": "+1.00%",
            "monthly_return_now_source": "fallback_src",
            "rolling_30d_resolved_trades_new_condition": 7,
            "rolling_30d_resolved_trades_all": 21,
            "open_positions": 3,
            "observed_days": 2,
        }
    }

    monkeypatch.setattr(
        mod,
        "_NO_LONGSHOT_KPI_BUILDER",
        lambda _payload: {
            "monthly_return_now_text": "+9.99%",
            "monthly_return_now_source": "builder_src",
            "monthly_return_now_new_condition_text": "+8.88%",
            "monthly_return_now_new_condition_source": "builder_new_src",
            "monthly_return_now_all_text": "-7.77%",
            "monthly_return_now_all_source": "builder_all_src",
            "rolling_30d_resolved_trades": 15,
        },
    )

    out = mod.extract_no_longshot_monthly(payload)

    assert out["monthly_now_text"] == "+9.99%"
    assert out["monthly_now_source"] == "builder_src"
    assert out["monthly_new_text"] == "+8.88%"
    assert out["monthly_all_text"] == "-7.77%"
    assert out["rolling_30d_resolved_trades"] == 15
    assert out["rolling_30d_resolved_trades_new_condition"] == 7
    assert out["rolling_30d_resolved_trades_all"] == 21
    assert out["open_positions"] == 3
    assert out["obs_days_txt"] == "2"


def test_extract_no_longshot_monthly_fallback_without_builder(monkeypatch):
    payload = {
        "no_longshot_status": {
            "monthly_return_now_ratio": 0.01234,
            "monthly_return_now_new_condition_ratio": 0.04567,
            "monthly_return_now_all_ratio": -0.1111,
            "rolling_30d_resolved_trades": "8",
            "open_positions": 2,
            "observed_days": 5,
        }
    }

    monkeypatch.setattr(mod, "_NO_LONGSHOT_KPI_BUILDER", None)
    out = mod.extract_no_longshot_monthly(payload)

    assert out["monthly_now_text"] == "+1.23%"
    assert out["monthly_new_text"] == "+4.57%"
    assert out["monthly_all_text"] == "-11.11%"
    assert out["monthly_now_source"] == "-"
    assert out["rolling_30d_resolved_trades"] == 8
    assert out["open_positions"] == 2
    assert out["obs_days_txt"] == "5"
