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

try:
    from report_no_longshot_monthly_return import build_kpi as _NO_LONGSHOT_KPI_BUILDER
except Exception:
    _NO_LONGSHOT_KPI_BUILDER = None

DEFAULT_UNCORRELATED_STRATEGY_IDS = (
    "weather_clob_arb_buckets_observe,"
    "no_longshot_daily_observe,"
    "link_intake_walletseed_cohort_observe,"
    "gamma_eventpair_exec_edge_filter_observe,"
    "hourly_updown_highprob_calibration_observe"
)


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


def extract_no_longshot_monthly(payload: Dict[str, Any]) -> Dict[str, Any]:
    no_longshot = payload.get("no_longshot_status") if isinstance(payload.get("no_longshot_status"), dict) else {}
    kpi: Dict[str, Any] = {}
    if callable(_NO_LONGSHOT_KPI_BUILDER):
        try:
            raw = _NO_LONGSHOT_KPI_BUILDER(payload)
            if isinstance(raw, dict):
                kpi = raw
        except Exception:
            kpi = {}

    monthly_now_text = str(
        kpi.get("monthly_return_now_text")
        or no_longshot.get("monthly_return_now_text")
        or fmt_ratio_pct(no_longshot.get("monthly_return_now_ratio"))
    )
    monthly_now_source = str(
        kpi.get("monthly_return_now_source")
        or no_longshot.get("monthly_return_now_source")
        or "-"
    )
    monthly_new_text = str(
        kpi.get("monthly_return_now_new_condition_text")
        or no_longshot.get("monthly_return_now_new_condition_text")
        or fmt_ratio_pct(no_longshot.get("monthly_return_now_new_condition_ratio"))
    )
    monthly_new_source = str(
        kpi.get("monthly_return_now_new_condition_source")
        or no_longshot.get("monthly_return_now_new_condition_source")
        or "-"
    )
    monthly_all_text = str(
        kpi.get("monthly_return_now_all_text")
        or no_longshot.get("monthly_return_now_all_text")
        or fmt_ratio_pct(no_longshot.get("monthly_return_now_all_ratio"))
    )
    monthly_all_source = str(
        kpi.get("monthly_return_now_all_source")
        or no_longshot.get("monthly_return_now_all_source")
        or "-"
    )
    obs_days_val = no_longshot.get("observed_days")
    obs_days_txt = str(_as_int(obs_days_val, 0)) if obs_days_val is not None else "n/a"
    resolved = _as_int(
        kpi.get("rolling_30d_resolved_trades"),
        _as_int(
            no_longshot.get("rolling_30d_resolved_trades"),
            _as_int(no_longshot.get("resolved_positions"), 0),
        ),
    )
    resolved_new = _as_int(no_longshot.get("rolling_30d_resolved_trades_new_condition"), 0)
    resolved_all = _as_int(no_longshot.get("rolling_30d_resolved_trades_all"), 0)
    open_positions = _as_int(no_longshot.get("open_positions"), 0)

    return {
        "monthly_now_text": monthly_now_text,
        "monthly_now_source": monthly_now_source,
        "monthly_new_text": monthly_new_text,
        "monthly_new_source": monthly_new_source,
        "monthly_all_text": monthly_all_text,
        "monthly_all_source": monthly_all_source,
        "obs_days_txt": obs_days_txt,
        "rolling_30d_resolved_trades": resolved,
        "rolling_30d_resolved_trades_new_condition": resolved_new,
        "rolling_30d_resolved_trades_all": resolved_all,
        "open_positions": open_positions,
    }


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


def _find_health_artifact_row(payload: Dict[str, Any], suffix: str) -> Optional[Dict[str, Any]]:
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
    target = str(suffix or "").replace("/", "\\").lower().strip()
    if not target:
        return None
    for row in artifacts:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").replace("/", "\\").lower().strip()
        if path.endswith(target):
            return row
    return None


