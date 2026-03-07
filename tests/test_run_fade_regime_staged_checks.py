from __future__ import annotations

from types import SimpleNamespace

import pytest

import run_fade_regime_staged_checks as mod


def _arm(arm_id: str, label: str) -> mod.ArmSpec:
    return mod.ArmSpec(
        arm_id=arm_id,
        label=label,
        baseline_json=f"logs/{arm_id}_baseline.json",
        metrics_file=f"logs/{arm_id}_metrics.jsonl",
        state_file=f"logs/{arm_id}_state.json",
        log_file=f"logs/{arm_id}.log",
        supervisor_job_name=arm_id,
        checkpoint_json=f"logs/{arm_id}_checkpoint.json",
        checkpoint_txt=f"logs/{arm_id}_checkpoint.txt",
        decision_json=f"logs/{arm_id}_decision.json",
        decision_txt=f"logs/{arm_id}_decision.txt",
    )


def _args(**overrides) -> SimpleNamespace:
    base = {
        "hours": 24.0,
        "metric_scope": "since_baseline",
        "tail_bytes": 1024,
        "supervisor_state_file": "logs/fade_observe_supervisor_state.json",
        "control_arm_id": "regime_both_core",
        "control_self_for_control_arm": True,
        "control_go_max_dd_ratio_vs_self": 1.0,
        "control_final_max_dd_ratio_vs_self": 1.0,
        "control_go_min_trade_ratio_vs_self": 1.0,
        "phase_trades_mid": 150,
        "phase_trades_go": 300,
        "phase_trades_final": 500,
        "mid_min_pnl_per_trade_cents": -0.03,
        "mid_max_timeout_rate": 0.92,
        "mid_min_profit_factor": 0.9,
        "mid_max_dd_ratio_vs_control": 1.2,
        "go_min_net_pnl_usd": 0.0,
        "go_min_pnl_per_trade_cents": 0.01,
        "go_max_timeout_rate": 0.8,
        "go_min_profit_factor": 1.1,
        "go_max_dd_ratio_vs_control": 0.75,
        "go_min_trade_ratio_vs_control": 0.3,
        "final_min_net_pnl_usd": 0.0,
        "final_min_pnl_per_trade_cents": 0.02,
        "final_max_timeout_rate": 0.78,
        "final_min_profit_factor": 1.15,
        "final_max_dd_ratio_vs_control": 0.7,
        "final_stress_min_net_pnl_usd": 0.0,
        "final_stress_min_profit_factor": 1.05,
        "stress_checkpoint_json": "",
        "out_json": "logs/fade_regime_staged_decision_latest.json",
        "out_txt": "logs/fade_regime_staged_decision_latest.txt",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _decision(decision: str, phase: str, exits: int) -> dict:
    return {
        "decision": decision,
        "phase": phase,
        "summary": {
            "exits_closed_trades": exits,
            "failed_conditions": [],
            "candidate_metrics": {
                "net_pnl_usd": 1.25,
                "pnl_per_trade_cents_per_share": 0.06,
                "timeout_rate": 0.2,
                "profit_factor": 1.4,
                "max_drawdown_usd": 0.7,
            },
        },
    }


def test_run_raises_on_unknown_control_arm_id(monkeypatch):
    monkeypatch.setattr(mod, "_default_arms", lambda logs: [_arm("regime_both_core", "Both")])
    with pytest.raises(RuntimeError, match="unknown --control-arm-id"):
        mod.run(_args(control_arm_id="missing"))


def test_run_applies_control_self_threshold_overrides(monkeypatch):
    control = _arm("regime_both_core", "Both")
    challenger = _arm("regime_long_strict", "Long")
    calls: list[dict] = []

    monkeypatch.setattr(mod, "_default_arms", lambda logs: [control, challenger])
    monkeypatch.setattr(mod, "_build_checkpoint_for_arm", lambda args, arm: {"arm_id": arm.arm_id})

    def _fake_judge(args, arm, control_checkpoint_json, go_max_dd_ratio_vs_control, final_max_dd_ratio_vs_control, go_min_trade_ratio_vs_control):
        calls.append(
            {
                "arm_id": arm.arm_id,
                "control_checkpoint_json": control_checkpoint_json,
                "go_dd": go_max_dd_ratio_vs_control,
                "final_dd": final_max_dd_ratio_vs_control,
                "go_trade_ratio": go_min_trade_ratio_vs_control,
            }
        )
        if arm.arm_id == control.arm_id:
            return _decision("GO", "GO_300", 320)
        return _decision("HOLD", "MID_150", 190)

    monkeypatch.setattr(mod, "_judge_for_arm", _fake_judge)

    payload = mod.run(
        _args(
            control_self_for_control_arm=True,
            control_go_max_dd_ratio_vs_self=1.11,
            control_final_max_dd_ratio_vs_self=1.22,
            control_go_min_trade_ratio_vs_self=0.95,
            go_max_dd_ratio_vs_control=0.75,
            final_max_dd_ratio_vs_control=0.7,
            go_min_trade_ratio_vs_control=0.3,
        )
    )

    by_arm = {row["arm_id"]: row for row in calls}
    assert by_arm["regime_both_core"]["control_checkpoint_json"] == control.checkpoint_json
    assert by_arm["regime_both_core"]["go_dd"] == pytest.approx(1.11)
    assert by_arm["regime_both_core"]["final_dd"] == pytest.approx(1.22)
    assert by_arm["regime_both_core"]["go_trade_ratio"] == pytest.approx(0.95)

    assert by_arm["regime_long_strict"]["control_checkpoint_json"] == control.checkpoint_json
    assert by_arm["regime_long_strict"]["go_dd"] == pytest.approx(0.75)
    assert by_arm["regime_long_strict"]["final_dd"] == pytest.approx(0.7)
    assert by_arm["regime_long_strict"]["go_trade_ratio"] == pytest.approx(0.3)

    assert payload["decision_counts"] == {"GO": 1, "HOLD": 1}
    assert payload["phase_counts"] == {"GO_300": 1, "MID_150": 1}


def test_run_skips_control_checkpoint_when_control_self_disabled(monkeypatch):
    control = _arm("regime_both_core", "Both")
    calls: list[dict] = []

    monkeypatch.setattr(mod, "_default_arms", lambda logs: [control])
    monkeypatch.setattr(mod, "_build_checkpoint_for_arm", lambda args, arm: {"arm_id": arm.arm_id})

    def _fake_judge(args, arm, control_checkpoint_json, go_max_dd_ratio_vs_control, final_max_dd_ratio_vs_control, go_min_trade_ratio_vs_control):
        calls.append(
            {
                "control_checkpoint_json": control_checkpoint_json,
                "go_dd": go_max_dd_ratio_vs_control,
                "final_dd": final_max_dd_ratio_vs_control,
                "go_trade_ratio": go_min_trade_ratio_vs_control,
            }
        )
        return _decision("GO", "GO_300", 301)

    monkeypatch.setattr(mod, "_judge_for_arm", _fake_judge)

    mod.run(
        _args(
            control_self_for_control_arm=False,
            go_max_dd_ratio_vs_control=0.8,
            final_max_dd_ratio_vs_control=0.6,
            go_min_trade_ratio_vs_control=0.33,
        )
    )

    assert calls[0]["control_checkpoint_json"] == ""
    assert calls[0]["go_dd"] == pytest.approx(0.8)
    assert calls[0]["final_dd"] == pytest.approx(0.6)
    assert calls[0]["go_trade_ratio"] == pytest.approx(0.33)


def test_render_summary_includes_arm_line():
    payload = {
        "generated_local": "2026-03-06 12:00:00",
        "metric_scope": "since_baseline",
        "hours": 24.0,
        "control_arm_id": "regime_both_core",
        "control_self_for_control_arm": True,
        "decision_counts": {"GO": 1},
        "phase_counts": {"GO_300": 1},
        "arms": [
            {
                "arm_id": "regime_both_core",
                "decision": "GO",
                "phase": "GO_300",
                "exits_closed_trades": 321,
                "net_pnl_usd": 1.5,
                "pnl_per_trade_cents_per_share": 0.05,
                "timeout_rate": 0.2,
                "profit_factor": 1.4,
            }
        ],
    }
    text = mod.render_summary(payload)
    assert "Fade Regime Staged Batch (2026-03-06 12:00:00 local)" in text
    assert 'decision_counts={"GO": 1}' in text
    assert "- regime_both_core: decision=GO phase=GO_300 exits=321 net=+1.5000" in text
