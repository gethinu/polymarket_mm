from __future__ import annotations

from lib.runtime_common import env_bool, parse_iso_or_epoch_to_ms


def test_env_bool_parsing(monkeypatch):
    monkeypatch.setenv("UT_BOOL", "yes")
    assert env_bool("UT_BOOL") is True
    monkeypatch.setenv("UT_BOOL", "0")
    assert env_bool("UT_BOOL") is False
    monkeypatch.setenv("UT_BOOL", "maybe")
    assert env_bool("UT_BOOL") is None


def test_parse_iso_or_epoch_to_ms():
    assert parse_iso_or_epoch_to_ms(1704067200) == 1704067200000
    assert parse_iso_or_epoch_to_ms("2026-02-25T00:00:00Z") == 1771977600000
    assert parse_iso_or_epoch_to_ms("invalid") is None

