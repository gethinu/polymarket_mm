from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

import event_driven_monitor_dashboard as mod


def _utc_ts(minutes_ago: int) -> str:
    ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def test_parse_signal_defaults_unclassified_and_unknown_side():
    line = json.dumps(
        {
            "ts": _utc_ts(1),
            "side": "MAYBE",
            "edge_cents": 0.8,
            "confidence": 0.61,
        }
    )
    row = mod.parse_signal(line)
    assert row is not None
    assert row.side == "-"
    assert row.event_class == "unclassified"


def test_parse_metric_returns_none_for_invalid_json():
    assert mod.parse_metric("{bad json}") is None


def test_build_snapshot_aggregates_recent_artifacts(tmp_path):
    signals_path = tmp_path / "event-driven-observe-signals.jsonl"
    metrics_path = tmp_path / "event-driven-observe-metrics.jsonl"
    log_path = tmp_path / "event-driven-observe.log"
    profit_path = tmp_path / "event_driven_profit_window_latest.json"
    summary_path = tmp_path / "event_driven_daily_summary.txt"

    signals_rows = [
        {
            "ts": _utc_ts(20),
            "side": "YES",
            "edge_cents": 1.2,
            "confidence": 0.63,
            "suggested_stake_usd": 1.5,
            "event_class": "sports",
            "event_slug": "evt_a",
            "market_id": "m_a",
            "question": "A?",
        },
        {
            "ts": _utc_ts(8),
            "side": "NO",
            "edge_cents": -0.4,
            "confidence": 0.42,
            "suggested_stake_usd": 2.0,
            "event_slug": "evt_b",
            "market_id": "m_b",
            "question": "B?",
        },
    ]
    signals_path.write_text("\n".join(json.dumps(r) for r in signals_rows) + "\n", encoding="utf-8")

    metrics_rows = [
        {
            "ts": _utc_ts(25),
            "scanned": 100,
            "eligible_count": 12,
            "event_count": 10,
            "candidate_count": 10,
            "suppressed_count": 4,
            "top_written": 2,
            "runtime_sec": 1.2,
        },
        {
            "ts": _utc_ts(4),
            "scanned": 110,
            "eligible_count": 15,
            "event_count": 12,
            "candidate_count": 20,
            "suppressed_count": 10,
            "top_written": 3,
            "runtime_sec": 1.6,
        },
    ]
    metrics_path.write_text("\n".join(json.dumps(r) for r in metrics_rows) + "\n", encoding="utf-8")

    log_lines = [
        f"[{_utc_ts(10)}] run=1 written=2",
        f"[{_utc_ts(3)}] ERROR sample error",
        f"# signal edge=1.1",
    ]
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    profit_payload = {
        "decision": {
            "decision": "GO",
            "projected_monthly_return": 0.14,
            "target_monthly_return": 0.10,
            "reasons": ["sufficient opportunities"],
        },
        "selected_threshold": {
            "threshold_cents": 1.1,
            "hit_ratio": 0.57,
            "unique_events": 18,
            "opportunities_per_day_capped": 3.4,
        },
    }
    profit_path.write_text(json.dumps(profit_payload) + "\n", encoding="utf-8")
    summary_path.write_text("daily summary line", encoding="utf-8")

    cfg = mod.Config(
        host="127.0.0.1",
        port=8788,
        signals_file=str(signals_path),
        metrics_file=str(metrics_path),
        log_file=str(log_path),
        profit_json=str(profit_path),
        summary_txt=str(summary_path),
        window_minutes=180.0,
        tail_lines=1000,
        max_signals=10,
        refresh_ms=1000,
    )
    snapshot = mod.build_snapshot(cfg, minutes=60.0)

    totals = snapshot["totals"]
    assert totals["runs"] == 2
    assert totals["signals"] == 2
    assert totals["yes"] == 1
    assert totals["no"] == 1
    assert totals["unique_events"] == 2
    assert totals["unique_markets"] == 2
    assert totals["write_rate"] == pytest.approx(5.0 / 30.0)
    assert totals["suppressed_rate"] == pytest.approx(14.0 / 30.0)
    assert totals["latest_candidate"] == 20
    assert totals["latest_written"] == 3
    assert totals["latest_suppressed"] == 10

    class_names = {row["name"] for row in snapshot["classes"]}
    assert "sports" in class_names
    assert "unclassified" in class_names

    assert len(snapshot["recent_signals"]) == 2
    assert snapshot["profit"]["decision"] == "GO"
    assert snapshot["profit"]["selected_threshold_cents"] == pytest.approx(1.1)
    assert snapshot["profit"]["reasons"] == ["sufficient opportunities"]
    assert snapshot["summary_text"] == "daily summary line"


def test_load_text_truncates_long_content(tmp_path):
    p = Path(tmp_path) / "summary.txt"
    p.write_text("x" * 30, encoding="utf-8")
    out = mod.load_text(str(p), limit=12)
    assert out.endswith("...(truncated)")
