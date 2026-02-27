from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import no_longshot_daily_daemon as mod


class _Logger:
    def __init__(self) -> None:
        self.messages = []

    def info(self, msg: str) -> None:
        self.messages.append(str(msg))


def _mk_run_args(repo_root: Path, script_path: str, **overrides):
    base = {
        "repo_root": str(repo_root),
        "script_path": script_path,
        "powershell_exe": "powershell.exe",
        "runner_realized_fast_max_pages": 120,
        "runner_realized_fast_yes_min": 0.16,
        "runner_realized_fast_yes_max": 0.20,
        "runner_realized_fast_max_hours_to_end": 72.0,
        "runner_gap_outcome_tag": "prod",
        "runner_gap_error_alert_rate_7d": 0.2,
        "runner_gap_error_alert_min_runs_7d": 5,
        "skip_refresh": True,
        "discord": False,
        "runner_fail_on_gap_scan_error": True,
        "runner_fail_on_gap_error_rate_high": True,
        "max_run_seconds": 1800,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _mk_main_args(tmp_path: Path, **overrides):
    base = {
        "repo_root": str(tmp_path),
        "script_path": "scripts/run_no_longshot_daily_report.ps1",
        "powershell_exe": "powershell.exe",
        "run_at_hhmm": "00:05",
        "poll_sec": 15.0,
        "retry_delay_sec": 900.0,
        "max_run_seconds": 1800,
        "run_seconds": 0,
        "max_consecutive_failures": 6,
        "run_on_start": False,
        "skip_refresh": True,
        "discord": False,
        "runner_realized_fast_max_pages": 120,
        "runner_realized_fast_yes_min": 0.16,
        "runner_realized_fast_yes_max": 0.20,
        "runner_realized_fast_max_hours_to_end": 72.0,
        "runner_gap_outcome_tag": "prod",
        "runner_gap_error_alert_rate_7d": 0.2,
        "runner_gap_error_alert_min_runs_7d": 5,
        "runner_fail_on_gap_scan_error": False,
        "runner_fail_on_gap_error_rate_high": False,
        "python_exe": "python",
        "realized_refresh_sec": 0.0,
        "realized_timeout_sec": 240.0,
        "realized_tool_path": "scripts/record_no_longshot_realized_daily.py",
        "realized_screen_csv": "logs/no_longshot_fast_screen_lowyes_latest.csv",
        "realized_positions_json": "logs/no_longshot_forward_positions.json",
        "realized_out_daily_jsonl": "logs/no_longshot_realized_daily.jsonl",
        "realized_out_latest_json": "logs/no_longshot_realized_latest.json",
        "realized_out_monthly_txt": "logs/no_longshot_monthly_return_latest.txt",
        "realized_entry_top_n": 0,
        "realized_per_trade_cost": 0.002,
        "realized_api_timeout_sec": 20.0,
        "allow_realized_entry_ingest": False,
        "log_file": str(tmp_path / "logs" / "daemon.log"),
        "state_file": str(tmp_path / "logs" / "daemon_state.json"),
        "lock_file": str(tmp_path / "logs" / "daemon.lock"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_run_daily_once_passes_gap_guard_flags(monkeypatch, tmp_path: Path):
    repo_root = tmp_path
    script_path = repo_root / "scripts" / "run_no_longshot_daily_report.ps1"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("# test\n", encoding="utf-8")

    args = _mk_run_args(repo_root, "scripts/run_no_longshot_daily_report.ps1")
    logger = _Logger()
    captured = {}

    class _Proc:
        pid = 123

        @staticmethod
        def poll():
            return 0

    def _fake_popen(cmd, cwd, stdout, stderr, shell):
        captured["cmd"] = list(cmd)
        captured["cwd"] = cwd
        captured["shell"] = shell
        return _Proc()

    monkeypatch.setattr(mod.subprocess, "Popen", _fake_popen)

    rc, reason = mod.run_daily_once(args, logger)
    assert rc == 0
    assert reason == ""
    cmd = captured["cmd"]
    assert "-GapOutcomeTag" in cmd and "prod" in cmd
    assert "-GapErrorAlertRate7d" in cmd and "0.2" in cmd
    assert "-GapErrorAlertMinRuns7d" in cmd and "5" in cmd
    assert "-FailOnGapScanError" in cmd
    assert "-FailOnGapErrorRateHigh" in cmd
    assert captured["cwd"] == str(repo_root.resolve())
    assert captured["shell"] is False


def test_run_daily_once_omits_optional_flags_when_disabled(monkeypatch, tmp_path: Path):
    repo_root = tmp_path
    script_path = repo_root / "scripts" / "run_no_longshot_daily_report.ps1"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("# test\n", encoding="utf-8")

    args = _mk_run_args(
        repo_root,
        "scripts/run_no_longshot_daily_report.ps1",
        skip_refresh=False,
        discord=False,
        runner_fail_on_gap_scan_error=False,
        runner_fail_on_gap_error_rate_high=False,
    )
    captured = {}

    class _Proc:
        pid = 1

        @staticmethod
        def poll():
            return 0

    def _fake_popen(cmd, cwd, stdout, stderr, shell):
        captured["cmd"] = list(cmd)
        return _Proc()

    monkeypatch.setattr(mod.subprocess, "Popen", _fake_popen)
    rc, _ = mod.run_daily_once(args, _Logger())
    assert rc == 0
    cmd = captured["cmd"]
    assert "-SkipRefresh" not in cmd
    assert "-Discord" not in cmd
    assert "-FailOnGapScanError" not in cmd
    assert "-FailOnGapErrorRateHigh" not in cmd


def test_main_refuses_invalid_gap_error_alert_rate(monkeypatch, tmp_path: Path):
    args = _mk_main_args(tmp_path, runner_gap_error_alert_rate_7d=1.5)
    monkeypatch.setattr(mod, "parse_args", lambda: args)
    assert mod.main() == 2


def test_main_refuses_invalid_gap_error_alert_min_runs(monkeypatch, tmp_path: Path):
    args = _mk_main_args(tmp_path, runner_gap_error_alert_min_runs_7d=0)
    monkeypatch.setattr(mod, "parse_args", lambda: args)
    assert mod.main() == 2


def test_main_refuses_invalid_gap_outcome_tag(monkeypatch, tmp_path: Path):
    args = _mk_main_args(tmp_path, runner_gap_outcome_tag="bad tag")
    monkeypatch.setattr(mod, "parse_args", lambda: args)
    assert mod.main() == 2
