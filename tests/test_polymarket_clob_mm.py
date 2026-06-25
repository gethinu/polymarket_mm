from __future__ import annotations

import polymarket_clob_mm as mm


def _fill(side: str, price: float, size: float) -> mm.TradeFill:
    return mm.TradeFill(
        trade_id=f"{side}-{price}-{size}",
        ts=0.0,
        created_at_raw=0,
        token_id="t1",
        side=side,
        price=price,
        size=size,
    )


def test_buy_accumulates_inventory_and_avg_cost():
    s = mm.TokenMMState(token_id="t1", label="A")
    mm._update_inventory_from_fill(s, _fill("BUY", 0.40, 10))
    mm._update_inventory_from_fill(s, _fill("BUY", 0.50, 10))
    assert s.inventory_shares == 20
    assert abs(s.avg_cost - 0.45) < 1e-9


def test_sell_within_inventory_books_pnl():
    s = mm.TokenMMState(token_id="t1", label="A", inventory_shares=10, avg_cost=0.40)
    mm._update_inventory_from_fill(s, _fill("SELL", 0.50, 10))
    assert s.inventory_shares == 0
    assert abs(s.realized_pnl - (0.50 - 0.40) * 10) < 1e-9


def test_sell_exceeding_inventory_does_not_book_phantom_pnl():
    # Hold 4 shares; a 10-share sell must realize PnL on 4 only, not 10.
    s = mm.TokenMMState(token_id="t1", label="A", inventory_shares=4, avg_cost=0.40)
    mm._update_inventory_from_fill(s, _fill("SELL", 0.50, 10))
    assert s.inventory_shares == 0
    assert abs(s.realized_pnl - (0.50 - 0.40) * 4) < 1e-9  # not * 10


def test_sell_with_no_inventory_books_nothing():
    s = mm.TokenMMState(token_id="t1", label="A", inventory_shares=0, avg_cost=0.0)
    mm._update_inventory_from_fill(s, _fill("SELL", 0.50, 5))
    assert s.inventory_shares == 0
    assert s.realized_pnl == 0.0
