#!/usr/bin/env python3
"""
Weather daily runner daemon (observe-only orchestration).

Runs these PowerShell runners once per local day at configured HH:MM:
- scripts/run_weather_mimic_pipeline_daily.ps1
- scripts/run_weather_top30_readiness_daily.ps1

Intended to be supervised by scripts/bot_supervisor.py.
"""

from __future__ import annotations

import atexit
import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_FILE = str(DEFAULT_REPO_ROOT / "logs" / "weather_daily_daemon.log")
DEFAULT_STATE_FILE = str(DEFAULT_REPO_ROOT / "logs" / "weather_daily_daemon_state.json")
DEFAULT_LOCK_FILE = str(DEFAULT_REPO_ROOT / "logs" / "weather_daily_daemon.lock")
DEFAULT_TOP30_SCRIPT = str(Path("scripts") / "run_weather_top30_readiness_daily.ps1")
DEFAULT_MIMIC_SCRIPT = str(Path("scripts") / "run_weather_mimic_pipeline_daily.ps1")


def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_ts() -> float:
    return time.time()


def today_key_local() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def parse_hhmm(value: str) -> Tuple[int, int]:
    s = str(value or "").strip()
    if ":" not in s:
        raise ValueError("HH:MM expected")
    hh_s, mm_s = s.split(":", 1)
    hh = int(hh_s)
    mm = int(mm_s)
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise ValueError("HH:MM out of range")
    return hh, mm


def due_today(run_at_hhmm: str) -> bool:
    hh, mm = parse_hhmm(run_at_hhmm)
    now = datetime.now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return now >= target


def parse_bool(value: str) -> bool:
    s = str(value or "").strip().lower()
    return s in {"1", "true", "yes", "y", "on"}


def resolve_repo_path(repo_root: Path, value: str) -> Path:
    p = Path(str(value or "").strip())
    if p.is_absolute():
        return p.resolve()
    return (repo_root / p).resolve()


def parse_profiles(raw: str) -> str:
    vals: List[str] = []
    for x in str(raw or "").replace("\r", "\n").split("\n"):
        for part in x.split(","):
            v = part.strip()
            if not v:
                continue
            if v not in vals:
                vals.append(v)
    return ",".join(vals)


class Logger:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def info(self, msg: str) -> None:
        print(msg)
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(msg + "\n")


def _parse_lock_pid(path: Path) -> Optional[int]:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            pid = obj.get("pid")
            if isinstance(pid, int) and pid > 0:
                return pid
    except Exception:
        pass
    try:
        pid2 = int(raw)
        if pid2 > 0:
            return pid2
    except Exception:
        return None
    return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if h:
                k32.CloseHandle(h)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _release_lock(path: Path) -> None:
    try:
        owner_pid = _parse_lock_pid(path)
        if owner_pid is not None and owner_pid != os.getpid():
            return
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _acquire_lock(path: Path, logger: Logger) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": int(os.getpid()),
        "acquired_at": iso_now(),
        "argv": list(sys.argv),
    }
    body = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, body.encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            owner = _parse_lock_pid(path)
            if owner is not None and owner != os.getpid() and _pid_alive(owner):
                logger.info(f"[{iso_now()}] lock busy: owner_pid={owner} path={path}")
                return False
            try:
                path.unlink(missing_ok=True)
            except Exception:
                logger.info(f"[{iso_now()}] lock stale but cannot remove: {path}")
                return False
        except Exception as e:
            logger.info(f"[{iso_now()}] lock acquire failed: {type(e).__name__}: {e}")
            return False
    return False


@dataclass
class RuntimeState:
    last_success_date_top30: str = ""
    last_success_date_mimic: str = ""
    last_attempt_ts_top30: float = 0.0
    last_attempt_ts_mimic: float = 0.0
    next_attempt_not_before_ts_top30: float = 0.0
    next_attempt_not_before_ts_mimic: float = 0.0
    consecutive_failures_top30: int = 0
    consecutive_failures_mimic: int = 0
    last_exit_code_top30: Optional[int] = None
    last_exit_code_mimic: Optional[int] = None
    halted: bool = False
    halt_reason: str = ""


