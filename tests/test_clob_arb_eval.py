from __future__ import annotations

from types import SimpleNamespace

import lib.clob_arb_eval as eval_mod
from lib.clob_arb_eval import (
    apply_observe_exec_edge_and_log_metrics,
    compute_candidate,
    make_signature,
    update_book_from_snapshot,
)
from lib.clob_arb_models import EventBasket, Leg, LocalBook


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
