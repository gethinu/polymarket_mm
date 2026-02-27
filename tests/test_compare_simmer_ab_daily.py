from __future__ import annotations

import json
from pathlib import Path

import compare_simmer_ab_daily as mod


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = str(line or "").strip()
        if not s:
            continue
        out.append(json.loads(s))
    return out


def test_append_history_upserts_same_window_and_keeps_other_windows(tmp_path):
    p = tmp_path / "history.jsonl"
    p.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "since": "2026-02-25 00:00:00",
                        "until": "2026-02-26 00:00:00",
                        "ts": "2026-02-26 00:01:00",
                        "decision": "INSUFFICIENT",
                    }
                ),
                json.dumps(
                    {
                        "since": "2026-02-25 00:00:00",
                        "until": "2026-02-26 00:00:00",
                        "ts": "2026-02-26 00:02:00",
                        "decision": "FAIL",
                    }
                ),
                json.dumps(
                    {
                        "since": "2026-02-26 00:00:00",
                        "until": "2026-02-27 00:00:00",
                        "ts": "2026-02-27 00:02:00",
                        "decision": "PASS",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    mod._append_history(
        str(p),
        {
            "since": "2026-02-25 00:00:00",
            "until": "2026-02-26 00:00:00",
            "ts": "2026-02-26 00:03:00",
            "decision": "PASS",
        },
    )

    rows = _read_jsonl(p)
    assert len(rows) == 2

    by_window = {(r["since"], r["until"]): r for r in rows}
    assert by_window[("2026-02-25 00:00:00", "2026-02-26 00:00:00")]["decision"] == "PASS"
    assert by_window[("2026-02-26 00:00:00", "2026-02-27 00:00:00")]["decision"] == "PASS"


def test_append_history_handles_payload_without_window_key(tmp_path):
    p = tmp_path / "history.jsonl"
    p.write_text(
        json.dumps(
            {
                "since": "2026-02-26 00:00:00",
                "until": "2026-02-27 00:00:00",
                "ts": "2026-02-27 00:02:00",
                "decision": "PASS",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    mod._append_history(
        str(p),
        {
            "ts": "2026-02-27 12:00:00",
            "decision": "INSUFFICIENT",
        },
    )

    rows = _read_jsonl(p)
    assert len(rows) == 2
    assert any(str(r.get("decision") or "") == "INSUFFICIENT" for r in rows)
