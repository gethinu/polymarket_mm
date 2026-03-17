from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import judge_btc5m_panic_downonly_trial as mod


def test_judge_pending_when_sample_gate_not_met(tmp_path: Path):
    eval_path = tmp_path / "eval.json"
    state_path = tmp_path / "state.json"
    supervisor_path = tmp_path / "supervisor.json"
    eval_path.write_text(
        json.dumps(
            {
                "metrics_span_hours": 2.0,
                "fee_adjusted": {"trade_count": 12, "net_pnl": 3.0, "max_drawdown": 5.0},
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"halted": False}), encoding="utf-8")
    supervisor_path.write_text(
        json.dumps(
            {
                "mode": "run",
                "supervisor_running": True,
                "jobs": [{"name": "btc5m_panic_downonly", "running": True}],
            }
        ),
        encoding="utf-8",
    )

    args = mod.parse_args([])
    args.eval_json = str(eval_path)
    args.state_json = str(state_path)
    args.supervisor_state_json = str(supervisor_path)
    result = mod.judge(args)
    assert result["decision"] == "PENDING"
    assert "sample_gate_not_met" in result["reason_codes"]


def test_judge_reject_when_sample_ready_but_gate_fails(tmp_path: Path):
    eval_path = tmp_path / "eval.json"
    state_path = tmp_path / "state.json"
    supervisor_path = tmp_path / "supervisor.json"
    eval_path.write_text(
        json.dumps(
            {
                "metrics_span_hours": 30.0,
                "fee_adjusted": {"trade_count": 120, "net_pnl": 4.0, "max_drawdown": 100.0},
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"halted": False}), encoding="utf-8")
    supervisor_path.write_text(
        json.dumps(
            {
                "mode": "run",
                "supervisor_running": True,
                "jobs": [{"name": "btc5m_panic_downonly", "running": True}],
            }
        ),
        encoding="utf-8",
    )

    args = mod.parse_args([])
    args.eval_json = str(eval_path)
    args.state_json = str(state_path)
    args.supervisor_state_json = str(supervisor_path)
    result = mod.judge(args)
    assert result["decision"] == "REJECT"
    assert "net_pnl_below_clear_positive_gate" in result["reason_codes"]


def test_judge_pass_candidate_when_ready_and_gates_pass(tmp_path: Path):
    eval_path = tmp_path / "eval.json"
    state_path = tmp_path / "state.json"
    supervisor_path = tmp_path / "supervisor.json"
    eval_path.write_text(
        json.dumps(
            {
                "metrics_span_hours": 26.0,
                "fee_adjusted": {"trade_count": 110, "net_pnl": 15.0, "max_drawdown": 70.0},
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(json.dumps({"halted": False}), encoding="utf-8")
    supervisor_path.write_text(
        json.dumps(
            {
                "mode": "run",
                "supervisor_running": True,
                "jobs": [{"name": "btc5m_panic_downonly", "running": True}],
            }
        ),
        encoding="utf-8",
    )

    args = mod.parse_args([])
    args.eval_json = str(eval_path)
    args.state_json = str(state_path)
    args.supervisor_state_json = str(supervisor_path)
    result = mod.judge(args)
    assert result["decision"] == "PASS_CANDIDATE"
