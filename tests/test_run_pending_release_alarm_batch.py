from __future__ import annotations

import json
from types import SimpleNamespace

import run_pending_release_alarm_batch as mod


def _args(**overrides):
    base = dict(
        strategy=[],
        python_exe="python",
        snapshot_json="logs/strategy_register_latest.json",
        state_json="logs/pending_release_alarm_state.json",
        log_file="logs/pending_release_alarm.log",
        lock_file="logs/pending_release_alarm.lock",
        monthly_json="",
        replay_json="",
        run_conservative=False,
        conservative_cost_cents=2.0,
        child_timeout_sec=60.0,
        discord=False,
        discord_webhook_env="",
        notify_on_reason_change=False,
        fail_on_lock_busy=False,
        out_json="logs/pending_release_batch_latest.json",
        pretty=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _payload(*, release_check: str, release_ready: bool) -> str:
    obj = {
        "ok": True,
        "strategy": "gamma_eventpair_exec_edge_filter_observe",
        "release_check": release_check,
        "release_ready": release_ready,
        "reason_codes": [],
        "notification_status": "skipped",
        "notification_reason": "no_state_change",
    }
    return json.dumps(obj, ensure_ascii=False)


def test_run_batch_default_strategy_hold_is_ok(monkeypatch):
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=10, stdout=_payload(release_check="HOLD", release_ready=False), stderr=""),
    )
    args = _args(strategy=[])
    code, out = mod.run_batch(args)

    assert code == 0
    assert out["decision"] == "OK"
    assert out["counts"]["ok"] == 1
    assert out["counts"]["warn"] == 0
    assert out["counts"]["error"] == 0
    assert out["strategies"] == ["gamma_eventpair_exec_edge_filter_observe"]


def test_run_batch_warn_lock_busy_without_fail(monkeypatch):
    calls = iter(
        [
            SimpleNamespace(returncode=0, stdout=_payload(release_check="NOOP", release_ready=False), stderr=""),
            SimpleNamespace(returncode=4, stdout=json.dumps({"ok": False, "error": "lock_busy"}), stderr=""),
        ]
    )
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: next(calls))
    args = _args(strategy=["s1"], run_conservative=True, fail_on_lock_busy=False)
    code, out = mod.run_batch(args)

    assert code == 0
    assert out["decision"] == "WARN"
    assert out["counts"]["warn"] == 1
    assert out["counts"]["error"] == 0


def test_run_batch_warn_lock_busy_with_fail(monkeypatch):
    calls = iter(
        [
            SimpleNamespace(returncode=0, stdout=_payload(release_check="NOOP", release_ready=False), stderr=""),
            SimpleNamespace(returncode=4, stdout=json.dumps({"ok": False, "error": "lock_busy"}), stderr=""),
        ]
    )
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: next(calls))
    args = _args(strategy=["s1"], run_conservative=True, fail_on_lock_busy=True)
    code, out = mod.run_batch(args)

    assert code == 4
    assert out["decision"] == "WARN"


def test_run_batch_parse_error_is_error(monkeypatch):
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="not-json", stderr=""),
    )
    args = _args(strategy=["s1"])
    code, out = mod.run_batch(args)

    assert code == 20
    assert out["decision"] == "ERROR"
    assert out["counts"]["error"] == 1


def test_run_batch_timeout_is_error(monkeypatch):
    def _raise_timeout(*args, **kwargs):
        raise mod.subprocess.TimeoutExpired(cmd=["python"], timeout=1.0)

    monkeypatch.setattr(mod.subprocess, "run", _raise_timeout)
    args = _args(strategy=["s1"], child_timeout_sec=1.0)
    code, out = mod.run_batch(args)

    assert code == 20
    assert out["decision"] == "ERROR"
    assert out["counts"]["error"] == 1
    assert out["runs"][0]["timed_out"] is True
    assert out["runs"][0]["exit_code"] == 124


def test_run_batch_notification_error_is_warn_but_exit_zero(monkeypatch):
    row = {
        "ok": True,
        "strategy": "gamma_eventpair_exec_edge_filter_observe",
        "release_check": "HOLD",
        "release_ready": False,
        "reason_codes": ["full_kelly_non_positive"],
        "notification_status": "error",
        "notification_reason": "request_failed",
    }
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=10, stdout=json.dumps(row, ensure_ascii=False), stderr=""),
    )
    args = _args(strategy=["s1"], fail_on_lock_busy=False)
    code, out = mod.run_batch(args)

    assert code == 0
    assert out["decision"] == "WARN"
    assert out["notification_warning_runs"] == 1
    assert out["notification_counts"]["error"] == 1
