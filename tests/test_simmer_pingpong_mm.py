from __future__ import annotations

import json

import simmer_pingpong_mm as mod


def test_should_send_discord_summary_dedupes_within_window(tmp_path, monkeypatch):
    state_file = tmp_path / "simmer_discord_summary_dedupe_state.json"

    monkeypatch.setattr(mod, "now_ts", lambda: 1000.0)
    assert mod._should_send_discord_summary(
        "SIMMER_PONG summary: pnl_today=+0.00 total=+14.00",
        state_file=state_file,
        dedupe_window_sec=55.0,
    )

    monkeypatch.setattr(mod, "now_ts", lambda: 1030.0)
    assert not mod._should_send_discord_summary(
        "SIMMER_PONG summary: pnl_today=+0.00 total=+14.00",
        state_file=state_file,
        dedupe_window_sec=55.0,
    )

    monkeypatch.setattr(mod, "now_ts", lambda: 1060.0)
    assert mod._should_send_discord_summary(
        "SIMMER_PONG summary: pnl_today=+0.00 total=+14.00",
        state_file=state_file,
        dedupe_window_sec=55.0,
    )


def test_should_send_discord_summary_bypasses_non_summary(tmp_path):
    state_file = tmp_path / "simmer_discord_summary_dedupe_state.json"
    assert mod._should_send_discord_summary(
        "SIMMER_PONG started (observe)",
        state_file=state_file,
        dedupe_window_sec=55.0,
    )
    assert not state_file.exists()


def test_should_send_discord_summary_recovers_from_invalid_state(tmp_path, monkeypatch):
    state_file = tmp_path / "simmer_discord_summary_dedupe_state.json"
    state_file.write_text("{invalid-json", encoding="utf-8")

    monkeypatch.setattr(mod, "now_ts", lambda: 2000.0)
    assert mod._should_send_discord_summary(
        "SIMMER_PONG summary: pnl_today=+2.50 total=+4.99",
        state_file=state_file,
        dedupe_window_sec=55.0,
    )

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert isinstance(saved, dict)
    assert len(saved) == 1

