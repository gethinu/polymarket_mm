#!/usr/bin/env python3
"""
Judge fade long-only staged checkpoint gates (150/300/500 closed trades).

Observe-only analytics. It does not place orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any, Optional


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _to_float_opt(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except Exception:
        return None
    if math.isnan(f):
        return None
    return f


def _load_json(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _ratio_opt(numer: Optional[float], denom: Optional[float]) -> Optional[float]:
    if numer is None or denom is None:
        return None
    if denom <= 0.0:
        return None
    return float(numer) / float(denom)


def _dd_ratio_opt(candidate_dd: Optional[float], control_dd: Optional[float]) -> Optional[float]:
    if candidate_dd is None or control_dd is None:
        return None
    if control_dd < 0.0:
        return None
    if control_dd == 0.0:
        if candidate_dd <= 0.0:
            return 0.0
        return float("inf")
    return float(candidate_dd) / float(control_dd)


def _extract_metrics(checkpoint: dict, metric_scope: str) -> dict:
    requested_scope = str(metric_scope or "").strip()
    if requested_scope not in {"since_baseline", "since_supervisor_start"}:
        requested_scope = "since_baseline"

    selected_scope = requested_scope
    since = checkpoint.get(selected_scope)
    if not isinstance(since, dict):
        fallback_scope = "since_supervisor_start" if selected_scope == "since_baseline" else "since_baseline"
        fallback = checkpoint.get(fallback_scope)
        if isinstance(fallback, dict):
            selected_scope = fallback_scope
            since = fallback
        else:
            since = {}

    delta = checkpoint.get("delta")
    delta = delta if isinstance(delta, dict) else {}
    runtime = checkpoint.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}

    if selected_scope == "since_baseline":
        dd_key = "since_baseline_max_drawdown_usd"
        pnl_per_trade_key = "since_baseline_pnl_per_trade_usd"
        pnl_per_trade_cents_key = "since_baseline_pnl_per_trade_cents_per_share"
    else:
        dd_key = "since_supervisor_start_max_drawdown_usd"
        pnl_per_trade_key = "since_supervisor_start_pnl_per_trade_usd"
        pnl_per_trade_cents_key = "since_supervisor_start_pnl_per_trade_cents_per_share"

    exits = _to_int(since.get("exits"))
    net_pnl_usd = _to_float_opt(since.get("net_pnl_usd"))
    timeout_rate = _to_float_opt(since.get("timeout_rate"))
    profit_factor = _to_float_opt(since.get("profit_factor"))
    max_drawdown_usd = _to_float_opt(delta.get(dd_key))
    position_size_shares = _to_float_opt(runtime.get("job_position_size_shares"))

    pnl_per_trade_usd = _to_float_opt(since.get("pnl_per_trade_usd"))
    if pnl_per_trade_usd is None and exits > 0 and net_pnl_usd is not None:
        pnl_per_trade_usd = float(net_pnl_usd) / float(exits)
    if pnl_per_trade_usd is None:
        pnl_per_trade_usd = _to_float_opt(delta.get(pnl_per_trade_key))

    pnl_per_trade_cents_per_share = _to_float_opt(since.get("pnl_per_trade_cents_per_share"))
    if pnl_per_trade_cents_per_share is None and pnl_per_trade_usd is not None and (position_size_shares or 0.0) > 0.0:
        pnl_per_trade_cents_per_share = (100.0 * float(pnl_per_trade_usd)) / float(position_size_shares)
    if pnl_per_trade_cents_per_share is None:
        pnl_per_trade_cents_per_share = _to_float_opt(delta.get(pnl_per_trade_cents_key))

    return {
        "generated_local": str(checkpoint.get("generated_local") or ""),
        "window_hours": _to_float(checkpoint.get("window_hours"), 0.0),
        "metric_scope_requested": requested_scope,
        "metric_scope_used": selected_scope,
        "metric_scope_fallback": bool(selected_scope != requested_scope),
        "exits": int(exits),
        "entries": _to_int(since.get("entries")),
        "net_pnl_usd": net_pnl_usd,
        "timeout_rate": timeout_rate,
        "profit_factor": profit_factor,
        "max_drawdown_usd": max_drawdown_usd,
        "pnl_per_trade_usd": pnl_per_trade_usd,
        "pnl_per_trade_cents_per_share": pnl_per_trade_cents_per_share,
    }


def _cond_cmp(name: str, actual: Optional[float], threshold: float, op: str) -> dict:
    if actual is None:
        return {
            "name": name,
            "operator": op,
            "threshold": float(threshold),
            "actual": None,
            "pass": False,
            "status": "MISSING",
        }
    if op == ">":
        ok = float(actual) > float(threshold)
    elif op == ">=":
        ok = float(actual) >= float(threshold)
    elif op == "<":
        ok = float(actual) < float(threshold)
    elif op == "<=":
        ok = float(actual) <= float(threshold)
    else:
        raise ValueError(f"unsupported operator: {op}")
    return {
        "name": name,
        "operator": op,
        "threshold": float(threshold),
        "actual": float(actual),
        "pass": bool(ok),
        "status": "PASS" if ok else "FAIL",
    }


def _phase_from_exits(exits: int, mid: int, go: int, final: int) -> str:
    if exits >= final:
        return "FINAL_500"
    if exits >= go:
        return "GO_300"
    if exits >= mid:
        return "MID_150"
    return "PRECHECK"


def _fail_reasons(conditions: list[dict]) -> list[str]:
    out: list[str] = []
    for c in conditions:
        if not isinstance(c, dict):
            continue
        if bool(c.get("pass")):
            continue
        name = str(c.get("name") or "unknown")
        status = str(c.get("status") or "FAIL")
        out.append(f"{name} [{status}]")
    return out


def _stress_pass(stress_metrics: Optional[dict], min_net_pnl_usd: float, min_profit_factor: float) -> tuple[list[dict], bool]:
    if not isinstance(stress_metrics, dict):
        missing = [
            _cond_cmp("stress.net_pnl_usd", None, min_net_pnl_usd, ">"),
            _cond_cmp("stress.profit_factor", None, min_profit_factor, ">="),
        ]
        return missing, False
    conds = [
        _cond_cmp("stress.net_pnl_usd", _to_float_opt(stress_metrics.get("net_pnl_usd")), min_net_pnl_usd, ">"),
        _cond_cmp("stress.profit_factor", _to_float_opt(stress_metrics.get("profit_factor")), min_profit_factor, ">="),
    ]
    return conds, all(bool(c.get("pass")) for c in conds)


def _stage_pass_status(conditions: list[dict]) -> tuple[bool, bool]:
    has_fail = False
    has_missing = False
    for c in conditions:
        if not isinstance(c, dict):
            continue
        status = str(c.get("status") or "")
        if status == "FAIL":
            has_fail = True
        elif status == "MISSING":
            has_missing = True
    return has_fail, has_missing


def judge(args) -> dict:
    checkpoint = _load_json(args.checkpoint_json)
    if not checkpoint:
        raise RuntimeError(f"checkpoint JSON missing or invalid: {args.checkpoint_json}")
    metric_scope = str(args.metric_scope or "since_baseline").strip()
    candidate = _extract_metrics(checkpoint, metric_scope=metric_scope)

    control_metrics = None
    if str(args.control_checkpoint_json or "").strip():
        control_payload = _load_json(args.control_checkpoint_json)
        if control_payload:
            control_metrics = _extract_metrics(control_payload, metric_scope=metric_scope)

    stress_metrics = None
    if str(args.stress_checkpoint_json or "").strip():
        stress_payload = _load_json(args.stress_checkpoint_json)
        if stress_payload:
            stress_metrics = _extract_metrics(stress_payload, metric_scope=metric_scope)

    dd_ratio_vs_control = _dd_ratio_opt(
        _to_float_opt(candidate.get("max_drawdown_usd")),
        _to_float_opt(control_metrics.get("max_drawdown_usd")) if isinstance(control_metrics, dict) else None,
    )
    trade_ratio_vs_control = _ratio_opt(
        _to_float_opt(candidate.get("exits")),
        _to_float_opt(control_metrics.get("exits")) if isinstance(control_metrics, dict) else None,
    )

    exits = _to_int(candidate.get("exits"))
    phase = _phase_from_exits(
        exits=exits,
        mid=max(1, int(args.phase_trades_mid)),
        go=max(1, int(args.phase_trades_go)),
        final=max(1, int(args.phase_trades_final)),
    )

    common = {
        "pnl_per_trade_cents_per_share": _to_float_opt(candidate.get("pnl_per_trade_cents_per_share")),
        "timeout_rate": _to_float_opt(candidate.get("timeout_rate")),
        "profit_factor": _to_float_opt(candidate.get("profit_factor")),
        "net_pnl_usd": _to_float_opt(candidate.get("net_pnl_usd")),
        "max_drawdown_ratio_vs_control": dd_ratio_vs_control,
        "trade_ratio_vs_control": trade_ratio_vs_control,
    }

    stage_conditions: list[dict] = []
    decision = "PENDING"
    recommended_action = "Collect more closed trades before staged gate judgement."

    if phase == "MID_150":
        stage_conditions = [
            _cond_cmp("pnl_per_trade_cents_per_share", common["pnl_per_trade_cents_per_share"], float(args.mid_min_pnl_per_trade_cents), ">="),
            _cond_cmp("timeout_rate", common["timeout_rate"], float(args.mid_max_timeout_rate), "<="),
            _cond_cmp("profit_factor", common["profit_factor"], float(args.mid_min_profit_factor), ">="),
            _cond_cmp("max_drawdown_ratio_vs_control", common["max_drawdown_ratio_vs_control"], float(args.mid_max_dd_ratio_vs_control), "<="),
        ]
        has_fail, has_missing = _stage_pass_status(stage_conditions)
        if has_fail:
            decision = "STOP"
            recommended_action = "150-trade kill gate failed. Stop this arm and re-tune entry gates."
        elif has_missing:
            decision = "PENDING_EVIDENCE"
            recommended_action = "150-trade gate needs missing evidence (typically control checkpoint)."
        else:
            decision = "CONTINUE"
            recommended_action = "150-trade kill gate passed. Continue observe run to 300 closed trades."

    elif phase == "GO_300":
        stage_conditions = [
            _cond_cmp("net_pnl_usd", common["net_pnl_usd"], float(args.go_min_net_pnl_usd), ">"),
            _cond_cmp("pnl_per_trade_cents_per_share", common["pnl_per_trade_cents_per_share"], float(args.go_min_pnl_per_trade_cents), ">="),
            _cond_cmp("timeout_rate", common["timeout_rate"], float(args.go_max_timeout_rate), "<="),
            _cond_cmp("profit_factor", common["profit_factor"], float(args.go_min_profit_factor), ">="),
            _cond_cmp("max_drawdown_ratio_vs_control", common["max_drawdown_ratio_vs_control"], float(args.go_max_dd_ratio_vs_control), "<="),
            _cond_cmp("trade_ratio_vs_control", common["trade_ratio_vs_control"], float(args.go_min_trade_ratio_vs_control), ">="),
        ]
        has_fail, has_missing = _stage_pass_status(stage_conditions)
        if has_fail:
            decision = "NO_GO"
            recommended_action = "300-trade GO gate failed. Keep observe-only, stop promotion, and re-tune."
        elif has_missing:
            decision = "PENDING_EVIDENCE"
            recommended_action = "300-trade GO gate needs missing evidence (control checkpoint)."
        else:
            decision = "GO_CANDIDATE"
            recommended_action = "300-trade GO gate passed. Keep this arm running until 500 closed trades for final judgement."

    elif phase == "FINAL_500":
        stage_conditions = [
            _cond_cmp("net_pnl_usd", common["net_pnl_usd"], float(args.final_min_net_pnl_usd), ">"),
            _cond_cmp("pnl_per_trade_cents_per_share", common["pnl_per_trade_cents_per_share"], float(args.final_min_pnl_per_trade_cents), ">="),
            _cond_cmp("timeout_rate", common["timeout_rate"], float(args.final_max_timeout_rate), "<="),
            _cond_cmp("profit_factor", common["profit_factor"], float(args.final_min_profit_factor), ">="),
            _cond_cmp("max_drawdown_ratio_vs_control", common["max_drawdown_ratio_vs_control"], float(args.final_max_dd_ratio_vs_control), "<="),
        ]
        stress_conditions, stress_ok = _stress_pass(
            stress_metrics=stress_metrics,
            min_net_pnl_usd=float(args.final_stress_min_net_pnl_usd),
            min_profit_factor=float(args.final_stress_min_profit_factor),
        )
        stage_conditions.extend(stress_conditions)
        has_fail, has_missing = _stage_pass_status(stage_conditions)
        if has_fail:
            decision = "NO_GO"
            recommended_action = "Final 500-trade gate failed. NO_GO and return to regime/side redesign."
        elif has_missing or (not stress_ok):
            decision = "PENDING_EVIDENCE"
            recommended_action = "Final gate needs missing evidence (control/stress checkpoints)."
        else:
            decision = "GO"
            recommended_action = "Final 500-trade gate passed with stress replay support. GO."

    now_local = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fail_reasons = _fail_reasons(stage_conditions)
    summary = {
        "generated_local": now_local,
        "phase": phase,
        "decision": decision,
        "exits_closed_trades": exits,
        "phase_thresholds": {
            "mid": int(args.phase_trades_mid),
            "go": int(args.phase_trades_go),
            "final": int(args.phase_trades_final),
        },
        "remaining_to_mid": int(max(0, int(args.phase_trades_mid) - exits)),
        "remaining_to_go": int(max(0, int(args.phase_trades_go) - exits)),
        "remaining_to_final": int(max(0, int(args.phase_trades_final) - exits)),
        "metric_scope_requested": metric_scope,
        "metric_scope_used": str(candidate.get("metric_scope_used") or metric_scope),
        "metric_scope_fallback": bool(candidate.get("metric_scope_fallback")),
        "has_control_checkpoint": bool(control_metrics is not None),
        "has_stress_checkpoint": bool(stress_metrics is not None),
        "candidate_metrics": candidate,
        "control_metrics": control_metrics,
        "stress_metrics": stress_metrics,
        "derived_metrics": {
            "max_drawdown_ratio_vs_control": dd_ratio_vs_control,
            "trade_ratio_vs_control": trade_ratio_vs_control,
        },
        "failed_conditions": fail_reasons,
    }

    return {
        "decision": decision,
        "phase": phase,
        "summary": summary,
        "conditions": stage_conditions,
        "recommended_action": recommended_action,
    }


def _fmt(v: Optional[float], prec: int = 4) -> str:
    if v is None:
        return "n/a"
    if math.isinf(v):
        return "inf"
    return f"{v:.{prec}f}"


def render_text(result: dict) -> str:
    summary = result.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    candidate = summary.get("candidate_metrics")
    candidate = candidate if isinstance(candidate, dict) else {}
    derived = summary.get("derived_metrics")
    derived = derived if isinstance(derived, dict) else {}
    conditions = result.get("conditions")
    conditions = conditions if isinstance(conditions, list) else []

    lines: list[str] = []
    lines.append("Fade Long-Only Staged Checkpoint Judge (observe-only)")
    lines.append(
        f"Phase: {str(result.get('phase') or 'UNKNOWN')} | Decision: {str(result.get('decision') or 'PENDING')}"
    )
    lines.append(
        "Metric scope: "
        f"requested={str(summary.get('metric_scope_requested') or 'n/a')} "
        f"used={str(summary.get('metric_scope_used') or 'n/a')} "
        f"fallback={'yes' if bool(summary.get('metric_scope_fallback')) else 'no'}"
    )
    lines.append(
        "Closed trades: "
        f"{_to_int(summary.get('exits_closed_trades'))} "
        f"(mid={_to_int((summary.get('phase_thresholds') or {}).get('mid'))}, "
        f"go={_to_int((summary.get('phase_thresholds') or {}).get('go'))}, "
        f"final={_to_int((summary.get('phase_thresholds') or {}).get('final'))})"
    )
    lines.append(
        "Selected scope metrics: "
        f"net_pnl={_fmt(_to_float_opt(candidate.get('net_pnl_usd')))} "
        f"pnl/trade(c/share)={_fmt(_to_float_opt(candidate.get('pnl_per_trade_cents_per_share')))} "
        f"timeout={_fmt(100.0 * _to_float_opt(candidate.get('timeout_rate')), 2) if _to_float_opt(candidate.get('timeout_rate')) is not None else 'n/a'}% "
        f"pf={_fmt(_to_float_opt(candidate.get('profit_factor')), 3)} "
        f"dd={_fmt(_to_float_opt(candidate.get('max_drawdown_usd')))}"
    )
    lines.append(
        "Control ratios: "
        f"trade_ratio={_fmt(_to_float_opt(derived.get('trade_ratio_vs_control')), 3)} "
        f"dd_ratio={_fmt(_to_float_opt(derived.get('max_drawdown_ratio_vs_control')), 3)} "
        f"(control={'yes' if bool(summary.get('has_control_checkpoint')) else 'no'})"
    )
    if str(result.get("phase") or "") == "FINAL_500":
        lines.append(f"Stress checkpoint: {'yes' if bool(summary.get('has_stress_checkpoint')) else 'no'}")

    if conditions:
        lines.append("Conditions:")
        for c in conditions:
            if not isinstance(c, dict):
                continue
            status = str(c.get("status") or "UNKNOWN")
            name = str(c.get("name") or "unknown")
            op = str(c.get("operator") or "?")
            actual = _to_float_opt(c.get("actual"))
            threshold = _to_float_opt(c.get("threshold"))
            lines.append(f"- {status}: {name} ({_fmt(actual)} {op} {_fmt(threshold)})")

    failed = summary.get("failed_conditions")
    if isinstance(failed, list) and failed:
        lines.append("Failed conditions:")
        for row in failed:
            lines.append(f"- {str(row)}")

    lines.append(f"Recommended Action: {str(result.get('recommended_action') or '')}")
    return "\n".join(lines)


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    logs = repo / "logs"
    p = argparse.ArgumentParser(description="Judge fade long-only staged checkpoint decision")
    p.add_argument(
        "--checkpoint-json",
        default=str(logs / "fade_longonly_24h_eval_current_latest.json"),
        help="Checkpoint JSON produced by report_fade_longonly_checkpoint.py",
    )
    p.add_argument(
        "--metric-scope",
        choices=["since_baseline", "since_supervisor_start"],
        default="since_baseline",
        help="Metric scope used for staged trade/PnL/PF/timeout/DD checks",
    )
    p.add_argument(
        "--control-checkpoint-json",
        default="",
        help="Optional control checkpoint JSON for DD/trade ratio checks",
    )
    p.add_argument(
        "--stress-checkpoint-json",
        default="",
        help="Optional stress replay checkpoint JSON for final gate checks",
    )
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

    p.add_argument(
        "--out-json",
        default=str(logs / "fade_longonly_checkpoint_decision_latest.json"),
        help="Output decision JSON path",
    )
    p.add_argument(
        "--out-txt",
        default=str(logs / "fade_longonly_checkpoint_decision_latest.txt"),
        help="Output decision text path",
    )
    args = p.parse_args()

    result = judge(args)
    text = render_text(result)
    print(text)

    out_json = Path(args.out_json)
    out_txt = Path(args.out_txt)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out_txt.write_text(text + "\n", encoding="utf-8")
    print(f"out_json={out_json}")
    print(f"out_txt={out_txt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
