from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import capture_fade_checkpoint_baseline as capture_mod
import judge_fade_longonly_checkpoint as judge_mod
import report_fade_longonly_checkpoint as report_mod


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_capture_sum_state_pnl_counts_open_positions():
    state = {
        "token_states": {
            "a": {"realized_pnl": 1.2, "unrealized_pnl": -0.1, "position_side": 1, "position_size": 2},
            "b": {"realized_pnl": -0.4, "unrealized_pnl": 0.3, "position_side": -1, "position_size": 1},
            "c": {"realized_pnl": 0.2, "unrealized_pnl": 0.0, "position_side": 0, "position_size": 0},
        }
    }
    realized, unrealized, open_positions = capture_mod._sum_state_pnl(state)
    assert realized == pytest.approx(1.0)
    assert unrealized == pytest.approx(0.2)
    assert open_positions == 2


def test_capture_build_payload_uses_state_day_anchor(tmp_path: Path):
    state_file = tmp_path / "state.json"
    _write_json(
        state_file,
        {
            "day_key": "2026-03-07",
            "day_anchor_total_pnl": 0.5,
            "entries": 10,
            "exits": 8,
            "signals_seen": 30,
            "long_trades": 6,
            "long_wins": 4,
            "short_trades": 4,
            "short_wins": 2,
            "halted": False,
            "token_states": {
                "x": {"realized_pnl": 1.0, "unrealized_pnl": 0.2, "position_side": 1, "position_size": 2},
                "y": {"realized_pnl": -0.1, "unrealized_pnl": 0.4, "position_side": 0, "position_size": 0},
            },
        },
    )
    args = SimpleNamespace(
        phase="fade_regime_long_redesign",
        baseline_tag="",
        eval_after_hours=24.0,
        state_file=str(state_file),
    )
    payload = capture_mod.build_payload(args)
    assert payload["phase"] == "fade_regime_long_redesign"
    assert payload["total_pnl_usd"] == pytest.approx(1.5)
    assert payload["realized_pnl_usd"] == pytest.approx(0.9)
    assert payload["unrealized_pnl_usd"] == pytest.approx(0.6)
    assert payload["open_positions_total"] == 1
    assert payload["day_pnl_usd"] == pytest.approx(1.0)
    assert payload["entries"] == 10
    assert payload["exits"] == 8


