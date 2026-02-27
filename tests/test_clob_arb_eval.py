from __future__ import annotations

import asyncio
from types import SimpleNamespace

import lib.clob_arb_eval as eval_mod
from lib.clob_arb_eval import (
    apply_observe_exec_edge_and_log_metrics,
    collect_impacted_events_from_payload,
    compute_candidate,
    make_signature,
    process_impacted_event,
    update_book_from_snapshot,
)
from lib.clob_arb_models import EventBasket, Leg, LocalBook, RunStats, RuntimeState


def test_update_book_from_snapshot_builds_synthetic_best_only_book():
    books = {}
    token = update_book_from_snapshot({"asset_id": "t1", "best_ask": 0.42, "best_bid": 0.41}, books)
    assert token == "t1"
    assert "t1" in books
    assert books["t1"].asks_synthetic is True
    assert books["t1"].bids_synthetic is True


def test_compute_candidate_from_single_leg_book():
    basket = EventBasket(
        key="k1",
        title="Title",
        strategy="buckets",
        legs=[Leg(market_id="m1", question="q", label="yes", token_id="t1")],
    )
    books = {"t1": LocalBook(asks=[{"price": 0.25, "size": 10}], bids=[])}
    c = compute_candidate(basket, books, shares_per_leg=2.0, winner_fee_rate=0.0, fixed_cost=0.0)
    assert c is not None
    assert c.basket_cost == 0.5
    assert c.net_edge == 1.5


def test_make_signature_contains_strategy_and_event_key():
    basket = EventBasket(
        key="event-1",
        title="Title",
        strategy="buckets",
        legs=[Leg(market_id="m1", question="q", label="yes", token_id="t1")],
    )
    books = {"t1": LocalBook(asks=[{"price": 0.25, "size": 10}], bids=[])}
    c = compute_candidate(basket, books, shares_per_leg=1.0, winner_fee_rate=0.0, fixed_cost=0.0)
    sig = make_signature(c)
    assert sig.startswith("buckets|event-1|")


def test_apply_observe_exec_edge_and_log_metrics_writes_threshold_reason():
    basket = EventBasket(
        key="event-1",
        title="Title",
        strategy="buckets",
        legs=[Leg(market_id="m1", question="q", label="yes", token_id="t1")],
    )
    books = {"t1": LocalBook(asks=[{"price": 0.25, "size": 10}], bids=[])}
    c = compute_candidate(basket, books, shares_per_leg=1.0, winner_fee_rate=0.0, fixed_cost=0.0)
    args = SimpleNamespace(
        min_edge_cents=1.0,
        exec_slippage_bps=0.0,
        universe="weather",
        metrics_log_all_candidates=False,
    )
    rows = []

    metrics_row, filtered = apply_observe_exec_edge_and_log_metrics(
        candidate=c,
        basket=basket,
        books=books,
        args=args,
        metrics_file="dummy.jsonl",
        observe_exec_edge_filter=False,
        observe_exec_edge_min_usd=0.0,
        observe_exec_edge_strike_limit=2,
        observe_exec_edge_cooldown_sec=30.0,
        observe_exec_edge_filter_strategies=set(),
        append_jsonl_func=lambda _path, row: rows.append(row),
        logger=SimpleNamespace(info=lambda _msg: None),
    )

    assert filtered is False
    assert isinstance(metrics_row, dict)
    assert len(rows) == 1
    assert rows[0]["reason"] == "threshold"


def test_apply_observe_exec_edge_and_log_metrics_blocks_after_strike(monkeypatch):
    basket = EventBasket(
        key="event-1",
        title="Title",
        strategy="event-yes",
        legs=[Leg(market_id="m1", question="q", label="yes", token_id="t1")],
    )
    basket.exec_edge_neg_streak = 1
    books = {"t1": LocalBook(asks=[{"price": 0.25, "size": 10}], bids=[])}
    c = compute_candidate(basket, books, shares_per_leg=1.0, winner_fee_rate=0.0, fixed_cost=0.0)
    args = SimpleNamespace(
        min_edge_cents=1.0,
        exec_slippage_bps=0.0,
        universe="weather",
        metrics_log_all_candidates=False,
    )
    rows = []
    logs = []
    monkeypatch.setattr(
        eval_mod,
        "compute_candidate_metrics_row",
        lambda **_kwargs: {"net_edge_exec_est": -0.01, "passes_raw_threshold": True},
    )

    metrics_row, filtered = apply_observe_exec_edge_and_log_metrics(
        candidate=c,
        basket=basket,
        books=books,
        args=args,
        metrics_file="dummy.jsonl",
        observe_exec_edge_filter=True,
        observe_exec_edge_min_usd=0.0,
        observe_exec_edge_strike_limit=2,
        observe_exec_edge_cooldown_sec=30.0,
        observe_exec_edge_filter_strategies={"event-yes"},
        append_jsonl_func=lambda _path, row: rows.append(row),
        logger=SimpleNamespace(info=lambda msg: logs.append(str(msg))),
    )

    assert filtered is True
    assert isinstance(metrics_row, dict)
    assert metrics_row["observe_exec_filter_blocked"] is True
    assert rows[0]["reason"] == "observe_exec_filter_blocked"
    assert basket.exec_edge_neg_streak == 0
    assert basket.exec_edge_filter_until_ts > 0.0
    assert any("observe exec-edge filter: muted event=event-1" in msg for msg in logs)


