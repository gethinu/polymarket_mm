from __future__ import annotations

import datetime as dt

import execute_event_driven_live as mod


class _Level:
    def __init__(self, price: float, size: float):
        self.price = str(price)
        self.size = str(size)


class _Book:
    def __init__(self, asks):
        self.asks = asks


def _row(market_id: str, side: str, minutes_ago: int, edge_cents: float) -> dict:
    return {
        "market_id": market_id,
        "side": side,
        "ts": dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago),
        "edge_cents": edge_cents,
        "confidence": 0.82,
        "selected_price": 0.11,
        "question": "Q?",
        "event_class": "election_politics",
        "liquidity_num": 5000.0,
        "volume_24h": 250.0,
    }


def test_unique_latest_rows_prefers_latest_per_market_side():
    rows = [
        _row("m1", "YES", 10, 5.0),
        _row("m1", "YES", 1, 6.0),
        _row("m1", "NO", 2, 7.0),
        _row("m2", "YES", 3, 4.0),
    ]
    out = mod.unique_latest_rows(rows)
    keys = [(row["market_id"], row["side"], row["edge_cents"]) for row in out]
    assert ("m1", "YES", 6.0) in keys
    assert ("m1", "NO", 7.0) in keys
    assert ("m2", "YES", 4.0) in keys
    assert len(out) == 3


def test_build_order_plan_respects_stake_cap():
    plan, reason = mod.build_order_plan(
        screen_price=0.10,
        live_price=0.11,
        max_entry_price=0.35,
        price_buffer_cents=0.2,
        max_stake_usd=5.0,
        min_order_size=1.0,
    )
    assert reason == ""
    assert plan is not None
    assert plan["limit_price"] == 0.12
    assert plan["size_shares"] == 41.5
    assert plan["notional_usd"] <= 5.0
    assert round(plan["notional_usd"], 2) == plan["notional_usd"]


def test_build_order_plan_skips_when_min_size_exceeds_cap():
    plan, reason = mod.build_order_plan(
        screen_price=0.80,
        live_price=0.80,
        max_entry_price=0.90,
        price_buffer_cents=0.2,
        max_stake_usd=5.0,
        min_order_size=10.0,
    )
    assert plan is None
    assert reason == "min_order_size_exceeds_cap"


def test_extract_best_ask_price_uses_lowest_ask():
    book = _Book([_Level(0.99, 10), _Level(0.21, 7), _Level(0.14, 35)])
    assert mod.extract_best_ask_price(book) == 0.14


def test_clip_plan_to_visible_ask_depth_reduces_size_to_book_depth():
    plan, reason = mod.build_order_plan(
        screen_price=0.11,
        live_price=0.14,
        max_entry_price=0.35,
        price_buffer_cents=0.2,
        max_stake_usd=5.0,
        min_order_size=1.0,
    )
    assert reason == ""
    assert plan is not None
    clipped, clip_reason = mod.clip_plan_to_visible_ask_depth(
        plan,
        _Book([_Level(0.14, 20), _Level(0.15, 5), _Level(0.21, 100)]),
        min_order_size=1.0,
    )
    assert clip_reason == ""
    assert clipped is not None
    assert clipped["limit_price"] == 0.15
    assert clipped["size_shares"] == 25.0
    assert clipped["notional_usd"] == 3.75
    assert clipped["visible_ask_depth"] == 25.0


def test_clip_plan_to_visible_ask_depth_skips_when_no_ask_inside_limit():
    plan, reason = mod.build_order_plan(
        screen_price=0.11,
        live_price=0.14,
        max_entry_price=0.35,
        price_buffer_cents=0.2,
        max_stake_usd=5.0,
        min_order_size=1.0,
    )
    assert reason == ""
    assert plan is not None
    clipped, clip_reason = mod.clip_plan_to_visible_ask_depth(
        plan,
        _Book([_Level(0.16, 20), _Level(0.21, 100)]),
        min_order_size=1.0,
    )
    assert clipped is None
    assert clip_reason == "no_visible_ask_depth_at_limit"


def test_extract_order_fields_from_nested_payload():
    payload = {"order": {"status": "matched", "size_matched": "7.35", "original_size": "10.0"}}
    assert mod.extract_order_status(payload) == "matched"
    assert mod.extract_order_size_matched(payload) == 7.35


def test_summarize_fak_fill_prefers_order_snapshot_size():
    out = mod.summarize_fak_fill(
        {"order": {"status": "matched", "size_matched": "7.35", "original_size": "10.0"}},
        {"status": "matched", "takingAmount": "10.0"},
        [],
        limit_price=0.20,
        requested_size_shares=10.0,
    )
    assert out["order_status"] == "matched"
    assert out["filled_size_shares"] == 7.35
    assert out["requested_size_shares"] == 10.0
    assert out["filled_notional_usd"] == 1.47
    assert out["avg_fill_price"] == 0.20


def test_summarize_fak_fill_falls_back_to_post_response_amount():
    out = mod.summarize_fak_fill(
        None,
        {"status": "matched", "takingAmount": "4.80"},
        [],
        limit_price=0.20,
        requested_size_shares=4.8,
    )
    assert out["order_status"] == "matched"
    assert out["filled_size_shares"] == 4.8
    assert out["requested_size_shares"] == 4.8
    assert out["filled_notional_usd"] == 0.96


def test_summarize_fak_fill_prefers_trade_history_when_present():
    out = mod.summarize_fak_fill(
        None,
        {"status": "matched", "takingAmount": "33.2"},
        [
            {"taker_order_id": "oid", "size": "20", "price": "0.14"},
            {"taker_order_id": "oid", "size": "15", "price": "0.14"},
        ],
        limit_price=0.15,
        requested_size_shares=33.2,
    )
    assert out["order_status"] == "matched"
    assert out["filled_size_shares"] == 35.0
    assert out["filled_notional_usd"] == 4.9
    assert out["avg_fill_price"] == 0.14
    assert out["requested_size_shares"] == 33.2
    assert out["trade_count"] == 2


def test_trade_matches_order_id_checks_taker_and_maker_orders():
    assert mod.trade_matches_order_id({"taker_order_id": "abc"}, "abc") is True
    assert mod.trade_matches_order_id({"maker_orders": [{"order_id": "abc"}]}, "abc") is True
    assert mod.trade_matches_order_id({"maker_orders": [{"order_id": "zzz"}]}, "abc") is False


def test_prune_recent_actions_keeps_only_unexpired_keys():
    ref = dt.datetime(2026, 3, 6, 15, 0, tzinfo=dt.timezone.utc)
    out = mod.prune_recent_actions(
        [
            {"position_key": "m1:YES", "ts_utc": "2026-03-06T14:30:00+00:00", "status": "observe_preview"},
            {"position_key": "m2:NO", "ts_utc": "2026-03-06T07:30:00+00:00", "status": "observe_preview"},
            {"position_key": "", "ts_utc": "2026-03-06T14:45:00+00:00", "status": "observe_preview"},
        ],
        cooldown_min=120,
        ref_ts=ref,
    )
    assert out == [
        {"position_key": "m1:YES", "ts_utc": "2026-03-06T14:30:00+00:00", "status": "observe_preview"}
    ]
