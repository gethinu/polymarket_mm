from __future__ import annotations

import report_automation_health as mod


def test_default_tasks_include_simmer_ab_daily_report():
    assert "SimmerABDailyReport" in mod.DEFAULT_TASKS


def test_default_optional_tasks_include_wallet_autopsy_daily_report():
    assert "WalletAutopsyDailyReport" in mod.DEFAULT_OPTIONAL_TASKS


def test_parse_artifact_specs_supports_optional_prefix():
    rows = mod._parse_artifact_specs(
        [
            "?logs/simmer-ab-daily-report.log:12",
            "logs/strategy_register_latest.json:30",
        ]
    )
    assert rows[0] == ("logs/simmer-ab-daily-report.log", 12.0, True)
    assert rows[1] == ("logs/strategy_register_latest.json", 30.0, False)


def test_default_artifacts_include_optional_simmer_ab_supervisor_state():
    assert "?logs/simmer_ab_supervisor_state.json:6" in mod.DEFAULT_ARTIFACT_SPECS


def test_parse_task_specs_supports_optional_prefix_and_dedupes():
    rows = mod._parse_task_specs(
        [
            "?WalletAutopsyDailyReport",
            "NoLongshotDailyReport",
            "NoLongshotDailyReport",
        ]
    )
    assert rows == [
        ("WalletAutopsyDailyReport", False),
        ("NoLongshotDailyReport", True),
    ]


def test_task_status_optional_missing_for_optional_task():
    row = {"exists": False, "required": False}
    assert mod._task_status(row) == "OPTIONAL_MISSING"


def test_soft_fail_override_for_simmer_daily_report():
    task_rows = [
        {
            "task_name": "SimmerABDailyReport",
            "status": "LAST_RUN_FAILED",
            "last_task_result": 3221225786,
        }
    ]
    artifact_rows = [
        {
            "path": r"C:\Repos\polymarket_mm\logs\simmer-ab-daily-report.log",
            "status": "FRESH",
        }
    ]

    mod._apply_soft_fail_overrides(task_rows, artifact_rows)

    assert task_rows[0]["status"] == "SOFT_FAIL_INTERRUPTED"
    assert "simmer runner log is fresh" in str(task_rows[0].get("status_note") or "")


def test_soft_fail_override_for_weather_top30_with_267014():
    task_rows = [
        {
            "task_name": "WeatherTop30ReadinessDaily",
            "status": "LAST_RUN_FAILED",
            "last_task_result": 267014,
        }
    ]
    artifact_rows = [
        {
            "path": r"C:\Repos\polymarket_mm\logs\weather_top30_readiness_report_latest.json",
            "status": "FRESH",
        }
    ]

    mod._apply_soft_fail_overrides(task_rows, artifact_rows)

    assert task_rows[0]["status"] == "SOFT_FAIL_INTERRUPTED"
    assert "top30 artifacts/log are fresh" in str(task_rows[0].get("status_note") or "")


def test_soft_fail_override_for_wallet_autopsy_daily_report():
    task_rows = [
        {
            "task_name": "WalletAutopsyDailyReport",
            "status": "LAST_RUN_FAILED",
            "last_task_result": 267014,
        }
    ]
    artifact_rows = [
        {
            "path": r"C:\Repos\polymarket_mm\logs\wallet_autopsy_daily_run.log",
            "status": "FRESH",
        }
    ]

    mod._apply_soft_fail_overrides(task_rows, artifact_rows)

    assert task_rows[0]["status"] == "SOFT_FAIL_INTERRUPTED"
    assert "wallet autopsy runner log is fresh" in str(task_rows[0].get("status_note") or "")


def test_duplicate_run_guard_marks_no_longshot_conflict(monkeypatch):
    task_rows = [
        {
            "task_name": "NoLongshotDailyReport",
            "status": "OK",
        }
    ]
    monkeypatch.setattr(
        mod,
        "_is_supervisor_job_enabled",
        lambda name: name == "no_longshot_daily_daemon",
    )

    mod._apply_duplicate_run_guard(task_rows)

    assert task_rows[0]["status"] == "DUPLICATE_RUN_RISK"
    assert "no_longshot_daily_daemon is enabled" in str(task_rows[0].get("status_note") or "")


def test_duplicate_run_guard_skips_when_task_is_suppressed(monkeypatch):
    task_rows = [
        {
            "task_name": "NoLongshotDailyReport",
            "status": "SUPPRESSED_BY_SUPERVISOR",
        }
    ]
    monkeypatch.setattr(
        mod,
        "_is_supervisor_job_enabled",
        lambda name: name == "no_longshot_daily_daemon",
    )

    mod._apply_duplicate_run_guard(task_rows)

    assert task_rows[0]["status"] == "SUPPRESSED_BY_SUPERVISOR"


def test_apply_morning_kpi_marker_check_marks_invalid_when_missing(tmp_path):
    p = tmp_path / "logs" / "morning_status_daily_run.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[2026-02-27 08:00:00] done\n", encoding="utf-8")
    rows = [
        {
            "path": str(p),
            "status": "FRESH",
            "required": True,
        }
    ]

    mod._apply_morning_kpi_marker_check(rows)

    assert rows[0]["status"] == "INVALID_CONTENT"
    assert "missing marker" in str(rows[0].get("status_note") or "")


