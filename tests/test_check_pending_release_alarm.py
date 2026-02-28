from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import check_pending_release_alarm as mod


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _args(tmp_path: Path, **overrides):
    base = dict(
        strategy="gamma_eventpair_exec_edge_filter_observe",
        snapshot_json=str(tmp_path / "snapshot.json"),
        state_json=str(tmp_path / "pending_release_alarm_state.json"),
        log_file=str(tmp_path / "pending_release_alarm.log"),
        lock_file=str(tmp_path / "pending_release_alarm.lock"),
        monthly_json="",
        replay_json="",
        min_gap_ms_per_event=5000,
        max_worst_stale_sec=10.0,
        conservative_costs=False,
        conservative_cost_cents=2.0,
        notify_on_reason_change=False,
        discord=False,
        discord_webhook_env="",
        pretty=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _checker_payload(*, release_check: str, release_ready: bool, reason_codes: list[str]) -> dict:
    return {
        "ok": True,
        "strategy": "gamma_eventpair_exec_edge_filter_observe",
        "current_status": "PENDING",
        "release_check": release_check,
        "release_ready": release_ready,
        "reason_codes": reason_codes,
        "execution_edge": 0.01,
        "full_kelly": 0.02,
    }


def test_run_alarm_first_run_is_noop_transition(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mod.checker,
        "run_check",
        lambda _args: (10, _checker_payload(release_check="HOLD", release_ready=False, reason_codes=["full_kelly_non_positive"])),
    )
    args = _args(tmp_path)

    code, out = mod.run_alarm(args)

    assert code == 0
    assert out["alarm_triggered"] is False
    assert out["checker_exit_code"] == 10
    assert out["discord_enabled"] is False
    assert out["discord_webhook_env"] == mod.DEFAULT_PENDING_RELEASE_DISCORD_WEBHOOK_ENV
    assert out["notification_attempted"] is False
    assert out["notification_status"] == "skipped"
    assert out["notification_reason"] == "no_state_change"
    state = _read_json(Path(args.state_json))
    assert state["last_release_check"] == "HOLD"
    assert state["last_reason_codes"] == ["full_kelly_non_positive"]
    assert Path(args.log_file).exists() is False


def test_run_alarm_triggers_when_release_check_changes(tmp_path, monkeypatch):
    state_path = tmp_path / "pending_release_alarm_state.json"
    _write_json(
        state_path,
        {
            "last_release_check": "HOLD",
            "last_release_ready": False,
            "last_reason_codes": ["full_kelly_non_positive"],
        },
    )
    monkeypatch.setattr(
        mod.checker,
        "run_check",
        lambda _args: (0, _checker_payload(release_check="RELEASE_READY", release_ready=True, reason_codes=[])),
    )
    args = _args(tmp_path)

    code, out = mod.run_alarm(args)

    assert code == 0
    assert out["alarm_triggered"] is True
    assert "release_check_changed" in out["alarm_reason_codes"]
    assert "release_ready_changed" in out["alarm_reason_codes"]
    lines = Path(args.log_file).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["previous_release_check"] == "HOLD"
    assert row["current_release_check"] == "RELEASE_READY"


def test_run_alarm_reason_change_requires_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mod.checker,
        "run_check",
        lambda _args: (10, _checker_payload(release_check="HOLD", release_ready=False, reason_codes=["execution_edge_non_positive"])),
    )

    state_path = tmp_path / "pending_release_alarm_state.json"
    _write_json(
        state_path,
        {
            "last_release_check": "HOLD",
            "last_release_ready": False,
            "last_reason_codes": ["full_kelly_non_positive"],
        },
    )
    args_no_reason_alarm = _args(tmp_path, notify_on_reason_change=False)
    code, out = mod.run_alarm(args_no_reason_alarm)
    assert code == 0
    assert out["reason_codes_changed"] is True
    assert out["alarm_triggered"] is False

    _write_json(
        state_path,
        {
            "last_release_check": "HOLD",
            "last_release_ready": False,
            "last_reason_codes": ["full_kelly_non_positive"],
        },
    )
    args_reason_alarm = _args(tmp_path, notify_on_reason_change=True)
    code2, out2 = mod.run_alarm(args_reason_alarm)
    assert code2 == 0
    assert out2["alarm_triggered"] is True
    assert "reason_codes_changed" in out2["alarm_reason_codes"]


def test_run_alarm_returns_error_when_checker_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mod.checker,
        "run_check",
        lambda _args: (21, {"ok": False, "error": "snapshot_missing_or_invalid"}),
    )
    args = _args(tmp_path)

    code, out = mod.run_alarm(args)

    assert code == 2
    assert out["ok"] is False
    assert out["error"] == "snapshot_missing_or_invalid"
    assert out["checker_exit_code"] == 21


