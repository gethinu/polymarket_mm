#!/usr/bin/env python3
"""
Simple multi-bot supervisor for local observe/live scripts.

Default usage is intended for observe-only profiles.
The supervisor itself never adds --execute flags.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import signal
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional


DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_FILE = str(DEFAULT_REPO_ROOT / "configs" / "bot_supervisor.observe.json")
DEFAULT_LOG_FILE = str(DEFAULT_REPO_ROOT / "logs" / "bot-supervisor.log")
DEFAULT_STATE_FILE = str(DEFAULT_REPO_ROOT / "logs" / "bot_supervisor_state.json")


def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _env_str(name: str) -> str:
    return str(os.environ.get(name, "") or "").strip()


def _env_int(name: str) -> Optional[int]:
    v = _env_str(name)
    if not v:
        return None
    try:
        return int(float(v))
    except Exception:
        return None


def _env_float(name: str) -> Optional[float]:
    v = _env_str(name)
    if not v:
        return None
    try:
        return float(v)
    except Exception:
        return None


class Logger:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, msg: str) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(msg + "\n")

    def info(self, msg: str) -> None:
        line = msg
        print(line)
        self._append(line)


@dataclass
class JobSpec:
    name: str
    command: List[str]
    enabled: bool = True
    cwd: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    restart: str = "always"  # always | on-failure | never
    restart_delay_sec: float = 2.0
    max_restarts_per_hour: int = 60


@dataclass
class JobRuntime:
    spec: JobSpec
    proc: Optional[subprocess.Popen] = None
    restart_times: Deque[float] = field(default_factory=deque)
    restart_count: int = 0
    starts: int = 0
    last_start_ts: float = 0.0
    last_exit_ts: float = 0.0
    last_exit_code: Optional[int] = None
    last_error: str = ""
    next_restart_ts: float = 0.0
    disabled_reason: str = ""


def _safe_json_load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config root must be JSON object")
    return data


def _parse_job(raw: dict, index: int) -> JobSpec:
    if not isinstance(raw, dict):
        raise ValueError(f"jobs[{index}] must be object")

    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError(f"jobs[{index}].name is required")

    cmd_raw = raw.get("command")
    if not isinstance(cmd_raw, list) or not cmd_raw:
        raise ValueError(f"jobs[{index}].command must be non-empty list")
    cmd = [str(x) for x in cmd_raw if str(x).strip()]
    if not cmd:
        raise ValueError(f"jobs[{index}].command has no valid args")

    enabled = bool(raw.get("enabled", True))
    cwd = str(raw.get("cwd") or "").strip()

    env_raw = raw.get("env") or {}
    if not isinstance(env_raw, dict):
        raise ValueError(f"jobs[{index}].env must be object")
    env: Dict[str, str] = {}
    for k, v in env_raw.items():
        env[str(k)] = str(v)

    restart = str(raw.get("restart") or "always").strip().lower()
    if restart not in {"always", "on-failure", "never"}:
        raise ValueError(f"jobs[{index}].restart must be always|on-failure|never")

    restart_delay_sec = float(raw.get("restart_delay_sec", 2.0) or 0.0)
    max_restarts_per_hour = int(raw.get("max_restarts_per_hour", 60) or 0)

    return JobSpec(
        name=name,
        command=cmd,
        enabled=enabled,
        cwd=cwd,
        env=env,
        restart=restart,
        restart_delay_sec=max(0.0, restart_delay_sec),
        max_restarts_per_hour=max(0, max_restarts_per_hour),
    )


def load_config(path: str) -> tuple[dict, List[JobSpec]]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")

    root = _safe_json_load(p)
    jobs_raw = root.get("jobs")
    if not isinstance(jobs_raw, list) or not jobs_raw:
        raise ValueError("config.jobs must be a non-empty list")

    names = set()
    jobs: List[JobSpec] = []
    for i, jraw in enumerate(jobs_raw):
        spec = _parse_job(jraw, i)
        if spec.name in names:
            raise ValueError(f"duplicate job name: {spec.name}")
        names.add(spec.name)
        jobs.append(spec)

    return root, jobs


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            cp = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            out = (cp.stdout or "").strip()
            if (not out) or ("No tasks are running" in out):
                return False
            reader = csv.reader(io.StringIO(out))
            for row in reader:
                # tasklist /FO CSV: "Image Name","PID","Session Name","Session#","Mem Usage"
                if len(row) >= 2:
                    try:
                        if int(str(row[1]).strip()) == int(pid):
                            return True
                    except Exception:
                        continue
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


def _write_state_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    tmp.replace(path)


def _runtime_to_state(
    mode: str,
    config_path: str,
    started_ts: float,
    runtimes: Dict[str, JobRuntime],
    stopping: bool,
) -> dict:
    jobs = []
    for name in sorted(runtimes.keys()):
        rt = runtimes[name]
        pid = rt.proc.pid if rt.proc is not None else 0
        jobs.append(
            {
                "name": rt.spec.name,
                "enabled": rt.spec.enabled,
                "running": bool(rt.proc is not None and rt.proc.poll() is None),
                "pid": int(pid or 0),
                "starts": int(rt.starts),
                "restart_count": int(rt.restart_count),
                "last_start_ts": int(rt.last_start_ts * 1000) if rt.last_start_ts > 0 else 0,
                "last_exit_ts": int(rt.last_exit_ts * 1000) if rt.last_exit_ts > 0 else 0,
                "last_exit_code": rt.last_exit_code,
                "next_restart_ts": int(rt.next_restart_ts * 1000) if rt.next_restart_ts > 0 else 0,
                "disabled_reason": rt.disabled_reason,
                "last_error": rt.last_error,
                "command": rt.spec.command,
            }
        )

    return {
        "mode": mode,
        "ts": iso_now(),
        "ts_ms": int(time.time() * 1000),
        "supervisor_pid": os.getpid(),
        "supervisor_running": True,
        "stopping": bool(stopping),
        "started_at": datetime.fromtimestamp(started_ts).strftime("%Y-%m-%d %H:%M:%S"),
        "config_path": str(config_path),
        "jobs": jobs,
    }


def _merge_env(extra: Dict[str, str]) -> Dict[str, str]:
    env = dict(os.environ)
    for k, v in extra.items():
        env[str(k)] = str(v)
    return env


def _job_cwd(repo_root: Path, cwd_value: str) -> Optional[str]:
    c = (cwd_value or "").strip()
    if not c:
        return str(repo_root)
    p = Path(c)
    if not p.is_absolute():
        p = (repo_root / p).resolve()
    return str(p)


def start_job(rt: JobRuntime, repo_root: Path, logger: Logger) -> bool:
    try:
        cwd = _job_cwd(repo_root, rt.spec.cwd)
        env = _merge_env(rt.spec.env)
        rt.proc = subprocess.Popen(
            rt.spec.command,
            cwd=cwd,
            env=env,
            shell=False,
        )
        rt.starts += 1
        rt.last_start_ts = time.time()
        rt.last_error = ""
        logger.info(
            f"[{iso_now()}] job start name={rt.spec.name} pid={rt.proc.pid} cmd={' '.join(rt.spec.command)}"
        )
        return True
    except Exception as e:
        rt.proc = None
        rt.last_error = str(e)
        rt.last_exit_ts = time.time()
        rt.last_exit_code = None
        logger.info(f"[{iso_now()}] job start failed name={rt.spec.name} err={type(e).__name__}: {e}")
        return False


def _prune_restart_window(rt: JobRuntime, now: float) -> None:
    cutoff = now - 3600.0
    while rt.restart_times and rt.restart_times[0] < cutoff:
        rt.restart_times.popleft()


def _can_restart(rt: JobRuntime, exit_code: int, no_restart: bool) -> bool:
    if no_restart:
        return False

    if rt.spec.restart == "never":
        return False
    if rt.spec.restart == "on-failure" and exit_code == 0:
        return False

    now = time.time()
    _prune_restart_window(rt, now)
    if rt.spec.max_restarts_per_hour > 0 and len(rt.restart_times) >= rt.spec.max_restarts_per_hour:
        rt.disabled_reason = f"restart cap hit ({len(rt.restart_times)}/{rt.spec.max_restarts_per_hour} per hour)"
        return False
    return True


def _schedule_restart(rt: JobRuntime, logger: Logger) -> None:
    now = time.time()
    rt.restart_times.append(now)
    rt.restart_count += 1
    rt.next_restart_ts = now + max(0.0, float(rt.spec.restart_delay_sec))
    logger.info(
        f"[{iso_now()}] job restart scheduled name={rt.spec.name} at+{rt.spec.restart_delay_sec:.1f}s "
        f"count={rt.restart_count}"
    )


def _terminate_proc(proc: subprocess.Popen, timeout_sec: float) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:
        pass

    deadline = time.time() + max(0.1, timeout_sec)
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)

    try:
        proc.kill()
    except Exception:
        pass


def run_supervisor(args) -> int:
    repo_root = DEFAULT_REPO_ROOT
    config_raw, specs = load_config(args.config)

    log_file = args.log_file or str(config_raw.get("log_file") or DEFAULT_LOG_FILE)
    state_file = args.state_file or str(config_raw.get("state_file") or DEFAULT_STATE_FILE)

    logger = Logger(log_file)
    state_path = Path(state_file)

    runtimes: Dict[str, JobRuntime] = {}
    for s in specs:
        runtimes[s.name] = JobRuntime(spec=s)

    enabled = [rt for rt in runtimes.values() if rt.spec.enabled]
    if not enabled:
        logger.info(f"[{iso_now()}] no enabled jobs in config: {args.config}")
        return 2

    started_ts = time.time()
    logger.info("Bot Supervisor")
    logger.info("=" * 64)
    logger.info(f"[{iso_now()}] mode=run config={args.config}")
    logger.info(f"[{iso_now()}] log={log_file}")
    logger.info(f"[{iso_now()}] state={state_file}")
    logger.info(f"[{iso_now()}] jobs_enabled={len(enabled)}/{len(specs)}")

    stopping = False
    stop_reason = ""
    last_state_write_ts = 0.0

    def write_state() -> None:
        payload = _runtime_to_state(
            mode="run",
            config_path=args.config,
            started_ts=started_ts,
            runtimes=runtimes,
            stopping=stopping,
        )
        _write_state_atomic(state_path, payload)

    for rt in enabled:
        start_job(rt, repo_root=repo_root, logger=logger)

    try:
        while True:
            now = time.time()

            if args.run_seconds > 0 and (now - started_ts) >= float(args.run_seconds):
                stopping = True
                stop_reason = f"run-seconds reached ({args.run_seconds})"

            for rt in enabled:
                p = rt.proc

                if p is None:
                    if rt.next_restart_ts > 0 and now >= rt.next_restart_ts and not rt.disabled_reason and not stopping:
                        start_job(rt, repo_root=repo_root, logger=logger)
                        if rt.proc is not None:
                            rt.next_restart_ts = 0.0
                    continue

                rc = p.poll()
                if rc is None:
                    continue

                rt.last_exit_ts = now
                rt.last_exit_code = int(rc)
                rt.proc = None
                logger.info(f"[{iso_now()}] job exit name={rt.spec.name} code={rc}")

                if stopping:
                    continue

                if _can_restart(rt, exit_code=int(rc), no_restart=bool(args.no_restart)):
                    _schedule_restart(rt, logger=logger)
                else:
                    if not rt.disabled_reason:
                        rt.disabled_reason = f"restart policy blocked (mode={rt.spec.restart}, code={rc})"
                    logger.info(f"[{iso_now()}] job disabled name={rt.spec.name} reason={rt.disabled_reason}")

                    if args.halt_on_job_failure and rc != 0:
                        stopping = True
                        stop_reason = f"halt-on-job-failure ({rt.spec.name} code={rc})"

            if args.halt_when_all_stopped:
                any_running = any((rt.proc is not None and rt.proc.poll() is None) for rt in enabled)
                any_pending_restart = any((rt.proc is None and rt.next_restart_ts > 0 and not rt.disabled_reason) for rt in enabled)
                if (not any_running) and (not any_pending_restart):
                    stopping = True
                    if not stop_reason:
                        stop_reason = "all jobs stopped"

            if (now - last_state_write_ts) >= float(args.write_state_sec):
                write_state()
                last_state_write_ts = now

            if stopping:
                break

            time.sleep(max(0.1, float(args.poll_sec)))

    except KeyboardInterrupt:
        stopping = True
        stop_reason = "keyboard interrupt"
        logger.info(f"[{iso_now()}] stopping: {stop_reason}")
    finally:
        for rt in enabled:
            if rt.proc is not None and rt.proc.poll() is None:
                logger.info(f"[{iso_now()}] job terminate name={rt.spec.name} pid={rt.proc.pid}")
                _terminate_proc(rt.proc, timeout_sec=float(args.graceful_timeout_sec))

        # final state marks supervisor as no longer running
        payload = _runtime_to_state(
            mode="stopped",
            config_path=args.config,
            started_ts=started_ts,
            runtimes=runtimes,
            stopping=True,
        )
        payload["supervisor_running"] = False
        payload["stop_reason"] = stop_reason or "normal"
        payload["stopped_at"] = iso_now()
        _write_state_atomic(state_path, payload)

    logger.info(f"[{iso_now()}] supervisor stopped reason={stop_reason or 'normal'}")
    return 0


def _read_state(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def status_supervisor(args) -> int:
    state_path = Path(args.state_file or DEFAULT_STATE_FILE)
    st = _read_state(state_path)
    if not st:
        print(f"State file not found or invalid: {state_path}")
        return 2

    sup_pid = int(st.get("supervisor_pid") or 0)
    sup_alive = _pid_running(sup_pid)
    mode = str(st.get("mode") or "unknown")
    ts = str(st.get("ts") or "")
    cfg = str(st.get("config_path") or "")

    print(f"Supervisor status: mode={mode} alive={sup_alive} pid={sup_pid}")
    print(f"Updated: {ts}")
    print(f"Config: {cfg}")

    jobs = st.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        print("Jobs: none")
        return 1

    running_count = 0
    for j in jobs:
        if not isinstance(j, dict):
            continue
        name = str(j.get("name") or "")
        pid = int(j.get("pid") or 0)
        running = bool(j.get("running")) and _pid_running(pid)
        starts = int(j.get("starts") or 0)
        restarts = int(j.get("restart_count") or 0)
        last_exit = j.get("last_exit_code")
        reason = str(j.get("disabled_reason") or "")
        if running:
            running_count += 1
        tail = ""
        if reason:
            tail = f" disabled_reason={reason}"
        print(
            f"- {name}: running={running} pid={pid} starts={starts} restarts={restarts} "
            f"last_exit={last_exit}{tail}"
        )

    return 0 if sup_alive or running_count > 0 else 1


def _kill_pid_tree(pid: int) -> bool:
    if pid <= 0:
        return False
    if not _pid_running(pid):
        return False

    try:
        if os.name == "nt":
            # /T kills child process tree.
            cp = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return cp.returncode == 0

        os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        return False


def stop_supervisor(args) -> int:
    state_path = Path(args.state_file or DEFAULT_STATE_FILE)
    st = _read_state(state_path)
    if not st:
        print(f"State file not found or invalid: {state_path}")
        return 2

    killed = 0

    sup_pid = int(st.get("supervisor_pid") or 0)
    if sup_pid > 0 and sup_pid != os.getpid() and _kill_pid_tree(sup_pid):
        killed += 1

    jobs = st.get("jobs")
    if isinstance(jobs, list):
        for j in jobs:
            if not isinstance(j, dict):
                continue
            pid = int(j.get("pid") or 0)
            if pid > 0 and pid != os.getpid() and _kill_pid_tree(pid):
                killed += 1

    print(f"Stop requested. processes_killed={killed}")
    return 0


def _apply_env_overrides(args):
    # simple global overrides
    p = _env_str("BOTSUP_CONFIG")
    if p and getattr(args, "config", "") == DEFAULT_CONFIG_FILE:
        args.config = p

    lf = _env_str("BOTSUP_LOG_FILE")
    if lf and not getattr(args, "log_file", ""):
        args.log_file = lf

    sf = _env_str("BOTSUP_STATE_FILE")
    if sf and not getattr(args, "state_file", ""):
        args.state_file = sf

    for attr, env_name in (
        ("poll_sec", "BOTSUP_POLL_SEC"),
        ("write_state_sec", "BOTSUP_WRITE_STATE_SEC"),
        ("run_seconds", "BOTSUP_RUN_SECONDS"),
        ("graceful_timeout_sec", "BOTSUP_GRACEFUL_TIMEOUT_SEC"),
    ):
        if hasattr(args, attr):
            cur = getattr(args, attr)
            if isinstance(cur, int):
                v = _env_int(env_name)
            else:
                v = _env_float(env_name)
            if v is not None:
                setattr(args, attr, v)

    return args


def parse_args():
    p = argparse.ArgumentParser(description="Simple multi-bot supervisor")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run supervisor loop")
    run.add_argument("--config", default=DEFAULT_CONFIG_FILE, help="Supervisor config JSON path")
    run.add_argument("--log-file", default="", help="Supervisor log path")
    run.add_argument("--state-file", default="", help="Supervisor state path")
    run.add_argument("--poll-sec", type=float, default=1.0, help="Supervisor poll interval")
    run.add_argument("--write-state-sec", type=float, default=2.0, help="State write cadence")
    run.add_argument("--run-seconds", type=int, default=0, help="Auto-stop after N sec (0=forever)")
    run.add_argument("--graceful-timeout-sec", type=float, default=8.0, help="Terminate grace timeout per job")
    run.add_argument("--no-restart", action="store_true", help="Disable all job restarts")
    run.add_argument(
        "--halt-on-job-failure",
        action="store_true",
        help="Stop supervisor when any job exits non-zero and cannot be restarted",
    )
    run.add_argument(
        "--halt-when-all-stopped",
        action="store_true",
        help="Stop supervisor when no jobs are running and no pending restarts exist",
    )

    status = sub.add_parser("status", help="Show supervisor/job status from state file")
    status.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="Supervisor state path")

    stop = sub.add_parser("stop", help="Stop supervisor and known child jobs")
    stop.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="Supervisor state path")

    args = p.parse_args()
    return _apply_env_overrides(args)


def main() -> int:
    args = parse_args()

    if args.cmd == "run":
        return run_supervisor(args)
    if args.cmd == "status":
        return status_supervisor(args)
    if args.cmd == "stop":
        return stop_supervisor(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
