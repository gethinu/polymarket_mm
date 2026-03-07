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


def test_run_fade_regime_staged_checks_ps1_exposes_help_and_no_background_guard():
    text = _script_text_lower("run_fade_regime_staged_checks.ps1")

    assert '[alias("h")]' in text
    assert "[switch]$help" in text
    assert "function show-usage" in text
    assert "if ($help.ispresent -or ($invline -match" in text
    assert "if (-not $background -and -not $nobackground)" in text


def test_run_fade_regime_staged_checks_ps1_forwards_control_self_switches():
    text = _script_text_lower("run_fade_regime_staged_checks.ps1")

    assert "--no-control-self-for-control-arm" in text
    assert "--control-self-for-control-arm" in text
    assert "control_self={3}" in text


def test_install_fade_regime_staged_checks_task_ps1_enforces_runner_and_no_background():
    text = _script_text_lower("install_fade_regime_staged_checks_task.ps1")

    assert "scripts\\run_fade_regime_staged_checks.ps1" in text
    assert "-nobackground" in text
    assert "-nocontrolselfforcontrolarm" in text
    assert "-refreshstrategysnapshot" in text


def _powershell_exe() -> str:
    pwsh = shutil.which("powershell") or shutil.which("pwsh")
    assert pwsh, "PowerShell executable not found"
    return pwsh


@pytest.mark.skipif(os.name != "nt", reason="Windows-only PowerShell integration test")
def test_run_fade_regime_staged_checks_ps1_help_does_not_background():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_fade_regime_staged_checks.ps1"
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
def test_run_fade_regime_staged_checks_ps1_runs_foreground_with_stub_runner(tmp_path: Path):
    repo_root = tmp_path / "repo"
    scripts_dir = repo_root / "scripts"
    logs_dir = repo_root / "logs"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    runner_stub = scripts_dir / "run_fade_regime_staged_checks.py"
    runner_stub.write_text(
        "\n".join(
            [
                "import argparse, json",
                "from pathlib import Path",
                "p = argparse.ArgumentParser()",
                "p.add_argument('--hours')",
                "p.add_argument('--metric-scope')",
                "p.add_argument('--tail-bytes')",
                "p.add_argument('--supervisor-state-file')",
                "p.add_argument('--control-arm-id')",
                "p.add_argument('--out-json')",
                "p.add_argument('--out-txt')",
                "p.add_argument('--control-self-for-control-arm', dest='control_self', action='store_true')",
                "p.add_argument('--no-control-self-for-control-arm', dest='control_self', action='store_false')",
                "p.set_defaults(control_self=None)",
                "a = p.parse_args()",
                "out_json = Path(a.out_json)",
                "out_txt = Path(a.out_txt)",
                "out_json.parent.mkdir(parents=True, exist_ok=True)",
                "out_txt.parent.mkdir(parents=True, exist_ok=True)",
                "payload = {'ok': True, 'args': vars(a)}",
                "out_json.write_text(json.dumps(payload), encoding='utf-8')",
                "out_txt.write_text('stub summary\\n', encoding='utf-8')",
                "(Path(__file__).resolve().parents[1] / 'logs' / 'runner_args.json').write_text(json.dumps(payload), encoding='utf-8')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot_stub = scripts_dir / "render_strategy_register_snapshot.py"
    snapshot_stub.write_text(
        "\n".join(
            [
                "import json, sys",
                "from pathlib import Path",
                "p = Path(__file__).resolve().parents[1] / 'logs' / 'snapshot_marker.json'",
                "p.parent.mkdir(parents=True, exist_ok=True)",
                "p.write_text(json.dumps({'argv': sys.argv[1:]}), encoding='utf-8')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    run_script = Path(__file__).resolve().parents[1] / "scripts" / "run_fade_regime_staged_checks.ps1"
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
        "-Hours",
        "12",
        "-MetricScope",
        "since_baseline",
        "-TailBytes",
        "4096",
        "-SupervisorStateFile",
        "logs/fade_supervisor_state.json",
        "-ControlArmId",
        "regime_long_strict",
        "-NoControlSelfForControlArm",
        "-RefreshStrategySnapshot",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = (res.stdout or "") + (res.stderr or "")
    assert res.returncode == 0, out
    assert "Started in background:" not in out

    run_log = logs_dir / "fade_regime_staged_checks_run.log"
    assert run_log.exists()
    log_text = run_log.read_text(encoding="utf-8", errors="replace")
    assert "start hours=12 metric_scope=since_baseline control_arm=regime_long_strict control_self=False" in log_text
    assert "batch_check done ->" in log_text
    assert "strategy_snapshot done -> logs/strategy_register_latest.json" in log_text

    args_payload = json.loads((logs_dir / "runner_args.json").read_text(encoding="utf-8"))
    args = args_payload["args"]
    assert args["hours"] == "12"
    assert args["metric_scope"] == "since_baseline"
    assert args["tail_bytes"] == "4096"
    assert args["control_arm_id"] == "regime_long_strict"
    assert args["control_self"] is False
    assert Path(args["supervisor_state_file"]) == (repo_root / "logs" / "fade_supervisor_state.json")
    assert Path(args["out_json"]) == (repo_root / "logs" / "fade_regime_staged_decision_latest.json")
    assert Path(args["out_txt"]) == (repo_root / "logs" / "fade_regime_staged_decision_latest.txt")

    marker = json.loads((logs_dir / "snapshot_marker.json").read_text(encoding="utf-8"))
    assert "--pretty" in marker["argv"]
