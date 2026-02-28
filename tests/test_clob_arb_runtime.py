from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import lib.clob_arb_runtime as runtime
from lib.clob_arb_models import EventBasket, Leg, RunStats, RuntimeState


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, msg):
        self.messages.append(str(msg))


def test_load_save_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "day_key_local", lambda: "2026-02-26")
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


def test_maybe_rollover_daily_state_resets_and_persists(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "day_key_local", lambda: "2026-02-27")
    logger = _Logger()
    saves = []
    state_file = tmp_path / "state.json"
    current = RuntimeState(day="2026-02-26", executions_today=2, halted=True, halt_reason="x")

    updated = runtime.maybe_rollover_daily_state(
        state=current,
        state_file=state_file,
        logger=logger,
        save_state_func=lambda path, state: saves.append((path, state)),
    )

    assert updated.day == "2026-02-27"
    assert updated.executions_today == 0
    assert updated.halted is False
    assert len(saves) == 1
    assert saves[0][0] == state_file
    assert any("daily counters reset" in m for m in logger.messages)


def test_maybe_emit_periodic_summary_logs_and_resets(monkeypatch):
    monkeypatch.setattr(runtime, "now_ts", lambda: 100.0)
    logger = _Logger()
    stats = RunStats()
    stats.candidates_window = 3
    stats.best_window = object()
    stats.window_started_at = 60.0
    stats.last_summary_ts = 80.0

    runtime.maybe_emit_periodic_summary(
        stats=stats,
        summary_every=10.0,
        logger=logger,
        format_candidate_brief_func=lambda _c: "BEST",
    )

    assert any("summary(40s): candidates=3 | BEST" in m for m in logger.messages)
    assert stats.candidates_window == 0
    assert stats.best_window is None
    assert stats.window_started_at == 100.0
    assert stats.last_summary_ts == 100.0


def test_run_timeout_and_recv_timeout_helpers(monkeypatch):
    monkeypatch.setattr(runtime, "now_ts", lambda: 105.0)

    assert runtime.run_timeout_reached(run_started_at=100.0, run_seconds=5.0) is True
    assert runtime.run_timeout_reached(run_started_at=100.0, run_seconds=6.0) is False

    assert runtime.compute_recv_timeout_seconds(run_started_at=100.0, run_seconds=0.0) == 30.0
    assert runtime.compute_recv_timeout_seconds(run_started_at=100.0, run_seconds=4.0) is None
    assert runtime.compute_recv_timeout_seconds(run_started_at=100.0, run_seconds=5.1) == 0.2
    assert runtime.compute_recv_timeout_seconds(run_started_at=100.0, run_seconds=50.0) == 30.0


def test_should_log_idle_heartbeat_and_run_summary(monkeypatch):
    monkeypatch.setattr(runtime, "now_ts", lambda: 105.0)
    logger = _Logger()

    assert runtime.should_log_idle_heartbeat(run_started_at=100.0, run_seconds=0.0) is True
    assert runtime.should_log_idle_heartbeat(run_started_at=100.0, run_seconds=6.0) is False
    assert runtime.should_log_idle_heartbeat(run_started_at=100.0, run_seconds=20.0) is True

    stats = RunStats()
    stats.candidates_total = 7
    stats.best_all = object()
    runtime.maybe_emit_run_summary(
        stats=stats,
        summary_every=10.0,
        logger=logger,
        format_candidate_brief_func=lambda _c: "TOP",
    )
    assert any("run summary: candidates=7 | TOP" in m for m in logger.messages)


def test_resolve_monitor_paths_defaults(tmp_path):
    script_dir = tmp_path / "scripts"
    script_dir.mkdir(parents=True, exist_ok=True)
    args = SimpleNamespace(log_file="", state_file="", metrics_file="")

    out = runtime.resolve_monitor_paths(args=args, script_dir=script_dir)

    assert args.log_file.endswith("logs\\clob-arb-monitor.log") or args.log_file.endswith(
        "logs/clob-arb-monitor.log"
    )
    assert args.state_file.endswith("logs\\clob_arb_state.json") or args.state_file.endswith(
        "logs/clob_arb_state.json"
    )
    assert args.metrics_file.endswith("logs\\clob-arb-monitor-metrics.jsonl") or args.metrics_file.endswith(
        "logs/clob-arb-monitor-metrics.jsonl"
    )
    assert out["state_file"].name == "clob_arb_state.json"
    assert out["metrics_file"] is not None
    assert out["metrics_file"].parent.exists()


