from __future__ import annotations

from types import SimpleNamespace

import lib.clob_arb_runtime as runtime
from lib.clob_arb_models import EventBasket, Leg, RuntimeState


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, msg):
        self.messages.append(str(msg))


def test_load_save_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = RuntimeState(day="2026-02-26", executions_today=3, notional_today=12.5)
    runtime.save_state(path, state)
    loaded = runtime.load_state(path)
    assert loaded.day == "2026-02-26"
    assert loaded.executions_today == 3
    assert loaded.notional_today == 12.5


def test_maybe_apply_daily_loss_guard_halts_on_drawdown(monkeypatch):
    monkeypatch.setenv("SIMMER_API_KEY", "dummy")
    monkeypatch.setattr(runtime, "fetch_simmer_portfolio", lambda _api_key: {"pnl_total": 90.0})

    state = RuntimeState(day="2026-02-26", start_pnl_total=100.0, last_pnl_check_ts=0.0)
    args = SimpleNamespace(daily_loss_limit_usd=5.0, pnl_check_interval_sec=0.0)
    logger = _Logger()
    notifications = []

    ok = runtime.maybe_apply_daily_loss_guard(
        state,
        args,
        logger,
        notify_func=lambda _logger, msg: notifications.append(msg),
    )

    assert ok is False
    assert state.halted is True
    assert "Daily loss guard hit" in state.halt_reason
    assert len(notifications) == 1


def _mk_args(**overrides):
    base = {
        "execute": False,
        "confirm_live": "",
        "exec_backend": "auto",
        "simmer_venue": "polymarket",
        "simmer_source": "sdk:clob-arb",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _mk_basket(key: str, simmer_market_id: str) -> EventBasket:
    return EventBasket(
        key=key,
        title=key,
        strategy="yes-no",
        legs=[
            Leg(
                market_id=f"m-{key}",
                question=key,
                label="YES",
                token_id=f"t-{key}",
                side="yes",
                simmer_market_id=simmer_market_id,
            )
        ],
    )


def test_initialize_execution_backend_observe_mode():
    args = _mk_args(execute=False)
    logger = _Logger()
    res = runtime.initialize_execution_backend(
        args=args,
        logger=logger,
        baskets=[_mk_basket("a", "sm1")],
        build_clob_client_func=lambda _args: object(),
    )
    assert res["ok"] is True
    assert res["exec_backend"] == "none"
    assert res["baskets_changed"] is False


def test_initialize_execution_backend_rejects_without_confirm():
    args = _mk_args(execute=True, confirm_live="NO")
    logger = _Logger()
    res = runtime.initialize_execution_backend(
        args=args,
        logger=logger,
        baskets=[],
        build_clob_client_func=lambda _args: object(),
    )
    assert res["ok"] is False
    assert res["exit_code"] == 1
    assert any("Refusing live mode" in m for m in logger.messages)


def test_initialize_execution_backend_auto_clob(monkeypatch):
    monkeypatch.setenv("PM_PRIVATE_KEY", "x")
    monkeypatch.setenv("PM_FUNDER", "y")
    monkeypatch.delenv("SIMMER_API_KEY", raising=False)
    args = _mk_args(execute=True, confirm_live="YES", exec_backend="auto")
    logger = _Logger()
    called = {"count": 0}

    def _build(_args):
        called["count"] += 1
        return "clob-client"

    res = runtime.initialize_execution_backend(
        args=args,
        logger=logger,
        baskets=[_mk_basket("a", "sm1")],
        build_clob_client_func=_build,
    )
    assert res["ok"] is True
    assert res["exec_backend"] == "clob"
    assert res["client"] == "clob-client"
    assert called["count"] == 1


def test_initialize_execution_backend_simmer_filters_unmapped(monkeypatch):
    monkeypatch.setenv("SIMMER_API_KEY", "dummy")
    monkeypatch.delenv("PM_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("PM_FUNDER", raising=False)
    monkeypatch.setattr(
        runtime,
        "fetch_simmer_settings",
        lambda _api_key: {
            "trading_paused": False,
            "sdk_real_trading_enabled": True,
            "polymarket_usdc_balance": 10.0,
        },
    )
    args = _mk_args(execute=True, confirm_live="YES", exec_backend="simmer")
    logger = _Logger()
    baskets = [
        _mk_basket("mapped", "sm1"),
        _mk_basket("unmapped", ""),
    ]

    res = runtime.initialize_execution_backend(
        args=args,
        logger=logger,
        baskets=baskets,
        build_clob_client_func=lambda _args: object(),
    )
    assert res["ok"] is True
    assert res["exec_backend"] == "simmer"
    assert res["simmer_api_key"] == "dummy"
    assert res["baskets_changed"] is True
    assert len(res["baskets"]) == 1
    assert res["baskets"][0].key == "mapped"


def test_initialize_execution_backend_simmer_paused_returns_zero(monkeypatch):
    monkeypatch.setenv("SIMMER_API_KEY", "dummy")
    monkeypatch.setattr(runtime, "fetch_simmer_settings", lambda _api_key: {"trading_paused": True})
    args = _mk_args(execute=True, confirm_live="YES", exec_backend="simmer")
    logger = _Logger()

    res = runtime.initialize_execution_backend(
        args=args,
        logger=logger,
        baskets=[_mk_basket("mapped", "sm1")],
        build_clob_client_func=lambda _args: object(),
    )
    assert res["ok"] is False
    assert res["exit_code"] == 0
    assert any("trading_paused=true" in m for m in logger.messages)


def test_build_monitor_loop_tuning_normalizes_values():
    args = _mk_args(
        execute=False,
        summary_every_sec="10",
        min_eval_interval_ms="250",
        observe_notify_min_interval_sec="3",
        observe_exec_edge_filter=True,
        observe_exec_edge_min_usd="-0.01",
        observe_exec_edge_strike_limit="0",
        observe_exec_edge_cooldown_sec="0",
        observe_exec_edge_filter_strategies=" event-yes ,event-no ,,",
    )
    cfg = runtime.build_monitor_loop_tuning(args)
    assert cfg["summary_every"] == 10.0
    assert cfg["min_eval_interval"] == 0.25
    assert cfg["observe_notify_min_interval"] == 3.0
    assert cfg["observe_exec_edge_filter"] is True
    assert cfg["observe_exec_edge_min_usd"] == -0.01
    assert cfg["observe_exec_edge_strike_limit"] == 1
    assert cfg["observe_exec_edge_cooldown_sec"] == 1.0
    assert cfg["observe_exec_edge_filter_strategies"] == {"event-yes", "event-no"}


def test_log_monitor_startup_emits_basics():
    logger = _Logger()
    args = _mk_args(
        execute=False,
        min_edge_cents=1.0,
        shares=5.0,
        winner_fee_rate=0.0,
        fixed_cost=0.0,
        strategy="both",
        summary_every_sec=0,
        metrics_log_all_candidates=False,
        observe_exec_edge_filter=False,
        notify_observe_signals=False,
    )
    runtime.log_monitor_startup(
        logger=logger,
        args=args,
        universe="weather",
        baskets_count=2,
        token_ids_count=4,
        max_tokens=0,
        metrics_file=None,
    )
    assert any("Loaded baskets: 2" in m for m in logger.messages)
    assert any("Subscribed token IDs: 4" in m for m in logger.messages)
    assert any("Metrics: disabled" in m for m in logger.messages)