def _resolve_simmer_interim_target(summary: Dict[str, Any], target: str) -> Dict[str, Any]:
    interim = summary.get("interim_milestones") if isinstance(summary.get("interim_milestones"), dict) else {}
    t = str(target or "").strip().lower()
    key = "tentative_7d" if t == "7d" else "intermediate_14d"
    row = interim.get(key) if isinstance(interim.get(key), dict) else {}
    if not row:
        return {
            "target_key": key,
            "decision": "UNKNOWN",
            "reached": False,
            "data_sufficient_days": _as_int(summary.get("data_sufficient_days"), 0),
            "target_days": 7 if t == "7d" else 14,
        }
    out = dict(row)
    out["target_key"] = key
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Morning strategy gate check (observe-only).")
    p.add_argument("--no-refresh", action="store_true", help="Skip refresh commands and only read existing snapshot.")
    p.add_argument("--skip-health", action="store_true", help="Skip automation health refresh/check.")
    p.add_argument("--skip-gate-alarm", action="store_true", help="Skip strategy gate alarm refresh/check.")
    p.add_argument("--skip-uncorrelated-portfolio", action="store_true", help="Skip uncorrelated portfolio refresh/check.")
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
        "--uncorrelated-json",
        default="logs/uncorrelated_portfolio_proxy_analysis_latest.json",
        help="Uncorrelated portfolio analysis JSON path.",
    )
    p.add_argument(
        "--uncorrelated-strategy-ids",
        default=DEFAULT_UNCORRELATED_STRATEGY_IDS,
        help=(
            "Comma-separated strategy ids passed to report_uncorrelated_portfolio.py. "
            "Default uses fixed 5-strategy diagnostic cohort."
        ),
    )
    p.add_argument(
        "--uncorrelated-corr-threshold-abs",
        type=float,
        default=0.30,
        help="Absolute correlation threshold passed to report_uncorrelated_portfolio.py.",
    )
    p.add_argument(
        "--uncorrelated-min-overlap-days",
        type=int,
        default=2,
        help="Minimum overlap days passed to report_uncorrelated_portfolio.py.",
    )
    p.add_argument(
        "--uncorrelated-min-realized-days-for-correlation",
        type=int,
        default=7,
        help="Minimum realized days passed to report_uncorrelated_portfolio.py before proxy fallback.",
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
        "--no-longshot-practical-decision-date",
        default="2026-03-02",
        help="Initial practical judgment date for no_longshot resolved-trade gate (YYYY-MM-DD).",
    )
    p.add_argument(
        "--no-longshot-practical-slide-days",
        type=int,
        default=3,
        help="When practical gate is unmet on judgment date, slide the date by this many days.",
    )
    p.add_argument(
        "--no-longshot-practical-min-resolved-trades",
        type=int,
        default=30,
        help="Resolved-trade threshold for no_longshot practical gate.",
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
        "--fail-on-simmer-ab-interim-no-go",
        action="store_true",
        help="Exit non-zero when selected Simmer A/B interim milestone is reached and decision is not GO.",
    )
    p.add_argument(
        "--simmer-ab-interim-target",
        choices=["7d", "14d"],
        default="7d",
        help="Interim milestone target used by --fail-on-simmer-ab-interim-no-go (default: 7d).",
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
                "--no-longshot-practical-decision-date",
                str(args.no_longshot_practical_decision_date),
                "--no-longshot-practical-slide-days",
                str(max(1, int(args.no_longshot_practical_slide_days))),
                "--no-longshot-practical-min-resolved-trades",
                str(max(1, int(args.no_longshot_practical_min_resolved_trades))),
            ]
            if args.discord_gate_alarm:
                alarm_cmd.append("--discord")
                discord_webhook_env = str(args.discord_webhook_env or "").strip()
                if discord_webhook_env:
                    alarm_cmd.extend(["--discord-webhook-env", discord_webhook_env])
            rc = run_cmd(alarm_cmd)
            if rc != 0:
                return rc
        if not args.skip_uncorrelated_portfolio:
            uncorrelated_ids = str(args.uncorrelated_strategy_ids or "").strip()
            uncorrelated_ids_args: list[str] = []
            if uncorrelated_ids:
                uncorrelated_ids_args = ["--strategy-ids", uncorrelated_ids]
            rc = run_cmd(
                [
                    "python",
                    "scripts/report_uncorrelated_portfolio.py",
                    "--out-json",
                    str(args.uncorrelated_json),
                    "--no-memo",
                    "--corr-threshold-abs",
                    str(float(args.uncorrelated_corr_threshold_abs)),
                    "--min-overlap-days",
                    str(max(2, int(args.uncorrelated_min_overlap_days))),
                    "--min-realized-days-for-correlation",
                    str(max(2, int(args.uncorrelated_min_realized_days_for_correlation))),
                    *uncorrelated_ids_args,
                ]
            )
            if rc != 0:
                return rc
        if not args.skip_implementation_ledger:
            rc = run_cmd(["python", "scripts/render_implementation_ledger.py"])
            if rc != 0:
                return rc

    health_payload: Dict[str, Any] = {}
    uncorrelated_payload: Dict[str, Any] = {}
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
    if not args.skip_uncorrelated_portfolio:
        uncorrelated_path = resolve_repo_path(
            str(args.uncorrelated_json),
            "logs/uncorrelated_portfolio_proxy_analysis_latest.json",
        )
        if uncorrelated_path.exists():
            try:
                uncorrelated_payload = load_json(uncorrelated_path)
            except Exception:
                uncorrelated_payload = {}

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
    no_longshot_monthly = extract_no_longshot_monthly(payload)
    no_longshot_monthly_text = str(no_longshot_monthly.get("monthly_now_text") or "n/a")
    no_longshot_source = str(no_longshot_monthly.get("monthly_now_source") or "-")
    no_longshot_monthly_new_text = str(no_longshot_monthly.get("monthly_new_text") or "n/a")
    no_longshot_monthly_new_source = str(no_longshot_monthly.get("monthly_new_source") or "-")
    no_longshot_monthly_all_text = str(no_longshot_monthly.get("monthly_all_text") or "n/a")
    no_longshot_monthly_all_source = str(no_longshot_monthly.get("monthly_all_source") or "-")
    no_longshot_obs_days_txt = str(no_longshot_monthly.get("obs_days_txt") or "n/a")
    no_longshot_resolved = _as_int(no_longshot_monthly.get("rolling_30d_resolved_trades"), 0)
    no_longshot_resolved_new = _as_int(no_longshot_monthly.get("rolling_30d_resolved_trades_new_condition"), 0)
    no_longshot_resolved_all = _as_int(no_longshot_monthly.get("rolling_30d_resolved_trades_all"), 0)
    no_longshot_open = _as_int(no_longshot_monthly.get("open_positions"), 0)
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
    if no_longshot_monthly_new_text != "n/a":
        print(
            "no_longshot_monthly_new_condition="
            f"{no_longshot_monthly_new_text} source={no_longshot_monthly_new_source} "
            f"rolling_30d_resolved={no_longshot_resolved_new}"
        )
    if no_longshot_monthly_all_text != "n/a":
        print(
            "no_longshot_monthly_all="
            f"{no_longshot_monthly_all_text} source={no_longshot_monthly_all_source} "
            f"rolling_30d_resolved={no_longshot_resolved_all}"
        )
    print(f"no_longshot_confidence={no_longshot_conf} threshold_resolved_trades>=30")
    gate_alarm_state_path = resolve_repo_path(
        str(args.gate_alarm_state_json),
        "logs/strategy_gate_alarm_state.json",
    )
    if gate_alarm_state_path.exists():
        try:
            gate_alarm_state = load_json(gate_alarm_state_path)
        except Exception:
            gate_alarm_state = {}
        practical_status = str(gate_alarm_state.get("no_longshot_practical_status") or "").strip()
        if practical_status:
            practical_date = str(gate_alarm_state.get("no_longshot_practical_active_decision_date") or "n/a")
            practical_remaining = _as_int(gate_alarm_state.get("no_longshot_practical_remaining_days"), -1)
            practical_threshold_met = bool(gate_alarm_state.get("no_longshot_practical_threshold_met"))
            practical_min_resolved = _as_int(
                gate_alarm_state.get("no_longshot_practical_min_resolved_trades"),
                max(1, int(args.no_longshot_practical_min_resolved_trades)),
            )
            practical_rollover = bool(gate_alarm_state.get("no_longshot_practical_rollover_triggered_last_run"))
            practical_rollover_from = str(gate_alarm_state.get("no_longshot_practical_rollover_from_date_last_run") or "")
            practical_rollover_to = str(gate_alarm_state.get("no_longshot_practical_rollover_to_date_last_run") or "")
            practical_remaining_txt = str(practical_remaining) if practical_remaining >= 0 else "n/a"
            print(
                "no_longshot_practical_gate="
                f"{practical_status} decision_date={practical_date} "
                f"remaining_days={practical_remaining_txt} "
                f"threshold_met={'yes' if practical_threshold_met else 'no'} "
                f"resolved={no_longshot_resolved}/{practical_min_resolved}"
            )
            if practical_rollover:
                print(
                    "no_longshot_practical_rollover="
                    f"{practical_rollover_from or '-'}->{practical_rollover_to or '-'}"
                )
    print(f"readiness_strict=GO {strict_go}/{strict_cnt} readiness_quality=GO {quality_go}/{quality_cnt}")
    print(f"snapshot={snapshot_path}")

    simmer_decision_available = False
    simmer_decision_value = "UNKNOWN"
    simmer_decision_stage = "UNKNOWN"
    simmer_final_ready = False
    simmer_file_age_hours: Optional[float] = None
    simmer_summary: Dict[str, Any] = {}

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
                simmer_interim = (
                    simmer_summary.get("interim_milestones")
                    if isinstance(simmer_summary.get("interim_milestones"), dict)
                    else {}
                )
                simmer_interim_7 = (
                    simmer_interim.get("tentative_7d")
                    if isinstance(simmer_interim.get("tentative_7d"), dict)
                    else {}
                )
                simmer_interim_14 = (
                    simmer_interim.get("intermediate_14d")
                    if isinstance(simmer_interim.get("intermediate_14d"), dict)
                    else {}
                )
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
                    "simmer_ab_interim="
                    f"7d:{str(simmer_interim_7.get('decision') or 'UNKNOWN')} "
                    f"({int(simmer_interim_7.get('data_sufficient_days') or simmer_days)}/"
                    f"{int(simmer_interim_7.get('target_days') or 7)}) "
                    f"14d:{str(simmer_interim_14.get('decision') or 'UNKNOWN')} "
                    f"({int(simmer_interim_14.get('data_sufficient_days') or simmer_days)}/"
                    f"{int(simmer_interim_14.get('target_days') or 14)})"
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
        simmer_supervisor_row = _find_health_artifact_row(
            health_payload,
            r"logs\simmer_ab_supervisor_state.json",
        )
        if simmer_supervisor_row is not None:
            sup_status = str(simmer_supervisor_row.get("status") or "UNKNOWN")
            sup_age = _as_float(simmer_supervisor_row.get("age_hours"), None)
            sup_max_age = _as_float(simmer_supervisor_row.get("max_age_hours"), None)
            sup_note = str(simmer_supervisor_row.get("status_note") or "").strip()
            sup_age_text = "n/a" if sup_age is None else f"{sup_age:.2f}"
            sup_max_text = "n/a" if sup_max_age is None else f"{sup_max_age:.2f}"
            sup_note_text = f" note={sup_note}" if sup_note else ""
            print(
                "simmer_ab_supervisor_health="
                f"{sup_status} age_hours={sup_age_text} max_allowed={sup_max_text}"
                f"{sup_note_text}"
            )
    elif not args.skip_health:
        print("automation_health=UNKNOWN reasons=health_json_missing_or_invalid")

    if not args.skip_uncorrelated_portfolio:
        if uncorrelated_payload:
            recommendation = (
                uncorrelated_payload.get("recommendation")
                if isinstance(uncorrelated_payload.get("recommendation"), dict)
                else {}
            )
            best_pair = (
                recommendation.get("best_pair_with_monthly_proxy")
                if isinstance(recommendation.get("best_pair_with_monthly_proxy"), dict)
                else {}
            )
            recommended_set = (
                recommendation.get("recommended_min_set")
                if isinstance(recommendation.get("recommended_min_set"), list)
                else []
            )
            low_pairs = (
                recommendation.get("low_corr_pairs")
                if isinstance(recommendation.get("low_corr_pairs"), list)
                else []
            )
            monthly_proxy = (
                uncorrelated_payload.get("portfolio_monthly_proxy")
                if isinstance(uncorrelated_payload.get("portfolio_monthly_proxy"), dict)
                else {}
            )
            risk_proxy = (
                uncorrelated_payload.get("portfolio_risk_proxy")
                if isinstance(uncorrelated_payload.get("portfolio_risk_proxy"), dict)
                else {}
            )
            print(
                "uncorrelated_low_pairs="
                f"{len(low_pairs)} threshold_abs={_as_float(recommendation.get('low_corr_threshold_abs'), 0.3):.2f}"
            )
            if recommended_set:
                print(f"uncorrelated_recommended_set={','.join(str(x) for x in recommended_set)}")
            else:
                print("uncorrelated_recommended_set=n/a")
            if best_pair:
                pair = best_pair.get("pair")
                corr = _as_float(best_pair.get("corr"), None)
                avg_m = _as_float(best_pair.get("avg_monthly_return_proxy_ratio"), None)
                pair_txt = ",".join(str(x) for x in pair) if isinstance(pair, list) else "n/a"
                corr_txt = "n/a" if corr is None else f"{corr:+.4f}"
                print(
                    "uncorrelated_best_pair="
                    f"{pair_txt} corr={corr_txt} avg_monthly_proxy={fmt_ratio_pct(avg_m)}"
                )
            else:
                print("uncorrelated_best_pair=n/a")
            if monthly_proxy:
                print(
                    "uncorrelated_portfolio_monthly_proxy="
                    f"{fmt_ratio_pct(monthly_proxy.get('monthly_return_proxy_equal_weight'))} "
                    f"improve_vs_no_longshot={fmt_ratio_pct(monthly_proxy.get('improvement_vs_no_longshot_monthly_proxy'))}"
                )
            if risk_proxy:
                print(
                    "uncorrelated_risk_reduction_proxy="
                    f"{fmt_ratio_pct(risk_proxy.get('risk_reduction_vs_avg_std'))} "
                    f"overlap_days={_as_int(risk_proxy.get('overlap_days'), 0)}"
                )
        else:
            print("uncorrelated_portfolio=UNKNOWN reason=uncorrelated_json_missing_or_invalid")

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

    if args.fail_on_simmer_ab_interim_no_go:
        interim_target = str(args.simmer_ab_interim_target or "7d").strip().lower()
        if args.skip_simmer_ab:
            print("simmer_ab_interim_gate=SKIPPED_BY_FLAG")
        elif not simmer_decision_available:
            print("simmer_ab_interim_gate=FAIL reason=decision_missing_or_invalid")
            return 7
        elif simmer_file_age_hours is None or simmer_file_age_hours > float(args.simmer_ab_max_stale_hours):
            stale_text = "unknown" if simmer_file_age_hours is None else f"{simmer_file_age_hours:.2f}"
            print(
                "simmer_ab_interim_gate=FAIL "
                f"reason=decision_stale age_hours={stale_text} "
                f"max_allowed={float(args.simmer_ab_max_stale_hours):.2f}"
            )
            return 7
        else:
            row = _resolve_simmer_interim_target(simmer_summary, interim_target)
            interim_decision = str(row.get("decision") or "UNKNOWN")
            interim_reached = bool(row.get("reached"))
            interim_days = _as_int(row.get("data_sufficient_days"), 0)
            interim_target_days = _as_int(row.get("target_days"), 7 if interim_target == "7d" else 14)
            if interim_reached and interim_decision != "GO":
                print(
                    "simmer_ab_interim_gate=FAIL "
                    f"target={interim_target} decision={interim_decision} "
                    f"days={interim_days}/{interim_target_days}"
                )
                return 7
            if interim_reached and interim_decision == "GO":
                print(
                    "simmer_ab_interim_gate=PASS "
                    f"target={interim_target} decision=GO "
                    f"days={interim_days}/{interim_target_days}"
                )
            else:
                print(
                    "simmer_ab_interim_gate=WAIT "
                    f"target={interim_target} decision={interim_decision} "
                    f"days={interim_days}/{interim_target_days}"
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
