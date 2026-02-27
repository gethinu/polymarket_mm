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


def test_mode_off_stops_matching_daemon_process_and_updates_config(tmp_path: Path):
    repo_root = tmp_path / "repo"
    cfg_dir = repo_root / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "bot_supervisor.observe.json"
    cfg_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {"name": "no_longshot_daily_daemon", "enabled": True},
                ]
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )

    daemon_proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(120)",
            "no_longshot_daily_daemon.py",
            str(repo_root),
        ]
    )
    try:
        script_path = Path(__file__).resolve().parents[1] / "scripts" / "set_no_longshot_daily_mode.ps1"
        cmd = [
            _powershell_exe(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-NoBackground",
            "-Mode",
            "off",
            "-RepoRoot",
            str(repo_root),
            "-ConfigFile",
            "configs/bot_supervisor.observe.json",
            "-TaskName",
            "__MissingNoLongshotTask__",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"
        daemon_proc.wait(timeout=10)
        out = (res.stdout or "") + (res.stderr or "")
        assert "daemon_processes_stopped=" in out

        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        job = next(j for j in cfg.get("jobs", []) if str(j.get("name")) == "no_longshot_daily_daemon")
        assert bool(job.get("enabled")) is False
    finally:
        if daemon_proc.poll() is None:
            daemon_proc.kill()
