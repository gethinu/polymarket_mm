#!/usr/bin/env python3
"""
One-command morning strategy gate check (observe-only).

Default behavior:
1) refresh realized snapshot and strategy register snapshot
2) print concise gate and readiness summary
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_repo_path(raw: str, default_rel: str) -> Path:
    s = (raw or "").strip()
    if not s:
        return repo_root() / default_rel
    p = Path(s)
    if p.is_absolute():
        return p
    return repo_root() / p


def run_cmd(args: list[str]) -> int:
    proc = subprocess.run(
        args,
        cwd=str(repo_root()),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        print(f"[morning-check] command failed: {' '.join(args)}")
        if proc.stdout.strip():
            print(proc.stdout.strip())
        if proc.stderr.strip():
            print(proc.stderr.strip())
    return int(proc.returncode)


def load_json(path: Path) -> Dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("snapshot JSON root must be object")
    return raw


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _as_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        x = float(v)
    except Exception:
        return default
    if x != x or x in (float("inf"), float("-inf")):
        return default
    return x


def fmt_ratio_pct(v: Any) -> str:
    x = _as_float(v, None)
    if x is None:
        return "n/a"
    return f"{x * 100.0:+.2f}%"


def no_longshot_confidence_label(resolved_trades: int) -> str:
    n = max(0, int(resolved_trades))
    if n >= 30:
        return "HIGH"
    if n >= 10:
        return "MEDIUM"
    if n >= 3:
        return "LOW"
    return "VERY_LOW"


def real_capital_gate_decision(
    gate_decision_3stage: str,
    resolved_trades: int,
    health_decision: str,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if str(gate_decision_3stage or "").strip() != "READY_FINAL":
        reasons.append(f"strategy_stage={gate_decision_3stage or 'UNKNOWN'}")
    if int(resolved_trades) < 30:
        reasons.append(f"rolling_30d_resolved_trades={int(resolved_trades)}<30")
    if str(health_decision or "").strip() not in {"GO", "SKIPPED"}:
        reasons.append(f"automation_health={health_decision or 'UNKNOWN'}")
    return ("HOLD", reasons) if reasons else ("ELIGIBLE_REVIEW", [])


def _count_condition_passes(conditions: dict[str, Any]) -> tuple[int, int]:
    total = 0
    passed = 0
    for v in conditions.values():
        if not isinstance(v, dict):
            continue
        total += 1
        if bool(v.get("pass")):
            passed += 1
    return passed, total


def _file_age_hours(path: Path) -> Optional[float]:
    try:
        mtime = dt.datetime.fromtimestamp(path.stat().st_mtime)
    except Exception:
        return None
    now = dt.datetime.now()
    return max(0.0, float((now - mtime).total_seconds() / 3600.0))


def main() -> int:
    p = argparse.ArgumentParser(description="Morning strategy gate check (observe-only).")
    p.add_argument("--no-refresh", action="store_true", help="Skip refresh commands and only read existing snapshot.")
    p.add_argument("--skip-health", action="store_true", help="Skip automation health refresh/check.")
    p.add_argument("--skip-gate-alarm", action="store_true", help="Skip strategy gate alarm refresh/check.")
    p.add_argument(
        "--skip-implementation-ledger",
        action="store_true",
        help="Skip implementation ledger refresh (render_implementation_ledger.py).",
    )
    p.add_argument("--strategy-id", default="weather_clob_arb_buckets_observe", help="Target strategy id for gate eval.")
    p.add_argument("--min-realized-days", type=int, default=30, help="Required realized days for gate.")
    p.add_argument("--skip-process-scan", action="store_true", help="Pass --skip-process-scan to strategy snapshot render.")
    p.add_argument(
        "--snapshot-json",
        default="logs/strategy_register_latest.json",
        help="Strategy register snapshot JSON path.",
    )
    p.add_argument(
        "--fail-on-gate-not-ready",
        action="store_true",
        help="Exit non-zero when realized_30d_gate is not READY_FOR_JUDGMENT.",
    )
    p.add_argument(
        "--health-json",
        default="logs/automation_health_latest.json",
        help="Automation health JSON path.",
    )
    p.add_argument(
        "--gate-alarm-state-json",
        default="logs/strategy_gate_alarm_state.json",
        help="Strategy gate alarm state JSON path.",
    )
    p.add_argument(
        "--gate-alarm-log-file",
        default="logs/strategy_gate_alarm.log",
        help="Strategy gate alarm log file path.",
    )
    p.add_argument(
        "--discord-gate-alarm",
        action="store_true",
        help="Pass --discord to check_strategy_gate_alarm.py (transition notification).",
    )
    p.add_argument(
        "--discord-webhook-env",
        default="",
        help="Pass --discord-webhook-env to check_strategy_gate_alarm.py.",
    )
    p.add_argument(
        "--fail-on-health-no-go",
        action="store_true",
        help="Exit non-zero when automation health decision is not GO.",
    )
    p.add_argument(
        "--fail-on-stage-not-final",
        action="store_true",
        help="Exit non-zero when realized_30d_gate.decision_3stage is not READY_FINAL.",
    )
    p.add_argument(
        "--skip-simmer-ab",
        action="store_true",
        help="Skip Simmer A/B decision summary readout.",
    )
    p.add_argument(
        "--simmer-ab-decision-json",
        default="logs/simmer-ab-decision-latest.json",
        help="Simmer A/B decision JSON path.",
    )
    p.add_argument(
        "--fail-on-simmer-ab-final-no-go",
        action="store_true",
        help="Exit non-zero when Simmer A/B decision is FINAL and not GO (or decision JSON is missing/invalid).",
    )
    p.add_argument(
        "--simmer-ab-max-stale-hours",
        type=float,
        default=30.0,
        help="Maximum allowed age for Simmer A/B decision JSON when fail-on-simmer gate is enabled.",
    )
    args = p.parse_args()

    if not args.no_refresh:
        rc = run_cmd(["python", "scripts/record_simmer_realized_daily.py"])
        if rc != 0:
            return rc
        rc = run_cmd(
            [
                "python",
                "scripts/materialize_strategy_realized_daily.py",
                "--strategy-id",
                str(args.strategy_id),
                "--source-jsonl",
                "logs/clob_arb_realized_daily.jsonl",
                "--out-jsonl",
                "logs/strategy_realized_pnl_daily.jsonl",
                "--out-latest-json",
                "logs/strategy_realized_latest.json",
            ]
        )
        if rc != 0:
            return rc
        cmd = [
            "python",
            "scripts/render_strategy_register_snapshot.py",
            "--realized-strategy-id",
            str(args.strategy_id),
            "--min-realized-days",
            str(max(1, int(args.min_realized_days))),
        ]
        if args.skip_process_scan:
            cmd.append("--skip-process-scan")
        rc = run_cmd(cmd)
        if rc != 0:
            return rc
        if not args.skip_gate_alarm:
            alarm_cmd = [
                "python",
                "scripts/check_strategy_gate_alarm.py",
                "--snapshot-json",
                str(args.snapshot_json),
                "--state-json",
                str(args.gate_alarm_state_json),
                "--log-file",
                str(args.gate_alarm_log_file),
                "--strategy-id",
                str(args.strategy_id),
            ]
            if args.discord_gate_alarm:
                alarm_cmd.append("--discord")
                discord_webhook_env = str(args.discord_webhook_env or "").strip()
                if discord_webhook_env:
                    alarm_cmd.extend(["--discord-webhook-env", discord_webhook_env])
            rc = run_cmd(alarm_cmd)
            if rc != 0:
                return rc
        if not args.skip_implementation_ledger:
            rc = run_cmd(["python", "scripts/render_implementation_ledger.py"])
            if rc != 0:
                return rc

    health_payload: Dict[str, Any] = {}
    if not args.skip_health:
        rc = run_cmd(["python", "scripts/report_automation_health.py"])
        if rc != 0:
            return rc
        health_path = resolve_repo_path(str(args.health_json), "logs/automation_health_latest.json")
        if health_path.exists():
            try:
                health_payload = load_json(health_path)
            except Exception:
                health_payload = {}

    snapshot_path = resolve_repo_path(str(args.snapshot_json), "logs/strategy_register_latest.json")
    if not snapshot_path.exists():
        print(f"[morning-check] snapshot not found: {snapshot_path}")
        return 2

    try:
        payload = load_json(snapshot_path)
    except Exception as exc:
        print(f"[morning-check] failed to parse snapshot: {exc}")
        return 2

    gate = payload.get("realized_30d_gate") if isinstance(payload.get("realized_30d_gate"), dict) else {}
    monthly = payload.get("realized_monthly_return") if isinstance(payload.get("realized_monthly_return"), dict) else {}
    no_longshot = payload.get("no_longshot_status") if isinstance(payload.get("no_longshot_status"), dict) else {}
    readiness = payload.get("readiness") if isinstance(payload.get("readiness"), dict) else {}
    readiness_summary = readiness.get("summary") if isinstance(readiness.get("summary"), dict) else {}
    strict = readiness_summary.get("strict") if isinstance(readiness_summary.get("strict"), dict) else {}
    quality = readiness_summary.get("quality") if isinstance(readiness_summary.get("quality"), dict) else {}

    gate_decision = str(gate.get("decision") or "UNKNOWN")
    gate_decision_3 = str(gate.get("decision_3stage") or "UNKNOWN")
    gate_decision_3_ja = str(gate.get("decision_3stage_label_ja") or "-")
    stage_label = str(gate.get("stage_label") or "-")
    stage_label_ja = str(gate.get("stage_label_ja") or "-")
    obs_days = _as_int(gate.get("observed_realized_days"), 0)
    min_days = _as_int(gate.get("min_realized_days"), _as_int(args.min_realized_days, 30))
    next_stage = gate.get("next_stage") if isinstance(gate.get("next_stage"), dict) else {}
    next_label = str(next_stage.get("label") or "-")
    next_label_ja = str(next_stage.get("label_ja") or "-")
    next_remain = _as_int(next_stage.get("remaining_days"), -1)
    proj_monthly_text = str(monthly.get("projected_monthly_return_text") or fmt_ratio_pct(monthly.get("projected_monthly_return_ratio")))
    rolling_30d_text = str(monthly.get("rolling_30d_return_text") or fmt_ratio_pct(monthly.get("rolling_30d_return_ratio")))
    no_longshot_monthly_text = str(
        no_longshot.get("monthly_return_now_text")
        or fmt_ratio_pct(no_longshot.get("monthly_return_now_ratio"))
    )
    no_longshot_source = str(no_longshot.get("monthly_return_now_source") or "-")
    no_longshot_obs_days_val = no_longshot.get("observed_days")
    no_longshot_obs_days_txt = (
        str(_as_int(no_longshot_obs_days_val, 0)) if no_longshot_obs_days_val is not None else "n/a"
    )
    no_longshot_resolved = _as_int(
        no_longshot.get("rolling_30d_resolved_trades"),
        _as_int(no_longshot.get("resolved_positions"), 0),
    )
    no_longshot_open = _as_int(no_longshot.get("open_positions"), 0)
    no_longshot_conf = no_longshot_confidence_label(no_longshot_resolved)

    strict_go = _as_int(strict.get("go_count"), 0)
    strict_cnt = _as_int(strict.get("count"), 0)
    quality_go = _as_int(quality.get("go_count"), 0)
    quality_cnt = _as_int(quality.get("count"), 0)

    print("[morning-check] strategy gate summary")
    print(f"strategy_id={args.strategy_id}")
    print(
        "gate="
        f"{gate_decision} (3stage={gate_decision_3} [{gate_decision_3_ja}], "
        f"stage={stage_label} [{stage_label_ja}])"
    )
    print(
        "observed_days="
        f"{obs_days}/{min_days} "
        f"next_stage={next_label_ja}/{next_label} "
        f"remaining_days={next_remain if next_remain >= 0 else 'n/a'}"
    )
    print(f"strategy_projected_monthly={proj_monthly_text} strategy_rolling_30d={rolling_30d_text}")
    print(
        "no_longshot_monthly_now="
        f"{no_longshot_monthly_text} source={no_longshot_source} "
        f"rolling_30d_resolved={no_longshot_resolved} open_positions={no_longshot_open} "
        f"observed_days={no_longshot_obs_days_txt}"
    )
    print(f"no_longshot_confidence={no_longshot_conf} threshold_resolved_trades>=30")
    print(f"readiness_strict=GO {strict_go}/{strict_cnt} readiness_quality=GO {quality_go}/{quality_cnt}")
    print(f"snapshot={snapshot_path}")

    simmer_decision_available = False
    simmer_decision_value = "UNKNOWN"
    simmer_decision_stage = "UNKNOWN"
    simmer_final_ready = False
    simmer_file_age_hours: Optional[float] = None

    if not args.skip_simmer_ab:
        simmer_decision_path = resolve_repo_path(
            str(args.simmer_ab_decision_json),
            "logs/simmer-ab-decision-latest.json",
        )
        if simmer_decision_path.exists():
            try:
                simmer_file_age_hours = _file_age_hours(simmer_decision_path)
                simmer_payload = load_json(simmer_decision_path)
                simmer_decision_available = True
                simmer_decision = str(simmer_payload.get("decision") or "UNKNOWN")
                simmer_decision_value = simmer_decision
                simmer_summary = (
                    simmer_payload.get("summary")
                    if isinstance(simmer_payload.get("summary"), dict)
                    else {}
                )
                simmer_conditions = (
                    simmer_payload.get("conditions")
                    if isinstance(simmer_payload.get("conditions"), dict)
                    else {}
                )
                simmer_cov = (
                    simmer_summary.get("coverage_data_sufficient")
                    if isinstance(simmer_summary.get("coverage_data_sufficient"), dict)
                    else {}
                )
                simmer_days = _as_int(simmer_summary.get("data_sufficient_days"), 0)
                simmer_min_days = _as_int(simmer_summary.get("min_days_required"), 0)
                simmer_timing = str(simmer_summary.get("decision_timing_status") or "UNKNOWN")
                simmer_decision_date = str(simmer_summary.get("decision_date") or "N/A")
                simmer_today = str(simmer_summary.get("today_local") or "N/A")
                simmer_days_until = _as_int(simmer_summary.get("days_until_decision"), 0)
                simmer_stage = str(simmer_summary.get("decision_stage") or "UNKNOWN")
                simmer_decision_stage = simmer_stage
                simmer_final_ready = bool(simmer_summary.get("final_decision_ready"))
                simmer_passed, simmer_total = _count_condition_passes(simmer_conditions)
                simmer_action = str(simmer_payload.get("recommended_action") or "-")
                print(
                    "simmer_ab_decision="
                    f"{simmer_decision} "
                    f"data_sufficient_days={simmer_days}/{simmer_min_days} "
                    f"conditions_pass={simmer_passed}/{simmer_total}"
                )
                print(
                    "simmer_ab_coverage_data_sufficient="
                    f"{str(simmer_cov.get('since') or 'N/A')} -> "
                    f"{str(simmer_cov.get('until') or 'N/A')}"
                )
                print(
                    "simmer_ab_timing="
                    f"{simmer_timing} "
                    f"today={simmer_today} decision_date={simmer_decision_date} "
                    f"days_until={simmer_days_until:+d} "
                    f"final_ready={'yes' if simmer_final_ready else 'no'} stage={simmer_stage}"
                )
                print(
                    "simmer_ab_freshness_hours="
                    f"{(simmer_file_age_hours if simmer_file_age_hours is not None else -1.0):.2f} "
                    f"max_allowed={float(args.simmer_ab_max_stale_hours):.2f}"
                )
                print(f"simmer_ab_action={simmer_action}")
            except Exception as exc:
                print(
                    "simmer_ab_decision=UNKNOWN "
                    f"reason=parse_error:{type(exc).__name__} path={simmer_decision_path}"
                )
        else:
            print(f"simmer_ab_decision=MISSING path={simmer_decision_path}")

    health_decision = "SKIPPED"
    if health_payload:
        health_decision = str(health_payload.get("decision") or "UNKNOWN")
        reasons = health_payload.get("reasons") if isinstance(health_payload.get("reasons"), list) else []
        reason_txt = "; ".join(str(x) for x in reasons if str(x).strip()) if reasons else "-"
        print(f"automation_health={health_decision} reasons={reason_txt}")
    elif not args.skip_health:
        print("automation_health=UNKNOWN reasons=health_json_missing_or_invalid")

    capital_gate, capital_reasons = real_capital_gate_decision(
        gate_decision_3stage=gate_decision_3,
        resolved_trades=no_longshot_resolved,
        health_decision=health_decision,
    )
    reason_txt = "; ".join(capital_reasons) if capital_reasons else "all checks passed"
    print(f"real_capital_gate={capital_gate} reasons={reason_txt}")

    if args.fail_on_simmer_ab_final_no_go:
        if args.skip_simmer_ab:
            print("simmer_ab_final_gate=SKIPPED_BY_FLAG")
        elif not simmer_decision_available:
            print("simmer_ab_final_gate=FAIL reason=decision_missing_or_invalid")
            return 6
        elif simmer_file_age_hours is None or simmer_file_age_hours > float(args.simmer_ab_max_stale_hours):
            stale_text = "unknown" if simmer_file_age_hours is None else f"{simmer_file_age_hours:.2f}"
            print(
                "simmer_ab_final_gate=FAIL "
                f"reason=decision_stale age_hours={stale_text} "
                f"max_allowed={float(args.simmer_ab_max_stale_hours):.2f}"
            )
            return 6
        elif simmer_decision_stage == "FINAL" and simmer_decision_value != "GO":
            print(
                "simmer_ab_final_gate=FAIL "
                f"decision={simmer_decision_value} stage={simmer_decision_stage} final_ready={'yes' if simmer_final_ready else 'no'}"
            )
            return 6
        elif simmer_decision_stage == "FINAL" and simmer_decision_value == "GO":
            print("simmer_ab_final_gate=PASS decision=GO stage=FINAL")
        else:
            print(
                "simmer_ab_final_gate=WAIT "
                f"decision={simmer_decision_value} stage={simmer_decision_stage}"
            )

    if args.fail_on_gate_not_ready and gate_decision != "READY_FOR_JUDGMENT":
        return 3
    if args.fail_on_stage_not_final and gate_decision_3 != "READY_FINAL":
        return 5
    if args.fail_on_health_no_go and health_decision not in ("GO", "SKIPPED"):
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
