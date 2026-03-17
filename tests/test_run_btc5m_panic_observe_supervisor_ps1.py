from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-only PowerShell integration test")


def _powershell_exe() -> str:
    pwsh = shutil.which("powershell") or shutil.which("pwsh")
    assert pwsh, "PowerShell executable not found"
    return pwsh


def test_run_btc5m_panic_observe_supervisor_runs_dummy_config(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    config_path = tmp_path / "panic-supervisor.json"
    log_path = tmp_path / "panic-supervisor.log"
    state_path = tmp_path / "panic-supervisor-state.json"
    lock_path = tmp_path / "panic-supervisor.lock"

    config_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "dummy",
                        "enabled": True,
                        "command": [
                            sys.executable,
                            "-c",
                            "import time; time.sleep(0.5)",
                        ],
                        "restart": "never",
                    }
                ]
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )

    script_path = repo_root / "scripts" / "run_btc5m_panic_observe_supervisor.ps1"
    cmd = [
        _powershell_exe(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-NoBackground",
        "-RepoRoot",
        str(repo_root),
        "-PythonExe",
        sys.executable,
        "-ConfigFile",
        str(config_path),
        "-LogFile",
        str(log_path),
        "-StateFile",
        str(state_path),
        "-LockFile",
        str(lock_path),
        "-RunSeconds",
        "1",
    ]

    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"
    assert log_path.exists()
    assert state_path.exists()
    assert not lock_path.exists()

    log_text = log_path.read_text(encoding="utf-8")
    assert "start config=" in log_text
    assert "end exit_code=0" in log_text

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["mode"] == "stopped"
    assert state["supervisor_running"] is False
    assert state["jobs"][0]["name"] == "dummy"
