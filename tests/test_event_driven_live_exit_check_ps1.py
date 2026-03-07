from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _script_text_lower(name: str) -> str:
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    return path.read_text(encoding="utf-8", errors="replace").lower()


def _powershell_exe() -> str:
    pwsh = shutil.which("powershell") or shutil.which("pwsh")
    assert pwsh, "PowerShell executable not found"
    return pwsh


def test_run_event_driven_live_exit_check_ps1_exposes_help_and_no_background_guard():
    text = _script_text_lower("run_event_driven_live_exit_check.ps1")

    assert '[alias("h")]' in text
    assert "[switch]$help" in text
    assert "function show-usage" in text
    assert "if (-not $background -and -not $nobackground)" in text
    assert "--exit-only" in text
    assert "--execute" in text


def test_install_event_driven_live_exit_check_task_ps1_targets_runner_and_60m_defaults():
    text = _script_text_lower("install_event_driven_live_exit_check_task.ps1")

    assert "eventdrivenliveexitcheck60m" in text
    assert "[int]$intervalminutes = 60" in text
    assert "scripts\\run_event_driven_live_exit_check.ps1" in text
    assert "-nobackground" in text
    assert "-liveexecute" in text


@pytest.mark.skipif(os.name != "nt", reason="Windows-only PowerShell integration test")
def test_run_event_driven_live_exit_check_ps1_help_does_not_background():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_event_driven_live_exit_check.ps1"
    cmd = [
        _powershell_exe(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-Help",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    out = (res.stdout or "") + (res.stderr or "")
    assert res.returncode == 0, out
    assert "Usage:" in out
    assert "Started in background:" not in out


@pytest.mark.skipif(os.name != "nt", reason="Windows-only PowerShell integration test")
def test_run_event_driven_live_exit_check_ps1_runs_foreground_with_stub_runner(tmp_path: Path):
    repo_root = tmp_path / "repo"
    scripts_dir = repo_root / "scripts"
    logs_dir = repo_root / "logs"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    runner_stub = scripts_dir / "execute_event_driven_live.py"
    runner_stub.write_text(
        "\n".join(
            [
                "import argparse, json",
                "from pathlib import Path",
                "p = argparse.ArgumentParser()",
                "p.add_argument('--signals-file')",
                "p.add_argument('--state-file')",
                "p.add_argument('--exec-log-file')",
                "p.add_argument('--log-file')",
                "p.add_argument('--win-threshold')",
                "p.add_argument('--lose-threshold')",
                "p.add_argument('--api-timeout-sec')",
                "p.add_argument('--confirm-live')",
                "p.add_argument('--execute', action='store_true')",
                "p.add_argument('--exit-only', action='store_true')",
                "p.add_argument('--pretty', action='store_true')",
                "a = p.parse_args()",
                "payload = vars(a)",
                "root = Path(__file__).resolve().parents[1] / 'logs'",
                "root.mkdir(parents=True, exist_ok=True)",
                "(root / 'event_driven_live_exit_stub_args.json').write_text(json.dumps(payload), encoding='utf-8')",
                "Path(a.state_file).write_text(json.dumps({'ok': True, 'mode': 'LIVE_EXIT_ONLY'}), encoding='utf-8')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    run_script = Path(__file__).resolve().parents[1] / "scripts" / "run_event_driven_live_exit_check.ps1"
    cmd = [
        _powershell_exe(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(run_script),
        "-NoBackground",
        "-RepoRoot",
        str(repo_root),
        "-PythonExe",
        sys.executable,
        "-LiveExecute",
        "-LiveConfirm",
        "YES",
        "-WinThreshold",
        "0.99",
        "-LoseThreshold",
        "0.01",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = (res.stdout or "") + (res.stderr or "")
    assert res.returncode == 0, out
    assert "Started in background:" not in out

    run_log = logs_dir / "event_driven_live_exit_check.log"
    assert run_log.exists()
    log_text = run_log.read_text(encoding="utf-8", errors="replace")
    assert "start live_execute=True" in log_text
    assert "done" in log_text

    payload = json.loads((logs_dir / "event_driven_live_exit_stub_args.json").read_text(encoding="utf-8"))
    assert payload["execute"] is True
    assert payload["exit_only"] is True
    assert payload["confirm_live"] == "YES"
