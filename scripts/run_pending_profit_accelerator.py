#!/usr/bin/env python3
"""
Observe-only accelerator batch for pending BTC strategies.

Runs two pending strategies end-to-end in one command:
1) BTC 15m lag observe -> offline fee-adjusted evaluation
2) BTC up/down yes-no probe -> Kelly replay summary

No live orders are placed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def now_utc_tag() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def logs_dir() -> Path:
    p = repo_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_output_path(raw: str, default_name: str) -> Path:
    s = str(raw or "").strip()
    if not s:
        return logs_dir() / default_name
    p = Path(s)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs_dir() / p.name
    return repo_root() / p


def write_json(path: Path, payload: dict, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        if pretty:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        else:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(text or ""), encoding="utf-8")


def _tail(text: str, n: int = 80) -> str:
    rows = [str(x) for x in str(text or "").splitlines() if str(x).strip()]
    if len(rows) <= n:
        return "\n".join(rows)
    return "\n".join(rows[-n:])


def run_child(cmd: List[str], timeout_sec: float) -> dict:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, float(timeout_sec)),
            check=False,
        )
        return {
            "ok": int(proc.returncode) == 0,
            "exit_code": int(proc.returncode),
            "timed_out": False,
            "command": " ".join(cmd),
            "stdout_tail": _tail(str(proc.stdout or "")),
            "stderr_tail": _tail(str(proc.stderr or "")),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "exit_code": 124,
            "timed_out": True,
            "command": " ".join(cmd),
            "stdout_tail": _tail(str(exc.stdout or "")),
            "stderr_tail": _tail(str(exc.stderr or "")),
        }


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def as_float(x, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def as_int(x, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


@dataclass(frozen=True)
class Lag15Paths:
    log: Path
    state: Path
    metrics: Path
    eval_json: Path


@dataclass(frozen=True)
class YesNoPaths:
    log: Path
    state: Path
    metrics: Path
    replay_json: Path


def build_lag15_paths(tag: str) -> Lag15Paths:
    base = logs_dir()
    return Lag15Paths(
        log=base / f"btc15m-lag-observe-accel-{tag}.log",
        state=base / f"btc15m_lag_observe_accel_{tag}.json",
        metrics=base / f"btc15m-lag-observe-accel-{tag}.jsonl",
        eval_json=base / f"btc15m_strategy_eval_accel_{tag}.json",
    )


def build_yesno_paths(tag: str) -> YesNoPaths:
    base = logs_dir()
    return YesNoPaths(
        log=base / f"btc_updown_yesno_probe_accel_{tag}.log",
        state=base / f"btc_updown_yesno_probe_state_accel_{tag}.json",
        metrics=base / f"btc_updown_yesno_probe_metrics_accel_{tag}.jsonl",
        replay_json=base / f"btc_updown_yesno_probe_kelly_accel_{tag}.json",
    )


def build_lag15_observe_cmd(args: argparse.Namespace, p: Lag15Paths) -> List[str]:
    cmd = [
        str(args.python_exe),
        "scripts/polymarket_btc15m_lag_observe.py",
        "--run-seconds",
        str(int(args.lag15_run_seconds)),
        "--poll-sec",
        str(float(args.lag15_poll_sec)),
        "--summary-every-sec",
        str(float(args.lag15_summary_every_sec)),
        "--metrics-sample-sec",
        str(float(args.lag15_metrics_sample_sec)),
        "--shares",
        str(float(args.lag15_shares)),
        "--entry-edge-cents",
        str(float(args.lag15_entry_edge_cents)),
        "--alert-edge-cents",
        str(float(args.lag15_alert_edge_cents)),
        "--allowed-side-mode",
        str(args.lag15_allowed_side_mode),
        "--regime-mode",
        str(args.lag15_regime_mode),
        "--regime-short-lookback-sec",
        str(float(args.lag15_regime_short_lookback_sec)),
        "--regime-long-lookback-sec",
        str(float(args.lag15_regime_long_lookback_sec)),
        "--regime-short-threshold-pct",
        str(float(args.lag15_regime_short_threshold_pct)),
        "--regime-long-threshold-pct",
        str(float(args.lag15_regime_long_threshold_pct)),
        "--regime-opposite-edge-penalty-cents",
        str(float(args.lag15_regime_opposite_edge_penalty_cents)),
        "--fair-model",
        str(args.lag15_fair_model),
        "--drift-trend-lookback-sec",
        str(float(args.lag15_drift_trend_lookback_sec)),
        "--drift-max-adjustment",
        str(float(args.lag15_drift_max_adjustment)),
        "--drift-trend-reference-move-pct",
        str(float(args.lag15_drift_trend_reference_move_pct)),
        "--drift-open-gap-reference-pct",
        str(float(args.lag15_drift_open_gap_reference_pct)),
        "--entry-price-min",
        str(float(args.lag15_entry_price_min)),
        "--entry-price-max",
        str(float(args.lag15_entry_price_max)),
        "--up-entry-price-min",
        str(float(args.lag15_up_entry_price_min)),
        "--up-entry-price-max",
        str(float(args.lag15_up_entry_price_max)),
        "--down-entry-price-min",
        str(float(args.lag15_down_entry_price_min)),
        "--down-entry-price-max",
        str(float(args.lag15_down_entry_price_max)),
        "--no-max-one-entry-per-window",
        "--min-remaining-sec",
        str(float(args.lag15_min_remaining_sec)),
        "--max-remaining-sec",
        str(float(args.lag15_max_remaining_sec)),
        "--max-spread-cents",
        str(float(args.lag15_max_spread_cents)),
        "--min-ask-depth",
        str(float(args.lag15_min_ask_depth)),
        "--log-file",
        str(p.log),
        "--state-file",
        str(p.state),
        "--metrics-file",
        str(p.metrics),
    ]
    if bool(args.lag15_require_reversal):
        cmd.extend(
            [
                "--require-reversal",
                "--reversal-lookback-sec",
                str(float(args.lag15_reversal_lookback_sec)),
                "--reversal-min-move-usd",
                str(float(args.lag15_reversal_min_move_usd)),
            ]
        )
    if bool(args.lag15_require_aligned_momentum):
        cmd.append("--require-aligned-momentum")
    return cmd


def build_lag15_eval_cmd(args: argparse.Namespace, p: Lag15Paths) -> List[str]:
    cmd = [
        str(args.python_exe),
        "scripts/report_btc5m_strategy_eval.py",
        "--mode",
        "lag15",
        "--log-file",
        str(p.log),
        "--state-file",
        str(p.state),
        "--metrics-file",
        str(p.metrics),
        "--out-json",
        str(p.eval_json),
    ]
    if bool(args.pretty):
        cmd.append("--pretty")
    return cmd


def build_yesno_probe_cmd(args: argparse.Namespace, p: YesNoPaths) -> List[str]:
    cmd = [
        str(args.python_exe),
        "scripts/polymarket_clob_arb_realtime.py",
        "--universe",
        "btc-updown",
        "--strategy",
        "yes-no",
        "--btc-updown-window-minutes",
        str(args.yesno_window_minutes),
        "--btc-5m-windows-back",
        str(int(args.yesno_windows_back)),
        "--btc-5m-windows-forward",
        str(int(args.yesno_windows_forward)),
        "--min-edge-cents",
        str(float(args.yesno_min_edge_cents)),
        "--summary-every-sec",
        str(float(args.yesno_summary_every_sec)),
        "--run-seconds",
        str(int(args.yesno_run_seconds)),
        "--metrics-file",
        str(p.metrics),
        "--state-file",
        str(p.state),
        "--log-file",
        str(p.log),
    ]
    if bool(args.yesno_metrics_log_all_candidates):
        cmd.append("--metrics-log-all-candidates")
    return cmd


def build_yesno_replay_cmd(args: argparse.Namespace, p: YesNoPaths) -> List[str]:
    cmd = [
        str(args.python_exe),
        "scripts/replay_clob_arb_kelly.py",
        "--metrics-file",
        str(p.metrics),
        "--edge-mode",
        "exec",
        "--fill-ratio-mode",
        "min",
        "--miss-penalty",
        str(float(args.replay_miss_penalty)),
        "--stale-grace-sec",
        str(float(args.replay_stale_grace_sec)),
        "--stale-penalty-per-sec",
        str(float(args.replay_stale_penalty_per_sec)),
        "--max-worst-stale-sec",
        str(float(args.replay_max_worst_stale_sec)),
        "--min-gap-ms-per-event",
        str(int(args.replay_min_gap_ms_per_event)),
        "--scales",
        str(args.replay_scales),
        "--bootstrap-iters",
        str(int(args.replay_bootstrap_iters)),
        "--out-json",
        str(p.replay_json),
    ]
    if bool(args.replay_require_threshold_pass):
        cmd.append("--require-threshold-pass")
    if bool(args.pretty):
        cmd.append("--pretty")
    return cmd


def classify_lag15(eval_payload: dict, min_trades: int) -> dict:
    adj = eval_payload.get("fee_adjusted") if isinstance(eval_payload.get("fee_adjusted"), dict) else {}
    trades = as_int(adj.get("trade_count"), 0)
    net = as_float(adj.get("net_pnl"), 0.0)
    dd = as_float(adj.get("max_drawdown"), 0.0)
    wr = as_float(adj.get("win_rate"), 0.0)

    if trades <= 0:
        status = "NO_DATA"
        reason = "no closed fee-adjusted trades"
    elif trades < int(min_trades):
        status = "COLLECT_MORE"
        reason = f"trade_count={trades} < gate={int(min_trades)}"
    elif net <= 0:
        status = "NEGATIVE_EDGE"
        reason = f"net_pnl={net:+.2f} <= 0"
    else:
        status = "CANDIDATE"
        reason = f"trade_count={trades}, net_pnl={net:+.2f}"

    return {
        "status": status,
        "reason": reason,
        "trade_count": trades,
        "net_pnl": net,
        "max_drawdown": dd,
        "win_rate": wr,
    }


def _best_scale_row(replay_payload: dict) -> dict:
    kelly = replay_payload.get("kelly") if isinstance(replay_payload.get("kelly"), dict) else {}
    rows = kelly.get("scales") if isinstance(kelly.get("scales"), list) else []
    best: dict = {}
    best_growth = float("-inf")
    for row in rows:
        if not isinstance(row, dict):
            continue
        g = as_float(row.get("expected_log_growth"), float("-inf"))
        if g > best_growth:
            best_growth = g
            best = row
    return best


def classify_yesno_replay(replay_payload: dict, min_samples: int) -> dict:
    data = replay_payload.get("data") if isinstance(replay_payload.get("data"), dict) else {}
    kelly = replay_payload.get("kelly") if isinstance(replay_payload.get("kelly"), dict) else {}

    sample_count = as_int(data.get("sample_count"), 0)
    full_kelly = as_float(kelly.get("full_fraction_estimate"), 0.0)
    best_row = _best_scale_row(replay_payload)
    best_growth = as_float(best_row.get("expected_log_growth"), 0.0)
    best_scale = as_float(best_row.get("scale_of_full_kelly"), 0.0)

    if sample_count <= 0:
        status = "NO_DATA"
        reason = "no usable replay samples"
    elif sample_count < int(min_samples):
        status = "COLLECT_MORE"
        reason = f"sample_count={sample_count} < gate={int(min_samples)}"
    elif full_kelly <= 0 or best_growth <= 0:
        status = "NO_EDGE"
        reason = f"full_kelly={full_kelly:.4f}, best_log_growth={best_growth:.6f}"
    else:
        status = "CANDIDATE"
        reason = f"sample_count={sample_count}, best_log_growth={best_growth:.6f}"

    return {
        "status": status,
        "reason": reason,
        "sample_count": sample_count,
        "full_kelly_estimate": full_kelly,
        "best_scale_of_full_kelly": best_scale,
        "best_expected_log_growth": best_growth,
    }


def build_overall_decision(children_ok: bool, lag15_status: str, yesno_status: str) -> Tuple[str, str]:
    if not bool(children_ok):
        return "ERROR", "at least one child command failed"
    if lag15_status == "CANDIDATE" and yesno_status == "CANDIDATE":
        return "READY_FOR_REVIEW", "both pending BTC strategies reached candidate state"
    if lag15_status == "CANDIDATE" or yesno_status == "CANDIDATE":
        return "PARTIAL_PROGRESS", "one pending BTC strategy reached candidate state"
    return "ACCELERATE_OBSERVE", "evidence still insufficient; keep accelerated observe loop"


def build_next_actions(decision: str, lag: dict, yesno: dict) -> List[str]:
    actions: List[str] = []
    if decision == "ERROR":
        actions.append("Fix failing child command(s) and rerun the accelerator batch.")

    if lag.get("status") in {"NO_DATA", "COLLECT_MORE"}:
        actions.append("Extend lag15 observe runtime or run this batch repeatedly to accumulate >= gate trades.")
    elif lag.get("status") == "NEGATIVE_EDGE":
        actions.append("Tighten lag15 entry filters and rerun until fee-adjusted net PnL turns positive.")

    if yesno.get("status") in {"NO_DATA", "COLLECT_MORE"}:
        actions.append("Increase yes-no probe runtime/window coverage to accumulate >= gate replay samples.")
    elif yesno.get("status") == "NO_EDGE":
        actions.append("Retune yes-no edge thresholds/slippage assumptions and revalidate with replay.")

    if lag.get("status") == "CANDIDATE" or yesno.get("status") == "CANDIDATE":
        actions.append("Keep observe-only and run gate check refresh before any status promotion.")

    if not actions:
        actions.append("No action required.")
    return actions


def classify_step_level(step: str, exit_code: int) -> str:
    code = int(exit_code)
    if code == 0:
        return "ok"
    if step == "yesno_probe":
        return "warn"
    if step == "yesno_replay" and code in {1, 2}:
        return "warn"
    if step == "lag15_observe":
        return "warn"
    return "error"


def format_summary_text(payload: dict) -> str:
    lag = payload.get("lag15_judgement") if isinstance(payload.get("lag15_judgement"), dict) else {}
    yesno = payload.get("yesno_judgement") if isinstance(payload.get("yesno_judgement"), dict) else {}
    counts = payload.get("level_counts") if isinstance(payload.get("level_counts"), dict) else {}
    lines = [
        (
            "pending_profit_accelerator"
            f" | decision={payload.get('decision')}"
            f" | ok={payload.get('ok')}"
            f" | level(ok/warn/error)="
            f"{int(counts.get('ok') or 0)}/{int(counts.get('warn') or 0)}/{int(counts.get('error') or 0)}"
        ),
        f"run_tag={payload.get('run_tag')}",
        (
            "lag15"
            f" status={lag.get('status')}"
            f" trades={lag.get('trade_count')}"
            f" net_pnl={as_float(lag.get('net_pnl'), 0.0):+.2f}"
            f" reason={lag.get('reason')}"
        ),
        (
            "yesno"
            f" status={yesno.get('status')}"
            f" samples={yesno.get('sample_count')}"
            f" full_kelly={as_float(yesno.get('full_kelly_estimate'), 0.0):.4f}"
            f" best_log_growth={as_float(yesno.get('best_expected_log_growth'), 0.0):.6f}"
            f" reason={yesno.get('reason')}"
        ),
        "next_actions:",
    ]
    for i, action in enumerate(payload.get("next_actions") or [], start=1):
        lines.append(f"{i}. {action}")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run observe-only pending BTC strategy accelerator batch")
    p.add_argument("--python-exe", default=sys.executable, help="Python executable for child scripts")
    p.add_argument(
        "--run-tag",
        default="latest",
        help="Artifact tag. Use a fixed value (default: latest) to accumulate evidence across runs.",
    )
    p.add_argument("--child-timeout-sec", type=float, default=2100.0, help="Per-child timeout seconds")

    p.add_argument(
        "--skip-lag15",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Skip lag15 observe/eval stage (default: false).",
    )
    p.add_argument("--skip-yesno", action="store_true", help="Skip yes-no probe/replay stage")

    p.add_argument("--lag15-run-seconds", type=int, default=900)
    p.add_argument("--lag15-poll-sec", type=float, default=1.0)
    p.add_argument("--lag15-summary-every-sec", type=float, default=30.0)
    p.add_argument("--lag15-metrics-sample-sec", type=float, default=2.0)
    p.add_argument("--lag15-shares", type=float, default=25.0)
    p.add_argument("--lag15-entry-edge-cents", type=float, default=2.0)
    p.add_argument("--lag15-alert-edge-cents", type=float, default=0.4)
    p.add_argument("--lag15-allowed-side-mode", choices=("both", "down", "up"), default="both")
    p.add_argument("--lag15-regime-mode", choices=("prefer", "strict", "off"), default="prefer")
    p.add_argument("--lag15-regime-short-lookback-sec", type=float, default=1800.0)
    p.add_argument("--lag15-regime-long-lookback-sec", type=float, default=7200.0)
    p.add_argument("--lag15-regime-short-threshold-pct", type=float, default=0.0015)
    p.add_argument("--lag15-regime-long-threshold-pct", type=float, default=0.0030)
    p.add_argument("--lag15-regime-opposite-edge-penalty-cents", type=float, default=4.0)
    p.add_argument("--lag15-fair-model", choices=("hybrid", "drift"), default="hybrid")
    p.add_argument("--lag15-drift-trend-lookback-sec", type=float, default=300.0)
    p.add_argument("--lag15-drift-max-adjustment", type=float, default=0.10)
    p.add_argument("--lag15-drift-trend-reference-move-pct", type=float, default=0.0010)
    p.add_argument("--lag15-drift-open-gap-reference-pct", type=float, default=0.0015)
    p.add_argument("--lag15-entry-price-min", type=float, default=0.0)
    p.add_argument("--lag15-entry-price-max", type=float, default=1.0)
    p.add_argument("--lag15-up-entry-price-min", type=float, default=-1.0)
    p.add_argument("--lag15-up-entry-price-max", type=float, default=-1.0)
    p.add_argument("--lag15-down-entry-price-min", type=float, default=-1.0)
    p.add_argument("--lag15-down-entry-price-max", type=float, default=-1.0)
    p.add_argument("--lag15-require-aligned-momentum", action="store_true")
    p.add_argument("--lag15-require-reversal", action="store_true")
    p.add_argument("--lag15-reversal-lookback-sec", type=float, default=180.0)
    p.add_argument("--lag15-reversal-min-move-usd", type=float, default=15.0)
    p.add_argument("--lag15-min-remaining-sec", type=float, default=45.0)
    p.add_argument("--lag15-max-remaining-sec", type=float, default=0.0)
    p.add_argument("--lag15-max-spread-cents", type=float, default=8.0)
    p.add_argument("--lag15-min-ask-depth", type=float, default=5.0)
    p.add_argument("--lag15-min-trades-gate", type=int, default=20)

    p.add_argument("--yesno-run-seconds", type=int, default=900)
    p.add_argument("--yesno-summary-every-sec", type=float, default=30.0)
    p.add_argument("--yesno-min-edge-cents", type=float, default=0.3)
    p.add_argument("--yesno-windows-back", type=int, default=3)
    p.add_argument("--yesno-windows-forward", type=int, default=3)
    p.add_argument("--yesno-window-minutes", default="5,15")
    p.add_argument("--yesno-min-samples-gate", type=int, default=30)
    p.add_argument(
        "--yesno-metrics-log-all-candidates",
        action="store_true",
        help="Pass --metrics-log-all-candidates to yes-no probe.",
    )

    p.add_argument("--replay-miss-penalty", type=float, default=0.005)
    p.add_argument("--replay-stale-grace-sec", type=float, default=2.0)
    p.add_argument("--replay-stale-penalty-per-sec", type=float, default=0.001)
    p.add_argument("--replay-max-worst-stale-sec", type=float, default=10.0)
    p.add_argument("--replay-min-gap-ms-per-event", type=int, default=5000)
    p.add_argument("--replay-scales", default="0.1,0.25,0.5,0.75,1.0")
    p.add_argument("--replay-bootstrap-iters", type=int, default=2000)
    p.add_argument(
        "--replay-require-threshold-pass",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Require threshold-passed rows in replay sampling (default true).",
    )

    p.add_argument("--out-json", default="logs/pending_profit_accelerator_latest.json")
    p.add_argument("--out-txt", default="logs/pending_profit_accelerator_latest.txt")
    p.add_argument("--pretty", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    tag = str(args.run_tag or "").strip() or now_utc_tag()
    lag_paths = build_lag15_paths(tag)
    yesno_paths = build_yesno_paths(tag)
    run_rows: List[dict] = []

    lag_eval_payload: dict = {}
    replay_payload: dict = {}
    lag_judgement = {"status": "SKIPPED", "reason": "skip requested"}
    yesno_judgement = {"status": "SKIPPED", "reason": "skip requested"}

    if not bool(args.skip_lag15):
        lag_paths.log.parent.mkdir(parents=True, exist_ok=True)
        lag_paths.metrics.touch(exist_ok=True)
        lag_observe = run_child(build_lag15_observe_cmd(args, lag_paths), timeout_sec=float(args.child_timeout_sec))
        lag_observe["step"] = "lag15_observe"
        lag_observe["level"] = classify_step_level("lag15_observe", int(lag_observe.get("exit_code") or 0))
        run_rows.append(lag_observe)

        lag_eval = run_child(build_lag15_eval_cmd(args, lag_paths), timeout_sec=float(args.child_timeout_sec))
        lag_eval["step"] = "lag15_eval"
        lag_eval["level"] = classify_step_level("lag15_eval", int(lag_eval.get("exit_code") or 0))
        run_rows.append(lag_eval)

        lag_eval_payload = read_json(lag_paths.eval_json)
        lag_judgement = classify_lag15(
            eval_payload=lag_eval_payload,
            min_trades=int(args.lag15_min_trades_gate),
        )

    if not bool(args.skip_yesno):
        yesno_paths.log.parent.mkdir(parents=True, exist_ok=True)
        yesno_paths.metrics.touch(exist_ok=True)
        yesno_probe = run_child(build_yesno_probe_cmd(args, yesno_paths), timeout_sec=float(args.child_timeout_sec))
        yesno_probe["step"] = "yesno_probe"
        yesno_probe["level"] = classify_step_level("yesno_probe", int(yesno_probe.get("exit_code") or 0))
        run_rows.append(yesno_probe)

        yesno_replay = run_child(build_yesno_replay_cmd(args, yesno_paths), timeout_sec=float(args.child_timeout_sec))
        yesno_replay["step"] = "yesno_replay"
        yesno_replay["level"] = classify_step_level("yesno_replay", int(yesno_replay.get("exit_code") or 0))
        run_rows.append(yesno_replay)

        replay_payload = read_json(yesno_paths.replay_json)
        yesno_judgement = classify_yesno_replay(
            replay_payload=replay_payload,
            min_samples=int(args.yesno_min_samples_gate),
        )

    level_counts: Dict[str, int] = {"ok": 0, "warn": 0, "error": 0}
    for row in run_rows:
        lvl = str(row.get("level") or "error")
        if lvl not in level_counts:
            lvl = "error"
        level_counts[lvl] += 1

    children_ok = level_counts["error"] == 0 and bool(run_rows)
    decision, decision_reason = build_overall_decision(
        children_ok=children_ok,
        lag15_status=str(lag_judgement.get("status") or ""),
        yesno_status=str(yesno_judgement.get("status") or ""),
    )
    next_actions = build_next_actions(decision=decision, lag=lag_judgement, yesno=yesno_judgement)

    payload = {
        "ok": bool(children_ok),
        "generated_utc": now_utc_iso(),
        "run_tag": tag,
        "decision": decision,
        "decision_reason": decision_reason,
        "level_counts": level_counts,
        "lag15_judgement": lag_judgement,
        "yesno_judgement": yesno_judgement,
        "next_actions": next_actions,
        "artifacts": {
            "lag15": {
                "log": str(lag_paths.log),
                "state": str(lag_paths.state),
                "metrics": str(lag_paths.metrics),
                "eval_json": str(lag_paths.eval_json),
            },
            "yesno": {
                "log": str(yesno_paths.log),
                "state": str(yesno_paths.state),
                "metrics": str(yesno_paths.metrics),
                "replay_json": str(yesno_paths.replay_json),
            },
        },
        "runs": run_rows,
        "lag15_eval_payload": lag_eval_payload,
        "yesno_replay_payload": replay_payload,
    }

    out_json = resolve_output_path(str(args.out_json or ""), "pending_profit_accelerator_latest.json")
    out_txt = resolve_output_path(str(args.out_txt or ""), "pending_profit_accelerator_latest.txt")
    write_json(out_json, payload, pretty=bool(args.pretty))
    write_text(out_txt, format_summary_text(payload))

    print(format_summary_text(payload).rstrip("\n"))
    print(f"saved_json={out_json}")
    print(f"saved_txt={out_txt}")

    return 0 if payload["ok"] else 20


if __name__ == "__main__":
    raise SystemExit(main())
