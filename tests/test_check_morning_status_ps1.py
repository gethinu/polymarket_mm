from __future__ import annotations

from pathlib import Path


def _script_text_lower() -> str:
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_morning_status.ps1"
    return path.read_text(encoding="utf-8", errors="replace").lower()


def test_check_morning_status_ps1_exposes_simmer_interim_params():
    text = _script_text_lower()

    assert '[validateset("7d", "14d")]' in text
    assert '[string]$simmerabinterimtarget = "7d"' in text
    assert "[switch]$failonsimmerabinterimnogo" in text


def test_check_morning_status_ps1_forwards_simmer_interim_args_to_python():
    text = _script_text_lower()

    assert '"--simmer-ab-interim-target", $simmerabinterimtarget' in text
    assert '--fail-on-simmer-ab-interim-no-go' in text
