from __future__ import annotations

import record_no_longshot_realized_daily as mod


def _market(closed: bool, no_price: float = 0.995, yes_price: float = 0.005,
            end: str = "2020-01-01T00:00:00Z") -> dict:
    return {
        "id": "m1",
        "outcomes": ["YES", "NO"],
        "clobTokenIds": ["ty", "tn"],
        "outcomePrices": [str(yes_price), str(no_price)],
        "closed": closed,
        "endDate": end,
    }


def test_market_is_settled_flags():
    assert mod.market_is_settled({"closed": True}) is True
    assert mod.market_is_settled({"umaResolutionStatus": "resolved"}) is True
    assert mod.market_is_settled({"closed": False}) is False
    assert mod.market_is_settled({}) is False


def test_resolve_requires_settlement(monkeypatch):
    # Price at extreme but NOT settled => must not book a (premature) win.
    monkeypatch.setattr(mod, "fetch_market_by_id", lambda mid, timeout_sec: _market(closed=False))
    pos = {"status": "open", "market_id": "m1", "entry_no_price": 0.82, "entry_per_trade_cost": 0.0}
    assert mod.try_resolve_position(
        pos=pos, timeout_sec=1.0, win_threshold=0.99, lose_threshold=0.01, require_settled=True
    ) is False
    assert pos["status"] == "open"


def test_resolve_settled_books_win_on_observation_day(monkeypatch):
    monkeypatch.setattr(mod, "fetch_market_by_id", lambda mid, timeout_sec: _market(closed=True))
    pos = {"status": "open", "market_id": "m1", "entry_no_price": 0.82, "entry_per_trade_cost": 0.0}
    assert mod.try_resolve_position(
        pos=pos, timeout_sec=1.0, win_threshold=0.99, lose_threshold=0.01, require_settled=True
    ) is True
    assert pos["status"] == "resolved"
    assert pos["resolution"] == "NO_WIN"
    # resolved_day is the observation day, never the back-dated 2020 endDate
    assert pos["resolved_day"] != "2020-01-01"
    assert abs(pos["pnl_per_share"] - (1.0 - 0.82)) < 1e-9


def test_resolve_proxy_only_when_settlement_not_required(monkeypatch):
    monkeypatch.setattr(mod, "fetch_market_by_id", lambda mid, timeout_sec: _market(closed=False))
    pos = {"status": "open", "market_id": "m1", "entry_no_price": 0.82, "entry_per_trade_cost": 0.0}
    assert mod.try_resolve_position(
        pos=pos, timeout_sec=1.0, win_threshold=0.99, lose_threshold=0.01, require_settled=False
    ) is True
    assert pos["status"] == "resolved"