def test_report_build_checkpoint_computes_since_baseline_metrics(tmp_path: Path):
    now = dt.datetime.now()
    t1 = (now - dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    t2 = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

    metrics_file = tmp_path / "metrics.jsonl"
    metrics_file.write_text(
        "\n".join(
            [
                json.dumps({"ts": t1, "ts_ms": int((now - dt.timedelta(hours=2)).timestamp() * 1000), "total_pnl": 10.5, "day_pnl": 0.2, "realized_total": 8.0, "unrealized_total": 2.5, "open_positions_total": 1}),
                json.dumps({"ts": t2, "ts_ms": int((now - dt.timedelta(hours=1)).timestamp() * 1000), "total_pnl": 11.0, "day_pnl": 0.7, "realized_total": 8.3, "unrealized_total": 2.7, "open_positions_total": 1}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    state_file = tmp_path / "state.json"
    _write_json(
        state_file,
        {
            "entries": 20,
            "exits": 10,
            "signals_seen": 60,
            "long_trades": 10,
            "long_wins": 6,
            "token_states": {"a": {"position_side": 1, "position_size": 1}},
        },
    )

    log_file = tmp_path / "observe.log"
    log_file.write_text(
        "\n".join(
            [
                f"[{t1}] entry side=long token=a",
                f"[{t2}] exit tp pnl=+0.2000 size=1.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    baseline_file = tmp_path / "baseline.json"
    _write_json(
        baseline_file,
        {
            "baseline_tag": "b1",
            "started_at_local": t1,
            "total_pnl_usd": 10.0,
            "entries": 12,
            "signals_seen": 40,
        },
    )

    supervisor_state = tmp_path / "supervisor_state.json"
    _write_json(
        supervisor_state,
        {
            "jobs": [
                {
                    "name": "fade_long_canary",
                    "last_start_ts": int((now - dt.timedelta(hours=1, minutes=30)).timestamp() * 1000),
                    "command": ["python", "x.py", "--position-size-shares", "12"],
                }
            ]
        },
    )

    args = SimpleNamespace(
        hours=24.0,
        baseline_json=str(baseline_file),
        baseline_start_local="",
        metrics_file=str(metrics_file),
        state_file=str(state_file),
        log_file=str(log_file),
        supervisor_state_file=str(supervisor_state),
        supervisor_job_name="fade_long_canary",
        out_json=str(tmp_path / "out.json"),
        out_txt=str(tmp_path / "out.txt"),
        tail_bytes=4 * 1024 * 1024,
    )
    payload, summary = report_mod.build_checkpoint(args)

    assert payload["delta"]["since_baseline_total_pnl_change_usd"] == pytest.approx(1.0)
    assert payload["window_events"]["exits"] == 1
    assert payload["window_events"]["tp_rate"] == pytest.approx(1.0)
    assert payload["runtime"]["job_position_size_shares"] == pytest.approx(12.0)
    assert payload["delta"]["since_baseline_pnl_per_trade_cents_per_share"] == pytest.approx((100.0 * 0.2) / 12.0)
    assert "Since baseline delta: +1.0000" in summary


def _default_judge_args(checkpoint_json: str, control_checkpoint_json: str = "", stress_checkpoint_json: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        checkpoint_json=checkpoint_json,
        metric_scope="since_baseline",
        control_checkpoint_json=control_checkpoint_json,
        stress_checkpoint_json=stress_checkpoint_json,
        phase_trades_mid=150,
        phase_trades_go=300,
        phase_trades_final=500,
        mid_min_pnl_per_trade_cents=-0.03,
        mid_max_timeout_rate=0.92,
        mid_min_profit_factor=0.90,
        mid_max_dd_ratio_vs_control=1.2,
        go_min_net_pnl_usd=0.0,
        go_min_pnl_per_trade_cents=0.01,
        go_max_timeout_rate=0.8,
        go_min_profit_factor=1.1,
        go_max_dd_ratio_vs_control=0.75,
        go_min_trade_ratio_vs_control=0.3,
        final_min_net_pnl_usd=0.0,
        final_min_pnl_per_trade_cents=0.02,
        final_max_timeout_rate=0.78,
        final_min_profit_factor=1.15,
        final_max_dd_ratio_vs_control=0.70,
        final_stress_min_net_pnl_usd=0.0,
        final_stress_min_profit_factor=1.05,
        out_json="",
        out_txt="",
    )


def test_judge_go_300_returns_go_candidate_when_all_conditions_pass(tmp_path: Path):
    checkpoint = tmp_path / "checkpoint.json"
    control = tmp_path / "control.json"

    _write_json(
        checkpoint,
        {
            "generated_local": "2026-03-07 12:00:00",
            "window_hours": 24.0,
            "runtime": {"job_position_size_shares": 12},
            "delta": {"since_baseline_max_drawdown_usd": 0.5},
            "since_baseline": {
                "exits": 320,
                "entries": 360,
                "net_pnl_usd": 2.4,
                "timeout_rate": 0.2,
                "profit_factor": 1.3,
                "pnl_per_trade_cents_per_share": 0.06,
            },
        },
    )
    _write_json(
        control,
        {
            "runtime": {"job_position_size_shares": 12},
            "delta": {"since_baseline_max_drawdown_usd": 1.0},
            "since_baseline": {"exits": 400},
        },
    )

    result = judge_mod.judge(_default_judge_args(str(checkpoint), control_checkpoint_json=str(control)))
    assert result["phase"] == "GO_300"
    assert result["decision"] == "GO_CANDIDATE"
    assert result["summary"]["failed_conditions"] == []


def test_judge_final_without_stress_returns_pending_evidence(tmp_path: Path):
    checkpoint = tmp_path / "checkpoint_final.json"
    control = tmp_path / "control_final.json"

    _write_json(
        checkpoint,
        {
            "generated_local": "2026-03-07 12:00:00",
            "window_hours": 24.0,
            "runtime": {"job_position_size_shares": 12},
            "delta": {"since_baseline_max_drawdown_usd": 0.4},
            "since_baseline": {
                "exits": 520,
                "entries": 560,
                "net_pnl_usd": 3.0,
                "timeout_rate": 0.2,
                "profit_factor": 1.4,
                "pnl_per_trade_cents_per_share": 0.07,
            },
        },
    )
    _write_json(
        control,
        {
            "delta": {"since_baseline_max_drawdown_usd": 1.0},
            "since_baseline": {"exits": 600},
        },
    )

    result = judge_mod.judge(_default_judge_args(str(checkpoint), control_checkpoint_json=str(control)))
    assert result["phase"] == "FINAL_500"
    assert result["decision"] == "PENDING_EVIDENCE"
    text = judge_mod.render_text(result)
    assert "Stress checkpoint: no" in text

