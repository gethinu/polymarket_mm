from __future__ import annotations

import json
import sys
from pathlib import Path

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


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _base_snapshot_payload() -> dict:
    return {
        "realized_30d_gate": {
            "decision": "PENDING_30D",
            "decision_3stage": "PENDING_TENTATIVE",
            "decision_3stage_label_ja": "7日暫定判定待ち",
            "stage_label": "PRE_TENTATIVE_7D",
            "stage_label_ja": "7日暫定到達前",
            "observed_realized_days": 4,
            "min_realized_days": 30,
            "next_stage": {"label": "7d tentative", "label_ja": "7日暫定", "remaining_days": 3},
        },
        "realized_monthly_return": {
            "projected_monthly_return_text": "+0.00%",
            "rolling_30d_return_text": "n/a",
        },
        "no_longshot_status": {
            "monthly_return_now_text": "+9.89%",
            "monthly_return_now_source": "realized_rolling_30d_new_condition",
            "monthly_return_now_new_condition_text": "+9.89%",
            "monthly_return_now_all_text": "-14.82%",
            "rolling_30d_resolved_trades": 21,
            "rolling_30d_resolved_trades_new_condition": 21,
            "rolling_30d_resolved_trades_all": 46,
            "open_positions": 0,
        },
        "readiness": {
            "summary": {
                "strict": {"go_count": 3, "count": 3},
                "quality": {"go_count": 2, "count": 2},
            }
        },
    }


def _base_simmer_payload() -> dict:
    return {
        "decision": "NO_GO",
        "summary": {
            "data_sufficient_days": 1,
            "min_days_required": 25,
            "decision_timing_status": "BEFORE_DECISION_DATE",
            "decision_date": "2026-03-22",
            "today_local": "2026-02-27",
            "days_until_decision": 23,
            "decision_stage": "PROVISIONAL",
            "final_decision_ready": False,
            "coverage_data_sufficient": {
                "since": "2026-02-27 08:58:30",
                "until": "2026-02-27 09:01:30",
            },
            "interim_milestones": {
                "tentative_7d": {
                    "target_days": 7,
                    "data_sufficient_days": 1,
                    "remaining_days": 6,
                    "reached": False,
                    "conditions_pass": True,
                    "decision": "PENDING",
                },
                "intermediate_14d": {
                    "target_days": 14,
                    "data_sufficient_days": 1,
                    "remaining_days": 13,
                    "reached": False,
                    "conditions_pass": True,
                    "decision": "PENDING",
                },
            },
        },
        "conditions": {
            "turnover_day_ge_baseline": {"pass": True},
            "median_hold_shorter_than_baseline": {"pass": True},
            "expectancy_not_worse_than_10pct": {"pass": True},
            "error_halt_frequency_not_higher": {"pass": True},
        },
        "recommended_action": "NO_GO: Continue daily observe runs until data-sufficient days reach --min-days, then re-judge.",
    }


def test_fail_on_simmer_ab_interim_no_go_wait_when_not_reached(tmp_path: Path, monkeypatch, capsys):
    snapshot = tmp_path / "snapshot.json"
    simmer = tmp_path / "simmer.json"
    _write_json(snapshot, _base_snapshot_payload())
    _write_json(simmer, _base_simmer_payload())

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_morning_status.py",
            "--no-refresh",
            "--skip-health",
            "--skip-uncorrelated-portfolio",
            "--skip-gate-alarm",
            "--skip-implementation-ledger",
            "--snapshot-json",
            str(snapshot),
            "--simmer-ab-decision-json",
            str(simmer),
            "--fail-on-simmer-ab-interim-no-go",
            "--simmer-ab-interim-target",
            "7d",
        ],
    )

    rc = mod.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "simmer_ab_interim_gate=WAIT target=7d decision=PENDING days=1/7" in out


def test_fail_on_simmer_ab_interim_no_go_returns_7_when_reached_but_not_go(tmp_path: Path, monkeypatch, capsys):
    snapshot = tmp_path / "snapshot.json"
    simmer = tmp_path / "simmer.json"
    _write_json(snapshot, _base_snapshot_payload())
    payload = _base_simmer_payload()
    payload["summary"]["interim_milestones"]["tentative_7d"] = {
        "target_days": 7,
        "data_sufficient_days": 7,
        "remaining_days": 0,
        "reached": True,
        "conditions_pass": False,
        "decision": "NO_GO",
    }
    _write_json(simmer, payload)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_morning_status.py",
            "--no-refresh",
            "--skip-health",
            "--skip-uncorrelated-portfolio",
            "--skip-gate-alarm",
            "--skip-implementation-ledger",
            "--snapshot-json",
            str(snapshot),
            "--simmer-ab-decision-json",
            str(simmer),
            "--fail-on-simmer-ab-interim-no-go",
            "--simmer-ab-interim-target",
            "7d",
        ],
    )

    rc = mod.main()
    out = capsys.readouterr().out

    assert rc == 7
    assert "simmer_ab_interim_gate=FAIL target=7d decision=NO_GO days=7/7" in out


def test_fail_on_simmer_ab_interim_no_go_pass_when_reached_and_go_for_14d(tmp_path: Path, monkeypatch, capsys):
    snapshot = tmp_path / "snapshot.json"
    simmer = tmp_path / "simmer.json"
    _write_json(snapshot, _base_snapshot_payload())
    payload = _base_simmer_payload()
    payload["summary"]["interim_milestones"]["intermediate_14d"] = {
        "target_days": 14,
        "data_sufficient_days": 14,
        "remaining_days": 0,
        "reached": True,
        "conditions_pass": True,
        "decision": "GO",
    }
    _write_json(simmer, payload)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_morning_status.py",
            "--no-refresh",
            "--skip-health",
            "--skip-uncorrelated-portfolio",
            "--skip-gate-alarm",
            "--skip-implementation-ledger",
            "--snapshot-json",
            str(snapshot),
            "--simmer-ab-decision-json",
            str(simmer),
            "--fail-on-simmer-ab-interim-no-go",
            "--simmer-ab-interim-target",
            "14d",
        ],
    )

    rc = mod.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "simmer_ab_interim_gate=PASS target=14d decision=GO days=14/14" in out
