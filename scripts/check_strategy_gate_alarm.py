#!/usr/bin/env python3
"""
Observe-only gate transition alarm checker.

Reads strategy register snapshot, compares current 3-stage gate decision
with last seen state, and emits one alarm event on stage transitions.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Optional
from urllib import error, request


DEFAULT_STRATEGY_ID = "weather_clob_arb_buckets_observe"
DEFAULT_MIN_RESOLVED_TRADES = 30


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
    p = Path(raw)
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
    with path.open("w", encoding="utf-8") as f:
        if pretty:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        else:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        f.write("\n")


def _env_any(name: str) -> str:
    v = os.environ.get(name)
    if v and str(v).strip():
        return str(v).strip()
    # Best-effort HKCU lookup for Windows sessions where process env is stale.
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


def discord_webhook_url(preferred_env: str = "") -> str:
    env_name = str(preferred_env or "").strip()
    if env_name:
        # If a dedicated env var is supplied, use it strictly (no fallback).
        return _env_any(env_name)
    return _env_any("CLOBBOT_DISCORD_WEBHOOK_URL") or _env_any("DISCORD_WEBHOOK_URL")


def post_discord(content: str, preferred_env: str = "") -> tuple[bool, str]:
    url = discord_webhook_url(preferred_env=preferred_env)
    if not url:
        return False, "webhook_missing"
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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Check strategy 3-stage gate transition alarm (observe-only).")
    p.add_argument("--snapshot-json", default="logs/strategy_register_latest.json")
    p.add_argument("--state-json", default="logs/strategy_gate_alarm_state.json")
    p.add_argument("--log-file", default="logs/strategy_gate_alarm.log")
    p.add_argument("--strategy-id", default=DEFAULT_STRATEGY_ID)
    p.add_argument(
        "--capital-min-resolved-trades",
        type=int,
        default=DEFAULT_MIN_RESOLVED_TRADES,
        help="Minimum rolling-30d resolved trades required for capital gate ELIGIBLE_REVIEW.",
    )
    p.add_argument("--discord", action="store_true", help="Send Discord notification when transition is detected.")
    p.add_argument(
        "--discord-webhook-env",
        default="",
        help="Environment variable name for Discord webhook URL (strict; no fallback when set).",
    )
    p.add_argument("--pretty", action="store_true")
    return p.parse_args()


def load_gate(snapshot: dict) -> dict:
    gate = snapshot.get("realized_30d_gate") if isinstance(snapshot.get("realized_30d_gate"), dict) else {}
    return {
        "decision": str(gate.get("decision") or "").strip(),
        "decision_3stage": str(gate.get("decision_3stage") or gate.get("decision") or "").strip(),
        "decision_3stage_label_ja": str(gate.get("decision_3stage_label_ja") or "").strip(),
        "stage_label": str(gate.get("stage_label") or "").strip(),
        "stage_label_ja": str(gate.get("stage_label_ja") or "").strip(),
        "observed_realized_days": gate.get("observed_realized_days"),
        "reason": str(gate.get("reason_3stage") or gate.get("reason") or "").strip(),
    }


def _as_int(v: object, default: int = 0) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except Exception:
        return default


def load_no_longshot(snapshot: dict) -> dict:
    no_longshot = snapshot.get("no_longshot_status") if isinstance(snapshot.get("no_longshot_status"), dict) else {}
    resolved = _as_int(no_longshot.get("rolling_30d_resolved_trades"), _as_int(no_longshot.get("resolved_positions"), 0))
    monthly_now = str(no_longshot.get("monthly_return_now_text") or "").strip()
    monthly_src = str(no_longshot.get("monthly_return_now_source") or "").strip()
    return {
        "rolling_30d_resolved_trades": resolved,
        "monthly_return_now_text": monthly_now,
        "monthly_return_now_source": monthly_src,
    }


def capital_gate_core(decision_3stage: str, resolved_trades: int, min_resolved_trades: int) -> tuple[str, str]:
    stage = str(decision_3stage or "").strip() or "UNKNOWN"
    if stage != "READY_FINAL":
        return "HOLD", f"strategy_stage={stage}"
    if int(resolved_trades) < int(max(1, min_resolved_trades)):
        return "HOLD", f"rolling_30d_resolved_trades={int(resolved_trades)}<{int(max(1, min_resolved_trades))}"
    return "ELIGIBLE_REVIEW", "all core checks passed"


def main() -> int:
    args = parse_args()
    snapshot_path = resolve_path(str(args.snapshot_json), "strategy_register_latest.json")
    state_path = resolve_path(str(args.state_json), "strategy_gate_alarm_state.json")
    log_path = resolve_path(str(args.log_file), "strategy_gate_alarm.log")
    strategy_id = str(args.strategy_id or "").strip() or DEFAULT_STRATEGY_ID
    discord_webhook_env = str(args.discord_webhook_env or "").strip()

    snapshot = read_json(snapshot_path)
    if snapshot is None:
        print(f"[strategy-gate-alarm] snapshot missing/invalid: {snapshot_path}")
        return 2

    gate = load_gate(snapshot)
    no_longshot = load_no_longshot(snapshot)
    current_decision = str(gate.get("decision_3stage") or "").strip()
    if not current_decision:
        print(f"[strategy-gate-alarm] gate data missing in snapshot: {snapshot_path}")
        return 2
    min_resolved = max(1, int(args.capital_min_resolved_trades))
    current_capital_gate, current_capital_reason = capital_gate_core(
        decision_3stage=current_decision,
        resolved_trades=_as_int(no_longshot.get("rolling_30d_resolved_trades"), 0),
        min_resolved_trades=min_resolved,
    )

    prev = read_json(state_path) or {}
    prev_decision = str(prev.get("last_decision_3stage") or "").strip()
    has_prev = bool(prev_decision)
    prev_capital_gate = str(prev.get("last_capital_gate_core") or "").strip()
    has_prev_capital = bool(prev_capital_gate)

    changed = has_prev and (prev_decision != current_decision)
    reached_final = has_prev and current_decision == "READY_FINAL" and prev_decision != "READY_FINAL"
    capital_changed = has_prev_capital and (prev_capital_gate != current_capital_gate)
    reached_eligible_review = (
        has_prev_capital and current_capital_gate == "ELIGIBLE_REVIEW" and prev_capital_gate != "ELIGIBLE_REVIEW"
    )
    should_alarm = bool(changed or reached_final or capital_changed or reached_eligible_review)

    now = now_utc().isoformat()
    event = {
        "ts_utc": now,
        "event": "strategy_gate_transition",
        "strategy_id": strategy_id,
        "snapshot_json": str(snapshot_path),
        "previous_decision_3stage": prev_decision or None,
        "current_decision_3stage": current_decision,
        "current_decision_3stage_label_ja": str(gate.get("decision_3stage_label_ja") or ""),
        "current_stage_label": str(gate.get("stage_label") or ""),
        "current_stage_label_ja": str(gate.get("stage_label_ja") or ""),
        "observed_realized_days": gate.get("observed_realized_days"),
        "reason": str(gate.get("reason") or ""),
        "previous_capital_gate_core": prev_capital_gate or None,
        "current_capital_gate_core": current_capital_gate,
        "current_capital_gate_reason": current_capital_reason,
        "rolling_30d_resolved_trades": _as_int(no_longshot.get("rolling_30d_resolved_trades"), 0),
        "capital_min_resolved_trades": min_resolved,
        "alarm_triggered": should_alarm,
    }

    state_payload = {
        "updated_utc": now,
        "strategy_id": strategy_id,
        "snapshot_json": str(snapshot_path),
        "last_decision_3stage": current_decision,
        "last_decision_3stage_label_ja": str(gate.get("decision_3stage_label_ja") or ""),
        "last_stage_label": str(gate.get("stage_label") or ""),
        "last_stage_label_ja": str(gate.get("stage_label_ja") or ""),
        "last_observed_realized_days": gate.get("observed_realized_days"),
        "last_reason": str(gate.get("reason") or ""),
        "previous_decision_3stage": prev_decision or None,
        "last_capital_gate_core": current_capital_gate,
        "last_capital_gate_reason": current_capital_reason,
        "last_rolling_30d_resolved_trades": _as_int(no_longshot.get("rolling_30d_resolved_trades"), 0),
        "capital_min_resolved_trades": min_resolved,
        "previous_capital_gate_core": prev_capital_gate or None,
        "alarm_triggered_last_run": should_alarm,
    }
    write_json(state_path, state_payload, pretty=bool(args.pretty))

    discord_status = "skipped"
    if should_alarm:
        append_jsonl(log_path, event)
        if bool(args.discord):
            mention = _env_any("CLOBBOT_DISCORD_MENTION")
            label_ja = str(gate.get("decision_3stage_label_ja") or "-")
            days = gate.get("observed_realized_days")
            resolved = _as_int(no_longshot.get("rolling_30d_resolved_trades"), 0)
            body = (
                "[Strategy Gate Alarm]\n"
                f"strategy={strategy_id}\n"
                f"gate: {prev_decision or '-'} -> {current_decision} ({label_ja})\n"
                f"capital_gate: {prev_capital_gate or '-'} -> {current_capital_gate}\n"
                f"observed_days={days} resolved_trades={resolved}"
            )
            if mention:
                body = f"{mention} {body}"
            ok, status = post_discord(body, preferred_env=discord_webhook_env)
            discord_status = status if ok else f"failed:{status}"

    summary = {
        "ok": True,
        "strategy_id": strategy_id,
        "snapshot_json": str(snapshot_path),
        "state_json": str(state_path),
        "log_file": str(log_path),
        "previous_decision_3stage": prev_decision or None,
        "current_decision_3stage": current_decision,
        "current_decision_3stage_label_ja": str(gate.get("decision_3stage_label_ja") or ""),
        "observed_realized_days": gate.get("observed_realized_days"),
        "previous_capital_gate_core": prev_capital_gate or None,
        "current_capital_gate_core": current_capital_gate,
        "current_capital_gate_reason": current_capital_reason,
        "rolling_30d_resolved_trades": _as_int(no_longshot.get("rolling_30d_resolved_trades"), 0),
        "capital_min_resolved_trades": min_resolved,
        "capital_alarm_triggered": bool(capital_changed or reached_eligible_review),
        "alarm_triggered": should_alarm,
        "discord": discord_status,
    }
    if args.pretty:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
