#!/usr/bin/env python3
"""
No-Longshot daily runner daemon (observe-only orchestration).

Runs `scripts/run_no_longshot_daily_report.ps1` once per local day at HH:MM.
Intended to be supervised by `scripts/bot_supervisor.py`.
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
from typing import Optional, Tuple


DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCRIPT_PATH = str(Path("scripts") / "run_no_longshot_daily_report.ps1")
DEFAULT_LOG_FILE = str(DEFAULT_REPO_ROOT / "logs" / "no_longshot_daily_daemon.log")
DEFAULT_STATE_FILE = str(DEFAULT_REPO_ROOT / "logs" / "no_longshot_daily_daemon_state.json")
DEFAULT_LOCK_FILE = str(DEFAULT_REPO_ROOT / "logs" / "no_longshot_daily_daemon.lock")
DEFAULT_REALIZED_TOOL_PATH = str(Path("scripts") / "record_no_longshot_realized_daily.py")
DEFAULT_REALIZED_SCREEN_CSV = str(Path("logs") / "no_longshot_fast_screen_lowyes_latest.csv")
DEFAULT_REALIZED_POSITIONS_JSON = str(Path("logs") / "no_longshot_forward_positions.json")
DEFAULT_REALIZED_DAILY_JSONL = str(Path("logs") / "no_longshot_realized_daily.jsonl")
DEFAULT_REALIZED_LATEST_JSON = str(Path("logs") / "no_longshot_realized_latest.json")
DEFAULT_REALIZED_MONTHLY_TXT = str(Path("logs") / "no_longshot_monthly_return_latest.txt")


def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_ts() -> float:
    return time.time()


def today_key_local() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def parse_hhmm(value: str) -> Tuple[int, int]:
    s = str(value or "").strip()
    if ":" not in s:
        raise ValueError("run-at-hhmm must be HH:MM")
    hh_s, mm_s = s.split(":", 1)
    hh = int(hh_s)
    mm = int(mm_s)
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise ValueError("run-at-hhmm out of range")
    return hh, mm


def due_today(run_at_hhmm: str) -> bool:
    hh, mm = parse_hhmm(run_at_hhmm)
    now = datetime.now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return now >= target


def resolve_repo_path(repo_root: Path, value: str) -> Path:
    p = Path(str(value or "").strip())
    if p.is_absolute():
        return p.resolve()
    return (repo_root / p).resolve()


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
        pid = int(raw)
        if pid > 0:
            return pid
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
    except OSError:
        return False
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


def _acquire_lock(path: Path, logger: "Logger") -> bool:
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
            owner_pid = _parse_lock_pid(path)
            if owner_pid is not None and owner_pid != os.getpid() and _pid_alive(owner_pid):
                logger.info(
                    f"[{iso_now()}] lock busy: {path} owner_pid={owner_pid}; "
                    "another daemon instance is active"
                )
                return False
            try:
                path.unlink(missing_ok=True)
            except Exception:
                logger.info(f"[{iso_now()}] lock stale but cannot remove: {path}")
                return False
        except Exception as e:
            logger.info(f"[{iso_now()}] lock acquire failed: {path} ({type(e).__name__}: {e})")
            return False
    logger.info(f"[{iso_now()}] lock acquire failed after retry: {path}")
    return False


class Logger:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, msg: str) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(msg + "\n")

    def info(self, msg: str) -> None:
        print(msg)
        self._append(msg)


@dataclass
class RuntimeState:
    last_success_date: str = ""
    last_success_ts: float = 0.0
    last_attempt_ts: float = 0.0
    next_attempt_not_before_ts: float = 0.0
    last_exit_code: Optional[int] = None
    consecutive_failures: int = 0
    halted: bool = False
    halt_reason: str = ""
    last_realized_attempt_ts: float = 0.0
    last_realized_success_ts: float = 0.0
    next_realized_not_before_ts: float = 0.0
    last_realized_exit_code: Optional[int] = None
    realized_consecutive_failures: int = 0


def load_state(path: Path) -> RuntimeState:
    if not path.exists():
        return RuntimeState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return RuntimeState()
        return RuntimeState(
            last_success_date=str(raw.get("last_success_date") or ""),
            last_success_ts=float(raw.get("last_success_ts") or 0.0),
            last_attempt_ts=float(raw.get("last_attempt_ts") or 0.0),
            next_attempt_not_before_ts=float(raw.get("next_attempt_not_before_ts") or 0.0),
            last_exit_code=raw.get("last_exit_code"),
            consecutive_failures=int(raw.get("consecutive_failures") or 0),
            halted=bool(raw.get("halted") or False),
            halt_reason=str(raw.get("halt_reason") or ""),
            last_realized_attempt_ts=float(raw.get("last_realized_attempt_ts") or 0.0),
            last_realized_success_ts=float(raw.get("last_realized_success_ts") or 0.0),
            next_realized_not_before_ts=float(raw.get("next_realized_not_before_ts") or 0.0),
            last_realized_exit_code=raw.get("last_realized_exit_code"),
            realized_consecutive_failures=int(raw.get("realized_consecutive_failures") or 0),
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


def run_daily_once(args, logger: Logger) -> Tuple[int, str]:
    repo_root = Path(args.repo_root).resolve()
    script_path = Path(args.script_path)
    if not script_path.is_absolute():
        script_path = (repo_root / script_path).resolve()
    if not script_path.exists():
        return 2, f"runner script not found: {script_path}"

    cmd = [
        str(args.powershell_exe),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-NoBackground",
    ]
    if args.skip_refresh:
        cmd.append("-SkipRefresh")
    if args.discord:
        cmd.append("-Discord")

    logger.info(
        f"[{iso_now()}] no_longshot run start "
        f"(skip_refresh={args.skip_refresh} discord={args.discord} timeout={args.max_run_seconds}s)"
    )
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
        return 2, f"spawn failed: {type(e).__name__}: {e}"

    while True:
        rc = proc.poll()
        if rc is not None:
            dt = now_ts() - t0
            logger.info(f"[{iso_now()}] no_longshot run end rc={rc} elapsed={dt:.1f}s")
            return int(rc), ""

        if args.max_run_seconds > 0 and (now_ts() - t0) >= float(args.max_run_seconds):
            _kill_proc_tree(proc)
            dt = now_ts() - t0
            logger.info(f"[{iso_now()}] no_longshot run timeout elapsed={dt:.1f}s")
            return 124, "timeout"

        time.sleep(1.0)


def run_realized_refresh_once(args, logger: Logger) -> Tuple[int, str]:
    repo_root = Path(args.repo_root).resolve()
    tool_path = resolve_repo_path(repo_root, args.realized_tool_path)
    if not tool_path.exists():
        return 2, f"realized tool not found: {tool_path}"

    cmd = [
        str(args.python_exe),
        str(tool_path),
        "--screen-csv",
        str(resolve_repo_path(repo_root, args.realized_screen_csv)),
        "--positions-json",
        str(resolve_repo_path(repo_root, args.realized_positions_json)),
        "--out-daily-jsonl",
        str(resolve_repo_path(repo_root, args.realized_out_daily_jsonl)),
        "--out-latest-json",
        str(resolve_repo_path(repo_root, args.realized_out_latest_json)),
        "--out-monthly-txt",
        str(resolve_repo_path(repo_root, args.realized_out_monthly_txt)),
        "--entry-top-n",
        str(int(args.realized_entry_top_n)),
        "--per-trade-cost",
        str(float(args.realized_per_trade_cost)),
    ]
    if float(args.realized_api_timeout_sec) > 0:
        cmd.extend(["--api-timeout-sec", str(float(args.realized_api_timeout_sec))])

    logger.info(
        f"[{iso_now()}] no_longshot realized refresh start "
        f"(entry_top_n={int(args.realized_entry_top_n)} timeout={args.realized_timeout_sec}s)"
    )
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
        return 2, f"realized spawn failed: {type(e).__name__}: {e}"

    while True:
        rc = proc.poll()
        if rc is not None:
            dt = now_ts() - t0
            logger.info(f"[{iso_now()}] no_longshot realized refresh end rc={rc} elapsed={dt:.1f}s")
            return int(rc), ""

        if args.realized_timeout_sec > 0 and (now_ts() - t0) >= float(args.realized_timeout_sec):
            _kill_proc_tree(proc)
            dt = now_ts() - t0
            logger.info(f"[{iso_now()}] no_longshot realized refresh timeout elapsed={dt:.1f}s")
            return 124, "timeout"

        time.sleep(1.0)


def parse_args():
    p = argparse.ArgumentParser(description="No-Longshot daily runner daemon")
    p.add_argument("--repo-root", default=str(DEFAULT_REPO_ROOT), help="Repository root path")
    p.add_argument("--script-path", default=DEFAULT_SCRIPT_PATH, help="Path to run_no_longshot_daily_report.ps1")
    p.add_argument("--powershell-exe", default="powershell.exe", help="PowerShell executable")
    p.add_argument("--run-at-hhmm", default="00:05", help="Local daily run time (HH:MM)")
    p.add_argument("--poll-sec", type=float, default=15.0, help="Daemon loop interval")
    p.add_argument("--retry-delay-sec", type=float, default=900.0, help="Retry delay after failed attempt")
    p.add_argument("--max-run-seconds", type=int, default=1800, help="Per-run timeout seconds (0=disabled)")
    p.add_argument("--run-seconds", type=int, default=0, help="Auto-exit daemon after N seconds (0=forever)")
    p.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=6,
        help="Halt daemon after N consecutive failed attempts (0=disabled)",
    )
    p.add_argument(
        "--run-on-start",
        action="store_true",
        help="Run immediately on startup if today has not succeeded yet",
    )
    p.add_argument(
        "--skip-refresh",
        dest="skip_refresh",
        action="store_true",
        help="Pass -SkipRefresh to daily report script",
    )
    p.add_argument(
        "--no-skip-refresh",
        dest="skip_refresh",
        action="store_false",
        help="Do not pass -SkipRefresh to daily report script",
    )
    p.set_defaults(skip_refresh=True)
    p.add_argument("--discord", action="store_true", help="Pass -Discord to daily report script")
    p.add_argument("--python-exe", default="python", help="Python executable for realized-refresh runner")
    p.add_argument(
        "--realized-refresh-sec",
        type=float,
        default=0.0,
        help="Run realized tracker refresh every N seconds (0=disabled).",
    )
    p.add_argument(
        "--realized-timeout-sec",
        type=float,
        default=240.0,
        help="Per-refresh timeout seconds for realized tracker (0=disabled).",
    )
    p.add_argument(
        "--realized-tool-path",
        default=DEFAULT_REALIZED_TOOL_PATH,
        help="Path to record_no_longshot_realized_daily.py",
    )
    p.add_argument(
        "--realized-screen-csv",
        default=DEFAULT_REALIZED_SCREEN_CSV,
        help="Screen CSV path passed to realized tracker",
    )
    p.add_argument(
        "--realized-positions-json",
        default=DEFAULT_REALIZED_POSITIONS_JSON,
        help="Positions JSON path passed to realized tracker",
    )
    p.add_argument(
        "--realized-out-daily-jsonl",
        default=DEFAULT_REALIZED_DAILY_JSONL,
        help="Realized daily JSONL output path",
    )
    p.add_argument(
        "--realized-out-latest-json",
        default=DEFAULT_REALIZED_LATEST_JSON,
        help="Realized latest JSON output path",
    )
    p.add_argument(
        "--realized-out-monthly-txt",
        default=DEFAULT_REALIZED_MONTHLY_TXT,
        help="Realized rolling-30d monthly return txt output path",
    )
    p.add_argument(
        "--realized-entry-top-n",
        type=int,
        default=0,
        help="Entry ingest count for realized refresh calls (default 0 = resolve-only).",
    )
    p.add_argument(
        "--realized-per-trade-cost",
        type=float,
        default=0.002,
        help="Per-trade cost passed to realized tracker.",
    )
    p.add_argument(
        "--realized-api-timeout-sec",
        type=float,
        default=20.0,
        help="Gamma API timeout seconds passed to realized tracker.",
    )
    p.add_argument(
        "--allow-realized-entry-ingest",
        action="store_true",
        help="Allow realized refresh to ingest new entries when realized-entry-top-n > 0.",
    )
    p.add_argument("--log-file", default=DEFAULT_LOG_FILE, help="Daemon log file path")
    p.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="Daemon state file path")
    p.add_argument("--lock-file", default=DEFAULT_LOCK_FILE, help="Single-instance lock file path")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logger = Logger(args.log_file)
    if (
        float(args.realized_refresh_sec) > 0.0
        and int(args.realized_entry_top_n) > 0
        and (not bool(args.allow_realized_entry_ingest))
    ):
        logger.info(
            f"[{iso_now()}] refused: realized-entry-top-n={int(args.realized_entry_top_n)} "
            "requires --allow-realized-entry-ingest"
        )
        return 2
    repo_root = Path(args.repo_root).resolve()
    lock_path = resolve_repo_path(repo_root, str(args.lock_file))
    if not _acquire_lock(lock_path, logger):
        return 3
    atexit.register(_release_lock, lock_path)

    state_path = Path(args.state_file)
    state = load_state(state_path)

    logger.info("No-Longshot Daily Daemon")
    logger.info("=" * 64)
    logger.info(
        f"[{iso_now()}] cfg run_at={args.run_at_hhmm} poll={args.poll_sec:.1f}s "
        f"retry_delay={args.retry_delay_sec:.1f}s max_run={args.max_run_seconds}s "
        f"skip_refresh={args.skip_refresh} run_on_start={args.run_on_start} "
        f"realized_refresh_sec={args.realized_refresh_sec:.1f} realized_entry_top_n={int(args.realized_entry_top_n)}"
    )
    logger.info(f"[{iso_now()}] log={args.log_file}")
    logger.info(f"[{iso_now()}] state={args.state_file}")
    logger.info(f"[{iso_now()}] lock={lock_path}")
    if state.halted:
        logger.info(f"[{iso_now()}] resume state: HALTED ({state.halt_reason})")

    start_ts = now_ts()
    startup_checked = False

    while True:
        now = now_ts()
        if args.run_seconds > 0 and (now - start_ts) >= float(args.run_seconds):
            logger.info(f"[{iso_now()}] run-seconds reached ({args.run_seconds})")
            save_state(state_path, state)
            return 0

        if state.halted:
            logger.info(f"[{iso_now()}] HALTED: {state.halt_reason}")
            save_state(state_path, state)
            return 2

        today = today_key_local()
        should_attempt = False
        why = ""

        if state.last_success_date != today and now >= float(state.next_attempt_not_before_ts):
            if args.run_on_start and not startup_checked:
                should_attempt = True
                why = "startup"
            elif due_today(args.run_at_hhmm):
                should_attempt = True
                why = "schedule"

        if should_attempt:
            state.last_attempt_ts = now
            state.next_attempt_not_before_ts = now + max(0.0, float(args.retry_delay_sec))
            save_state(state_path, state)
            logger.info(f"[{iso_now()}] attempt trigger={why} today={today}")

            rc, reason = run_daily_once(args, logger)
            state.last_exit_code = int(rc)
            if rc == 0:
                state.last_success_date = today
                state.last_success_ts = now_ts()
                state.consecutive_failures = 0
                state.next_attempt_not_before_ts = 0.0
                logger.info(f"[{iso_now()}] daily success date={today}")
            else:
                state.consecutive_failures += 1
                logger.info(
                    f"[{iso_now()}] daily failed rc={rc} reason={reason or '-'} "
                    f"consecutive_failures={state.consecutive_failures}"
                )
                if (
                    args.max_consecutive_failures > 0
                    and state.consecutive_failures >= int(args.max_consecutive_failures)
                ):
                    state.halted = True
                    state.halt_reason = (
                        f"consecutive failure cap reached "
                        f"({state.consecutive_failures}/{args.max_consecutive_failures})"
                    )
                    logger.info(f"[{iso_now()}] HALT: {state.halt_reason}")

            save_state(state_path, state)

        if float(args.realized_refresh_sec) > 0.0 and now >= float(state.next_realized_not_before_ts):
            state.last_realized_attempt_ts = now
            state.next_realized_not_before_ts = now + max(1.0, float(args.realized_refresh_sec))
            save_state(state_path, state)

            rc, reason = run_realized_refresh_once(args, logger)
            state.last_realized_exit_code = int(rc)
            if rc == 0:
                state.last_realized_success_ts = now_ts()
                state.realized_consecutive_failures = 0
            else:
                state.realized_consecutive_failures += 1
                logger.info(
                    f"[{iso_now()}] realized refresh failed rc={rc} reason={reason or '-'} "
                    f"consecutive_failures={state.realized_consecutive_failures}"
                )
                retry_after = max(5.0, min(float(args.retry_delay_sec), float(args.realized_refresh_sec)))
                state.next_realized_not_before_ts = now_ts() + retry_after

            save_state(state_path, state)

        startup_checked = True
        time.sleep(max(0.2, float(args.poll_sec)))


if __name__ == "__main__":
    raise SystemExit(main())
