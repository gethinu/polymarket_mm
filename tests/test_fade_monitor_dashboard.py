from __future__ import annotations

import datetime as dt
import json

import pytest

import fade_monitor_dashboard as mod


def _ts(minutes_ago: int) -> tuple[str, int]:
    now = dt.datetime.now() - dt.timedelta(minutes=minutes_ago)
    return now.strftime("%Y-%m-%d %H:%M:%S"), int(now.timestamp() * 1000.0)


def test_build_snapshot_marks_shadow_run_mode(tmp_path):
    metrics_path = tmp_path / "fade-metrics.jsonl"
    state_path = tmp_path / "fade-state.json"
    log_path = tmp_path / "fade.log"

    ts_a, ts_a_ms = _ts(12)
    ts_b, ts_b_ms = _ts(3)
    metrics_rows = [
        {
            "ts": ts_a,
            "ts_ms": ts_a_ms,
            "token_id": "tok-a",
            "label": "Token A",
            "consensus_score": 1.2,
            "consensus_side": 1,
            "consensus_agree": 2,
            "position_side": 1,
            "position_size": 2,
            "unrealized_pnl": 0.04,
            "realized_pnl": 0.02,
            "trade_count": 3,
            "win_count": 2,
            "total_pnl": 0.11,
            "day_pnl": 0.05,
        },
        {
            "ts": ts_b,
            "ts_ms": ts_b_ms,
            "token_id": "tok-a",
            "label": "Token A",
            "consensus_score": 1.5,
            "consensus_side": 1,
            "consensus_agree": 3,
            "position_side": 1,
            "position_size": 2,
            "unrealized_pnl": 0.06,
            "realized_pnl": 0.03,
            "trade_count": 4,
            "win_count": 3,
            "total_pnl": 0.21,
            "day_pnl": 0.09,
        },
        {
            "ts": ts_b,
            "ts_ms": ts_b_ms,
            "token_id": "tok-b",
            "label": "Token B",
            "consensus_score": -1.1,
            "consensus_side": -1,
            "consensus_agree": 2,
            "position_side": 0,
            "position_size": 0,
            "unrealized_pnl": -0.01,
            "realized_pnl": -0.02,
            "trade_count": 2,
            "win_count": 1,
            "total_pnl": 0.21,
            "day_pnl": 0.09,
        },
    ]
    metrics_path.write_text("\n".join(json.dumps(r) for r in metrics_rows) + "\n", encoding="utf-8")

    state_path.write_text(
        json.dumps(
            {
                "entries": 5,
                "exits": 4,
                "signals_seen": 12,
                "universe_refresh_count": 9,
                "token_states": {
                    "tok-a": {"token_id": "tok-a", "position_side": 1, "position_size": 2},
                },
                "active_token_ids": ["tok-a"],
                "long_trades": 4,
                "short_trades": 2,
                "long_wins": 3,
                "short_wins": 1,
            }
        ),
        encoding="utf-8",
    )
    log_path.write_text(f"[{ts_b}] summary ok\n[{ts_b}] entry LONG tok-a\n", encoding="utf-8")

    snap = mod._build_single_snapshot(
        metrics_file=str(metrics_path),
        state_file=str(state_path),
        log_file=str(log_path),
        window_minutes=60.0,
        tail_lines=200,
        max_tokens=10,
    )

    assert snap["mode"]["observe_only"] is True
    assert snap["mode"]["observe_only_note"] == "Shadow-run only. Not live realized PnL or live win rate."
    assert snap["mode"]["pnl_basis_note"] == "PnL, drawdown, and win rate below come from observe-only fade simulation state."
    assert snap["totals"]["total_pnl"] == pytest.approx(0.21)
    assert snap["totals"]["day_pnl"] == pytest.approx(0.09)
    assert snap["totals"]["win_rate"] == pytest.approx(4 / 6)
    assert snap["state"]["entry_exit_gap"] == 1


def test_dashboard_html_calls_out_shadow_run_not_realized():
    assert "This page does not show live realized PnL." in mod.HTML_TEMPLATE
    assert "Shadow PnL + Signal Pulse" in mod.HTML_TEMPLATE
    assert "Shadow Total PnL" in mod.HTML_TEMPLATE
