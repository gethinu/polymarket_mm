#!/usr/bin/env python3
"""
Analyze Polymarket trade JSON and print an "autopsy" summary (read-only).

Input can be:
  - raw trade list
  - object with {"meta": ..., "trades": [...]}
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def fmt_ts(ts: int) -> str:
    if ts <= 0:
        return "-"
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fmt_pct(value: float) -> str:
    return f"{value:.2f}%"


def fmt_usd(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):.4f}"


@dataclass(frozen=True)
class TradeRow:
    timestamp: int
    side: str
    outcome: str
    price: float
    size: float
    condition_id: str
    slug: str


def normalize_trade_rows(raw_rows: List[dict]) -> List[TradeRow]:
    out: List[TradeRow] = []
    for r in raw_rows:
        ts = as_int(r.get("timestamp"), 0)
        side = str(r.get("side") or "").strip().upper()
        outcome = str(r.get("outcome") or "").strip()
        price = as_float(r.get("price"), math.nan)
        size = as_float(r.get("size"), math.nan)
        cond = str(r.get("conditionId") or "").strip()
        slug = str(r.get("slug") or "").strip()

        if ts <= 0 or side not in {"BUY", "SELL"}:
            continue
        if not outcome or not math.isfinite(price) or not math.isfinite(size):
            continue
        if price <= 0 or size <= 0:
            continue

        out.append(
            TradeRow(
                timestamp=ts,
                side=side,
                outcome=outcome,
                price=price,
                size=size,
                condition_id=cond,
                slug=slug,
            )
        )
    out.sort(key=lambda x: x.timestamp)
    return out


def _inventory_avg_prices(rows: List[TradeRow]) -> Tuple[Dict[str, float], Dict[str, float]]:
    qty: Dict[str, float] = {}
    cost: Dict[str, float] = {}
    for t in rows:
        o = t.outcome
        q = qty.get(o, 0.0)
        c = cost.get(o, 0.0)
        if t.side == "BUY":
            q += t.size
            c += t.size * t.price
        else:
            if q > 1e-12:
                sold = min(t.size, q)
                avg = c / q if q > 0 else 0.0
                q -= sold
                c -= sold * avg
            else:
                # Ignore fresh short for this "current long inventory" view.
                q = q
                c = c

        if q <= 1e-12:
            q = 0.0
            c = 0.0
        qty[o] = q
        cost[o] = c

    avg: Dict[str, float] = {}
    for o, q in qty.items():
        if q > 1e-12:
            avg[o] = cost[o] / q
    return avg, qty


def _timeline_profitability(rows: List[TradeRow], outcomes: List[str]) -> Tuple[float, float, float]:
    if len(rows) < 2:
        return 0.0, 0.0, 0.0

    cash = 0.0
    pos: Dict[str, float] = {o: 0.0 for o in outcomes}
    marks: Dict[str, float] = {}
    points: List[Tuple[int, float]] = []
    binary = len(outcomes) == 2

    for t in rows:
        if t.side == "BUY":
            pos[t.outcome] = pos.get(t.outcome, 0.0) + t.size
            cash -= t.size * t.price
        else:
            pos[t.outcome] = pos.get(t.outcome, 0.0) - t.size
            cash += t.size * t.price

        marks[t.outcome] = t.price
        if binary:
            other = outcomes[1] if t.outcome == outcomes[0] else outcomes[0]
            marks[other] = 1.0 - t.price

        equity = cash
        for o, q in pos.items():
            equity += q * marks.get(o, 0.0)
        points.append((t.timestamp, equity))

    start = rows[0].timestamp
    end = rows[-1].timestamp
    total_sec = max(0.0, float(end - start))
    if total_sec <= 0:
        return 0.0, 0.0, 0.0

    prof_sec = 0.0
    loss_sec = 0.0
    for i in range(len(points) - 1):
        t0, eq0 = points[i]
        t1, _eq1 = points[i + 1]
        dt_sec = max(0.0, float(t1 - t0))
        if eq0 >= 0:
            prof_sec += dt_sec
        else:
            loss_sec += dt_sec

    prof_pct = (prof_sec / total_sec) * 100.0 if total_sec > 0 else 0.0
    return prof_pct, prof_sec / 3600.0, loss_sec / 3600.0


def _trading_intensity(intervals_min: List[float]) -> str:
    if not intervals_min:
        return "INSUFFICIENT_DATA"
    avg = statistics.mean(intervals_min)
    if avg <= 0.10:
        return "BOT_LIKE"
    if avg <= 1.0:
        return "HIGH"
    if avg <= 5.0:
        return "MEDIUM"
    return "LOW"


def _accumulation_label(early_avg: float, late_avg: float) -> Tuple[str, float]:
    if early_avg <= 0:
        return "INSUFFICIENT_DATA", 0.0
    ratio = late_avg / early_avg
    if ratio >= 1.25:
        return "INCREASING", ratio
    if ratio <= 0.80:
        return "DECREASING", ratio
    return "FLAT", ratio


def _price_trend_label(early_avg: float, late_avg: float) -> str:
    if early_avg <= 0 or late_avg <= 0:
        return "INSUFFICIENT_DATA"
    delta = late_avg - early_avg
    if delta >= 0.01:
        return "CHASING_HIGHER"
    if delta <= -0.01:
        return "AVERAGING_DOWN"
    return "STABLE"


def analyze_trades(raw_rows: List[dict], market_title: str = "", wallet: str = "") -> dict:
    rows = normalize_trade_rows(raw_rows)
    if not rows:
        return {
            "ok": False,
            "reason": "No valid trades found",
            "market_title": market_title,
            "wallet": wallet,
            "trade_count": 0,
        }

    outcomes = sorted({r.outcome for r in rows})
    ts_values = [r.timestamp for r in rows]
    intervals_min = [
        (ts_values[i] - ts_values[i - 1]) / 60.0
        for i in range(1, len(ts_values))
        if ts_values[i] >= ts_values[i - 1]
    ]

    total_duration_hours = max(0, rows[-1].timestamp - rows[0].timestamp) / 3600.0
    avg_interval_min = statistics.mean(intervals_min) if intervals_min else 0.0
    intensity = _trading_intensity(intervals_min)

    profitable_pct, prof_hours, loss_hours = _timeline_profitability(rows, outcomes)
    unprofitable_pct = max(0.0, 100.0 - profitable_pct)

    avg_prices, qtys = _inventory_avg_prices(rows)
    combined_avg: Optional[float] = None
    hedge_status = "UNHEDGED"
    hedge_edge_pct: Optional[float] = None
    hedge_outcomes: List[str] = []
    hedgeable_shares = 0.0
    hedge_balance_ratio: Optional[float] = None
    if len(outcomes) == 2 and all(qtys.get(o, 0.0) > 1e-9 for o in outcomes):
        a = avg_prices.get(outcomes[0], 0.0)
        b = avg_prices.get(outcomes[1], 0.0)
        combined_avg = a + b
        hedge_edge_pct = (1.0 - combined_avg) * 100.0
        hedge_outcomes = outcomes[:]
        q_a = qtys.get(outcomes[0], 0.0)
        q_b = qtys.get(outcomes[1], 0.0)
        hedgeable_shares = min(q_a, q_b)
        max_shares = max(q_a, q_b)
        hedge_balance_ratio = (hedgeable_shares / max_shares) if max_shares > 1e-12 else None

        if hedgeable_shares < 1.0 or (hedge_balance_ratio is not None and hedge_balance_ratio < 0.20):
            hedge_status = "PARTIAL_HEDGE"
        elif combined_avg < 1.0 - 1e-6:
            hedge_status = "ARBITRAGE_CANDIDATE"
        elif combined_avg > 1.0 + 1e-6:
            hedge_status = "NEGATIVE_EDGE"
        else:
            hedge_status = "BREAKEVEN"

    buys = [r for r in rows if r.side == "BUY"]
    behavior = {
        "accumulation": "INSUFFICIENT_DATA",
        "accumulation_ratio": 0.0,
        "price_trend": "INSUFFICIENT_DATA",
        "primary_outcome": "",
    }
    if len(buys) >= 4:
        half = len(buys) // 2
        early = buys[:half]
        late = buys[half:]
        early_size = statistics.mean([x.size for x in early]) if early else 0.0
        late_size = statistics.mean([x.size for x in late]) if late else 0.0
        acc_label, acc_ratio = _accumulation_label(early_size, late_size)
        behavior["accumulation"] = acc_label
        behavior["accumulation_ratio"] = acc_ratio

        by_outcome: Dict[str, float] = {}
        for b in buys:
            by_outcome[b.outcome] = by_outcome.get(b.outcome, 0.0) + b.size
        primary = max(by_outcome.items(), key=lambda kv: kv[1])[0]
        behavior["primary_outcome"] = primary

        pbuys = [b for b in buys if b.outcome == primary]
        if len(pbuys) >= 4:
            half2 = len(pbuys) // 2
            p_early = pbuys[:half2]
            p_late = pbuys[half2:]
            early_p = statistics.mean([x.price for x in p_early]) if p_early else 0.0
            late_p = statistics.mean([x.price for x in p_late]) if p_late else 0.0
            behavior["price_trend"] = _price_trend_label(early_p, late_p)

    buy_notional = sum(r.size * r.price for r in rows if r.side == "BUY")
    sell_notional = sum(r.size * r.price for r in rows if r.side == "SELL")

    classification = "MIXED"
    if profitable_pct >= 80.0 and hedge_status == "ARBITRAGE_CANDIDATE":
        classification = "SNIPER_ARBITRAGE"
    elif profitable_pct >= 80.0:
        classification = "SNIPER_TIMING"
    elif profitable_pct <= 30.0:
        classification = "HIGH_DRAWDOWN_STYLE"

    out = {
        "ok": True,
        "market_title": market_title,
        "wallet": wallet,
        "trade_count": len(rows),
        "outcomes": outcomes,
        "first_trade_ts": rows[0].timestamp,
        "last_trade_ts": rows[-1].timestamp,
        "duration_hours": total_duration_hours,
        "timeline": {
            "time_profitable_pct": profitable_pct,
            "time_profitable_hours": prof_hours,
            "time_unprofitable_pct": unprofitable_pct,
            "time_unprofitable_hours": loss_hours,
        },
        "trading_behavior": {
            "avg_interval_min": avg_interval_min,
            "intensity": intensity,
        },
        "inventory": {
            "avg_prices": avg_prices,
            "qty": qtys,
            "hedge_outcomes": hedge_outcomes,
            "hedgeable_shares": hedgeable_shares,
            "hedge_balance_ratio": hedge_balance_ratio,
            "combined_avg": combined_avg,
            "hedge_edge_pct": hedge_edge_pct,
            "hedge_status": hedge_status,
        },
        "pattern": behavior,
        "notional": {
            "buy_notional": buy_notional,
            "sell_notional": sell_notional,
        },
        "classification": classification,
    }
    return out


def print_report(summary: dict) -> None:
    if not summary.get("ok"):
        print(f"Analysis failed: {summary.get('reason', 'unknown error')}")
        return

    print("=" * 60)
    print("POLYMARKET TRADE AUTOPSY REPORT")
    print("=" * 60)
    if summary.get("market_title"):
        print(f"Market: {summary['market_title']}")
    if summary.get("wallet"):
        print(f"Wallet: {summary['wallet']}")
    print(f"Trades: {summary['trade_count']}")
    print(f"Outcomes: {', '.join(summary.get('outcomes', []))}")
    print(
        f"Period: {fmt_ts(summary['first_trade_ts'])} -> {fmt_ts(summary['last_trade_ts'])} "
        f"({summary['duration_hours']:.2f}h)"
    )
    print()

    tl = summary["timeline"]
    print("PROFITABILITY TIMELINE")
    print(f"Time profitable:   {fmt_pct(tl['time_profitable_pct'])} ({tl['time_profitable_hours']:.2f}h)")
    print(f"Time unprofitable: {fmt_pct(tl['time_unprofitable_pct'])} ({tl['time_unprofitable_hours']:.2f}h)")
    print()

    tb = summary["trading_behavior"]
    print("TRADING BEHAVIOR")
    print(f"Intensity:         {tb['intensity']}")
    print(f"Avg interval:      {tb['avg_interval_min']:.2f} min")
    print()

    inv = summary["inventory"]
    print("CURRENT INVENTORY STATUS")
    avg_prices = inv.get("avg_prices") or {}
    qtys = inv.get("qty") or {}
    for outcome in sorted(avg_prices.keys()):
        print(f"{outcome} avg entry:     {fmt_usd(avg_prices[outcome])} on {qtys.get(outcome, 0.0):.4f} shares")
    if inv.get("combined_avg") is not None:
        print(f"Combined avg:      {fmt_usd(inv['combined_avg'])}")
        hedge_edge_pct = inv.get("hedge_edge_pct")
        if hedge_edge_pct is not None:
            print(f"Hedge edge:        {hedge_edge_pct:+.2f}%")
        print(f"Hedgeable shares:  {float(inv.get('hedgeable_shares') or 0.0):.4f}")
        ratio = inv.get("hedge_balance_ratio")
        if ratio is not None:
            print(f"Balance ratio:     {float(ratio):.4f}")
    print(f"Hedge status:      {inv.get('hedge_status', 'UNKNOWN')}")
    print()

    pattern = summary["pattern"]
    print("PATTERN INSIGHTS")
    print(f"Accumulation:      {pattern['accumulation']} ({pattern['accumulation_ratio']:.2f}x late/early)")
    print(f"Primary outcome:   {pattern['primary_outcome'] or '-'}")
    print(f"Price trend:       {pattern['price_trend']}")
    print()

    nt = summary["notional"]
    print("FLOW")
    print(f"Buy notional:      {fmt_usd(nt['buy_notional'])}")
    print(f"Sell notional:     {fmt_usd(nt['sell_notional'])}")
    print()

    print(f"Classification:    {summary.get('classification', 'MIXED')}")


def load_input_trades(path: Path) -> Tuple[dict, List[dict]]:
    with path.open("r", encoding="utf-8") as f:
        obj = json.load(f)
    if isinstance(obj, list):
        return {}, obj
    if isinstance(obj, dict):
        trades = obj.get("trades")
        if isinstance(trades, list):
            return obj.get("meta") if isinstance(obj.get("meta"), dict) else {}, trades
    raise ValueError("Invalid input JSON shape")


def main() -> int:
    p = argparse.ArgumentParser(description="Analyze Polymarket trade JSON (observe-only)")
    p.add_argument("input_json", help="Input JSON file path")
    p.add_argument("--wallet", default="", help="Override wallet label in report")
    p.add_argument("--market-title", default="", help="Override market title in report")
    p.add_argument("--out", default="", help="Optional path to save analysis summary JSON")
    p.add_argument("--pretty", action="store_true", help="Pretty-print summary JSON")
    args = p.parse_args()

    input_path = Path(args.input_json)
    if not input_path.exists():
        print(f"Input JSON not found: {input_path}")
        return 2

    meta, rows = load_input_trades(input_path)
    wallet = args.wallet or str(meta.get("user") or "")
    market_title = args.market_title or str(meta.get("market_title") or "")

    summary = analyze_trades(rows, market_title=market_title, wallet=wallet)
    print_report(summary)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            if args.pretty:
                json.dump(summary, f, indent=2, ensure_ascii=False)
            else:
                json.dump(summary, f, separators=(",", ":"), ensure_ascii=False)
        print()
        print(f"Saved analysis summary: {out_path}")

    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
