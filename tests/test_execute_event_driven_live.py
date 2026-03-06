from __future__ import annotations

import datetime as dt

import execute_event_driven_live as mod


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
    assert plan["size_shares"] == 41.66
    assert plan["notional_usd"] <= 5.0


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
