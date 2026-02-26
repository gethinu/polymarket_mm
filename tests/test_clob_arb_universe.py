from __future__ import annotations

from types import SimpleNamespace

import lib.clob_arb_universe as universe
from lib.clob_arb_models import EventBasket, Leg


def test_parse_btc_updown_window_minutes_filters_and_keeps_order():
    actual = universe.parse_btc_updown_window_minutes("15, 5, bad, 15, 30")
    assert actual == [15, 5]


def test_parse_btc_updown_window_minutes_default():
    assert universe.parse_btc_updown_window_minutes("") == [5]


def test_build_btc_updown_baskets_minimal_event(monkeypatch):
    monkeypatch.setattr(universe.time, "time", lambda: 1_700_000_000)

    def _fake_fetch(_slug: str):
        return {
            "id": "ev1",
            "slug": "btc-updown-5m-1700000000",
            "title": "BTC Up/Down",
            "markets": [
                {
                    "id": "m1",
                    "enableOrderBook": True,
                    "clobTokenIds": ["t_yes", "t_no"],
                    "outcomes": ["Yes", "No"],
                    "conditionId": "cond1",
                    "question": "BTC up?",
                    "liquidityNum": 10_000,
                    "volume24hr": 2_000,
                }
            ],
        }

    monkeypatch.setattr(universe, "fetch_gamma_event_by_slug", _fake_fetch)
    baskets = universe.build_btc_updown_baskets([5], windows_back=0, windows_forward=0)
    assert len(baskets) == 1
    assert baskets[0].strategy == "yes-no"
    assert len(baskets[0].legs) == 2


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, msg):
        self.messages.append(str(msg))


def _mk_basket(key: str, token_a: str, token_b: str) -> EventBasket:
    return EventBasket(
        key=key,
        title=key,
        strategy="yes-no",
        legs=[
            Leg(market_id=f"m-{key}", question=key, label="YES", token_id=token_a, side="yes"),
            Leg(market_id=f"m-{key}", question=key, label="NO", token_id=token_b, side="no"),
        ],
    )


def test_normalize_universe_unknown_defaults_weather():
    assert universe.normalize_universe("unknown") == "weather"


def test_build_universe_baskets_empty_weather_returns_notify(monkeypatch):
    monkeypatch.setattr(universe, "build_event_baskets", lambda **_kwargs: [])
    args = SimpleNamespace(universe="weather", limit=10, min_outcomes=2, workers=1, strategy="both")
    logger = _Logger()

    resolved_universe, baskets, notify = universe.build_universe_baskets(args=args, logger=logger)

    assert resolved_universe == "weather"
    assert baskets == []
    assert notify == "CLOBBOT: weather universe empty (no baskets)."


def test_apply_subscription_token_cap_non_gamma_reduces_baskets():
    args = SimpleNamespace(max_subscribe_tokens=2)
    logger = _Logger()
    baskets = [
        _mk_basket("b1", "t1", "t2"),
        _mk_basket("b2", "t3", "t4"),
    ]

    selected, notify = universe.apply_subscription_token_cap(
        baskets=baskets,
        universe="weather",
        args=args,
        logger=logger,
    )

    assert notify is None
    assert len(selected) == 1
    assert selected[0].key == "b1"


def test_apply_subscription_token_cap_gamma_empty_when_all_scores_negative(monkeypatch):
    monkeypatch.setattr(universe, "score_gamma_basket", lambda *_args, **_kwargs: -1.0)
    args = SimpleNamespace(
        max_subscribe_tokens=4,
        wallet_signal_enable=False,
        max_markets_per_event=0,
    )
    logger = _Logger()

    selected, notify = universe.apply_subscription_token_cap(
        baskets=[_mk_basket("g1", "tg1", "tg2")],
        universe="gamma-active",
        args=args,
        logger=logger,
    )

    assert selected == []
    assert notify is not None
    assert "selection empty" in notify


def test_build_subscription_maps_deduplicates_tokens():
    baskets = [
        _mk_basket("a", "t1", "t2"),
        _mk_basket("b", "t2", "t3"),
    ]

    token_to_events, event_map, token_ids = universe.build_subscription_maps(baskets)

    assert set(token_ids) == {"t1", "t2", "t3"}
    assert event_map["a"].key == "a"
    assert token_to_events["t2"] == {"a", "b"}