def load_state(path: Path) -> RuntimeState:
    if not path.exists():
        return RuntimeState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return RuntimeState()
        return RuntimeState(
            last_success_date_top30=str(raw.get("last_success_date_top30") or ""),
            last_success_date_mimic=str(raw.get("last_success_date_mimic") or ""),
            last_attempt_ts_top30=float(raw.get("last_attempt_ts_top30") or 0.0),
            last_attempt_ts_mimic=float(raw.get("last_attempt_ts_mimic") or 0.0),
            next_attempt_not_before_ts_top30=float(raw.get("next_attempt_not_before_ts_top30") or 0.0),
            next_attempt_not_before_ts_mimic=float(raw.get("next_attempt_not_before_ts_mimic") or 0.0),
            consecutive_failures_top30=int(raw.get("consecutive_failures_top30") or 0),
            consecutive_failures_mimic=int(raw.get("consecutive_failures_mimic") or 0),
            last_exit_code_top30=raw.get("last_exit_code_top30"),
            last_exit_code_mimic=raw.get("last_exit_code_mimic"),
            halted=bool(raw.get("halted") or False),
            halt_reason=str(raw.get("halt_reason") or ""),
        )
    except Exception:
        return RuntimeState()


def save_state(path: Path, state: RuntimeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), ensure_ascii=True, indent=2), encoding="utf-8")
    tmp.replace(path)


def _kill_proc_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    pid = int(proc.pid)
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    time.sleep(0.5)
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


