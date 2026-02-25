#!/usr/bin/env python3
"""
Judge practical deployment readiness of weather Top30 consensus output.

Observe-only helper:
  - reads consensus watchlist JSON
  - applies configurable quantitative and operational gates
  - writes decision JSON under logs/
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
from pathlib import Path
from typing import Dict, List, Sequence, Tuple


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_tag() -> str:
    return now_utc().strftime("%Y%m%d_%H%M%S")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def logs_dir() -> Path:
    p = repo_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def as_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def as_bool(v, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off"}:
            return False
    return default


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_input_path(raw: str, default_path: Path) -> Path:
    if not raw.strip():
        return default_path
    p = Path(raw)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs_dir() / p.name
    return repo_root() / p


def resolve_output_path(raw: str, default_path: Path) -> Path:
    if not raw.strip():
        return default_path
    p = Path(raw)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs_dir() / p.name
    return repo_root() / p


def safe_mean(values: Sequence[float]) -> float:
    xs = [x for x in values if math.isfinite(x)]
    if not xs:
        return math.nan
    return float(sum(xs)) / float(len(xs))


def safe_median(values: Sequence[float]) -> float:
    xs = [x for x in values if math.isfinite(x)]
    if not xs:
        return math.nan
    return float(statistics.median(xs))


def infer_profile_name(consensus_path: Path) -> str:
    stem = consensus_path.stem
    suffix = "_consensus_watchlist_latest"
    if stem.endswith(suffix):
        base = stem[: -len(suffix)]
        if base:
            return base
    return "weather_top30"


def rank_key(row: dict, fallback_rank: int) -> int:
    r = as_float(row.get("rank"), math.nan)
    if math.isfinite(r) and r > 0:
        return int(r)
    return int(fallback_rank)


def compute_metrics(rows: List[dict]) -> dict:
    row_count = len(rows)
    both_count = 0
    no_count = 0
    yes_count = 0
    other_side_count = 0

    net_yields: List[float] = []
    max_profits: List[float] = []
    liquidities: List[float] = []
    volumes: List[float] = []
    hours_to_end: List[float] = []
    scores: List[float] = []

    ordered_rows = sorted(
        list(enumerate(rows, start=1)),
        key=lambda x: rank_key(x[1], x[0]),
    )
    top10_rows = [row for _, row in ordered_rows[: min(10, row_count)]]
    top10_max_profits = [as_float(r.get("max_profit"), math.nan) for r in top10_rows]
    top10_scores = [as_float(r.get("score_total"), math.nan) for r in top10_rows]

    for row in rows:
        in_no = as_bool(row.get("in_no_longshot"), False)
        in_late = as_bool(row.get("in_lateprob"), False)
        if in_no and in_late:
            both_count += 1

        side = str(row.get("side_hint") or "").strip().lower()
        if side == "no":
            no_count += 1
        elif side == "yes":
            yes_count += 1
        else:
            other_side_count += 1

        net_yields.append(as_float(row.get("net_yield_per_day"), math.nan))
        max_profits.append(as_float(row.get("max_profit"), math.nan))
        liquidities.append(as_float(row.get("liquidity_num"), math.nan))
        volumes.append(as_float(row.get("volume_24h"), math.nan))
        hours_to_end.append(as_float(row.get("hours_to_end"), math.nan))
        scores.append(as_float(row.get("score_total"), math.nan))

    both_ratio = (float(both_count) / float(row_count)) if row_count > 0 else 0.0
    yes_ratio = (float(yes_count) / float(row_count)) if row_count > 0 else 0.0
    no_ratio = (float(no_count) / float(row_count)) if row_count > 0 else 0.0

    return {
        "row_count": row_count,
        "both_count": both_count,
        "both_ratio": both_ratio,
        "side_no_count": no_count,
        "side_yes_count": yes_count,
        "side_other_count": other_side_count,
        "side_no_ratio": no_ratio,
        "side_yes_ratio": yes_ratio,
        "median_net_yield_per_day": safe_median(net_yields),
        "median_max_profit": safe_median(max_profits),
        "median_liquidity": safe_median(liquidities),
        "median_volume_24h": safe_median(volumes),
        "median_hours_to_end": safe_median(hours_to_end),
        "median_score_total": safe_median(scores),
        "top10_avg_max_profit": safe_mean(top10_max_profits),
        "top10_avg_score_total": safe_mean(top10_scores),
    }


def detect_execution_ready_from_supervisor(supervisor_path: Path) -> Tuple[bool, List[str], str]:
    if not supervisor_path.exists():
        return False, [], "supervisor config not found"

    try:
        payload = load_json(supervisor_path)
    except Exception:
        return False, [], "supervisor config unreadable"

    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else []
    hits: List[str] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_name = str(job.get("name") or "unnamed")
        cmd = job.get("command")
        if isinstance(cmd, list):
            tokens = [str(x).strip().lower() for x in cmd]
        elif isinstance(cmd, str):
            tokens = [cmd.strip().lower()]
        else:
            tokens = []
        joined = " ".join(tokens)
        if "--execute" in tokens or "--confirm-live" in tokens or "confirm-live yes" in joined:
            hits.append(job_name)

    if hits:
        return True, hits, "execution flags detected in supervisor command set"
    return False, [], "no execution flags found in supervisor command set"


def validate_execution_plan(payload: dict) -> Tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "execution plan is not an object"

    if not as_bool(payload.get("ready_for_deploy"), False):
        return False, "ready_for_deploy is not true"

    reviewed = str(payload.get("reviewed_at_utc") or "").strip()
    if not reviewed:
        return False, "reviewed_at_utc is missing"

    guards = payload.get("guards") if isinstance(payload.get("guards"), dict) else {}
    required_guard_keys = (
        "require_explicit_execute_flag",
        "require_confirm_live",
        "default_execute_off",
    )
    for k in required_guard_keys:
        if not as_bool(guards.get(k), False):
            return False, f"guards.{k} is not true"

    checklist = payload.get("checklist") if isinstance(payload.get("checklist"), dict) else {}
    required_check_keys = (
        "risk_limits_defined",
        "order_size_defined",
        "rollback_defined",
        "monitoring_defined",
        "operator_ack",
    )
    for k in required_check_keys:
        if not as_bool(checklist.get(k), False):
            return False, f"checklist.{k} is not true"

    return True, "execution plan file validated"


def detect_execution_ready_from_plan_file(execution_plan_file: Path) -> Tuple[bool, str]:
    if not execution_plan_file.exists():
        return False, "execution plan file not found"

    try:
        payload = load_json(execution_plan_file)
    except Exception:
        return False, "execution plan file unreadable"

    return validate_execution_plan(payload)


def gate_result(name: str, metric: float, op: str, threshold: float, hard: bool) -> dict:
    if not math.isfinite(metric):
        passed = False
    elif op == ">=":
        passed = metric >= threshold
    elif op == "<=":
        passed = metric <= threshold
    else:
        raise ValueError(f"unsupported op: {op}")
    return {
        "name": name,
        "op": op,
        "metric": metric,
        "threshold": threshold,
        "passed": bool(passed),
        "hard": bool(hard),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Judge weather Top30 practical deployment readiness (observe-only).")
    p.add_argument(
        "--consensus-json",
        default="logs/weather_7acct_auto_consensus_watchlist_latest.json",
        help="Input watchlist JSON from build_weather_consensus_watchlist.py",
    )
    p.add_argument(
        "--supervisor-config",
        default="",
        help="Optional supervisor config for execution-readiness check",
    )
    p.add_argument(
        "--execution-plan-file",
        default="",
        help="Optional execution plan JSON. If valid, this satisfies execution_plan_present without live flags.",
    )
    p.add_argument("--min-row-count", type=int, default=20)
    p.add_argument("--min-both-ratio", type=float, default=0.70)
    p.add_argument("--min-median-net-yield-per-day", type=float, default=0.05)
    p.add_argument("--min-top10-avg-max-profit", type=float, default=0.07)
    p.add_argument("--min-median-liquidity", type=float, default=700.0)
    p.add_argument("--min-median-volume-24h", type=float, default=500.0)
    p.add_argument("--max-median-hours-to-end", type=float, default=30.0)
    exec_group = p.add_mutually_exclusive_group()
    exec_group.add_argument("--require-execution-plan", dest="require_execution_plan", action="store_true")
    exec_group.add_argument("--no-require-execution-plan", dest="require_execution_plan", action="store_false")
    p.set_defaults(require_execution_plan=True)
    p.add_argument("--out-json", default="", help="Output JSON path (simple filename goes under logs/)")
    p.add_argument(
        "--out-latest-json",
        default="",
        help="Optional latest-pointer JSON path (simple filename goes under logs/)",
    )
    p.add_argument("--pretty", action="store_true")
    args = p.parse_args()

    consensus_path = resolve_input_path(
        args.consensus_json,
        logs_dir() / "weather_7acct_auto_consensus_watchlist_latest.json",
    )
    if not consensus_path.exists():
        print(f"Consensus JSON not found: {consensus_path}")
        return 2

    payload = load_json(consensus_path)
    rows = payload.get("top") if isinstance(payload.get("top"), list) else []
    rows = [r for r in rows if isinstance(r, dict)]
    profile_name = infer_profile_name(consensus_path)

    default_supervisor = logs_dir() / f"bot_supervisor.{profile_name}.observe.json"
    supervisor_path = resolve_input_path(args.supervisor_config, default_supervisor)
    default_execution_plan = logs_dir() / f"{profile_name}_execution_plan_latest.json"
    execution_plan_file = resolve_input_path(args.execution_plan_file, default_execution_plan)

    execution_ready_supervisor, execution_jobs, supervisor_note = detect_execution_ready_from_supervisor(supervisor_path)
    execution_ready_plan, execution_plan_note = detect_execution_ready_from_plan_file(execution_plan_file)
    execution_ready = bool(execution_ready_supervisor or execution_ready_plan)
    if execution_ready:
        source_parts = []
        if execution_ready_supervisor:
            source_parts.append("supervisor")
        if execution_ready_plan:
            source_parts.append("plan_file")
        execution_note = f"execution readiness source={'+'.join(source_parts)}"
    else:
        execution_note = f"supervisor={supervisor_note}; plan_file={execution_plan_note}"

    metrics = compute_metrics(rows)
    gates: List[dict] = []
    gates.append(gate_result("row_count", float(metrics["row_count"]), ">=", float(args.min_row_count), hard=True))
    gates.append(gate_result("both_ratio", float(metrics["both_ratio"]), ">=", float(args.min_both_ratio), hard=True))
    gates.append(
        gate_result(
            "median_net_yield_per_day",
            float(metrics["median_net_yield_per_day"]),
            ">=",
            float(args.min_median_net_yield_per_day),
            hard=True,
        )
    )
    gates.append(
        gate_result(
            "top10_avg_max_profit",
            float(metrics["top10_avg_max_profit"]),
            ">=",
            float(args.min_top10_avg_max_profit),
            hard=True,
        )
    )
    gates.append(
        gate_result(
            "median_liquidity",
            float(metrics["median_liquidity"]),
            ">=",
            float(args.min_median_liquidity),
            hard=True,
        )
    )
    gates.append(
        gate_result(
            "median_volume_24h",
            float(metrics["median_volume_24h"]),
            ">=",
            float(args.min_median_volume_24h),
            hard=True,
        )
    )
    gates.append(
        gate_result(
            "median_hours_to_end",
            float(metrics["median_hours_to_end"]),
            "<=",
            float(args.max_median_hours_to_end),
            hard=True,
        )
    )

    observe_only_meta = False
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    observe_only_meta = as_bool(meta.get("observe_only"), False)
    gates.append(
        {
            "name": "observe_only_meta",
            "op": "==",
            "metric": observe_only_meta,
            "threshold": True,
            "passed": bool(observe_only_meta),
            "hard": True,
        }
    )

    gates.append(
        {
            "name": "execution_plan_present",
            "op": "==",
            "metric": execution_ready,
            "threshold": bool(args.require_execution_plan),
            "passed": bool(execution_ready) if args.require_execution_plan else True,
            "hard": bool(args.require_execution_plan),
        }
    )

    hard_gates = [g for g in gates if bool(g.get("hard"))]
    hard_failed = [g for g in hard_gates if not bool(g.get("passed"))]
    decision = "GO" if not hard_failed else "NO_GO"

    reason_lines: List[str] = []
    if hard_failed:
        names = ", ".join(str(g["name"]) for g in hard_failed)
        reason_lines.append(f"hard gate failed: {names}")
    else:
        reason_lines.append("all hard gates passed")
    decision_reason = "; ".join(reason_lines)

    tag = utc_tag()
    out_default = logs_dir() / f"{profile_name}_top30_readiness_{tag}.json"
    latest_default = logs_dir() / f"{profile_name}_top30_readiness_latest.json"
    out_json = resolve_output_path(args.out_json, out_default)
    out_latest_json = resolve_output_path(args.out_latest_json, latest_default)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_latest_json.parent.mkdir(parents=True, exist_ok=True)

    result = {
        "generated_utc": now_utc().isoformat(),
        "meta": {
            "observe_only": True,
            "source": "scripts/judge_weather_top30_readiness.py",
            "profile_name": profile_name,
            "consensus_json": str(consensus_path),
            "supervisor_config": str(supervisor_path),
            "execution_plan_file": str(execution_plan_file),
        },
        "thresholds": {
            "min_row_count": int(args.min_row_count),
            "min_both_ratio": float(args.min_both_ratio),
            "min_median_net_yield_per_day": float(args.min_median_net_yield_per_day),
            "min_top10_avg_max_profit": float(args.min_top10_avg_max_profit),
            "min_median_liquidity": float(args.min_median_liquidity),
            "min_median_volume_24h": float(args.min_median_volume_24h),
            "max_median_hours_to_end": float(args.max_median_hours_to_end),
            "require_execution_plan": bool(args.require_execution_plan),
        },
        "metrics": metrics,
        "execution_check": {
            "ready": bool(execution_ready),
            "jobs_with_execute_flags": execution_jobs,
            "note": execution_note,
            "supervisor_ready": bool(execution_ready_supervisor),
            "supervisor_note": supervisor_note,
            "plan_file_ready": bool(execution_ready_plan),
            "plan_file_note": execution_plan_note,
        },
        "gates": gates,
        "decision": decision,
        "decision_reason": decision_reason,
        "artifacts": {
            "out_json": str(out_json),
            "latest_json": str(out_latest_json),
        },
    }

    with out_json.open("w", encoding="utf-8") as f:
        if args.pretty:
            json.dump(result, f, ensure_ascii=False, indent=2)
        else:
            json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    with out_latest_json.open("w", encoding="utf-8") as f:
        if args.pretty:
            json.dump(result, f, ensure_ascii=False, indent=2)
        else:
            json.dump(result, f, ensure_ascii=False, separators=(",", ":"))

    hard_pass = len([g for g in hard_gates if bool(g.get("passed"))])
    print(f"[readiness] decision={decision} hard_pass={hard_pass}/{len(hard_gates)}")
    for g in gates:
        status = "PASS" if bool(g.get("passed")) else "FAIL"
        op = str(g.get("op"))
        print(
            f"[gate] {status:4s} {g['name']} metric={g['metric']} op={op} threshold={g['threshold']} hard={g['hard']}"
        )
    print(f"[readiness] {decision_reason}")
    print(f"[readiness] wrote {out_json}")
    print(f"[readiness] wrote {out_latest_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
