#!/usr/bin/env python3
"""
Thin batch runner for pending-release transition alarms.

Purpose:
- Keep scheduler/cron wiring simple.
- Delegate all strategy logic to check_pending_release_alarm.py.
- Normalize exit handling for operational automation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple


DEFAULT_STRATEGIES = ["gamma_eventpair_exec_edge_filter_observe"]


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def logs_dir() -> Path:
    p = repo_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_path(raw: str, default_name: str) -> Path:
    if not str(raw or "").strip():
        return logs_dir() / default_name
    p = Path(str(raw))
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs_dir() / p.name
    return repo_root() / p


def write_json(path: Path, payload: dict, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        if pretty:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        else:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run pending-release alarm checks for one or more strategies.")
    p.add_argument(
        "--strategy",
        action="append",
        default=[],
        help="Target strategy id (repeatable). Defaults to gamma_eventpair_exec_edge_filter_observe when omitted.",
    )
    p.add_argument("--python-exe", default=sys.executable, help="Python executable used for subprocess calls.")
    p.add_argument("--snapshot-json", default="logs/strategy_register_latest.json")
    p.add_argument("--state-json", default="logs/pending_release_alarm_state.json")
    p.add_argument("--log-file", default="logs/pending_release_alarm.log")
    p.add_argument("--lock-file", default="logs/pending_release_alarm.lock")
    p.add_argument("--monthly-json", default="", help="Optional monthly-estimate override passed through.")
    p.add_argument("--replay-json", default="", help="Optional replay override passed through.")
    p.add_argument("--run-conservative", action="store_true", help="Run extra conservative-cost context per strategy.")
    p.add_argument("--conservative-cost-cents", type=float, default=2.0)
    p.add_argument("--child-timeout-sec", type=float, default=60.0, help="Per-check subprocess timeout seconds.")
    p.add_argument("--discord", action="store_true")
    p.add_argument("--discord-webhook-env", default="")
    p.add_argument("--notify-on-reason-change", action="store_true")
    p.add_argument(
        "--fail-on-lock-busy",
        action="store_true",
        help="Return exit 4 when any run returns lock-busy exit code 4.",
    )
    p.add_argument("--out-json", default="logs/pending_release_batch_latest.json")
    p.add_argument("--pretty", action="store_true")
    return p.parse_args()


def _build_cmd(args: argparse.Namespace, strategy: str, conservative: bool) -> List[str]:
    cmd: List[str] = [
        str(args.python_exe),
        "scripts/check_pending_release_alarm.py",
        "--strategy",
        str(strategy),
        "--snapshot-json",
        str(args.snapshot_json),
        "--state-json",
        str(args.state_json),
        "--log-file",
        str(args.log_file),
        "--lock-file",
        str(args.lock_file),
    ]
    if str(args.monthly_json or "").strip():
        cmd.extend(["--monthly-json", str(args.monthly_json)])
    if str(args.replay_json or "").strip():
        cmd.extend(["--replay-json", str(args.replay_json)])
    if bool(conservative):
        cmd.append("--conservative-costs")
        cmd.extend(["--conservative-cost-cents", str(float(args.conservative_cost_cents or 0.0))])
    if bool(args.discord):
        cmd.append("--discord")
    if str(args.discord_webhook_env or "").strip():
        cmd.extend(["--discord-webhook-env", str(args.discord_webhook_env)])
    if bool(args.notify_on_reason_change):
        cmd.append("--notify-on-reason-change")
    return cmd


def _parse_json_line(text: str) -> Tuple[dict, str]:
    line = ""
    for row in str(text or "").splitlines():
        s = str(row or "").strip()
        if not s:
            continue
        line = s
    if not line:
        return {}, "stdout_empty"
    try:
        obj = json.loads(line)
    except Exception:
        return {}, "stdout_json_parse_failed"
    if not isinstance(obj, dict):
        return {}, "stdout_json_not_object"
    return obj, ""


def _classify_exit(code: int) -> str:
    if code in {0, 10}:
        return "ok"
    if code == 4:
        return "warn"
    return "error"


def run_batch(args: argparse.Namespace) -> Tuple[int, dict]:
    strategy_list = [str(x).strip() for x in list(args.strategy or []) if str(x).strip()]
    if not strategy_list:
        strategy_list = list(DEFAULT_STRATEGIES)

    contexts = ["base", "conservative"] if bool(args.run_conservative) else ["base"]
    rows: List[dict] = []
    counts: Dict[str, int] = {"ok": 0, "warn": 0, "error": 0}
    notification_counts: Dict[str, int] = {"sent": 0, "skipped": 0, "error": 0, "unknown": 0}
    notification_warn_runs = 0

    for strategy in strategy_list:
        for ctx in contexts:
            conservative = ctx == "conservative"
            cmd = _build_cmd(args, strategy=strategy, conservative=conservative)
            timed_out = False
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(repo_root()),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=max(1.0, float(args.child_timeout_sec or 60.0)),
                )
                exit_code = int(proc.returncode)
                payload, parse_error = _parse_json_line(str(proc.stdout or ""))
                level = _classify_exit(exit_code)
                if parse_error:
                    level = "error"
                stdout_text = str(proc.stdout or "").strip()
                stderr_text = str(proc.stderr or "").strip()
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                exit_code = 124
                payload = {}
                parse_error = "child_timeout"
                level = "error"
                stdout_text = str(exc.stdout or "").strip()
                stderr_text = str(exc.stderr or "").strip()
            counts[level] += 1
            notification_status = str(payload.get("notification_status") or "").strip().lower()
            notification_reason = str(payload.get("notification_reason") or "").strip()
            if notification_status in notification_counts:
                notification_counts[notification_status] += 1
            else:
                notification_counts["unknown"] += 1
            if notification_status == "error":
                notification_warn_runs += 1
            rows.append(
                {
                    "strategy": strategy,
                    "context": ctx,
                    "exit_code": exit_code,
                    "level": level,
                    "timed_out": timed_out,
                    "json_parse_error": parse_error,
                    "notification_status": notification_status or "unknown",
                    "notification_reason": notification_reason,
                    "payload": payload,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "command": " ".join(cmd),
                }
            )

    if counts["error"] > 0:
        decision = "ERROR"
        final_exit = 20
    elif counts["warn"] > 0 or notification_warn_runs > 0:
        decision = "WARN"
        final_exit = 4 if bool(args.fail_on_lock_busy) else 0
    else:
        decision = "OK"
        final_exit = 0

    out = {
        "ok": counts["error"] == 0,
        "generated_utc": now_utc(),
        "decision": decision,
        "strategies": strategy_list,
        "contexts": contexts,
        "counts": counts,
        "notification_counts": notification_counts,
        "notification_warning_runs": notification_warn_runs,
        "runs": rows,
        "exit_policy": {
            "ok_codes": [0, 10],
            "warn_codes": [4],
            "error_codes": "others",
            "notification_error_affects_exit": False,
            "fail_on_lock_busy": bool(args.fail_on_lock_busy),
        },
    }
    return int(final_exit), out


def _summary_line(payload: dict) -> str:
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    return (
        "pending_release_batch"
        f" | decision={str(payload.get('decision') or 'UNKNOWN')}"
        f" | strategies={len(payload.get('strategies') or [])}"
        f" | runs={len(payload.get('runs') or [])}"
        f" | ok={int(counts.get('ok') or 0)}"
        f" | warn={int(counts.get('warn') or 0)}"
        f" | error={int(counts.get('error') or 0)}"
    )


def main() -> int:
    args = parse_args()
    code, payload = run_batch(args)
    print(_summary_line(payload))
    if bool(args.pretty):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    out_path = resolve_path(str(args.out_json or ""), "pending_release_batch_latest.json")
    write_json(out_path, payload, pretty=bool(args.pretty))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