def test_run_alarm_discord_missing_env_is_skipped(tmp_path, monkeypatch):
    state_path = tmp_path / "pending_release_alarm_state.json"
    _write_json(
        state_path,
        {
            "last_release_check": "HOLD",
            "last_release_ready": False,
            "last_reason_codes": ["full_kelly_non_positive"],
        },
    )
    monkeypatch.setattr(
        mod.checker,
        "run_check",
        lambda _args: (0, _checker_payload(release_check="RELEASE_READY", release_ready=True, reason_codes=[])),
    )
    monkeypatch.setattr(mod, "_env_any", lambda _name: "")
    args = _args(tmp_path, discord=True)

    code, out = mod.run_alarm(args)

    assert code == 0
    assert out["alarm_triggered"] is True
    assert out["notification_attempted"] is False
    assert out["notification_status"] == "skipped"
    assert out["notification_reason"] == "webhook_env_missing"


def test_run_alarm_discord_send_failure_is_nonfatal(tmp_path, monkeypatch):
    state_path = tmp_path / "pending_release_alarm_state.json"
    _write_json(
        state_path,
        {
            "last_release_check": "HOLD",
            "last_release_ready": False,
            "last_reason_codes": ["full_kelly_non_positive"],
        },
    )
    monkeypatch.setattr(
        mod.checker,
        "run_check",
        lambda _args: (0, _checker_payload(release_check="RELEASE_READY", release_ready=True, reason_codes=[])),
    )

    def _env(name: str) -> str:
        if name == mod.DEFAULT_PENDING_RELEASE_DISCORD_WEBHOOK_ENV:
            return "https://example.invalid/webhook"
        return ""

    monkeypatch.setattr(mod, "_env_any", _env)
    monkeypatch.setattr(mod, "post_discord", lambda _content, webhook_url: (False, "http_500"))
    args = _args(tmp_path, discord=True)

    code, out = mod.run_alarm(args)

    assert code == 0
    assert out["alarm_triggered"] is True
    assert out["notification_attempted"] is True
    assert out["notification_status"] == "error"
    assert out["notification_reason"] == "request_failed"
    assert out["notification_error_detail"] == "http_500"


def test_run_alarm_state_is_partitioned_by_context_key(tmp_path, monkeypatch):
    def _run_check(check_args):
        if bool(getattr(check_args, "conservative_costs", False)):
            return 10, _checker_payload(
                release_check="HOLD",
                release_ready=False,
                reason_codes=["conservative_execution_edge_non_positive"],
            )
        return 10, _checker_payload(
            release_check="HOLD",
            release_ready=False,
            reason_codes=["full_kelly_non_positive"],
        )

    monkeypatch.setattr(mod.checker, "run_check", _run_check)

    args_base = _args(tmp_path, conservative_costs=False, conservative_cost_cents=2.0)
    code1, out1 = mod.run_alarm(args_base)
    assert code1 == 0
    assert out1["alarm_triggered"] is False

    args_cons = _args(tmp_path, conservative_costs=True, conservative_cost_cents=2.0)
    code2, out2 = mod.run_alarm(args_cons)
    assert code2 == 0
    assert out2["alarm_triggered"] is False
    assert out2["previous_release_check"] is None
    assert out2["previous_release_ready"] is None

    state = _read_json(Path(args_base.state_json))
    contexts = state.get("contexts")
    assert isinstance(contexts, dict)
    assert out1["state_context_key"] in contexts
    assert out2["state_context_key"] in contexts
    assert out1["state_context_key"] != out2["state_context_key"]


def test_instance_lock_acquire_release_cycle(tmp_path):
    lock_path = tmp_path / "pending_release_alarm.lock"
    ok1 = mod._acquire_lock(lock_path)
    assert ok1 is True

    ok2 = mod._acquire_lock(lock_path)
    assert ok2 is False

    mod._release_lock(lock_path)
    ok3 = mod._acquire_lock(lock_path)
    assert ok3 is True
    mod._release_lock(lock_path)


def test_main_returns_4_when_lock_is_busy(tmp_path, monkeypatch):
    lock_path = tmp_path / "pending_release_alarm.lock"
    assert mod._acquire_lock(lock_path) is True

    monkeypatch.setattr(
        mod,
        "parse_args",
        lambda: argparse.Namespace(
            strategy="gamma_eventpair_exec_edge_filter_observe",
            snapshot_json=str(tmp_path / "snapshot.json"),
            state_json=str(tmp_path / "pending_release_alarm_state.json"),
            log_file=str(tmp_path / "pending_release_alarm.log"),
            lock_file=str(lock_path),
            monthly_json="",
            replay_json="",
            min_gap_ms_per_event=5000,
            max_worst_stale_sec=10.0,
            conservative_costs=False,
            conservative_cost_cents=2.0,
            notify_on_reason_change=False,
            discord=False,
            discord_webhook_env="",
            pretty=False,
        ),
    )
    monkeypatch.setattr(mod, "run_alarm", lambda _args: (_ for _ in ()).throw(AssertionError("run_alarm should not run")))

    code = mod.main()
    assert code == 4
    mod._release_lock(lock_path)
