"""Tests for report_btc5m_panic_reconcile helpers."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import report_btc5m_panic_reconcile as reconcile_mod


def test_collect_live_orders_dedupes_by_order_id():
    orders = reconcile_mod.collect_live_orders(
        [
            {"ts": "2026-03-07 10:00:00", "order_id": "ord1", "token_id": "tok1", "status": "submitted"},
            {"ts": "2026-03-07 10:00:02", "order_id": "ord1", "token_id": "tok1", "status": "filled"},
            {"ts": "2026-03-07 10:01:00", "order_id": "ord2", "token_id": "tok2", "status": "submitted"},
        ],
        hours=0.0,
    )
    assert len(orders) == 2
    assert orders[0]["order_id"] == "ord1"
    assert orders[0]["status"] == "filled"


def test_build_report_counts_matched_orders():
    report = reconcile_mod.build_report(
        [
            {"requested_shares": 10.0, "actual_fill_shares": 8.0, "actual_fill_notional_usd": 0.4},
            {"requested_shares": 5.0, "actual_fill_shares": 0.0, "actual_fill_notional_usd": 0.0},
        ],
        state={},
    )
    assert report["order_count"] == 2
    assert report["matched_orders"] == 1
    assert report["requested_shares_total"] == 15.0
    assert report["actual_fill_shares_total"] == 8.0
