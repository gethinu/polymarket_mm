from __future__ import annotations

from lib.clob_arb_eval import compute_candidate, make_signature, update_book_from_snapshot
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

