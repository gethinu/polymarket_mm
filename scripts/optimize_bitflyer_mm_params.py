#!/usr/bin/env python3
"""
Grid-search parameters for bitFlyer MM observe strategy using recorded metrics.

This script replays top-of-book samples from:
  logs/bitflyer-mm-observe-metrics.jsonl

It never places real orders. Use this to narrow parameter ranges before
running a fresh observe-only session.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


def _parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _parse_float_list(s: str) -> list[float]:
    out: list[float] = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    return out


def _q_down(x: float, tick: float) -> float:
    if tick <= 0:
        return x
    return math.floor(x / tick + 1e-12) * tick


def _q_up(x: float, tick: float) -> float:
    if tick <= 0:
        return x
    return math.ceil(x / tick - 1e-12) * tick


@dataclass(frozen=True)
class Sample:
    ts: dt.datetime
    best_bid_jpy: float
    best_ask_jpy: float
    mid_jpy: float


@dataclass(frozen=True)
class SimResult:
    half_spread_yen: float
    order_size_btc: float
    max_inventory_btc: float
    fills_buy: int
    fills_sell: int
    inventory_btc: float
    avg_entry_jpy: float
    realized_pnl_jpy: float
    unrealized_pnl_jpy: float
    total_pnl_jpy: float
    max_drawdown_jpy: float
    turnover_jpy: float
    score: float


def iter_samples(lines: Iterable[str]) -> Iterable[Sample]:
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        try:
            o = json.loads(line)
            ts_raw = str(o.get("ts") or "").strip()
            if ts_raw:
                ts = _parse_ts(ts_raw)
            else:
                ts_ms = int(o.get("ts_ms") or 0)
                if ts_ms <= 0:
                    continue
                ts = dt.datetime.fromtimestamp(ts_ms / 1000.0)
            best_bid = float(o.get("best_bid_jpy") or 0.0)
            best_ask = float(o.get("best_ask_jpy") or 0.0)
            mid = float(o.get("mid_jpy") or 0.0)
            if mid <= 0 and best_bid > 0 and best_ask > 0 and best_ask >= best_bid:
                mid = (best_bid + best_ask) / 2.0
            if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid or mid <= 0:
                continue
            yield Sample(
                ts=ts,
                best_bid_jpy=best_bid,
                best_ask_jpy=best_ask,
                mid_jpy=mid,
            )
        except Exception:
            continue


def _simulate(
    samples: list[Sample],
    half_spread_yen: float,
    order_size_btc: float,
    max_inventory_btc: float,
    tick_size_jpy: float,
    maker_fee_bps: float,
    dd_penalty: float,
) -> SimResult:
    inv = 0.0
    avg = 0.0
    realized = 0.0
    fills_buy = 0
    fills_sell = 0
    turnover = 0.0

    peak = 0.0
    max_dd = 0.0

    for s in samples:
        bid_quote = _q_down(s.mid_jpy - half_spread_yen, tick_size_jpy)
        ask_quote = _q_up(s.mid_jpy + half_spread_yen, tick_size_jpy)
        if ask_quote <= bid_quote:
            ask_quote = bid_quote + max(1.0, tick_size_jpy)

        if s.best_ask_jpy <= bid_quote and (inv + order_size_btc) <= (max_inventory_btc + 1e-12):
            notional = bid_quote * order_size_btc
            fee = notional * (maker_fee_bps / 10000.0)
            cost_before = avg * inv
            inv += order_size_btc
            avg = (cost_before + notional + fee) / inv if inv > 0 else 0.0
            fills_buy += 1
            turnover += notional

        if s.best_bid_jpy >= ask_quote and inv >= order_size_btc - 1e-12:
            size = min(order_size_btc, inv)
            notional = ask_quote * size
            fee = notional * (maker_fee_bps / 10000.0)
            proceeds = notional - fee
            cost = avg * size
            realized += proceeds - cost
            inv = max(0.0, inv - size)
            if inv <= 0:
                avg = 0.0
            fills_sell += 1
            turnover += notional

        unrealized = (s.mid_jpy - avg) * inv if (inv > 0 and avg > 0) else 0.0
        total = realized + unrealized
        peak = max(peak, total)
        dd = peak - total
        max_dd = max(max_dd, dd)

    last_mid = samples[-1].mid_jpy if samples else 0.0
    unrealized = (last_mid - avg) * inv if (inv > 0 and avg > 0 and last_mid > 0) else 0.0
    total = realized + unrealized
    score = total - (dd_penalty * max_dd)

    return SimResult(
        half_spread_yen=half_spread_yen,
        order_size_btc=order_size_btc,
        max_inventory_btc=max_inventory_btc,
        fills_buy=fills_buy,
        fills_sell=fills_sell,
        inventory_btc=inv,
        avg_entry_jpy=avg,
        realized_pnl_jpy=realized,
        unrealized_pnl_jpy=unrealized,
        total_pnl_jpy=total,
        max_drawdown_jpy=max_dd,
        turnover_jpy=turnover,
        score=score,
    )


def _format_table(rows: list[SimResult]) -> str:
    header = (
        "rank  half(y)  size(btc)  max_inv  fills(b/s)  pnl_total  pnl_realized  dd_max  score  turnover"
    )
    lines = [header]
    for i, r in enumerate(rows, start=1):
        lines.append(
            f"{i:>4d}  "
            f"{r.half_spread_yen:>7.1f}  "
            f"{r.order_size_btc:>9.6f}  "
            f"{r.max_inventory_btc:>7.6f}  "
            f"{r.fills_buy:>4d}/{r.fills_sell:<4d}  "
            f"{r.total_pnl_jpy:>9.1f}  "
            f"{r.realized_pnl_jpy:>12.1f}  "
            f"{r.max_drawdown_jpy:>7.1f}  "
            f"{r.score:>8.1f}  "
            f"{r.turnover_jpy:>8.1f}"
        )
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Optimize bitFlyer MM observe parameters from metrics replay")
    p.add_argument(
        "--metrics-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "bitflyer-mm-observe-metrics.jsonl"),
        help="Path to metrics JSONL",
    )
    p.add_argument("--hours", type=float, default=24.0, help="Lookback window in hours")
    p.add_argument("--since", default="", help='Start timestamp "YYYY-MM-DD HH:MM:SS" (overrides --hours)')
    p.add_argument("--until", default="", help='End timestamp "YYYY-MM-DD HH:MM:SS" (default: now)')
    p.add_argument("--tick-size-jpy", type=float, default=1.0, help="Quote tick size in JPY")
    p.add_argument("--maker-fee-bps", type=float, default=0.0, help="Assumed maker fee in bps")
    p.add_argument("--max-inventory-btc", type=float, default=0.01, help="Max inventory cap for simulation")
    p.add_argument("--half-spreads-yen", default="150,250,350,500,800", help="Comma-separated half spreads (JPY)")
    p.add_argument("--order-sizes-btc", default="0.0005,0.001,0.002", help="Comma-separated order sizes (BTC)")
    p.add_argument("--dd-penalty", type=float, default=0.0, help="Score penalty weight for max drawdown (score=total-dd_penalty*dd)")
    p.add_argument("--top-n", type=int, default=10, help="Show top N candidates")
    p.add_argument("--min-samples", type=int, default=50, help="Minimum samples required")
    args = p.parse_args()

    if not os.path.exists(args.metrics_file):
        print(f"Metrics file not found: {args.metrics_file}")
        return 2

    now = dt.datetime.now()
    until: dt.datetime = _parse_ts(args.until) if args.until else now
    since: dt.datetime = _parse_ts(args.since) if args.since else (until - dt.timedelta(hours=float(args.hours)))

    with open(args.metrics_file, "r", encoding="utf-8", errors="replace") as f:
        samples = [s for s in iter_samples(f) if since <= s.ts <= until]

    if len(samples) < int(args.min_samples):
        print(
            f"Not enough samples in window: {len(samples)} < {int(args.min_samples)} "
            f"(window {since:%Y-%m-%d %H:%M:%S} -> {until:%Y-%m-%d %H:%M:%S})"
        )
        return 3

    half_spreads = _parse_float_list(args.half_spreads_yen)
    order_sizes = _parse_float_list(args.order_sizes_btc)
    if not half_spreads or not order_sizes:
        print("half-spreads-yen or order-sizes-btc is empty.")
        return 4

    results: list[SimResult] = []
    for h in half_spreads:
        if h <= 0:
            continue
        for sz in order_sizes:
            if sz <= 0:
                continue
            results.append(
                _simulate(
                    samples=samples,
                    half_spread_yen=float(h),
                    order_size_btc=float(sz),
                    max_inventory_btc=float(args.max_inventory_btc),
                    tick_size_jpy=float(args.tick_size_jpy),
                    maker_fee_bps=float(args.maker_fee_bps),
                    dd_penalty=float(args.dd_penalty),
                )
            )

    if not results:
        print("No valid parameter candidates.")
        return 5

    ranked = sorted(
        results,
        key=lambda r: (r.score, r.total_pnl_jpy, -r.max_drawdown_jpy, r.turnover_jpy),
        reverse=True,
    )
    top_n = max(1, int(args.top_n))
    top = ranked[:top_n]

    print(f"Window: {since:%Y-%m-%d %H:%M:%S} -> {until:%Y-%m-%d %H:%M:%S} | samples={len(samples)}")
    print(
        "Search: "
        f"half_spreads={len(half_spreads)} order_sizes={len(order_sizes)} "
        f"max_inventory={args.max_inventory_btc} maker_fee_bps={args.maker_fee_bps} dd_penalty={args.dd_penalty}"
    )
    print(_format_table(top))

    totals = [r.total_pnl_jpy for r in results]
    dds = [r.max_drawdown_jpy for r in results]
    print(
        "All candidates: "
        f"count={len(results)} pnl(mean/median/max)={statistics.mean(totals):.1f}/"
        f"{statistics.median(totals):.1f}/{max(totals):.1f} "
        f"dd(mean/max)={statistics.mean(dds):.1f}/{max(dds):.1f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
