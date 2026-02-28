from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import check_pending_release as mod


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")


def _args(tmp_path: Path, **overrides):
    base = dict(
        strategy="gamma_eventpair_exec_edge_filter_observe",
        snapshot_json=str(tmp_path / "snapshot.json"),
        strategy_md=str(tmp_path / "STRATEGY.md"),
        monthly_json="",
        replay_json="",
        min_gap_ms_per_event=5000,
        max_worst_stale_sec=10.0,
        conservative_costs=False,
        conservative_cost_cents=2.0,
        apply=False,
        out_json="",
        pretty=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _snapshot_with_status(path: Path, status: str) -> None:
    _write_json(
        path,
        {
            "strategy_register": {
                "entries": [
                    {
                        "strategy_id": "gamma_eventpair_exec_edge_filter_observe",
                        "status": status,
                        "evidence_refs": [],
                    }
                ]
            }
        },
    )


def test_run_check_noop_when_status_not_pending(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    _snapshot_with_status(snapshot, "ADOPTED")
    args = _args(tmp_path)

    code, out = mod.run_check(args)

    assert code == 0
    assert out["release_check"] == "NOOP"
    assert out["release_ready"] is False
    assert "status_not_pending" in out["reason_codes"]


def test_run_check_hold_when_execution_edge_non_positive(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    _snapshot_with_status(snapshot, "PENDING")

    metrics = tmp_path / "metrics.jsonl"
    _write_jsonl(
        metrics,
        [
            {
                "ts_ms": 1,
                "event_key": "ek1",
                "passes_raw_threshold": True,
                "net_edge_exec_est": -0.01,
                "worst_book_stale_sec": 1.0,
            }
        ],
    )
    monthly = tmp_path / "monthly.json"
    _write_json(monthly, {"source_metrics_files": [str(metrics)]})
    replay = tmp_path / "replay.json"
    _write_json(replay, {"kelly": {"full_fraction_estimate": 0.12}})

    args = _args(tmp_path, monthly_json=str(monthly), replay_json=str(replay))
    code, out = mod.run_check(args)

    assert code == 10
    assert out["release_check"] == "HOLD"
    assert out["checks"]["execution_edge_positive"] is False
    assert out["checks"]["full_kelly_positive"] is True
    assert "execution_edge_non_positive" in out["reason_codes"]


def test_run_check_release_ready_when_positive_metrics(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    _snapshot_with_status(snapshot, "PENDING")

    metrics = tmp_path / "metrics.jsonl"
    _write_jsonl(
        metrics,
        [
            {
                "ts_ms": 1000,
                "event_key": "ek1",
                "passes_raw_threshold": True,
                "net_edge_exec_est": 0.03,
                "worst_book_stale_sec": 1.0,
            }
        ],
    )
    monthly = tmp_path / "monthly.json"
    _write_json(monthly, {"source_metrics_files": [str(metrics)]})
    replay = tmp_path / "replay.json"
    _write_json(replay, {"kelly": {"full_fraction_estimate": 0.25}})

    args = _args(tmp_path, monthly_json=str(monthly), replay_json=str(replay))
    code, out = mod.run_check(args)

    assert code == 0
    assert out["release_check"] == "RELEASE_READY"
    assert out["release_ready"] is True
    assert out["checks"]["execution_edge_positive"] is True
    assert out["checks"]["full_kelly_positive"] is True
    assert out["reason_codes"] == []


def test_run_check_apply_updates_status_line_when_ready(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot.json"
    _snapshot_with_status(snapshot, "PENDING")

    strategy_md = tmp_path / "STRATEGY.md"
    strategy_md.write_text(
        "\n".join(
            [
                "## Pending Strategies",
                "",
                "1. `gamma_eventpair_exec_edge_filter_observe`",
                "",
                "- Status: `PENDING` (as of 2026-02-28, review-hold, observe-only).",
                "- Scope: test",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    metrics = tmp_path / "metrics.jsonl"
    _write_jsonl(
        metrics,
        [
            {
                "ts_ms": 1000,
                "event_key": "ek1",
                "passes_raw_threshold": True,
                "net_edge_exec_est": 0.05,
                "worst_book_stale_sec": 1.0,
            }
        ],
    )
    monthly = tmp_path / "monthly.json"
    _write_json(monthly, {"source_metrics_files": [str(metrics)]})
    replay = tmp_path / "replay.json"
    _write_json(replay, {"kelly": {"full_fraction_estimate": 0.33}})

    monkeypatch.setattr(
        mod,
        "_run_refresh_commands",
        lambda: [{"command": "x", "returncode": 0, "stdout": "", "stderr": "", "ok": True}],
    )

    args = _args(
        tmp_path,
        strategy_md=str(strategy_md),
        monthly_json=str(monthly),
        replay_json=str(replay),
        apply=True,
    )
    code, out = mod.run_check(args)

    assert code == 0
    assert out["apply"]["applied"] is True
    assert out["current_status"] == "ADOPTED"
    updated = strategy_md.read_text(encoding="utf-8")
    assert "- Status: `ADOPTED` (auto-promoted by check_pending_release.py on " in updated

