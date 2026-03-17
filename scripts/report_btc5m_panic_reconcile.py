#!/usr/bin/env python3
"""
Reconcile BTC panic live-order audit logs against CLOB trade history.

This script is read-only. It does not place or cancel orders.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(REPO_ROOT / "scripts"))

from lib.clob_auth import build_clob_client_from_env
from polymarket_btc5m_panic_live import (
    DEFAULT_EXEC_LOG_FILE,
    DEFAULT_STATE_FILE,
    fetch_matching_trades,
    summarize_matching_trades,
)

LOGS_DIR = REPO_ROOT / "logs"


def _parse_local_ts(text: str) -> Optional[float]:
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return None


def load_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    rows: List[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def collect_live_orders(events: List[dict], hours: float) -> List[dict]:
    cutoff = None
    if hours > 0:
        cutoff = datetime.now().timestamp() - (float(hours) * 3600.0)
    latest_by_order: Dict[str, dict] = {}
    for row in events:
        order_id = str(row.get("order_id") or "").strip()
        if not order_id:
            continue
        ts_value = _parse_local_ts(str(row.get("ts") or ""))
        if cutoff is not None and ts_value is not None and ts_value < cutoff:
            continue
        latest_by_order[order_id] = row
    return sorted(
        latest_by_order.values(),
        key=lambda row: str(row.get("ts") or ""),
    )


def maybe_add_active_order(orders: List[dict], state: dict) -> List[dict]:
    active = state.get("active_position")
    if not isinstance(active, dict):
        return orders
    order_id = str(active.get("order_id") or "").strip()
    token_id = str(active.get("token_id") or "").strip()
    if not order_id or not token_id:
        return orders
    known = {str(row.get("order_id") or "").strip() for row in orders}
    if order_id in known:
        return orders
    extra = {
        "ts": str(active.get("entry_ts") or ""),
        "mode": "LIVE",
        "status": "active_position",
        "order_id": order_id,
        "token_id": token_id,
        "side": str(active.get("side") or ""),
        "screen_entry_price": float(active.get("screen_entry_price") or 0.0),
        "limit_price": float(active.get("maker_limit_price") or active.get("entry_price") or 0.0),
        "shares_requested": float(active.get("shares") or 0.0),
        "market_id": str(active.get("market_id") or ""),
        "window_slug": str(active.get("window_slug") or ""),
    }
    return orders + [extra]


def reconcile_orders(client, orders: List[dict]) -> List[dict]:
    rows: List[dict] = []
    for order in orders:
        order_id = str(order.get("order_id") or "").strip()
        token_id = str(order.get("token_id") or "").strip()
        limit_price = float(order.get("limit_price") or order.get("screen_entry_price") or 0.0)
        trades = fetch_matching_trades(
            client=client,
            order_id=order_id,
            asset_id=token_id,
            retries=1,
            sleep_sec=0.0,
        )
        fill = summarize_matching_trades(trades, fallback_price=limit_price)
        rows.append(
            {
                "ts": str(order.get("ts") or ""),
                "window_slug": str(order.get("window_slug") or ""),
                "market_id": str(order.get("market_id") or ""),
                "order_id": order_id,
                "token_id": token_id,
                "side": str(order.get("side") or ""),
                "status": str(order.get("status") or ""),
                "requested_limit_price": limit_price,
                "requested_shares": float(order.get("shares_requested") or 0.0),
                "actual_fill_price": float(fill["actual_fill_price"]),
                "actual_fill_shares": float(fill["actual_fill_shares"]),
                "actual_fill_notional_usd": float(fill["actual_fill_notional_usd"]),
                "trade_count": int(fill["trade_count"]),
                "trade_ids": list(fill["trade_ids"]),
                "tx_hash": str(fill.get("tx_hash") or ""),
            }
        )
    return rows


def build_report(rows: List[dict], state: dict) -> dict:
    requested_shares = sum(float(row.get("requested_shares") or 0.0) for row in rows)
    actual_fill_shares = sum(float(row.get("actual_fill_shares") or 0.0) for row in rows)
    actual_fill_notional = sum(float(row.get("actual_fill_notional_usd") or 0.0) for row in rows)
    matched = sum(1 for row in rows if float(row.get("actual_fill_shares") or 0.0) > 0)
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "order_count": len(rows),
        "matched_orders": matched,
        "unmatched_orders": len(rows) - matched,
        "requested_shares_total": requested_shares,
        "actual_fill_shares_total": actual_fill_shares,
        "actual_fill_notional_total_usd": actual_fill_notional,
        "active_position": state.get("active_position") if isinstance(state.get("active_position"), dict) else None,
        "orders": rows,
    }


def format_report(report: dict) -> str:
    lines = [
        "=== BTC Panic Live Reconcile ===",
        f"Orders: {int(report.get('order_count') or 0)}",
        f"Matched: {int(report.get('matched_orders') or 0)}",
        f"Unmatched: {int(report.get('unmatched_orders') or 0)}",
        f"Requested shares: {float(report.get('requested_shares_total') or 0.0):.2f}",
        f"Actual fill shares: {float(report.get('actual_fill_shares_total') or 0.0):.2f}",
        f"Actual fill notional: ${float(report.get('actual_fill_notional_total_usd') or 0.0):.4f}",
        "",
    ]
    for row in report.get("orders", []):
        lines.append(
            f"{row['ts']} {row['side']} order_id={row['order_id'][:16]} "
            f"req={row['requested_shares']:.2f}@{row['requested_limit_price']:.4f} "
            f"fill={row['actual_fill_shares']:.2f}@{row['actual_fill_price']:.4f} "
            f"trades={row['trade_count']}"
        )
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Reconcile BTC panic live orders against CLOB trades"
    )
    parser.add_argument("--hours", type=float, default=72.0, help="Only inspect exec-log orders within this many recent hours (0=all)")
    parser.add_argument("--exec-log-file", default=DEFAULT_EXEC_LOG_FILE, help="Execution audit JSONL path")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="Live state JSON path")
    parser.add_argument("--out-json", default="", help="Output JSON path")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--clob-host", default="https://clob.polymarket.com")
    parser.add_argument("--chain-id", type=int, default=137)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    exec_log_path = Path(args.exec_log_file)
    state_path = Path(args.state_file)
    out_path = (
        Path(args.out_json)
        if args.out_json
        else LOGS_DIR / "btc5m-panic-reconcile-latest.json"
    )

    events = load_jsonl(exec_log_path)
    state = load_state(state_path)
    orders = collect_live_orders(events, hours=float(args.hours))
    orders = maybe_add_active_order(orders, state)

    client = build_clob_client_from_env(
        clob_host=str(args.clob_host),
        chain_id=int(args.chain_id),
        missing_env_message="Missing env for CLOB auth. Need PM_PRIVATE_KEY and PM_FUNDER.",
        invalid_key_message="Invalid PM_PRIVATE_KEY format.",
    )
    rows = reconcile_orders(client, orders)
    report = build_report(rows, state)
    print(format_report(report))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as fh:
        if args.pretty:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        else:
            json.dump(report, fh, ensure_ascii=False)
        fh.write("\n")
    print(f"\nSaved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