def test_apply_morning_kpi_marker_check_keeps_fresh_when_marker_present(tmp_path):
    p = tmp_path / "logs" / "morning_status_daily_run.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "[2026-02-27 08:00:00] kpi[post] no_longshot.monthly_return_now_text=+5.43%\n",
        encoding="utf-8",
    )
    rows = [
        {
            "path": str(p),
            "status": "FRESH",
            "required": True,
        }
    ]

    mod._apply_morning_kpi_marker_check(rows)

    assert rows[0]["status"] == "FRESH"


def test_apply_strategy_register_kpi_key_check_marks_invalid_when_missing(tmp_path):
    p = tmp_path / "logs" / "strategy_register_latest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        """{
  "no_longshot_status": {
    "monthly_return_now_text": "+5.43%"
  },
  "realized_30d_gate": {}
}
""",
        encoding="utf-8",
    )
    rows = [
        {
            "path": str(p),
            "status": "FRESH",
            "required": True,
        }
    ]

    mod._apply_strategy_register_kpi_key_check(rows)

    assert rows[0]["status"] == "INVALID_CONTENT"
    assert "missing_or_empty_keys=" in str(rows[0].get("status_note") or "")


def test_apply_strategy_register_kpi_key_check_keeps_fresh_when_keys_present(tmp_path):
    p = tmp_path / "logs" / "strategy_register_latest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        """{
  "no_longshot_status": {
    "monthly_return_now_text": "+5.43%",
    "monthly_return_now_source": "realized_rolling_30d_new_condition",
    "monthly_return_now_new_condition_text": "+5.43%",
    "monthly_return_now_all_text": "-19.90%"
  },
  "realized_30d_gate": {
    "decision": "PENDING_30D"
  }
}
""",
        encoding="utf-8",
    )
    rows = [
        {
            "path": str(p),
            "status": "FRESH",
            "required": True,
        }
    ]

    mod._apply_strategy_register_kpi_key_check(rows)

    assert rows[0]["status"] == "FRESH"


def test_apply_strategy_register_kpi_key_check_marks_invalid_when_source_is_not_realized(tmp_path):
    p = tmp_path / "logs" / "strategy_register_latest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        """{
  "no_longshot_status": {
    "monthly_return_now_text": "+4.20%",
    "monthly_return_now_source": "guarded_oos_annualized_fallback",
    "monthly_return_now_new_condition_text": "+4.20%",
    "monthly_return_now_all_text": "+3.10%"
  },
  "realized_30d_gate": {
    "decision": "PENDING_30D"
  }
}
""",
        encoding="utf-8",
    )
    rows = [
        {
            "path": str(p),
            "status": "FRESH",
            "required": True,
        }
    ]

    mod._apply_strategy_register_kpi_key_check(rows)

    assert rows[0]["status"] == "INVALID_CONTENT"
    assert "invalid_monthly_source=" in str(rows[0].get("status_note") or "")


def test_apply_simmer_ab_supervisor_state_check_keeps_fresh_when_alive(tmp_path, monkeypatch):
    p = tmp_path / "logs" / "simmer_ab_supervisor_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        """{
  "mode": "run",
  "supervisor_running": true,
  "supervisor_pid": 100,
  "jobs": [
    {"name":"simmer_ab_baseline","enabled":true,"running":true,"pid":101},
    {"name":"simmer_ab_candidate","enabled":true,"running":true,"pid":102}
  ]
}
""",
        encoding="utf-8",
    )
    rows = [
        {
            "path": str(p),
            "status": "FRESH",
            "required": False,
        }
    ]
    monkeypatch.setattr(mod, "_pid_running", lambda pid: int(pid) in {100, 101, 102})

    mod._apply_simmer_ab_supervisor_state_check(rows)

    assert rows[0]["status"] == "FRESH"


def test_apply_simmer_ab_supervisor_state_check_marks_invalid_when_supervisor_pid_dead(tmp_path, monkeypatch):
    p = tmp_path / "logs" / "simmer_ab_supervisor_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        """{
  "mode": "run",
  "supervisor_running": true,
  "supervisor_pid": 999,
  "jobs": [{"name":"simmer_ab_baseline","enabled":true,"running":true,"pid":101}]
}
""",
        encoding="utf-8",
    )
    rows = [
        {
            "path": str(p),
            "status": "FRESH",
            "required": False,
        }
    ]
    monkeypatch.setattr(mod, "_pid_running", lambda _pid: False)

    mod._apply_simmer_ab_supervisor_state_check(rows)

    assert rows[0]["status"] == "INVALID_CONTENT"
    assert "supervisor_pid_not_running:" in str(rows[0].get("status_note") or "")


def test_apply_simmer_ab_supervisor_state_check_marks_invalid_when_enabled_job_not_running(tmp_path, monkeypatch):
    p = tmp_path / "logs" / "simmer_ab_supervisor_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        """{
  "mode": "run",
  "supervisor_running": true,
  "supervisor_pid": 100,
  "jobs": [{"name":"simmer_ab_baseline","enabled":true,"running":false,"pid":101}]
}
""",
        encoding="utf-8",
    )
    rows = [
        {
            "path": str(p),
            "status": "FRESH",
            "required": False,
        }
    ]
    monkeypatch.setattr(mod, "_pid_running", lambda _pid: True)

    mod._apply_simmer_ab_supervisor_state_check(rows)

    assert rows[0]["status"] == "INVALID_CONTENT"
    assert "job_not_running:simmer_ab_baseline" in str(rows[0].get("status_note") or "")
