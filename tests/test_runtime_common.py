from __future__ import annotations

import json

from lib.runtime_common import (
    backup_corrupt_file,
    env_bool,
    parse_iso_or_epoch_to_ms,
    write_json_atomic,
)


def test_write_json_atomic_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    write_json_atomic(p, {"a": 1, "b": "x"})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "b": "x"}
    # overwrite is atomic and leaves no .tmp behind
    write_json_atomic(p, {"a": 2})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 2}
    assert not (tmp_path / "state.json.tmp").exists()


def test_backup_corrupt_file_preserves_then_returns_path(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not valid json", encoding="utf-8")
    bak = backup_corrupt_file(p)
    assert bak is not None and bak.exists()
    assert bak.read_text(encoding="utf-8") == "{not valid json"


def test_backup_corrupt_file_none_when_missing(tmp_path):
    assert backup_corrupt_file(tmp_path / "nope.json") is None


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

