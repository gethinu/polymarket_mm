from __future__ import annotations

from pathlib import Path


def _script_text_lower(name: str) -> str:
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    return path.read_text(encoding="utf-8", errors="replace").lower()


def test_run_morning_status_daily_ps1_exposes_and_forwards_simmer_interim_flags():
    text = _script_text_lower("run_morning_status_daily.ps1")

    assert '[validateset("7d", "14d")]' in text
    assert '[string]$simmerabinterimtarget = "7d"' in text
    assert "[switch]$failonsimmerabinterimnogo" in text
    assert '"--simmer-ab-interim-target", $simmerabinterimtarget' in text
    assert '--fail-on-simmer-ab-interim-no-go' in text


def test_install_morning_status_daily_task_ps1_exposes_and_forwards_simmer_interim_flags():
    text = _script_text_lower("install_morning_status_daily_task.ps1")

    assert '[validateset("7d", "14d")]' in text
    assert '[string]$simmerabinterimtarget = "7d"' in text
    assert "[switch]$failonsimmerabinterimnogo" in text
    assert '"-simmerabinterimtarget", $simmerabinterimtarget' in text
    assert "-failonsimmerabinterimnogo" in text
    assert "enforce interim fail gate by default" in text
    assert "if ($failonsimmerabinterimnogo.ispresent)" not in text
