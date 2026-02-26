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
import datetime as dt
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
    "MorningStrategyStatusDaily",
]

DEFAULT_ARTIFACT_SPECS = [
    "logs/strategy_register_latest.json:30",
    "logs/clob_arb_realized_daily.jsonl:30",
    "logs/strategy_realized_pnl_daily.jsonl:30",
    "logs/weather_top30_readiness_report_latest.json:30",
    "logs/weather_top30_readiness_daily_run.log:30",
    "logs/weather_mimic_pipeline_daily_run.log:30",
    "logs/no_longshot_daily_run.log:30",
    "logs/morning_status_daily_run.log:30",
    "?logs/simmer-ab-decision-latest.json:30",
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
        "    $rows += [PSCustomObject]@{task_name=$n;exists=$false;state='MISSING';last_run_time='';next_run_time='';last_task_result=$null};"
        "    continue;"
        "  }"
        "  $i=Get-ScheduledTaskInfo -TaskName $n -ErrorAction SilentlyContinue;"
        "  $rows += [PSCustomObject]@{"
        "    task_name=$n;"
        "    exists=$true;"
        "    state=[string]$t.State;"
        "    last_run_time=if($i){[string]$i.LastRunTime}else{''};"
        "    next_run_time=if($i){[string]$i.NextRunTime}else{''};"
        "    last_task_result=if($i){$i.LastTaskResult}else{$null}"
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
                "error": str(r.get("error") or ""),
            }
        )
    return out


def _task_status(row: dict) -> str:
    if not bool(row.get("exists")):
        return "MISSING"
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


def _is_fresh_artifact(artifact_rows: List[dict], suffix: str) -> bool:
    target = str(suffix or "").replace("/", "\\").lower()
    for row in artifact_rows:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").replace("/", "\\").lower()
        if path.endswith(target):
            return str(row.get("status") or "").upper() == "FRESH"
    return False


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
    morning_log_fresh = _is_fresh_artifact(artifact_rows, r"logs\morning_status_daily_run.log")

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

        if name == "MorningStrategyStatusDaily" and morning_log_fresh and code in (3221225786, 267014):
            row["status"] = "SOFT_FAIL_INTERRUPTED"
            row["status_note"] = "last_result indicates interrupted task host but morning status runner log is fresh"
            continue

        # Some Windows task host terminations report STATUS_CONTROL_C_EXIT (0xC000013A)
        # even when the pipeline artifacts were refreshed successfully.
        if code != 3221225786:
            continue

        if name == "WeatherTop30ReadinessDaily" and (top30_fresh or top30_log_fresh):
            row["status"] = "SOFT_FAIL_INTERRUPTED"
            row["status_note"] = "last_result=0xC000013A but top30 artifacts/log are fresh"
        elif name == "WeatherMimicPipelineDaily" and mimic_log_fresh:
            row["status"] = "SOFT_FAIL_INTERRUPTED"
            row["status_note"] = "last_result=0xC000013A but mimic runner log is fresh"
        elif name == "NoLongshotDailyReport" and no_longshot_log_fresh:
            row["status"] = "SOFT_FAIL_INTERRUPTED"
            row["status_note"] = "last_result=0xC000013A but no_longshot runner log is fresh"


def _apply_supervisor_overrides(task_rows: List[dict]) -> None:
    no_longshot_daemon_enabled = _is_supervisor_job_enabled("no_longshot_daily_daemon")
    weather_daemon_enabled = _is_supervisor_job_enabled("weather_daily_daemon")
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
        lines.append(
            "  {0}: {1} state={2} last_result={3} last_run={4} next_run={5}".format(
                str(t.get("task_name") or "-"),
                str(t.get("status") or "-"),
                str(t.get("state") or "-"),
                str(t.get("last_task_result") if t.get("last_task_result") is not None else "-"),
                str(t.get("last_run_time") or "-"),
                str(t.get("next_run_time") or "-"),
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
        lines.append(
            "  {0}: {1} age={2} max={3}h ({4})".format(
                str(a.get("path") or "-"),
                str(a.get("status") or "-"),
                age_txt,
                str(a.get("max_age_hours") if a.get("max_age_hours") is not None else "-"),
                req_txt,
            )
        )
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description="Report automation health (observe-only).")
    p.add_argument("--task", action="append", default=[], help="Repeatable scheduled task name to check")
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

    task_names = [str(x).strip() for x in (args.task or []) if str(x).strip()]
    if not task_names:
        task_names = list(DEFAULT_TASKS)
    artifact_specs = [str(x).strip() for x in (args.artifact or []) if str(x).strip()]
    if not artifact_specs:
        artifact_specs = list(DEFAULT_ARTIFACT_SPECS)
    parsed_specs = _parse_artifact_specs(artifact_specs)

    task_rows = _run_task_query(task_names)
    for r in task_rows:
        r["status"] = _task_status(r)

    art_rows = _artifact_rows(parsed_specs)
    _apply_soft_fail_overrides(task_rows, art_rows)
    _apply_supervisor_overrides(task_rows)

    reasons: List[str] = []
    bad_task = [r for r in task_rows if str(r.get("status")) in {"MISSING", "ERROR", "LAST_RUN_FAILED", "DISABLED"}]
    stale_art = [r for r in art_rows if str(r.get("status")) in {"MISSING", "STALE"}]
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
