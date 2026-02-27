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


def test_find_health_artifact_row_matches_suffix_case_insensitive():
    payload = {
        "artifacts": [
            {
                "path": r"C:\Repos\polymarket_mm\logs\simmer_ab_supervisor_state.json",
                "status": "FRESH",
            },
            {
                "path": r"C:\Repos\polymarket_mm\logs\strategy_register_latest.json",
                "status": "FRESH",
            },
        ]
    }

    row = mod._find_health_artifact_row(payload, "logs/SIMMER_AB_SUPERVISOR_STATE.json")

    assert isinstance(row, dict)
    assert row.get("status") == "FRESH"


def test_find_health_artifact_row_returns_none_when_missing():
    payload = {"artifacts": [{"path": r"C:\Repos\polymarket_mm\logs\strategy_register_latest.json"}]}

    row = mod._find_health_artifact_row(payload, r"logs\simmer_ab_supervisor_state.json")

    assert row is None


def test_resolve_simmer_interim_target_returns_row_for_7d():
    summary = {
        "data_sufficient_days": 3,
        "interim_milestones": {
            "tentative_7d": {"decision": "PENDING", "reached": False, "data_sufficient_days": 3, "target_days": 7},
            "intermediate_14d": {"decision": "PENDING", "reached": False, "data_sufficient_days": 3, "target_days": 14},
        },
    }

    row = mod._resolve_simmer_interim_target(summary, "7d")

    assert row["target_key"] == "tentative_7d"
    assert row["decision"] == "PENDING"
    assert row["target_days"] == 7


def test_resolve_simmer_interim_target_fallback_when_missing():
    summary = {"data_sufficient_days": 2}

    row = mod._resolve_simmer_interim_target(summary, "14d")

    assert row["target_key"] == "intermediate_14d"
    assert row["decision"] == "UNKNOWN"
    assert row["reached"] is False
    assert row["data_sufficient_days"] == 2
    assert row["target_days"] == 14
