#!/usr/bin/env python3
"""
Run observe-only staged checkpoint checks for fade regime/side redesign arms.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

from judge_fade_longonly_checkpoint import judge as judge_checkpoint
from judge_fade_longonly_checkpoint import render_text as render_judge_text
from report_fade_longonly_checkpoint import build_checkpoint


@dataclass(frozen=True)
class ArmSpec:
    arm_id: str
    label: str
    baseline_json: str
    metrics_file: str
    state_file: str
    log_file: str
    supervisor_job_name: str
    checkpoint_json: str
    checkpoint_txt: str
    decision_json: str
    decision_txt: str


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _now_local_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_json(path: str, payload: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: str, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text if text.endswith("\n") else (text + "\n"), encoding="utf-8")


def _default_arms(logs: Path) -> List[ArmSpec]:
    return [
        ArmSpec(
            arm_id="regime_both_core",
            label="Regime Both Core",
            baseline_json=str(logs / "fade_regime_both_baseline_latest.json"),
            metrics_file=str(logs / "clob-fade-observe-profit-regime-both-metrics.jsonl"),
            state_file=str(logs / "clob_fade_observe_profit_regime_both_state.json"),
            log_file=str(logs / "clob-fade-observe-profit-regime-both.log"),
            supervisor_job_name="fade_regime_both_core",
            checkpoint_json=str(logs / "fade_regime_both_eval_latest.json"),
            checkpoint_txt=str(logs / "fade_regime_both_eval_latest.txt"),
            decision_json=str(logs / "fade_regime_both_decision_latest.json"),
            decision_txt=str(logs / "fade_regime_both_decision_latest.txt"),
        ),
        ArmSpec(
            arm_id="regime_long_strict",
            label="Regime Long Strict",
            baseline_json=str(logs / "fade_regime_long_baseline_latest.json"),
            metrics_file=str(logs / "clob-fade-observe-profit-regime-long-metrics.jsonl"),
            state_file=str(logs / "clob_fade_observe_profit_regime_long_state.json"),
            log_file=str(logs / "clob-fade-observe-profit-regime-long.log"),
            supervisor_job_name="fade_regime_long_strict",
            checkpoint_json=str(logs / "fade_regime_long_eval_latest.json"),
            checkpoint_txt=str(logs / "fade_regime_long_eval_latest.txt"),
            decision_json=str(logs / "fade_regime_long_decision_latest.json"),
            decision_txt=str(logs / "fade_regime_long_decision_latest.txt"),
        ),
        ArmSpec(
            arm_id="regime_short_strict",
            label="Regime Short Strict",
            baseline_json=str(logs / "fade_regime_short_baseline_latest.json"),
            metrics_file=str(logs / "clob-fade-observe-profit-regime-short-metrics.jsonl"),
            state_file=str(logs / "clob_fade_observe_profit_regime_short_state.json"),
            log_file=str(logs / "clob-fade-observe-profit-regime-short.log"),
            supervisor_job_name="fade_regime_short_strict",
            checkpoint_json=str(logs / "fade_regime_short_eval_latest.json"),
            checkpoint_txt=str(logs / "fade_regime_short_eval_latest.txt"),
            decision_json=str(logs / "fade_regime_short_decision_latest.json"),
            decision_txt=str(logs / "fade_regime_short_decision_latest.txt"),
        ),
    ]


def _build_checkpoint_for_arm(args, arm: ArmSpec) -> dict:
    report_args = SimpleNamespace(
        hours=float(args.hours),
        baseline_json=str(arm.baseline_json),
        baseline_start_local="",
        metrics_file=str(arm.metrics_file),
        state_file=str(arm.state_file),
        log_file=str(arm.log_file),
        supervisor_state_file=str(args.supervisor_state_file),
        supervisor_job_name=str(arm.supervisor_job_name),
        out_json=str(arm.checkpoint_json),
        out_txt=str(arm.checkpoint_txt),
        tail_bytes=int(args.tail_bytes),
    )
    payload, text = build_checkpoint(report_args)
    _write_json(arm.checkpoint_json, payload)
    _write_text(arm.checkpoint_txt, text)
    return payload


def _judge_for_arm(
    args,
    arm: ArmSpec,
    control_checkpoint_json: str,
    go_max_dd_ratio_vs_control: float,
    final_max_dd_ratio_vs_control: float,
    go_min_trade_ratio_vs_control: float,
) -> dict:
    judge_args = SimpleNamespace(
        checkpoint_json=str(arm.checkpoint_json),
        metric_scope=str(args.metric_scope),
        control_checkpoint_json=str(control_checkpoint_json or ""),
        stress_checkpoint_json=str(args.stress_checkpoint_json or ""),
        phase_trades_mid=int(args.phase_trades_mid),
        phase_trades_go=int(args.phase_trades_go),
        phase_trades_final=int(args.phase_trades_final),
        mid_min_pnl_per_trade_cents=float(args.mid_min_pnl_per_trade_cents),
        mid_max_timeout_rate=float(args.mid_max_timeout_rate),
        mid_min_profit_factor=float(args.mid_min_profit_factor),
        mid_max_dd_ratio_vs_control=float(args.mid_max_dd_ratio_vs_control),
        go_min_net_pnl_usd=float(args.go_min_net_pnl_usd),
        go_min_pnl_per_trade_cents=float(args.go_min_pnl_per_trade_cents),
        go_max_timeout_rate=float(args.go_max_timeout_rate),
        go_min_profit_factor=float(args.go_min_profit_factor),
        go_max_dd_ratio_vs_control=float(go_max_dd_ratio_vs_control),
        go_min_trade_ratio_vs_control=float(go_min_trade_ratio_vs_control),
        final_min_net_pnl_usd=float(args.final_min_net_pnl_usd),
        final_min_pnl_per_trade_cents=float(args.final_min_pnl_per_trade_cents),
        final_max_timeout_rate=float(args.final_max_timeout_rate),
        final_min_profit_factor=float(args.final_min_profit_factor),
        final_max_dd_ratio_vs_control=float(final_max_dd_ratio_vs_control),
        final_stress_min_net_pnl_usd=float(args.final_stress_min_net_pnl_usd),
        final_stress_min_profit_factor=float(args.final_stress_min_profit_factor),
        out_json=str(arm.decision_json),
        out_txt=str(arm.decision_txt),
    )
    result = judge_checkpoint(judge_args)
    _write_json(arm.decision_json, result)
    _write_text(arm.decision_txt, render_judge_text(result))
    return result


def run(args) -> dict:
    repo = Path(__file__).resolve().parents[1]
    logs = repo / "logs"
    arms = _default_arms(logs)
    arms_by_id = {arm.arm_id: arm for arm in arms}

    if str(args.control_arm_id) not in arms_by_id:
        raise RuntimeError(f"unknown --control-arm-id: {args.control_arm_id}")

    checkpoints: Dict[str, dict] = {}
    for arm in arms:
        checkpoints[arm.arm_id] = _build_checkpoint_for_arm(args, arm)

    control_arm = arms_by_id[str(args.control_arm_id)]
    decision_rows: List[dict] = []
    decision_counts: Dict[str, int] = {}
    phase_counts: Dict[str, int] = {}

    for arm in arms:
        control_ckpt = ""
        go_dd_ratio = float(args.go_max_dd_ratio_vs_control)
        final_dd_ratio = float(args.final_max_dd_ratio_vs_control)
        go_trade_ratio = float(args.go_min_trade_ratio_vs_control)

        if arm.arm_id == control_arm.arm_id:
            if bool(args.control_self_for_control_arm):
                control_ckpt = str(control_arm.checkpoint_json)
                go_dd_ratio = float(args.control_go_max_dd_ratio_vs_self)
                final_dd_ratio = float(args.control_final_max_dd_ratio_vs_self)
                go_trade_ratio = float(args.control_go_min_trade_ratio_vs_self)
        else:
            control_ckpt = str(control_arm.checkpoint_json)

        decision = _judge_for_arm(
            args=args,
            arm=arm,
            control_checkpoint_json=control_ckpt,
            go_max_dd_ratio_vs_control=go_dd_ratio,
            final_max_dd_ratio_vs_control=final_dd_ratio,
            go_min_trade_ratio_vs_control=go_trade_ratio,
        )

        summary = decision.get("summary") if isinstance(decision.get("summary"), dict) else {}
        metrics = summary.get("candidate_metrics") if isinstance(summary.get("candidate_metrics"), dict) else {}
        row = {
            "arm_id": arm.arm_id,
            "label": arm.label,
            "checkpoint_json": arm.checkpoint_json,
            "decision_json": arm.decision_json,
            "decision": str(decision.get("decision") or "UNKNOWN"),
            "phase": str(decision.get("phase") or "UNKNOWN"),
            "exits_closed_trades": _to_int(summary.get("exits_closed_trades")),
            "net_pnl_usd": _to_float(metrics.get("net_pnl_usd")),
            "pnl_per_trade_cents_per_share": _to_float(metrics.get("pnl_per_trade_cents_per_share")),
            "timeout_rate": _to_float(metrics.get("timeout_rate")),
            "profit_factor": _to_float(metrics.get("profit_factor")),
            "max_drawdown_usd": _to_float(metrics.get("max_drawdown_usd")),
            "failed_conditions": summary.get("failed_conditions") if isinstance(summary.get("failed_conditions"), list) else [],
        }
        decision_rows.append(row)

        d = row["decision"]
        p = row["phase"]
        decision_counts[d] = decision_counts.get(d, 0) + 1
        phase_counts[p] = phase_counts.get(p, 0) + 1

    payload = {
        "generated_local": _now_local_text(),
        "metric_scope": str(args.metric_scope),
        "hours": float(args.hours),
        "control_arm_id": str(args.control_arm_id),
        "control_self_for_control_arm": bool(args.control_self_for_control_arm),
        "decision_counts": decision_counts,
        "phase_counts": phase_counts,
        "arms": decision_rows,
    }
    return payload


def render_summary(payload: dict) -> str:
    rows = payload.get("arms") if isinstance(payload.get("arms"), list) else []
    lines = [
        f"Fade Regime Staged Batch ({str(payload.get('generated_local') or '')} local)",
        f"metric_scope={str(payload.get('metric_scope') or 'since_baseline')} hours={_to_float(payload.get('hours'), 0.0):.1f}",
        f"control_arm_id={str(payload.get('control_arm_id') or '')} control_self_for_control_arm={bool(payload.get('control_self_for_control_arm'))}",
        f"decision_counts={json.dumps(payload.get('decision_counts') or {}, ensure_ascii=False)}",
        f"phase_counts={json.dumps(payload.get('phase_counts') or {}, ensure_ascii=False)}",
    ]
    for row in rows:
        if not isinstance(row, dict):
            continue
        lines.append(
            f"- {str(row.get('arm_id') or '')}: decision={str(row.get('decision') or 'UNKNOWN')} "
            f"phase={str(row.get('phase') or 'UNKNOWN')} exits={_to_int(row.get('exits_closed_trades'))} "
            f"net={_to_float(row.get('net_pnl_usd')):+.4f} "
            f"pnl/trade(c/share)={_to_float(row.get('pnl_per_trade_cents_per_share')):+.4f} "
            f"timeout={100.0 * _to_float(row.get('timeout_rate')):.1f}% pf={_to_float(row.get('profit_factor')):.3f}"
        )
    return "\n".join(lines) + "\n"


def parse_args():
    repo = Path(__file__).resolve().parents[1]
    logs = repo / "logs"
    p = argparse.ArgumentParser(description="Run fade regime/side staged checks (observe-only)")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--metric-scope", choices=["since_baseline", "since_supervisor_start"], default="since_baseline")
    p.add_argument("--tail-bytes", type=int, default=160 * 1024 * 1024)
    p.add_argument("--supervisor-state-file", default=str(logs / "fade_observe_supervisor_state.json"))
    p.add_argument("--control-arm-id", default="regime_both_core")
    p.add_argument("--control-self-for-control-arm", dest="control_self_for_control_arm", action="store_true", default=True)
    p.add_argument("--no-control-self-for-control-arm", dest="control_self_for_control_arm", action="store_false")
    p.add_argument("--control-go-max-dd-ratio-vs-self", type=float, default=1.0)
    p.add_argument("--control-final-max-dd-ratio-vs-self", type=float, default=1.0)
    p.add_argument("--control-go-min-trade-ratio-vs-self", type=float, default=1.0)

    p.add_argument("--phase-trades-mid", type=int, default=150)
    p.add_argument("--phase-trades-go", type=int, default=300)
    p.add_argument("--phase-trades-final", type=int, default=500)

    p.add_argument("--mid-min-pnl-per-trade-cents", type=float, default=-0.03)
    p.add_argument("--mid-max-timeout-rate", type=float, default=0.92)
    p.add_argument("--mid-min-profit-factor", type=float, default=0.90)
    p.add_argument("--mid-max-dd-ratio-vs-control", type=float, default=1.20)

    p.add_argument("--go-min-net-pnl-usd", type=float, default=0.0)
    p.add_argument("--go-min-pnl-per-trade-cents", type=float, default=0.01)
    p.add_argument("--go-max-timeout-rate", type=float, default=0.80)
    p.add_argument("--go-min-profit-factor", type=float, default=1.10)
    p.add_argument("--go-max-dd-ratio-vs-control", type=float, default=0.75)
    p.add_argument("--go-min-trade-ratio-vs-control", type=float, default=0.30)

    p.add_argument("--final-min-net-pnl-usd", type=float, default=0.0)
    p.add_argument("--final-min-pnl-per-trade-cents", type=float, default=0.02)
    p.add_argument("--final-max-timeout-rate", type=float, default=0.78)
    p.add_argument("--final-min-profit-factor", type=float, default=1.15)
    p.add_argument("--final-max-dd-ratio-vs-control", type=float, default=0.70)
    p.add_argument("--final-stress-min-net-pnl-usd", type=float, default=0.0)
    p.add_argument("--final-stress-min-profit-factor", type=float, default=1.05)
    p.add_argument("--stress-checkpoint-json", default="")

    p.add_argument("--out-json", default=str(logs / "fade_regime_staged_decision_latest.json"))
    p.add_argument("--out-txt", default=str(logs / "fade_regime_staged_decision_latest.txt"))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    payload = run(args)
    summary = render_summary(payload)

    _write_json(args.out_json, payload)
    _write_text(args.out_txt, summary)

    print(summary, end="")
    print(f"out_json={args.out_json}")
    print(f"out_txt={args.out_txt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