def test_resolve_monitor_paths_metrics_off(tmp_path):
    script_dir = tmp_path / "scripts"
    script_dir.mkdir(parents=True, exist_ok=True)
    args = SimpleNamespace(log_file="x.log", state_file="x.json", metrics_file="off")

    out = runtime.resolve_monitor_paths(args=args, script_dir=script_dir)

    assert args.metrics_file == ""
    assert out["metrics_file"] is None
    assert str(out["state_file"]).endswith("x.json")


def test_compile_gamma_market_regexes_logs_invalid():
    logger = _Logger()
    args = SimpleNamespace(gamma_include_regex="(", gamma_exclude_regex="[a-z")

    inc, exc = runtime.compile_gamma_market_regexes(args=args, logger=logger)

    assert inc is None
    assert exc is None
    assert any("invalid gamma_include_regex" in m for m in logger.messages)
    assert any("invalid gamma_exclude_regex" in m for m in logger.messages)


def test_should_skip_halted_execute_run():
    logger = _Logger()
    args = SimpleNamespace(execute=True)
    state = RuntimeState(day="2026-02-27", halted=True, halt_reason="cap")

    out = runtime.should_skip_halted_execute_run(args=args, state=state, logger=logger)

    assert out is True
    assert any("state halted: cap" in m for m in logger.messages)
    assert any("run skipped while halted" in m for m in logger.messages)


def test_prepare_monitor_runtime_returns_exit_on_empty_universe():
    logger = _Logger()
    notices = []
    args = _mk_args(
        execute=False,
        min_edge_cents=1.0,
        shares=1.0,
        winner_fee_rate=0.0,
        fixed_cost=0.0,
        strategy="both",
        summary_every_sec=0,
        metrics_log_all_candidates=False,
        observe_exec_edge_filter=False,
        notify_observe_signals=False,
        max_subscribe_tokens=0,
    )

    out = runtime.prepare_monitor_runtime(
        args=args,
        logger=logger,
        include_re=None,
        exclude_re=None,
        metrics_file=None,
        notify_func=lambda _logger, msg: notices.append(str(msg)),
        build_universe_baskets_func=lambda **_kw: ("weather", [], "empty"),
        apply_subscription_token_cap_func=lambda **_kw: ([], ""),
        build_subscription_maps_func=lambda _b: ({}, {}, []),
        initialize_execution_backend_func=lambda **_kw: {"ok": True},
        build_clob_client_func=lambda _args: object(),
    )

    assert out["ok"] is False
    assert out["exit_code"] == 1
    assert notices == ["empty"]


def test_prepare_monitor_runtime_success_rebuilds_maps_after_backend_filter():
    logger = _Logger()
    notices = []
    args = _mk_args(
        execute=True,
        min_edge_cents=1.0,
        shares=1.0,
        winner_fee_rate=0.0,
        fixed_cost=0.0,
        strategy="yes-no",
        summary_every_sec=0,
        metrics_log_all_candidates=False,
        observe_exec_edge_filter=False,
        notify_observe_signals=False,
        max_subscribe_tokens=0,
    )
    baskets = [_mk_basket("a", "sm1"), _mk_basket("b", "sm2")]
    calls = {"maps": 0}

    def _maps(_baskets):
        calls["maps"] += 1
        token_to_events = {}
        event_map = {}
        token_ids = []
        for b in _baskets:
            token = b.legs[0].token_id
            token_to_events[token] = {b.key}
            event_map[b.key] = b
            token_ids.append(token)
        return token_to_events, event_map, token_ids

    out = runtime.prepare_monitor_runtime(
        args=args,
        logger=logger,
        include_re=None,
        exclude_re=None,
        metrics_file=None,
        notify_func=lambda _logger, msg: notices.append(str(msg)),
        build_universe_baskets_func=lambda **_kw: ("weather", baskets, ""),
        apply_subscription_token_cap_func=lambda **_kw: (baskets, ""),
        build_subscription_maps_func=_maps,
        initialize_execution_backend_func=lambda **_kw: {
            "ok": True,
            "exec_backend": "simmer",
            "client": None,
            "simmer_api_key": "k",
            "baskets": [baskets[0]],
            "baskets_changed": True,
        },
        build_clob_client_func=lambda _args: object(),
    )

    assert out["ok"] is True
    assert out["exec_backend"] == "simmer"
    assert out["simmer_api_key"] == "k"
    assert len(out["baskets"]) == 1
    assert out["token_ids"] == [baskets[0].legs[0].token_id]
    assert calls["maps"] == 2
    assert any("CLOBBOT started (LIVE)" in n for n in notices)


