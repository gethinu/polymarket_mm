#!/usr/bin/env python3
"""
Observe-only transition alarm wrapper for check_pending_release.py.

This script keeps a last-seen state and emits an alarm event only when
release decision state changes (or optionally when reason codes change).
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import List, Optional
from urllib import error, request

import check_pending_release as checker


DEFAULT_PENDING_RELEASE_DISCORD_WEBHOOK_ENV = "POLYMARKET_PENDING_RELEASE_DISCORD_WEBHOOK"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


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


def read_json(path: Path) -> Optional[dict]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def write_json(path: Path, payload: dict, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_raw = tempfile.mkstemp(
        prefix=f"{path.name}.tmp.",
        suffix=".json",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_raw)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            if pretty:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            else:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(path))
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        f.write("\n")


def _parse_lock_pid(path: Path) -> Optional[int]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return int(raw.get("pid")) if raw.get("pid") is not None else None
    except Exception:
        pass
    return None


def _process_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError as exc:
        # EPERM can mean process exists but we do not have permission.
        return exc.errno == errno.EPERM
    except Exception:
        return False


def _acquire_lock(path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "acquired_at": now_utc().isoformat(),
    }
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except FileExistsError:
            owner_pid = _parse_lock_pid(path)
            if _process_alive(owner_pid):
                return False
            try:
                path.unlink()
            except Exception:
                return False
        except Exception:
            return False
    return False


def _release_lock(path: Path) -> None:
    try:
        owner_pid = _parse_lock_pid(path)
        if owner_pid is not None and owner_pid != os.getpid():
            return
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _env_any(name: str) -> str:
    v = os.environ.get(name)
    if v and str(v).strip():
        return str(v).strip()
    if sys.platform.startswith("win"):
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
                rv, _t = winreg.QueryValueEx(k, name)
                if rv and str(rv).strip():
                    return str(rv).strip()
        except Exception:
            pass
    return ""


def discord_webhook_env_name(preferred_env: str = "") -> str:
    env_name = str(preferred_env or "").strip()
    if env_name:
        return env_name
    return DEFAULT_PENDING_RELEASE_DISCORD_WEBHOOK_ENV


def post_discord(content: str, *, webhook_url: str) -> tuple[bool, str]:
    url = str(webhook_url or "").strip()
    if not url:
        return False, "webhook_env_missing"
    payload = {"content": content}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url=url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "polymarket-mm/1.0",
        },
    )
    try:
        with request.urlopen(req, timeout=15) as resp:
            _ = resp.read()
        return True, "sent"
    except error.HTTPError as exc:
        return False, f"http_{exc.code}"
    except Exception as exc:
        return False, f"send_failed:{type(exc).__name__}"


def _normalize_reason_codes(payload: dict) -> List[str]:
    raw = payload.get("reason_codes")
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    seen = set()
    for row in raw:
        s = str(row or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    out.sort()
    return out


def _state_context_key(
    *,
    strategy: str,
    conservative_costs: bool,
    conservative_cost_cents: float,
    monthly_json: str,
    replay_json: str,
) -> str:
    # Keep key deterministic and explicit so mixed monitor profiles don't overwrite each other.
    strategy_key = str(strategy or "").strip().lower()
    monthly_key = str(monthly_json or "").strip()
    replay_key = str(replay_json or "").strip()
    return (
        f"strategy={strategy_key}|"
        f"conservative_costs={int(bool(conservative_costs))}|"
        f"conservative_cost_cents={float(conservative_cost_cents):.6f}|"
        f"monthly_json={monthly_key}|"
        f"replay_json={replay_key}"
    )


def _load_prev_state_for_context(state_doc: dict, context_key: str) -> dict:
    if not isinstance(state_doc, dict):
        return {}
    contexts = state_doc.get("contexts")
    if isinstance(contexts, dict):
        row = contexts.get(context_key)
        if isinstance(row, dict):
            return row
        return {}
    # Backward compatibility with legacy single-slot state payload.
    if str(state_doc.get("last_release_check") or "").strip():
        return state_doc
    return {}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Check pending-release transitions and emit alarm only when state changes (observe-only)."
    )
    p.add_argument("--strategy", default=checker.DEFAULT_STRATEGY_ID)
    p.add_argument("--snapshot-json", default="logs/strategy_register_latest.json")
    p.add_argument("--state-json", default="logs/pending_release_alarm_state.json")
    p.add_argument("--log-file", default="logs/pending_release_alarm.log")
    p.add_argument("--lock-file", default="logs/pending_release_alarm.lock")
    p.add_argument("--monthly-json", default="", help="Override monthly-estimate JSON path.")
    p.add_argument("--replay-json", default="", help="Override Kelly replay JSON path.")
    p.add_argument("--min-gap-ms-per-event", type=int, default=5000)
    p.add_argument("--max-worst-stale-sec", type=float, default=10.0)
    p.add_argument("--conservative-costs", action="store_true")
    p.add_argument("--conservative-cost-cents", type=float, default=2.0)
    p.add_argument(
        "--notify-on-reason-change",
        action="store_true",
        help="Trigger alarm when reason_codes changed even if release_check is unchanged.",
    )
    p.add_argument("--discord", action="store_true", help="Send Discord notification only when alarm triggers.")
    p.add_argument(
        "--discord-webhook-env",
        default="",
        help=(
            "Environment variable name for Discord webhook URL. "
            f"Default when omitted: {DEFAULT_PENDING_RELEASE_DISCORD_WEBHOOK_ENV}"
        ),
    )
    p.add_argument("--pretty", action="store_true")
    return p.parse_args()


def run_alarm(args: argparse.Namespace) -> tuple[int, dict]:
    strategy_id = str(args.strategy or "").strip() or checker.DEFAULT_STRATEGY_ID
    snapshot_path = resolve_path(str(args.snapshot_json), "strategy_register_latest.json")
    state_path = resolve_path(str(args.state_json), "pending_release_alarm_state.json")
    log_path = resolve_path(str(args.log_file), "pending_release_alarm.log")
    monthly_override = str(args.monthly_json or "").strip()
    replay_override = str(args.replay_json or "").strip()
    monthly_scope = str(resolve_path(monthly_override, "monthly.json")) if monthly_override else ""
    replay_scope = str(resolve_path(replay_override, "replay.json")) if replay_override else ""
    context_key = _state_context_key(
        strategy=strategy_id,
        conservative_costs=bool(args.conservative_costs),
        conservative_cost_cents=float(args.conservative_cost_cents or 0.0),
        monthly_json=monthly_scope,
        replay_json=replay_scope,
    )

    check_args = argparse.Namespace(
        strategy=strategy_id,
        snapshot_json=str(snapshot_path),
        strategy_md="docs/llm/STRATEGY.md",
        monthly_json=monthly_override,
        replay_json=replay_override,
        min_gap_ms_per_event=max(0, int(args.min_gap_ms_per_event or 0)),
        max_worst_stale_sec=float(args.max_worst_stale_sec or 0.0),
        conservative_costs=bool(args.conservative_costs),
        conservative_cost_cents=float(args.conservative_cost_cents or 0.0),
        apply=False,
        out_json="",
        pretty=False,
    )
    checker_exit_code, checker_payload = checker.run_check(check_args)

    if int(checker_exit_code) >= 20 or not bool(checker_payload.get("ok")):
        out = {
            "ok": False,
            "strategy": strategy_id,
            "error": str(checker_payload.get("error") or "checker_failed"),
            "checker_exit_code": int(checker_exit_code),
            "checker_payload": checker_payload,
        }
        return 2, out

    current_release_check = str(checker_payload.get("release_check") or "").strip() or "UNKNOWN"
    current_release_ready = bool(checker_payload.get("release_ready"))
    current_reason_codes = _normalize_reason_codes(checker_payload)

    state_doc = read_json(state_path) or {}
    prev = _load_prev_state_for_context(state_doc, context_key)
    prev_release_check = str(prev.get("last_release_check") or "").strip()
    prev_release_ready = bool(prev.get("last_release_ready"))
    prev_reason_codes = (
        [str(x or "").strip() for x in prev.get("last_reason_codes")]
        if isinstance(prev.get("last_reason_codes"), list)
        else []
    )
    prev_reason_codes = sorted([x for x in prev_reason_codes if x])
    has_prev = bool(prev_release_check)

    release_check_changed = has_prev and (prev_release_check != current_release_check)
    release_ready_changed = has_prev and (prev_release_ready != current_release_ready)
    reason_codes_changed = has_prev and (prev_reason_codes != current_reason_codes)

    alarm_reason_codes: List[str] = []
    if release_check_changed:
        alarm_reason_codes.append("release_check_changed")
    if release_ready_changed:
        alarm_reason_codes.append("release_ready_changed")
    if bool(args.notify_on_reason_change) and reason_codes_changed:
        alarm_reason_codes.append("reason_codes_changed")

    should_alarm = bool(alarm_reason_codes)
    now = now_utc().isoformat()

    discord_enabled = bool(args.discord)
    webhook_env_name = discord_webhook_env_name(str(args.discord_webhook_env or ""))
    notification_attempted = False
    notification_status = "skipped"
    notification_reason = "no_state_change"
    notification_error_detail = ""
    if should_alarm:
        if not discord_enabled:
            notification_status = "skipped"
            notification_reason = "discord_disabled"
        else:
            webhook_url = _env_any(webhook_env_name)
            if not webhook_url:
                notification_status = "skipped"
                notification_reason = "webhook_env_missing"
            else:
                notification_attempted = True
                notification_status = "error"
                notification_reason = "request_failed"
                mention = _env_any("CLOBBOT_DISCORD_MENTION")
                body = (
                    "[Pending Release Alarm]\n"
                    f"strategy={strategy_id}\n"
                    f"release_check: {prev_release_check or '-'} -> {current_release_check}\n"
                    f"release_ready={current_release_ready}\n"
                    f"reason_codes={','.join(current_reason_codes) or '-'}"
                )
                if mention:
                    body = f"{mention} {body}"
                ok, status = post_discord(body, webhook_url=webhook_url)
                if ok:
                    notification_status = "sent"
                    notification_reason = "sent"
                else:
                    notification_status = "error"
                    notification_reason = "request_failed"
                    notification_error_detail = str(status or "")

    event = {
        "ts_utc": now,
        "event": "pending_release_transition",
        "strategy": strategy_id,
        "state_context_key": context_key,
        "snapshot_json": str(snapshot_path),
        "previous_release_check": prev_release_check or None,
        "current_release_check": current_release_check,
        "previous_release_ready": prev_release_ready if has_prev else None,
        "current_release_ready": current_release_ready,
        "previous_reason_codes": prev_reason_codes if has_prev else [],
        "current_reason_codes": current_reason_codes,
        "checker_exit_code": int(checker_exit_code),
        "checker_reason_codes": current_reason_codes,
        "alarm_reason_codes": alarm_reason_codes,
        "alarm_triggered": should_alarm,
        "discord_enabled": discord_enabled,
        "discord_webhook_env": webhook_env_name,
        "notification_attempted": notification_attempted,
        "notification_status": notification_status,
        "notification_reason": notification_reason,
        "notification_error_detail": notification_error_detail,
    }
    if should_alarm:
        append_jsonl(log_path, event)

    context_state_payload = {
        "updated_utc": now,
        "strategy": strategy_id,
        "state_context_key": context_key,
        "state_context": {
            "strategy": strategy_id,
            "conservative_costs": bool(args.conservative_costs),
            "conservative_cost_cents": float(args.conservative_cost_cents or 0.0),
            "monthly_json_override": monthly_scope,
            "replay_json_override": replay_scope,
        },
        "snapshot_json": str(snapshot_path),
        "last_release_check": current_release_check,
        "last_release_ready": current_release_ready,
        "last_reason_codes": current_reason_codes,
        "last_checker_exit_code": int(checker_exit_code),
        "last_current_status": str(checker_payload.get("current_status") or ""),
        "last_execution_edge": checker_payload.get("execution_edge"),
        "last_full_kelly": checker_payload.get("full_kelly"),
        "last_alarm_triggered": should_alarm,
        "last_alarm_reason_codes": alarm_reason_codes,
        "last_discord_enabled": discord_enabled,
        "last_discord_webhook_env": webhook_env_name,
        "last_notification_attempted": notification_attempted,
        "last_notification_status": notification_status,
        "last_notification_reason": notification_reason,
        "last_notification_error_detail": notification_error_detail,
        "previous_release_check": prev_release_check or None,
        "previous_release_ready": prev_release_ready if has_prev else None,
        "previous_reason_codes": prev_reason_codes if has_prev else [],
    }
    contexts = state_doc.get("contexts")
    if not isinstance(contexts, dict):
        contexts = {}
    contexts[context_key] = context_state_payload
    state_payload = {
        "updated_utc": now,
        "last_context_key": context_key,
        "contexts": contexts,
        # Backward-compatible mirror for tooling expecting single-slot keys.
        **context_state_payload,
    }
    write_json(state_path, state_payload, pretty=bool(args.pretty))

    # Backward-compatible compact status field.
    if notification_status == "error":
        discord_status = f"failed:{notification_error_detail or notification_reason}"
    elif notification_status == "sent":
        discord_status = "sent"
    else:
        discord_status = "skipped"

    out = {
        "ok": True,
        "strategy": strategy_id,
        "state_context_key": context_key,
        "state_json": str(state_path),
        "log_file": str(log_path),
        "checker_exit_code": int(checker_exit_code),
        "checker_release_check": current_release_check,
        "checker_release_ready": current_release_ready,
        "checker_reason_codes": current_reason_codes,
        "previous_release_check": prev_release_check or None,
        "previous_release_ready": prev_release_ready if has_prev else None,
        "release_check_changed": release_check_changed,
        "release_ready_changed": release_ready_changed,
        "reason_codes_changed": reason_codes_changed,
        "alarm_reason_codes": alarm_reason_codes,
        "alarm_triggered": should_alarm,
        "discord_enabled": discord_enabled,
        "discord_webhook_env": webhook_env_name,
        "notification_attempted": notification_attempted,
        "notification_status": notification_status,
        "notification_reason": notification_reason,
        "notification_error_detail": notification_error_detail,
        "discord": discord_status,
    }
    return 0, out


def main() -> int:
    args = parse_args()
    lock_path = resolve_path(str(args.lock_file), "pending_release_alarm.lock")
    if not _acquire_lock(lock_path):
        out = {
            "ok": False,
            "error": "lock_busy",
            "lock_file": str(lock_path),
        }
        if bool(args.pretty):
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
        return 4
    try:
        code, payload = run_alarm(args)
        if bool(args.pretty):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return int(code)
    finally:
        _release_lock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
