#!/usr/bin/env python3
"""
Judge the BTC 5m panic DOWN-only salvage trial (observe-only).

This reads the dedicated DOWN-only evaluation JSON and returns one of:
- PENDING: not enough fresh OOS evidence yet
- PASS_CANDIDATE: enough evidence and the salvage gate passed
- REJECT: enough evidence but the salvage gate failed
- BROKEN: supervisor/runtime is not healthy while evidence is still incomplete

No orders are placed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = REPO_ROOT / "logs"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _resolve_path(raw: str, default_name: str) -> Path:
    s = str(raw or "").strip()
    if not s:
        return LOGS_DIR / default_name
    p = Path(s)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return LOGS_DIR / p.name
    return REPO_ROOT / p


def _supervisor_running(supervisor_state: dict, expected_job_name: str) -> bool:
    if not isinstance(supervisor_state, dict):
        return False
    if not bool(supervisor_state.get("supervisor_running")):
        return False
    if str(supervisor_state.get("mode") or "").strip().lower() != "run":
        return False
    jobs = supervisor_state.get("jobs")
    if not isinstance(jobs, list):
        return False
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if str(job.get("name") or "").strip() != str(expected_job_name):
            continue
        return bool(job.get("running"))
    return False


def judge(args: argparse.Namespace) -> dict:
    eval_json = _resolve_path(args.eval_json, "btc5m_strategy_eval_downonly_latest.json")
    state_json = _resolve_path(args.state_json, "btc5m_panic_downonly_observe_state.json")
    supervisor_json = _resolve_path(
        args.supervisor_state_json,
        "btc5m_panic_downonly_supervisor_state.json",
    )

    eval_payload = _read_json(eval_json)
    state_payload = _read_json(state_json)
    supervisor_payload = _read_json(supervisor_json)

    fee_adj = eval_payload.get("fee_adjusted") if isinstance(eval_payload.get("fee_adjusted"), dict) else {}
    trade_count = _as_int(fee_adj.get("trade_count"), 0)
    net_pnl = _as_float(fee_adj.get("net_pnl"), 0.0)
    max_dd = _as_float(fee_adj.get("max_drawdown"), 0.0)
    span_hours = _as_float(eval_payload.get("metrics_span_hours"), 0.0)
    supervisor_running = _supervisor_running(
        supervisor_payload,
        expected_job_name=str(args.expected_job_name or "btc5m_panic_downonly"),
    )
    halted = bool(state_payload.get("halted"))

    baseline_dd = max(0.0, float(args.baseline_both_side_max_dd_usd))
    dd_improvement_ratio = max(0.0, min(1.0, float(args.required_dd_improvement_ratio)))
    max_allowed_dd = baseline_dd * dd_improvement_ratio
    sample_ready = (trade_count >= int(args.min_trades)) or (span_hours >= float(args.min_hours))

    reason_codes: list[str] = []
    if not eval_payload:
        reason_codes.append("eval_missing_or_invalid")
    if not state_payload:
        reason_codes.append("state_missing_or_invalid")
    if not supervisor_payload:
        reason_codes.append("supervisor_state_missing_or_invalid")
    if halted:
        reason_codes.append("runtime_halted")
    if not supervisor_running:
        reason_codes.append("supervisor_not_running")
    if not sample_ready:
        reason_codes.append("sample_gate_not_met")
    if sample_ready and net_pnl < float(args.min_net_pnl_usd):
        reason_codes.append("net_pnl_below_clear_positive_gate")
    if sample_ready and max_dd > max_allowed_dd:
        reason_codes.append("drawdown_not_improved_enough")

    if not eval_payload:
        decision = "BROKEN"
        action = "Fix evaluation/runtime artifacts first."
    elif not sample_ready:
        if supervisor_running and not halted:
            decision = "PENDING"
            action = "Keep DOWN-only observe running until 24h or 100 trades."
        else:
            decision = "BROKEN"
            action = "Runtime is not healthy before the salvage trial reached minimum evidence."
    elif net_pnl >= float(args.min_net_pnl_usd) and max_dd <= max_allowed_dd:
        decision = "PASS_CANDIDATE"
        action = "DOWN-only salvage gate passed. Re-review for guarded deployment."
    else:
        decision = "REJECT"
        action = "DOWN-only salvage gate failed. Mark the panic family REJECTED."

    return {
        "generated_local": "",
        "decision": decision,
        "recommended_action": action,
        "inputs": {
            "eval_json": str(eval_json),
            "state_json": str(state_json),
            "supervisor_state_json": str(supervisor_json),
        },
        "thresholds": {
            "min_trades": int(args.min_trades),
            "min_hours": float(args.min_hours),
            "min_net_pnl_usd": float(args.min_net_pnl_usd),
            "baseline_both_side_max_dd_usd": baseline_dd,
            "required_dd_improvement_ratio": dd_improvement_ratio,
            "max_allowed_dd_usd": max_allowed_dd,
        },
        "runtime": {
            "supervisor_running": supervisor_running,
            "halted": halted,
            "halt_reason": str(state_payload.get("halt_reason") or ""),
        },
        "metrics": {
            "trade_count": trade_count,
            "metrics_span_hours": span_hours,
            "net_pnl_usd": net_pnl,
            "max_drawdown_usd": max_dd,
        },
        "reason_codes": sorted(set(reason_codes)),
    }


def render_text(result: dict) -> str:
    th = result.get("thresholds") if isinstance(result.get("thresholds"), dict) else {}
    rt = result.get("runtime") if isinstance(result.get("runtime"), dict) else {}
    mx = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    lines = [
        "BTC 5m Panic DOWN-only Trial Judge (observe-only)",
        f"Decision: {str(result.get('decision') or 'UNKNOWN')}",
        f"Trade gate: {int(th.get('min_trades') or 0)} trades or {float(th.get('min_hours') or 0.0):.1f}h",
        f"Clear-positive net gate: ${float(th.get('min_net_pnl_usd') or 0.0):.2f}",
        f"Drawdown gate: <= ${float(th.get('max_allowed_dd_usd') or 0.0):.2f}",
        (
            f"Runtime: supervisor_running={bool(rt.get('supervisor_running'))} "
            f"halted={bool(rt.get('halted'))} reason={str(rt.get('halt_reason') or '-')}"
        ),
        (
            f"Observed: trades={int(mx.get('trade_count') or 0)} "
            f"span={float(mx.get('metrics_span_hours') or 0.0):.1f}h "
            f"net=${float(mx.get('net_pnl_usd') or 0.0):+.2f} "
            f"dd=${float(mx.get('max_drawdown_usd') or 0.0):.2f}"
        ),
        f"Action: {str(result.get('recommended_action') or '')}",
    ]
    reasons = result.get("reason_codes")
    if isinstance(reasons, list) and reasons:
        lines.append("Reason codes:")
        for row in reasons:
            lines.append(f"- {str(row)}")
    return "\n".join(lines)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Judge BTC 5m panic DOWN-only salvage trial")
    p.add_argument("--eval-json", default="logs/btc5m_strategy_eval_downonly_latest.json")
    p.add_argument("--state-json", default="logs/btc5m_panic_downonly_observe_state.json")
    p.add_argument("--supervisor-state-json", default="logs/btc5m_panic_downonly_supervisor_state.json")
    p.add_argument("--expected-job-name", default="btc5m_panic_downonly")
    p.add_argument("--min-trades", type=int, default=100)
    p.add_argument("--min-hours", type=float, default=24.0)
    p.add_argument("--min-net-pnl-usd", type=float, default=10.0)
    p.add_argument("--baseline-both-side-max-dd-usd", type=float, default=114.84)
    p.add_argument("--required-dd-improvement-ratio", type=float, default=0.75)
    p.add_argument("--out-json", default="logs/btc5m_panic_downonly_trial_decision_latest.json")
    p.add_argument("--out-txt", default="logs/btc5m_panic_downonly_trial_decision_latest.txt")
    p.add_argument("--pretty", action="store_true")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args()
    result = judge(args)
    text = render_text(result)
    print(text)

    out_json = _resolve_path(args.out_json, "btc5m_panic_downonly_trial_decision_latest.json")
    out_txt = _resolve_path(args.out_txt, "btc5m_panic_downonly_trial_decision_latest.txt")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        if args.pretty:
            json.dump(result, f, ensure_ascii=False, indent=2)
        else:
            json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    out_txt.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
