#!/usr/bin/env python3
"""
Report automation health for daily observe-only pipelines.

Checks:
- scheduled task existence/state/last result (Windows)
- artifact freshness under logs/

Outputs:
- logs/automation_health_latest.json
- logs/automation_health_latest.txt
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple


DEFAULT_TASKS = [
    "WeatherTop30ReadinessDaily",
    "WeatherMimicPipelineDaily",
    "NoLongshotDailyReport",
    "SimmerABDailyReport",
    "MorningStrategyStatusDaily",
]
DEFAULT_OPTIONAL_TASKS = [
    "WalletAutopsyDailyReport",
    "EventDrivenDailyReport",
]

DEFAULT_ARTIFACT_SPECS = [
    "logs/strategy_register_latest.json:30",
    "logs/clob_arb_realized_daily.jsonl:30",
    "logs/strategy_realized_pnl_daily.jsonl:30",
    "logs/weather_top30_readiness_report_latest.json:30",
    "logs/weather_top30_readiness_daily_run.log:30",
    "logs/weather_mimic_pipeline_daily_run.log:30",
    "logs/no_longshot_daily_run.log:30",
    "logs/no_longshot_daily_summary.txt:30",
    "?logs/event_driven_daily_run.log:30",
    "?logs/event_driven_daily_summary.txt:30",
    "?logs/event_driven_profit_window_latest.json:30",
    "?logs/wallet_autopsy_daily_run.log:30",
    "?logs/simmer-ab-daily-report.log:30",
    "logs/simmer-ab-daily-compare-latest.txt:30",
    "logs/simmer-ab-daily-compare-history.jsonl:30",
    "logs/morning_status_daily_run.log:30",
    "?logs/simmer-ab-decision-latest.json:30",
    "?logs/simmer_ab_supervisor_state.json:6",
    "?logs/bot_supervisor_state.json:6",
]


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def logs_dir() -> Path:
    p = repo_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_path(raw: str, default_name: str) -> Path:
    s = str(raw or "").strip()
    if not s:
        return logs_dir() / default_name
    p = Path(s)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs_dir() / p.name
    return repo_root() / p


def _parse_iso(ts: str) -> Optional[dt.datetime]:
    s = str(ts or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _normalize_ps_time(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    # Get-ScheduledTaskInfo often yields local datetime string without timezone.
    # Keep raw text for readability and infer staleness only when parse succeeds.
    return s


def _parse_artifact_specs(specs: List[str]) -> List[Tuple[str, float, bool]]:
    out: List[Tuple[str, float, bool]] = []
    for raw in specs:
        s = str(raw or "").strip()
        if not s:
            continue
        optional = False
        if s.startswith("?"):
            optional = True
            s = s[1:].strip()
        if not s:
            continue
        if ":" in s:
            p, h = s.rsplit(":", 1)
            try:
                max_h = float(h)
            except Exception:
                max_h = 30.0
            out.append((p.strip(), max(0.1, max_h), optional))
        else:
            out.append((s, 30.0, optional))
    return out


def _parse_task_specs(specs: List[str]) -> List[Tuple[str, bool]]:
    out: List[Tuple[str, bool]] = []
    seen: set[str] = set()
    for raw in specs:
        s = str(raw or "").strip()
        if not s:
            continue
        optional = False
        if s.startswith("?"):
            optional = True
            s = s[1:].strip()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append((s, not optional))
    return out


def _run_task_query(task_names: List[str]) -> List[dict]:
    if not task_names:
        return []
    if os.name != "nt":
        return [{"task_name": x, "exists": False, "error": "scheduled_task_query_supported_on_windows_only"} for x in task_names]

    escaped = ",".join("'" + n.replace("'", "''") + "'" for n in task_names)
    cmd = (
        "$names=@(" + escaped + ");"
        "$rows=@();"
        "foreach($n in $names){"
        "  $t=Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue;"
        "  if($null -eq $t){"
        "    $rows += [PSCustomObject]@{task_name=$n;exists=$false;state='MISSING';last_run_time='';next_run_time='';last_task_result=$null;action_arguments=''};"
        "    continue;"
        "  }"
        "  $i=Get-ScheduledTaskInfo -TaskName $n -ErrorAction SilentlyContinue;"
        "  $actionArgs='';"
        "  if($t -and $t.Actions){"
        "    $tmp=@();"
        "    foreach($a in @($t.Actions)){"
        "      if($null -ne $a -and $a.Arguments){$tmp += [string]$a.Arguments}"
        "    }"
        "    if($tmp.Count -gt 0){$actionArgs=($tmp -join ' || ')}"
        "  }"
        "  $rows += [PSCustomObject]@{"
        "    task_name=$n;"
        "    exists=$true;"
        "    state=[string]$t.State;"
        "    last_run_time=if($i){[string]$i.LastRunTime}else{''};"
        "    next_run_time=if($i){[string]$i.NextRunTime}else{''};"
        "    last_task_result=if($i){$i.LastTaskResult}else{$null};"
        "    action_arguments=$actionArgs"
        "  };"
        "};"
        "$rows | ConvertTo-Json -Depth 4 -Compress"
    )

    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            check=False,
            capture_output=True,
            text=True,
            # ScheduledTask cmdlets can be slow on some hosts; avoid false NO_GO due to short timeout.
            timeout=90,
        )
    except Exception as exc:
        return [{"task_name": x, "exists": False, "error": f"query_failed:{exc}"} for x in task_names]

    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip()[:300]
        return [{"task_name": x, "exists": False, "error": f"powershell_exit_{p.returncode}:{msg}"} for x in task_names]

    raw = (p.stdout or "").strip()
    if not raw:
        return [{"task_name": x, "exists": False, "error": "empty_query_output"} for x in task_names]

    try:
        obj = json.loads(raw)
    except Exception as exc:
        return [{"task_name": x, "exists": False, "error": f"json_parse_failed:{exc}"} for x in task_names]

    rows: List[dict]
    if isinstance(obj, list):
        rows = [r for r in obj if isinstance(r, dict)]
    elif isinstance(obj, dict):
        rows = [obj]
    else:
        rows = []

    by_name = {str(r.get("task_name") or ""): r for r in rows}
    out: List[dict] = []
    for name in task_names:
        r = by_name.get(name)
        if r is None:
            out.append({"task_name": name, "exists": False, "state": "MISSING", "last_run_time": "", "next_run_time": "", "last_task_result": None})
            continue
        out.append(
            {
                "task_name": name,
                "exists": bool(r.get("exists")),
                "state": str(r.get("state") or ""),
                "last_run_time": _normalize_ps_time(str(r.get("last_run_time") or "")),
                "next_run_time": _normalize_ps_time(str(r.get("next_run_time") or "")),
                "last_task_result": r.get("last_task_result"),
                "action_arguments": str(r.get("action_arguments") or ""),
                "error": str(r.get("error") or ""),
            }
        )
    return out


def _task_status(row: dict) -> str:
    required = bool(row.get("required", True))
    if not bool(row.get("exists")):
        return "MISSING" if required else "OPTIONAL_MISSING"
    if str(row.get("error") or "").strip():
        return "ERROR"
    state = str(row.get("state") or "").strip().lower()
    if state == "disabled":
        return "DISABLED"
    if state == "running":
        return "RUNNING"

    raw_result = row.get("last_task_result")
    result = None
    try:
        if raw_result is not None:
            result = int(raw_result)
    except Exception:
        result = None

    # 0 = success. when never run, Windows commonly reports large nonzero with empty/zeroed run time.
    last_run_text = str(row.get("last_run_time") or "").strip()
    if result is None:
        return "UNKNOWN"
    if result == 267009:
        return "RUNNING"
    if result == 0:
        return "OK"
    if not last_run_text or _looks_like_no_run_time(last_run_text):
        return "NO_RUN_YET"
    return "LAST_RUN_FAILED"


def _looks_like_no_run_time(last_run_text: str) -> bool:
    s = str(last_run_text or "").strip()
    if not s:
        return True
    if "0001" in s or "1601" in s:
        return True
    m = re.search(r"(19\d{2}|20\d{2})", s)
    if m:
        try:
            year = int(m.group(1))
            if year <= 2000:
                return True
        except Exception:
            pass
    return False


def _pid_running(pid: int) -> bool:
    if int(pid or 0) <= 0:
        return False
    if os.name == "nt":
        try:
            cp = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            out = (cp.stdout or "").strip()
            if (not out) or ("No tasks are running" in out):
                return False
            reader = csv.reader(io.StringIO(out))
            for row in reader:
                if len(row) < 2:
                    continue
                try:
                    if int(str(row[1]).strip()) == int(pid):
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def _is_fresh_artifact(artifact_rows: List[dict], suffix: str) -> bool:
    target = str(suffix or "").replace("/", "\\").lower()
    for row in artifact_rows:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").replace("/", "\\").lower()
        if path.endswith(target):
            return str(row.get("status") or "").upper() == "FRESH"
    return False


def _find_artifact_row(artifact_rows: List[dict], suffix: str) -> Optional[dict]:
    target = str(suffix or "").replace("/", "\\").lower()
    for row in artifact_rows:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").replace("/", "\\").lower()
        if path.endswith(target):
            return row
    return None


def _is_supervisor_job_enabled(job_name: str) -> bool:
    cfg = repo_root() / "configs" / "bot_supervisor.observe.json"
    if not cfg.exists():
        return False
    try:
        payload = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        return False
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        return False
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if str(job.get("name") or "").strip() != str(job_name):
            continue
        return bool(job.get("enabled"))
    return False


def _apply_soft_fail_overrides(task_rows: List[dict], artifact_rows: List[dict]) -> None:
    top30_fresh = _is_fresh_artifact(artifact_rows, r"logs\weather_top30_readiness_report_latest.json")
    top30_log_fresh = _is_fresh_artifact(artifact_rows, r"logs\weather_top30_readiness_daily_run.log")
    mimic_log_fresh = _is_fresh_artifact(artifact_rows, r"logs\weather_mimic_pipeline_daily_run.log")
    no_longshot_log_fresh = _is_fresh_artifact(artifact_rows, r"logs\no_longshot_daily_run.log")
    wallet_autopsy_log_fresh = _is_fresh_artifact(artifact_rows, r"logs\wallet_autopsy_daily_run.log")
    simmer_log_fresh = _is_fresh_artifact(artifact_rows, r"logs\simmer-ab-daily-report.log")
    morning_log_fresh = _is_fresh_artifact(artifact_rows, r"logs\morning_status_daily_run.log")
    event_driven_log_fresh = _is_fresh_artifact(artifact_rows, r"logs\event_driven_daily_run.log")
    interrupted_codes = {3221225786, 267014}

    for row in task_rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "")
        if status != "LAST_RUN_FAILED":
            continue
        name = str(row.get("task_name") or "")
        try:
            code = int(row.get("last_task_result"))
        except Exception:
            continue

        if name == "MorningStrategyStatusDaily" and morning_log_fresh and code in interrupted_codes:
            row["status"] = "SOFT_FAIL_INTERRUPTED"
            row["status_note"] = "last_result indicates interrupted task host but morning status runner log is fresh"
            continue

        # Some Windows task host terminations report interrupt-like nonzero codes
        # even when the pipeline artifacts were refreshed successfully.
        if code not in interrupted_codes:
            continue

        if name == "WeatherTop30ReadinessDaily" and (top30_fresh or top30_log_fresh):
            row["status"] = "SOFT_FAIL_INTERRUPTED"
            row["status_note"] = f"last_result={code} indicates interrupted task host but top30 artifacts/log are fresh"
        elif name == "WeatherMimicPipelineDaily" and mimic_log_fresh:
            row["status"] = "SOFT_FAIL_INTERRUPTED"
            row["status_note"] = f"last_result={code} indicates interrupted task host but mimic runner log is fresh"
        elif name == "NoLongshotDailyReport" and no_longshot_log_fresh:
            row["status"] = "SOFT_FAIL_INTERRUPTED"
            row["status_note"] = f"last_result={code} indicates interrupted task host but no_longshot runner log is fresh"
        elif name == "WalletAutopsyDailyReport" and wallet_autopsy_log_fresh:
            row["status"] = "SOFT_FAIL_INTERRUPTED"
            row["status_note"] = (
                f"last_result={code} indicates interrupted task host but wallet autopsy runner log is fresh"
            )
        elif name == "SimmerABDailyReport" and simmer_log_fresh:
            row["status"] = "SOFT_FAIL_INTERRUPTED"
            row["status_note"] = f"last_result={code} indicates interrupted task host but simmer runner log is fresh"
        elif name == "EventDrivenDailyReport" and event_driven_log_fresh:
            row["status"] = "SOFT_FAIL_INTERRUPTED"
            row["status_note"] = f"last_result={code} indicates interrupted task host but event-driven runner log is fresh"


def _apply_supervisor_overrides(task_rows: List[dict]) -> None:
    no_longshot_daemon_enabled = _is_supervisor_job_enabled("no_longshot_daily_daemon")
    weather_daemon_enabled = _is_supervisor_job_enabled("weather_daily_daemon")
    event_driven_enabled = _is_supervisor_job_enabled("event_driven")
    for row in task_rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "") != "DISABLED":
            continue
        task_name = str(row.get("task_name") or "")
        if task_name == "NoLongshotDailyReport" and no_longshot_daemon_enabled:
            row["status"] = "SUPPRESSED_BY_SUPERVISOR"
            row["status_note"] = "NoLongshotDailyReport disabled while no_longshot_daily_daemon is enabled"
        elif task_name in {"WeatherMimicPipelineDaily", "WeatherTop30ReadinessDaily"} and weather_daemon_enabled:
            row["status"] = "SUPPRESSED_BY_SUPERVISOR"
            row["status_note"] = f"{task_name} disabled while weather_daily_daemon is enabled"
        elif task_name == "EventDrivenDailyReport" and event_driven_enabled:
            row["status"] = "SUPPRESSED_BY_SUPERVISOR"
            row["status_note"] = "EventDrivenDailyReport disabled while event_driven supervisor job is enabled"


def _apply_duplicate_run_guard(task_rows: List[dict]) -> None:
    # Canon requires one no-longshot mode only: scheduled task or daemon.
    if not _is_supervisor_job_enabled("no_longshot_daily_daemon"):
        return

    for row in task_rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("task_name") or "") != "NoLongshotDailyReport":
            continue
        status = str(row.get("status") or "")
        if status in {"DISABLED", "SUPPRESSED_BY_SUPERVISOR", "MISSING"}:
            return
        row["status"] = "DUPLICATE_RUN_RISK"
        row["status_note"] = (
            "NoLongshotDailyReport is active while no_longshot_daily_daemon is enabled "
            "(disable one mode)"
        )
        return


def _apply_morning_task_argument_guard(task_rows: List[dict]) -> None:
    target_name = "MorningStrategyStatusDaily"
    required_value_flags = [
        "-nolongshotpracticaldecisiondate",
        "-nolongshotpracticalslidedays",
        "-nolongshotpracticalminresolvedtrades",
        "-simmerabinterimtarget",
    ]
    required_switch_flags = [
        "-failonsimmerabinterimnogo",
        "-nobackground",
    ]
    forbidden_flags = [
        "-norefresh",
        "-skiphealth",
        "-skipgatealarm",
        "-skipuncorrelatedportfolio",
        "-skipimplementationledger",
        "-skipsimmerab",
    ]

    for row in task_rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("task_name") or "") != target_name:
            continue
        if not bool(row.get("exists")):
            return
        status = str(row.get("status") or "").upper()
        if status in {"MISSING", "OPTIONAL_MISSING", "ERROR"}:
            return
        args_raw = str(row.get("action_arguments") or "").strip()
        if not args_raw:
            row["status"] = "INVALID_CONTENT"
            row["status_note"] = "missing_action_arguments"
            return
        if "||" in args_raw:
            row["status"] = "INVALID_CONTENT"
            row["status_note"] = "multiple_actions_detected"
            return

        def _has_flag_token(flag: str) -> bool:
            return bool(re.search(rf"(?i)(?:^|\s){re.escape(flag)}(?::|\s|$)", args_raw))

        def _switch_enabled(flag: str) -> bool:
            pattern = rf"(?i)(?:^|\s){re.escape(flag)}(?:\s*:\s*(\$?true|\$?false))?(?=\s|$)"
            for m in re.finditer(pattern, args_raw):
                raw = str(m.group(1) or "").strip().lower().lstrip("$")
                if not raw or raw == "true":
                    return True
                if raw == "false":
                    continue
                return True
            return False

        missing = [f for f in required_value_flags if not _has_flag_token(f)]
        missing += [f for f in required_switch_flags if not _switch_enabled(f)]
        forbidden = [f for f in forbidden_flags if _switch_enabled(f)]

        invalid_values: List[str] = []

        m_practical_date = re.search(
            r"(?i)(?:^|\s)-nolongshotpracticaldecisiondate(?:\s+|:)(\"[^\"]+\"|'[^']+'|[^\s|]+)",
            args_raw,
        )
        if m_practical_date:
            raw_date = str(m_practical_date.group(1) or "").strip().strip("\"'")
            try:
                dt.datetime.strptime(raw_date, "%Y-%m-%d")
            except Exception:
                invalid_values.append("nolongshotpracticaldecisiondate_not_yyyy-mm-dd")

        m_slide_days = re.search(
            r"(?i)(?:^|\s)-nolongshotpracticalslidedays(?:\s+|:)(\"[^\"]+\"|'[^']+'|[^\s|]+)",
            args_raw,
        )
        if m_slide_days:
            try:
                if int(str(m_slide_days.group(1) or "").strip().strip("\"'")) < 1:
                    invalid_values.append("nolongshotpracticalslidedays<1")
            except Exception:
                invalid_values.append("nolongshotpracticalslidedays_not_int")

        m_min_resolved = re.search(
            r"(?i)(?:^|\s)-nolongshotpracticalminresolvedtrades(?:\s+|:)(\"[^\"]+\"|'[^']+'|[^\s|]+)",
            args_raw,
        )
        if m_min_resolved:
            try:
                if int(str(m_min_resolved.group(1) or "").strip().strip("\"'")) < 1:
                    invalid_values.append("nolongshotpracticalminresolvedtrades<1")
            except Exception:
                invalid_values.append("nolongshotpracticalminresolvedtrades_not_int")

        interim_target_value: Optional[str] = None
        if _has_flag_token("-simmerabinterimtarget"):
            m = re.search(
                r"(?i)(?:^|\s)-simmerabinterimtarget(?:\s+|:)(\"[^\"]+\"|'[^']+'|[^\s|]+)",
                args_raw,
            )
            if m:
                candidate = str(m.group(1) or "").strip().strip("\"'")
                interim_target_value = "" if candidate.startswith("-") else candidate
            else:
                interim_target_value = ""
        invalid_interim_target = bool(interim_target_value is not None and interim_target_value.lower() not in {"7d", "14d"})

        if not missing and not forbidden and not invalid_values and not invalid_interim_target:
            return

        notes: List[str] = []
        if missing:
            notes.append("missing_required_flags=" + ",".join(missing))
        if forbidden:
            notes.append("forbidden_flags=" + ",".join(forbidden))
        if invalid_values:
            notes.append("invalid_values=" + ",".join(invalid_values))
        if invalid_interim_target:
            shown = interim_target_value if interim_target_value else "<missing>"
            notes.append(f"invalid_simmer_ab_interim_target={shown}; allowed=7d,14d")
        row["status"] = "INVALID_CONTENT"
        row["status_note"] = "; ".join(notes)
        return


def _apply_simmer_ab_task_argument_guard(task_rows: List[dict]) -> None:
    target_name = "SimmerABDailyReport"
    required_value_flags = [
        "-judgemindays",
        "-judgeminwindowhours",
        "-judgeexpectancyratiothreshold",
        "-judgedecisiondate",
    ]
    required_switch_flags = [
        "-failonfinalnogo",
        "-nobackground",
    ]
    forbidden_switch_flags = [
        "-skipjudge",
    ]

    for row in task_rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("task_name") or "") != target_name:
            continue
        if not bool(row.get("exists")):
            return
        status = str(row.get("status") or "").upper()
        if status in {"MISSING", "OPTIONAL_MISSING", "ERROR"}:
            return
        args_raw = str(row.get("action_arguments") or "").strip()
        if not args_raw:
            row["status"] = "INVALID_CONTENT"
            row["status_note"] = "missing_action_arguments"
            return
        if "||" in args_raw:
            row["status"] = "INVALID_CONTENT"
            row["status_note"] = "multiple_actions_detected"
            return

        def _has_flag_token(flag: str) -> bool:
            return bool(re.search(rf"(?i)(?:^|\s){re.escape(flag)}(?::|\s|$)", args_raw))

        def _switch_enabled(flag: str) -> bool:
            pattern = rf"(?i)(?:^|\s){re.escape(flag)}(?:\s*:\s*(\$?true|\$?false))?(?=\s|$)"
            for m in re.finditer(pattern, args_raw):
                raw = str(m.group(1) or "").strip().lower().lstrip("$")
                if not raw or raw == "true":
                    return True
                if raw == "false":
                    continue
                return True
            return False

        missing = [f for f in required_value_flags if not _has_flag_token(f)]
        missing += [f for f in required_switch_flags if not _switch_enabled(f)]
        forbidden = [f for f in forbidden_switch_flags if _switch_enabled(f)]

        # Parse key numeric/date values when present to catch malformed action strings.
        invalid_values: List[str] = []

        m_min_days = re.search(r"(?i)(?:^|\s)-judgemindays(?:\s+|:)([^\s|]+)", args_raw)
        if m_min_days:
            try:
                if int(str(m_min_days.group(1)).strip().strip("\"'")) < 1:
                    invalid_values.append("judgemindays<1")
            except Exception:
                invalid_values.append("judgemindays_not_int")

        m_min_window = re.search(r"(?i)(?:^|\s)-judgeminwindowhours(?:\s+|:)([^\s|]+)", args_raw)
        if m_min_window:
            try:
                if float(str(m_min_window.group(1)).strip().strip("\"'")) <= 0.0:
                    invalid_values.append("judgeminwindowhours<=0")
            except Exception:
                invalid_values.append("judgeminwindowhours_not_number")

        m_decision_date = re.search(r"(?i)(?:^|\s)-judgedecisiondate(?:\s+|:)([^\s|]+)", args_raw)
        if m_decision_date:
            raw_date = str(m_decision_date.group(1)).strip().strip("\"'")
            try:
                dt.datetime.strptime(raw_date, "%Y-%m-%d")
            except Exception:
                invalid_values.append("judgedecisiondate_not_yyyy-mm-dd")

        if not missing and not forbidden and not invalid_values:
            return

        notes: List[str] = []
        if missing:
            notes.append("missing_required_flags=" + ",".join(missing))
        if forbidden:
            notes.append("forbidden_flags=" + ",".join(forbidden))
        if invalid_values:
            notes.append("invalid_values=" + ",".join(invalid_values))
        row["status"] = "INVALID_CONTENT"
        row["status_note"] = "; ".join(notes)
        return


def _apply_event_driven_task_argument_guard(task_rows: List[dict]) -> None:
    target_name = "EventDrivenDailyReport"
    required_switch_flags = [
        "-nobackground",
    ]
    forbidden_switch_flags = [
        "-skipprofitwindow",
    ]

    for row in task_rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("task_name") or "") != target_name:
            continue
        if not bool(row.get("exists")):
            return
        status = str(row.get("status") or "").upper()
        if status in {"MISSING", "OPTIONAL_MISSING", "ERROR", "DISABLED", "SUPPRESSED_BY_SUPERVISOR"}:
            return
        args_raw = str(row.get("action_arguments") or "").strip()
        if not args_raw:
            row["status"] = "INVALID_CONTENT"
            row["status_note"] = "missing_action_arguments"
            return
        if "||" in args_raw:
            row["status"] = "INVALID_CONTENT"
            row["status_note"] = "multiple_actions_detected"
            return

        def _switch_enabled(flag: str) -> bool:
            pattern = rf"(?i)(?:^|\s){re.escape(flag)}(?:\s*:\s*(\$?true|\$?false))?(?=\s|$)"
            for m in re.finditer(pattern, args_raw):
                raw = str(m.group(1) or "").strip().lower().lstrip("$")
                if not raw or raw == "true":
                    return True
                if raw == "false":
                    continue
                return True
            return False

        missing = [f for f in required_switch_flags if not _switch_enabled(f)]
        forbidden = [f for f in forbidden_switch_flags if _switch_enabled(f)]
        if not missing and not forbidden:
            return

        notes: List[str] = []
        if missing:
            notes.append("missing_required_flags=" + ",".join(missing))
        if forbidden:
            notes.append("forbidden_flags=" + ",".join(forbidden))
        row["status"] = "INVALID_CONTENT"
        row["status_note"] = "; ".join(notes)
        return


def _artifact_rows(specs: List[Tuple[str, float, bool]]) -> List[dict]:
    now = now_utc()
    out: List[dict] = []
    for rel_path, max_age_h, optional in specs:
        p = resolve_path(rel_path, Path(rel_path).name or "artifact")
        exists = p.exists()
        row: Dict[str, object] = {
            "path": str(p),
            "exists": exists,
            "required": not bool(optional),
            "max_age_hours": float(max_age_h),
            "status": "MISSING",
            "age_hours": None,
            "modified_utc": "",
        }
        if exists:
            mt = dt.datetime.fromtimestamp(p.stat().st_mtime, tz=dt.timezone.utc)
            age_h = (now - mt).total_seconds() / 3600.0
            row["age_hours"] = float(age_h)
            row["modified_utc"] = mt.isoformat()
            row["status"] = "FRESH" if age_h <= float(max_age_h) else "STALE"
        elif optional:
            row["status"] = "OPTIONAL_MISSING"
        out.append(row)
    return out


def _has_morning_kpi_marker(path: Path, marker: str, tail_lines: int = 400) -> bool:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return False
    if tail_lines > 0 and len(lines) > tail_lines:
        lines = lines[-tail_lines:]
    for line in lines:
        if marker in line:
            return True
    return False


def _extract_nested(payload: dict, dotted_key: str) -> tuple[bool, object]:
    cur: object = payload
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _apply_strategy_register_kpi_key_check(artifact_rows: List[dict]) -> None:
    target_suffix = r"logs\strategy_register_latest.json"
    required_keys = [
        "kpi_core.daily_realized_pnl_usd",
        "kpi_core.monthly_return_now_text",
        "kpi_core.max_drawdown_30d_text",
        "no_longshot_status.monthly_return_now_text",
        "no_longshot_status.monthly_return_now_source",
        "no_longshot_status.monthly_return_now_new_condition_text",
        "no_longshot_status.monthly_return_now_all_text",
        "realized_30d_gate.decision",
    ]
    na_tokens = {"n/a", "na", "none", "null", "-"}

    for row in artifact_rows:
        if not isinstance(row, dict):
            continue
        path_text = str(row.get("path") or "").replace("/", "\\").lower()
        if not path_text.endswith(target_suffix):
            continue
        status = str(row.get("status") or "").upper()
        # Freshness check handles missing/stale cases; content check applies only to currently fresh files.
        if status != "FRESH":
            return
        p = Path(str(row.get("path") or ""))
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            if bool(row.get("required", True)):
                row["status"] = "INVALID_CONTENT"
            else:
                row["status"] = "OPTIONAL_INVALID_CONTENT"
            row["status_note"] = f"json_parse_failed:{type(exc).__name__}"
            return
        if not isinstance(payload, dict):
            if bool(row.get("required", True)):
                row["status"] = "INVALID_CONTENT"
            else:
                row["status"] = "OPTIONAL_INVALID_CONTENT"
            row["status_note"] = "invalid_json_root:not_object"
            return

        missing: List[str] = []
        for key in required_keys:
            ok, value = _extract_nested(payload, key)
            if not ok:
                missing.append(key)
                continue
            if isinstance(value, str) and not value.strip():
                missing.append(key)
        if missing:
            if bool(row.get("required", True)):
                row["status"] = "INVALID_CONTENT"
            else:
                row["status"] = "OPTIONAL_INVALID_CONTENT"
            row["status_note"] = "missing_or_empty_keys=" + ",".join(missing)
            return

        ok_text, monthly_now_text_obj = _extract_nested(payload, "kpi_core.monthly_return_now_text")
        ok_source, monthly_now_source_obj = _extract_nested(payload, "kpi_core.monthly_return_now_source")
        if (not ok_text) or (not ok_source):
            ok_text, monthly_now_text_obj = _extract_nested(payload, "no_longshot_status.monthly_return_now_text")
            ok_source, monthly_now_source_obj = _extract_nested(payload, "no_longshot_status.monthly_return_now_source")
        monthly_now_text = str(monthly_now_text_obj or "").strip().lower() if ok_text else ""
        monthly_now_source = str(monthly_now_source_obj or "").strip() if ok_source else ""
        has_real_value = monthly_now_text not in na_tokens
        source_ok = (
            monthly_now_source.startswith("realized_rolling_30d")
            or monthly_now_source == "realized_monthly_return.projected_monthly_return_text"
        )
        if has_real_value and not source_ok:
            if bool(row.get("required", True)):
                row["status"] = "INVALID_CONTENT"
            else:
                row["status"] = "OPTIONAL_INVALID_CONTENT"
            row["status_note"] = (
                "invalid_monthly_source="
                + monthly_now_source
                + " expected_prefix=realized_rolling_30d"
            )
        return


def _apply_morning_kpi_marker_check(artifact_rows: List[dict]) -> None:
    target_suffix = r"logs\morning_status_daily_run.log"
    marker = "kpi[post] no_longshot.monthly_return_now_text="

    for row in artifact_rows:
        if not isinstance(row, dict):
            continue
        path_text = str(row.get("path") or "").replace("/", "\\").lower()
        if not path_text.endswith(target_suffix):
            continue
        status = str(row.get("status") or "").upper()
        # Freshness check handles missing/stale cases; content check applies only to currently fresh logs.
        if status != "FRESH":
            return
        p = Path(str(row.get("path") or ""))
        if not _has_morning_kpi_marker(p, marker=marker, tail_lines=400):
            if bool(row.get("required", True)):
                row["status"] = "INVALID_CONTENT"
            else:
                row["status"] = "OPTIONAL_INVALID_CONTENT"
            row["status_note"] = f"missing marker: {marker}"
        return


def _apply_no_longshot_summary_mode_check(artifact_rows: List[dict]) -> None:
    target_suffix = r"logs\no_longshot_daily_summary.txt"
    required_marker = "- strict_realized_band_only: True"

    for row in artifact_rows:
        if not isinstance(row, dict):
            continue
        path_text = str(row.get("path") or "").replace("/", "\\").lower()
        if not path_text.endswith(target_suffix):
            continue
        status = str(row.get("status") or "").upper()
        # Freshness check handles missing/stale cases; content check applies only to currently fresh summary.
        if status != "FRESH":
            return
        p = Path(str(row.get("path") or ""))
        if not _has_morning_kpi_marker(p, marker=required_marker, tail_lines=400):
            if bool(row.get("required", True)):
                row["status"] = "INVALID_CONTENT"
            else:
                row["status"] = "OPTIONAL_INVALID_CONTENT"
            row["status_note"] = f"missing marker: {required_marker}"
        return


def _apply_simmer_ab_supervisor_state_check(artifact_rows: List[dict]) -> None:
    target_suffix = r"logs\simmer_ab_supervisor_state.json"

    def invalidate(row: dict, reason: str) -> None:
        # Optional artifact means "missing is tolerated"; if present+fresh, content must be valid.
        row["status"] = "INVALID_CONTENT"
        row["status_note"] = reason

    for row in artifact_rows:
        if not isinstance(row, dict):
            continue
        path_text = str(row.get("path") or "").replace("/", "\\").lower()
        if not path_text.endswith(target_suffix):
            continue
        status = str(row.get("status") or "").upper()
        if status != "FRESH":
            return

        p = Path(str(row.get("path") or ""))
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            invalidate(row, f"json_parse_failed:{type(exc).__name__}")
            return
        if not isinstance(payload, dict):
            invalidate(row, "invalid_json_root:not_object")
            return

        supervisor_running = bool(payload.get("supervisor_running"))
        supervisor_pid = int(payload.get("supervisor_pid") or 0)
        mode = str(payload.get("mode") or "").strip().lower()
        if mode != "run":
            invalidate(row, f"invalid_mode:{mode or '-'}")
            return
        if not supervisor_running:
            invalidate(row, "supervisor_running=false")
            return
        if supervisor_pid <= 0 or not _pid_running(supervisor_pid):
            invalidate(row, f"supervisor_pid_not_running:{supervisor_pid}")
            return

        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            invalidate(row, "invalid_jobs:not_list")
            return
        enabled_jobs = [j for j in jobs if isinstance(j, dict) and bool(j.get("enabled"))]
        if not enabled_jobs:
            invalidate(row, "enabled_jobs=0")
            return
        for job in enabled_jobs:
            name = str(job.get("name") or "-")
            running = bool(job.get("running"))
            pid = int(job.get("pid") or 0)
            if not running:
                invalidate(row, f"job_not_running:{name}")
                return
            if pid <= 0 or not _pid_running(pid):
                invalidate(row, f"job_pid_not_running:{name}:{pid}")
                return
        return


def _apply_bot_supervisor_state_check(artifact_rows: List[dict]) -> None:
    target_suffix = r"logs\bot_supervisor_state.json"

    def invalidate(row: dict, reason: str) -> None:
        row["status"] = "INVALID_CONTENT"
        row["status_note"] = reason

    for row in artifact_rows:
        if not isinstance(row, dict):
            continue
        path_text = str(row.get("path") or "").replace("/", "\\").lower()
        if not path_text.endswith(target_suffix):
            continue
        status = str(row.get("status") or "").upper()
        if status != "FRESH":
            return

        p = Path(str(row.get("path") or ""))
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            invalidate(row, f"json_parse_failed:{type(exc).__name__}")
            return
        if not isinstance(payload, dict):
            invalidate(row, "invalid_json_root:not_object")
            return

        mode = str(payload.get("mode") or "").strip().lower()
        supervisor_running = bool(payload.get("supervisor_running"))
        supervisor_pid = int(payload.get("supervisor_pid") or 0)

        if mode != "run":
            invalidate(row, f"invalid_mode:{mode or '-'}")
            return
        if not supervisor_running:
            invalidate(row, "supervisor_running=false")
            return
        if supervisor_pid <= 0 or not _pid_running(supervisor_pid):
            invalidate(row, f"supervisor_pid_not_running:{supervisor_pid}")
            return

        # Keep this focused: enforce event-driven runtime only when canonical config enables it.
        if not _is_supervisor_job_enabled("event_driven"):
            return

        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            invalidate(row, "invalid_jobs:not_list")
            return
        event_row = None
        for job in jobs:
            if not isinstance(job, dict):
                continue
            if str(job.get("name") or "").strip() != "event_driven":
                continue
            if not bool(job.get("enabled")):
                continue
            event_row = job
            break
        if not isinstance(event_row, dict):
            invalidate(row, "event_driven_enabled_in_config_but_missing_in_state")
            return

        running = bool(event_row.get("running"))
        pid = int(event_row.get("pid") or 0)
        if not running:
            invalidate(row, "event_driven_job_not_running")
            return
        if pid <= 0 or not _pid_running(pid):
            invalidate(row, f"event_driven_job_pid_not_running:{pid}")
            return
        return


def _apply_event_driven_supervisor_guard(task_rows: List[dict], artifact_rows: List[dict]) -> None:
    if not _is_supervisor_job_enabled("event_driven"):
        return

    target_task: Optional[dict] = None
    for row in task_rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("task_name") or "") == "EventDrivenDailyReport":
            target_task = row
            break
    if target_task is None:
        return

    if str(target_task.get("status") or "") != "SUPPRESSED_BY_SUPERVISOR":
        return

    state_row = _find_artifact_row(artifact_rows, r"logs\bot_supervisor_state.json")
    if state_row is None:
        target_task["status"] = "INVALID_CONTENT"
        target_task["status_note"] = "EventDrivenDailyReport suppressed but bot_supervisor_state check is missing"
        return

    state_status = str(state_row.get("status") or "").upper()
    if state_status != "FRESH":
        target_task["status"] = "INVALID_CONTENT"
        target_task["status_note"] = f"EventDrivenDailyReport suppressed but bot_supervisor_state status={state_status or '-'}"


def _render_txt(payload: dict) -> str:
    lines: List[str] = []
    lines.append(f"Automation health @ {payload.get('generated_utc','')}")
    lines.append(f"decision: {payload.get('decision','UNKNOWN')}")
    reasons = payload.get("reasons") if isinstance(payload.get("reasons"), list) else []
    for r in reasons:
        lines.append(f"- {r}")
    lines.append("")

    lines.append("Tasks:")
    tasks = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        note = str(t.get("status_note") or "").strip()
        note_txt = f" note={note}" if note else ""
        req_txt = "required" if bool(t.get("required", True)) else "optional"
        lines.append(
            "  {0}: {1} state={2} last_result={3} last_run={4} next_run={5} ({6})".format(
                str(t.get("task_name") or "-"),
                str(t.get("status") or "-"),
                str(t.get("state") or "-"),
                str(t.get("last_task_result") if t.get("last_task_result") is not None else "-"),
                str(t.get("last_run_time") or "-"),
                str(t.get("next_run_time") or "-"),
                req_txt,
            )
            + note_txt
        )

    lines.append("")
    lines.append("Artifacts:")
    arts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
    for a in arts:
        if not isinstance(a, dict):
            continue
        age_txt = "-"
        if a.get("age_hours") is not None:
            try:
                age_txt = f"{float(a.get('age_hours')):.2f}h"
            except Exception:
                age_txt = str(a.get("age_hours"))
        req_txt = "required" if bool(a.get("required", True)) else "optional"
        note = str(a.get("status_note") or "").strip()
        note_txt = f" note={note}" if note else ""
        lines.append(
            "  {0}: {1} age={2} max={3}h ({4})".format(
                str(a.get("path") or "-"),
                str(a.get("status") or "-"),
                age_txt,
                str(a.get("max_age_hours") if a.get("max_age_hours") is not None else "-"),
                req_txt,
            )
            + note_txt
        )
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description="Report automation health (observe-only).")
    p.add_argument(
        "--task",
        action="append",
        default=[],
        help="Repeatable scheduled task name to check; prefix with '?' to mark optional",
    )
    p.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="Repeatable artifact spec PATH[:MAX_AGE_HOURS] or ?PATH[:MAX_AGE_HOURS] for optional artifacts",
    )
    p.add_argument("--out-json", default="", help="Output JSON path (default logs/automation_health_latest.json)")
    p.add_argument("--out-txt", default="", help="Output TXT path (default logs/automation_health_latest.txt)")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = p.parse_args()

    task_specs = [str(x).strip() for x in (args.task or []) if str(x).strip()]
    if not task_specs:
        task_specs = list(DEFAULT_TASKS) + [f"?{x}" for x in DEFAULT_OPTIONAL_TASKS]
    parsed_task_specs = _parse_task_specs(task_specs)
    task_names = [name for name, _required in parsed_task_specs]
    task_required_map = {name: required for name, required in parsed_task_specs}
    artifact_specs = [str(x).strip() for x in (args.artifact or []) if str(x).strip()]
    if not artifact_specs:
        artifact_specs = list(DEFAULT_ARTIFACT_SPECS)
    parsed_specs = _parse_artifact_specs(artifact_specs)

    task_rows = _run_task_query(task_names)
    for r in task_rows:
        task_name = str(r.get("task_name") or "")
        r["required"] = bool(task_required_map.get(task_name, True))
        r["status"] = _task_status(r)

    art_rows = _artifact_rows(parsed_specs)
    _apply_strategy_register_kpi_key_check(art_rows)
    _apply_morning_kpi_marker_check(art_rows)
    _apply_no_longshot_summary_mode_check(art_rows)
    _apply_simmer_ab_supervisor_state_check(art_rows)
    _apply_bot_supervisor_state_check(art_rows)
    _apply_soft_fail_overrides(task_rows, art_rows)
    _apply_supervisor_overrides(task_rows)
    _apply_event_driven_supervisor_guard(task_rows, art_rows)
    _apply_duplicate_run_guard(task_rows)
    _apply_morning_task_argument_guard(task_rows)
    _apply_simmer_ab_task_argument_guard(task_rows)
    _apply_event_driven_task_argument_guard(task_rows)

    reasons: List[str] = []
    bad_task = [
        r
        for r in task_rows
        if bool(r.get("required", True))
        and str(r.get("status"))
        in {"MISSING", "ERROR", "LAST_RUN_FAILED", "DISABLED", "DUPLICATE_RUN_RISK", "INVALID_CONTENT"}
    ]
    optional_missing_tasks = [r for r in task_rows if str(r.get("status")) == "OPTIONAL_MISSING"]
    stale_art = [r for r in art_rows if str(r.get("status")) in {"MISSING", "STALE", "INVALID_CONTENT"}]
    duplicate_run_guard_violations = [r for r in task_rows if str(r.get("status")) == "DUPLICATE_RUN_RISK"]
    if duplicate_run_guard_violations:
        reasons.append(f"duplicate_run_guard_violations={len(duplicate_run_guard_violations)}")
    if bad_task:
        reasons.append(f"bad_tasks={len(bad_task)}")
    if stale_art:
        reasons.append(f"stale_or_missing_artifacts={len(stale_art)}")
    if not reasons:
        reasons.append("all required checks passed")

    decision = "GO" if (not bad_task and not stale_art) else "NO_GO"

    payload = {
        "generated_utc": now_utc().isoformat(),
        "meta": {"observe_only": True, "source": "scripts/report_automation_health.py"},
        "decision": decision,
        "reasons": reasons,
        "tasks": task_rows,
        "artifacts": art_rows,
        "summary": {
            "task_count": len(task_rows),
            "task_bad_count": len(bad_task),
            "task_optional_missing_count": len(optional_missing_tasks),
            "artifact_count": len(art_rows),
            "artifact_stale_or_missing_count": len(stale_art),
        },
    }

    out_json = resolve_path(str(args.out_json), "automation_health_latest.json")
    out_txt = resolve_path(str(args.out_txt), "automation_health_latest.txt")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_txt.parent.mkdir(parents=True, exist_ok=True)

    with out_json.open("w", encoding="utf-8") as f:
        if bool(args.pretty):
            json.dump(payload, f, ensure_ascii=False, indent=2)
        else:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    out_txt.write_text(_render_txt(payload), encoding="utf-8")

    print(f"[automation-health] decision={decision}")
    print(f"[automation-health] out_json={out_json}")
    print(f"[automation-health] out_txt={out_txt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