def test_build_live_execution_context_keeps_expected_keys(tmp_path):
    state_file = tmp_path / "state.json"
    state = RuntimeState(day="2026-02-28")

    ctx = runtime.build_live_execution_context(
        state=state,
        exec_backend="clob",
        client="client",
        simmer_api_key="",
        save_state_func=lambda _path, _state: None,
        state_file=state_file,
        sdk_request_func=lambda *_a, **_kw: {},
        fetch_simmer_positions_func=lambda *_a, **_kw: [],
        estimate_exec_cost_func=lambda *_a, **_kw: 0.0,
        execute_func=lambda *_a, **_kw: None,
    )

    assert ctx["state"] is state
    assert ctx["exec_backend"] == "clob"
    assert ctx["client"] == "client"
    assert ctx["state_file"] == state_file
    assert callable(ctx["execute_func"])


def test_run_connected_monitor_loop_processes_one_message_then_times_out(monkeypatch, tmp_path):
    class _WS:
        def __init__(self):
            self.sent = []
            self.recv_calls = 0

        async def send(self, payload):
            self.sent.append(payload)

        async def recv(self):
            self.recv_calls += 1
            return '{"ok":1}'

    ws = _WS()
    logger = _Logger()
    state = RuntimeState(day="2026-02-28")
    state_file = tmp_path / "state.json"
    stats = RunStats()
    args = _mk_args(execute=False, run_seconds=0)

    iter_count = {"n": 0}

    def _fake_timeout(**_kwargs):
        iter_count["n"] += 1
        return iter_count["n"] > 1

    processed = []

    async def _fake_process(**kwargs):
        processed.append(
            {
                "raw": kwargs["raw"],
                "last_observe_notify_ts": kwargs["last_observe_notify_ts"],
                "state": kwargs["state"],
            }
        )
        return kwargs["last_observe_notify_ts"] + 1.0

    monkeypatch.setattr(runtime, "run_timeout_reached", _fake_timeout)

    out_state = asyncio.run(
        runtime.run_connected_monitor_loop(
            ws=ws,
            ws_url="wss://example",
            token_ids=["t1", "t2"],
            state=state,
            state_file=state_file,
            args=args,
            stats=stats,
            start_ts=0.0,
            summary_every=0.0,
            logger=logger,
            format_candidate_brief_func=lambda _c: "x",
            maybe_apply_daily_loss_guard_func=lambda _s, _a, _l: True,
            save_state_func=lambda _path, _state: None,
            process_ws_raw_message_func=_fake_process,
            process_ws_raw_message_kwargs={},
            initial_last_observe_notify_ts=0.0,
        )
    )

    assert out_state is state
    assert ws.recv_calls == 1
    assert len(processed) == 1
    assert processed[0]["raw"] == '{"ok":1}'
    assert processed[0]["last_observe_notify_ts"] == 0.0
    assert processed[0]["state"] is state
    sent = json.loads(ws.sent[0])
    assert sent["type"] == "market"
    assert sent["assets_ids"] == ["t1", "t2"]


def test_run_connected_monitor_loop_breaks_on_halt_guard(monkeypatch, tmp_path):
    class _WS:
        async def send(self, _payload):
            return None

        async def recv(self):
            raise AssertionError("recv should not be called when halting before read")

    ws = _WS()
    logger = _Logger()
    state = RuntimeState(day="2026-02-28", halted=True)
    state_file = tmp_path / "state.json"
    stats = RunStats()
    args = _mk_args(execute=True, run_seconds=0)
    saves = []

    monkeypatch.setattr(runtime, "run_timeout_reached", lambda **_kwargs: False)

    out_state = asyncio.run(
        runtime.run_connected_monitor_loop(
            ws=ws,
            ws_url="wss://example",
            token_ids=["t1"],
            state=state,
            state_file=state_file,
            args=args,
            stats=stats,
            start_ts=0.0,
            summary_every=0.0,
            logger=logger,
            format_candidate_brief_func=lambda _c: "x",
            maybe_apply_daily_loss_guard_func=lambda _s, _a, _l: False,
            save_state_func=lambda path, s: saves.append((path, s)),
            process_ws_raw_message_func=lambda **_kw: 0.0,
            process_ws_raw_message_kwargs={},
            initial_last_observe_notify_ts=0.0,
        )
    )

    assert out_state is state
    assert len(saves) == 1
    assert saves[0][0] == state_file
    assert saves[0][1] is state
    assert any("run ending early due to halt state" in m for m in logger.messages)