def run_ps_once(
    repo_root: Path,
    powershell_exe: str,
    script_path: Path,
    script_args: List[str],
    max_run_seconds: float,
    logger: Logger,
    label: str,
) -> Tuple[int, str]:
    if not script_path.exists():
        return 2, f"{label}: script not found: {script_path}"

    cmd = [
        powershell_exe,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
    ]
    cmd.extend(script_args)

    logger.info(f"[{iso_now()}] {label} run start timeout={max_run_seconds:.0f}s")
    t0 = now_ts()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(repo_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except Exception as e:
        return 2, f"{label}: spawn failed: {type(e).__name__}: {e}"

    while True:
        rc = proc.poll()
        if rc is not None:
            dt = now_ts() - t0
            logger.info(f"[{iso_now()}] {label} run end rc={rc} elapsed={dt:.1f}s")
            return int(rc), ""
        if max_run_seconds > 0 and (now_ts() - t0) >= max_run_seconds:
            _kill_proc_tree(proc)
            dt = now_ts() - t0
            logger.info(f"[{iso_now()}] {label} run timeout elapsed={dt:.1f}s")
            return 124, "timeout"
        time.sleep(1.0)


def should_run_job(
    enabled: bool,
    run_at_hhmm: str,
    last_success_date: str,
    next_not_before_ts: float,
    force_first_run: bool,
) -> bool:
    if not enabled:
        return False
    if now_ts() < float(next_not_before_ts):
        return False
    today = today_key_local()
    if force_first_run and not str(last_success_date or "").strip():
        return True
    if last_success_date == today:
        return False
    return due_today(run_at_hhmm)


def main() -> int:
    p = argparse.ArgumentParser(description="Weather daily daemon (observe-only).")
    p.add_argument("--repo-root", default=str(DEFAULT_REPO_ROOT), help="Repository root")
    p.add_argument("--powershell-exe", default="powershell.exe", help="PowerShell executable")
    p.add_argument("--top30-script-path", default=DEFAULT_TOP30_SCRIPT, help="Top30 runner script path")
    p.add_argument("--mimic-script-path", default=DEFAULT_MIMIC_SCRIPT, help="Mimic runner script path")
    p.add_argument("--top30-run-at-hhmm", default="00:40", help="Top30 run local time HH:MM")
    p.add_argument("--mimic-run-at-hhmm", default="00:20", help="Mimic run local time HH:MM")
    p.add_argument("--top30-profiles", default="weather_7acct_auto,weather_visual_test", help="Top30 profiles csv")
    p.add_argument("--mimic-profile-name", default="weather_7acct_auto", help="Mimic profile name")
    p.add_argument("--mimic-user-file", default=str(Path("configs") / "weather_mimic_target_users.txt"), help="Mimic user file")
    p.add_argument("--mimic-no-run-scans", action="store_true", help="Pass -NoRunScans to mimic runner")
    p.add_argument("--top30-fail-on-no-go", action="store_true", help="Pass -FailOnNoGo to top30 runner")
    p.add_argument("--mimic-fail-on-readiness-no-go", action="store_true", help="Pass -FailOnReadinessNoGo to mimic runner")
    p.add_argument("--discord", action="store_true", help="Pass -Discord to both runners")
    p.add_argument("--disable-top30", action="store_true", help="Disable top30 runner")
    p.add_argument("--disable-mimic", action="store_true", help="Disable mimic runner")
    p.add_argument("--run-on-start", action="store_true", help="Run jobs once immediately when daemon starts")
    p.add_argument("--poll-sec", type=float, default=15.0, help="Polling interval seconds")
    p.add_argument("--retry-delay-sec", type=float, default=900.0, help="Retry delay after failure")
    p.add_argument("--max-run-seconds", type=float, default=7200.0, help="Per-run timeout seconds")
    p.add_argument("--max-consecutive-failures", type=int, default=5, help="Halt after N consecutive failures per job")
    p.add_argument("--log-file", default=DEFAULT_LOG_FILE, help="Daemon log file")
    p.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="Daemon state file")
    p.add_argument("--lock-file", default=DEFAULT_LOCK_FILE, help="Single-instance lock file")
    p.add_argument("--run-seconds", type=float, default=0.0, help="Optional daemon lifetime for testing")
    args = p.parse_args()

    repo_root = Path(args.repo_root).resolve()
    logger = Logger(args.log_file)
    state_path = Path(args.state_file).resolve()
    lock_path = Path(args.lock_file).resolve()
    top30_script = resolve_repo_path(repo_root, args.top30_script_path)
    mimic_script = resolve_repo_path(repo_root, args.mimic_script_path)
    mimic_user_file = resolve_repo_path(repo_root, args.mimic_user_file)
    top30_profiles = parse_profiles(args.top30_profiles)

    for hhmm in (args.top30_run_at_hhmm, args.mimic_run_at_hhmm):
        parse_hhmm(hhmm)
    if args.poll_sec <= 0:
        raise SystemExit("--poll-sec must be > 0")
    if args.retry_delay_sec < 0:
        raise SystemExit("--retry-delay-sec must be >= 0")
    if args.max_run_seconds <= 0:
        raise SystemExit("--max-run-seconds must be > 0")
    if args.max_consecutive_failures <= 0:
        raise SystemExit("--max-consecutive-failures must be > 0")
    if not args.disable_mimic and not mimic_user_file.exists():
        raise SystemExit(f"mimic user file not found: {mimic_user_file}")
    if not args.disable_top30 and not top30_profiles:
        raise SystemExit("top30 profiles are empty")

    if not _acquire_lock(lock_path, logger):
        return 3
    atexit.register(_release_lock, lock_path)

    state = load_state(state_path)
    if state.halted:
        logger.info(f"[{iso_now()}] daemon halted in state-file: reason={state.halt_reason}")
        return 4

    logger.info(
        f"[{iso_now()}] weather_daily_daemon start "
        f"(top30={not args.disable_top30}@{args.top30_run_at_hhmm} "
        f"mimic={not args.disable_mimic}@{args.mimic_run_at_hhmm} "
        f"run_on_start={args.run_on_start} poll={args.poll_sec}s)"
    )

    start_ts = now_ts()
    run_on_start_once = bool(args.run_on_start)
    while True:
        if args.run_seconds > 0 and (now_ts() - start_ts) >= float(args.run_seconds):
            logger.info(f"[{iso_now()}] weather_daily_daemon stop: run-seconds reached")
            save_state(state_path, state)
            return 0

        top30_due = should_run_job(
            enabled=not args.disable_top30,
            run_at_hhmm=args.top30_run_at_hhmm,
            last_success_date=state.last_success_date_top30,
            next_not_before_ts=state.next_attempt_not_before_ts_top30,
            force_first_run=run_on_start_once,
        )
        mimic_due = should_run_job(
            enabled=not args.disable_mimic,
            run_at_hhmm=args.mimic_run_at_hhmm,
            last_success_date=state.last_success_date_mimic,
            next_not_before_ts=state.next_attempt_not_before_ts_mimic,
            force_first_run=run_on_start_once,
        )

        if top30_due:
            state.last_attempt_ts_top30 = now_ts()
            save_state(state_path, state)
            rc, err = run_ps_once(
                repo_root=repo_root,
                powershell_exe=str(args.powershell_exe),
                script_path=top30_script,
                script_args=[
                    "-NoBackground",
                    "-Profiles",
                    top30_profiles,
                ]
                + (["-FailOnNoGo"] if args.top30_fail_on_no_go else [])
                + (["-Discord"] if args.discord else []),
                max_run_seconds=float(args.max_run_seconds),
                logger=logger,
                label="top30",
            )
            state.last_exit_code_top30 = int(rc)
            if rc == 0:
                state.last_success_date_top30 = today_key_local()
                state.next_attempt_not_before_ts_top30 = 0.0
                state.consecutive_failures_top30 = 0
            else:
                state.next_attempt_not_before_ts_top30 = now_ts() + float(args.retry_delay_sec)
                state.consecutive_failures_top30 += 1
                logger.info(f"[{iso_now()}] top30 failed rc={rc} err={err}")
                if state.consecutive_failures_top30 >= int(args.max_consecutive_failures):
                    state.halted = True
                    state.halt_reason = f"top30 failures >= {int(args.max_consecutive_failures)}"
            save_state(state_path, state)

        if mimic_due and not state.halted:
            state.last_attempt_ts_mimic = now_ts()
            save_state(state_path, state)
            rc, err = run_ps_once(
                repo_root=repo_root,
                powershell_exe=str(args.powershell_exe),
                script_path=mimic_script,
                script_args=[
                    "-NoBackground",
                    "-UserFile",
                    str(mimic_user_file),
                    "-ProfileName",
                    str(args.mimic_profile_name),
                ]
                + (["-NoRunScans"] if args.mimic_no_run_scans else [])
                + (["-FailOnReadinessNoGo"] if args.mimic_fail_on_readiness_no_go else [])
                + (["-Discord"] if args.discord else []),
                max_run_seconds=float(args.max_run_seconds),
                logger=logger,
                label="mimic",
            )
            state.last_exit_code_mimic = int(rc)
            if rc == 0:
                state.last_success_date_mimic = today_key_local()
                state.next_attempt_not_before_ts_mimic = 0.0
                state.consecutive_failures_mimic = 0
            else:
                state.next_attempt_not_before_ts_mimic = now_ts() + float(args.retry_delay_sec)
                state.consecutive_failures_mimic += 1
                logger.info(f"[{iso_now()}] mimic failed rc={rc} err={err}")
                if state.consecutive_failures_mimic >= int(args.max_consecutive_failures):
                    state.halted = True
                    state.halt_reason = f"mimic failures >= {int(args.max_consecutive_failures)}"
            save_state(state_path, state)

        run_on_start_once = False
        if state.halted:
            logger.info(f"[{iso_now()}] weather_daily_daemon halted: {state.halt_reason}")
            save_state(state_path, state)
            return 5

        time.sleep(float(args.poll_sec))


if __name__ == "__main__":
    raise SystemExit(main())