def test_process_impacted_event_observe_signal_notifies():
    basket = EventBasket(
        key="event-1",
        title="Title",
        strategy="buckets",
        legs=[Leg(market_id="m1", question="q", label="yes", token_id="t1")],
    )
    books = {"t1": LocalBook(asks=[{"price": 0.25, "size": 10}], bids=[])}
    args = SimpleNamespace(
        shares=1.0,
        winner_fee_rate=0.0,
        fixed_cost=0.0,
        min_edge_cents=1.0,
        alert_cooldown_sec=0.0,
        execute=False,
        notify_observe_signals=True,
        metrics_log_all_candidates=False,
        exec_slippage_bps=0.0,
        universe="weather",
    )
    notices = []
    stats = RunStats()

    updated = asyncio.run(
        process_impacted_event(
            basket=basket,
            books=books,
            args=args,
            stats=stats,
            min_eval_interval=0.0,
            metrics_file=None,
            observe_exec_edge_filter=False,
            observe_exec_edge_min_usd=0.0,
            observe_exec_edge_strike_limit=2,
            observe_exec_edge_cooldown_sec=30.0,
            observe_exec_edge_filter_strategies=set(),
            observe_notify_min_interval=0.0,
            last_observe_notify_ts=0.0,
            logger=SimpleNamespace(info=lambda _msg: None),
            append_jsonl_func=lambda _path, _row: None,
            notify_func=lambda _logger, msg: notices.append(str(msg)),
            live_execution_ctx={},
        )
    )

    assert updated > 0.0
    assert stats.candidates_total == 1
    assert stats.candidates_window == 1
    assert basket.last_signature
    assert len(notices) == 1
    assert notices[0].startswith("OBSERVE SIGNAL")


def test_process_impacted_event_execute_path_delegates_live_call():
    basket = EventBasket(
        key="event-1",
        title="Title",
        strategy="buckets",
        legs=[Leg(market_id="m1", question="q", label="yes", token_id="t1")],
    )
    books = {"t1": LocalBook(asks=[{"price": 0.25, "size": 10}], bids=[])}
    args = SimpleNamespace(
        shares=1.0,
        winner_fee_rate=0.0,
        fixed_cost=0.0,
        min_edge_cents=1.0,
        alert_cooldown_sec=0.0,
        execute=True,
        notify_observe_signals=False,
        metrics_log_all_candidates=False,
        exec_slippage_bps=0.0,
        universe="weather",
    )
    stats = RunStats()
    called = []

    async def _fake_execute(**kwargs):
        called.append(kwargs.get("candidate"))

    updated = asyncio.run(
        process_impacted_event(
            basket=basket,
            books=books,
            args=args,
            stats=stats,
            min_eval_interval=0.0,
            metrics_file=None,
            observe_exec_edge_filter=False,
            observe_exec_edge_min_usd=0.0,
            observe_exec_edge_strike_limit=2,
            observe_exec_edge_cooldown_sec=30.0,
            observe_exec_edge_filter_strategies=set(),
            observe_notify_min_interval=0.0,
            last_observe_notify_ts=0.0,
            logger=SimpleNamespace(info=lambda _msg: None),
            append_jsonl_func=lambda _path, _row: None,
            notify_func=lambda _logger, _msg: None,
            live_execution_ctx={
                "execute_func": _fake_execute,
                "state": RuntimeState(day="2026-02-26"),
                "exec_backend": "clob",
                "client": object(),
                "simmer_api_key": "",
                "save_state_func": lambda _path, _state: None,
                "state_file": "state.json",
                "sdk_request_func": lambda *_a, **_kw: {},
                "fetch_simmer_positions_func": lambda *_a, **_kw: [],
                "estimate_exec_cost_func": lambda *_a, **_kw: 0.0,
            },
        )
    )

    assert updated == 0.0
    assert stats.candidates_total == 1
    assert len(called) == 1


def test_collect_impacted_events_from_payload_unions_token_mappings():
    books = {}
    payload = [
        {"event_type": "book", "asset_id": "t1", "asks": [{"price": 0.4, "size": 1.0}]},
        {"event_type": "book", "asset_id": "t2", "best_ask": 0.3},
    ]
    token_to_events = {
        "t1": {"e1", "e2"},
        "t2": {"e2", "e3"},
    }
    impacted = collect_impacted_events_from_payload(payload, books, token_to_events)
    assert impacted == {"e1", "e2", "e3"}
    assert "t1" in books and "t2" in books


def test_collect_impacted_events_from_payload_empty_when_no_books():
    books = {}
    impacted = collect_impacted_events_from_payload(payload={"foo": "bar"}, books=books, token_to_events={})
    assert impacted == set()
    assert books == {}
