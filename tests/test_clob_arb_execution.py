from __future__ import annotations

import asyncio
from types import SimpleNamespace

import lib.clob_arb_execution as exec_mod
from lib.clob_arb_execution import can_execute_candidate, estimate_simmer_total_amount, extract_order_ids
from lib.clob_arb_models import Candidate, EventBasket, Leg, RuntimeState


def _candidate(shares_per_leg: float = 2.0) -> Candidate:
    leg = Leg(market_id="m1", question="q", label="A", token_id="t1", simmer_market_id="sm1")
    return Candidate(
        strategy="buckets",
        event_key="e1",
        title="title",
        shares_per_leg=shares_per_leg,
        basket_cost=1.2,
        payout_after_fee=2.0,
        fixed_cost=0.0,
        net_edge=0.8,
        edge_pct=0.4,
        leg_costs=[(leg, 1.2)],
    )


def _basket() -> EventBasket:
    return EventBasket(
        key="e1",
        title="title",
        strategy="buckets",
        legs=[Leg(market_id="m1", question="q", label="A", token_id="t1", simmer_market_id="sm1")],
    )


def test_extract_order_ids_dedupes_nested_payload():
    payload = {"orders": [{"id": "a1"}, {"order_id": "a1"}, {"orderId": "b2"}]}
    assert extract_order_ids(payload) == ["a1", "b2"]


def test_estimate_simmer_total_amount_respects_min_amount():
    args = SimpleNamespace(exec_slippage_bps=0.0, simmer_min_amount=2.0)
    c = _candidate()
    assert estimate_simmer_total_amount(c, args) == 2.0


def test_can_execute_candidate_rejects_fractional_shares_for_clob():
    state = RuntimeState(day="2026-02-26")
    c = _candidate(shares_per_leg=1.5)
    args = SimpleNamespace(
        max_legs=0,
        max_exec_per_day=10,
        max_notional_per_day=100.0,
        max_consecutive_failures=3,
        max_open_orders=0,
    )
    ok, reason = can_execute_candidate(state, c, args, logger=None, exec_backend="clob", client=None)
    assert ok is False
    assert "integer-like" in reason


def test_maybe_execute_candidate_live_skips_when_not_allowed(monkeypatch):
    c = _candidate()
    basket = _basket()
    state = RuntimeState(day="2026-02-26")
    args = SimpleNamespace(
        exec_cooldown_sec=0.0,
        min_edge_cents=1.0,
        exec_slippage_bps=50.0,
        max_consecutive_failures=3,
    )
    logger_msgs = []
    state_saves = []

    monkeypatch.setattr(exec_mod, "can_execute_candidate", lambda **_kwargs: (False, "cap"))

    asyncio.run(
        exec_mod.maybe_execute_candidate_live(
            candidate=c,
            basket=basket,
            state=state,
            args=args,
            logger=SimpleNamespace(info=lambda m: logger_msgs.append(str(m))),
            books={},
            exec_backend="clob",
            client=None,
            simmer_api_key="",
            now_ts_value=100.0,
            notify_func=lambda *_a, **_kw: None,
            save_state_func=lambda _path, _state: state_saves.append("saved"),
            state_file="state.json",
            sdk_request_func=lambda *_a, **_kw: {},
            fetch_simmer_positions_func=lambda *_a, **_kw: [],
            estimate_exec_cost_func=lambda *_a, **_kw: 1.0,
        )
    )

    assert any("live: skipped (cap)" in m for m in logger_msgs)
    assert state_saves == ["saved"]


def test_maybe_execute_candidate_live_success_clob_updates_state(monkeypatch):
    c = _candidate()
    basket = _basket()
    state = RuntimeState(day="2026-02-26")
    args = SimpleNamespace(
        exec_cooldown_sec=0.0,
        min_edge_cents=1.0,
        exec_slippage_bps=50.0,
        max_consecutive_failures=3,
    )
    logger_msgs = []
    notices = []
    state_saves = []

    monkeypatch.setattr(exec_mod, "can_execute_candidate", lambda **_kwargs: (True, ""))
    monkeypatch.setattr(exec_mod, "precheck_clob_books", lambda *_a, **_kw: (True, ""))

    async def _ok_exec(_client, _candidate, _books, _args, _logger):
        return True, {"attempt": 1, "fills": {"t1": 2.0}}

    monkeypatch.setattr(exec_mod, "execute_with_retries_clob", _ok_exec)

    asyncio.run(
        exec_mod.maybe_execute_candidate_live(
            candidate=c,
            basket=basket,
            state=state,
            args=args,
            logger=SimpleNamespace(info=lambda m: logger_msgs.append(str(m))),
            books={},
            exec_backend="clob",
            client="client",
            simmer_api_key="",
            now_ts_value=123.0,
            notify_func=lambda _logger, msg: notices.append(str(msg)),
            save_state_func=lambda _path, _state: state_saves.append("saved"),
            state_file="state.json",
            sdk_request_func=lambda *_a, **_kw: {},
            fetch_simmer_positions_func=lambda *_a, **_kw: [],
            estimate_exec_cost_func=lambda *_a, **_kw: 1.11,
        )
    )

    assert state.executions_today == 1
    assert state.consecutive_failures == 0
    assert state.notional_today == 1.11
    assert basket.last_exec_ts == 123.0
    assert state_saves == ["saved"]
    assert any("ENTRY (clob)" in m for m in notices)
    assert any("Filled (clob)" in m for m in notices)
