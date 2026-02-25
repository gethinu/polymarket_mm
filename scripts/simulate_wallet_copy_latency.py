#!/usr/bin/env python3
"""
Observe-only delayed copy-trade simulator for Polymarket wallet history.

Purpose:
- Estimate how a follower might perform when copying a wallet with latency.
- Use FIFO BUY->SELL roundtrips from historical wallet trades.
- Apply configurable execution penalty by latency bucket.

This script never places orders.

Examples:
  python scripts/simulate_wallet_copy_latency.py 0xWallet
  python scripts/simulate_wallet_copy_latency.py @0x8dxd --max-trades 3000
  python scripts/simulate_wallet_copy_latency.py logs/trades_0xabc_all.json --latency-sec-buckets 0,2,5,10
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Tuple

from fetch_trades import fetch_user_trades, resolve_user_identifier


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def as_float(v, default: float = math.nan) -> float:
    try:
        return float(v)
    except Exception:
        return default


def as_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_output_path(raw_out: str) -> Path:
    logs = repo_root() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if raw_out:
        p = Path(raw_out)
        if p.is_absolute():
            return p
        if len(p.parts) == 1:
            return logs / p.name
        return repo_root() / p
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    return logs / f"wallet_copy_latency_{stamp}.json"


def _load_json_rows(path: Path) -> Tuple[List[dict], dict]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    meta = {}
    rows: List[dict]
    if isinstance(obj, list):
        rows = obj
    elif isinstance(obj, dict):
        meta = dict(obj.get("meta") or {})
        rows_raw = obj.get("trades")
        if isinstance(rows_raw, list):
            rows = rows_raw
        else:
            raise ValueError("Input JSON object must contain trades[]")
    else:
        raise ValueError("Input JSON must be a list or object with trades[]")
    return rows, meta


@dataclass(frozen=True)
class Trade:
    timestamp: int
    side: str
    outcome: str
    price: float
    size: float
    condition_id: str
    slug: str
    title: str


@dataclass
class OpenLot:
    timestamp: int
    price: float
    size_left: float


@dataclass(frozen=True)
class Roundtrip:
    condition_id: str
    slug: str
    title: str
    outcome: str
    entry_ts: int
    exit_ts: int
    hold_sec: float
    size: float
    entry_price: float
    exit_price: float


def normalize_rows(raw_rows: Iterable[dict]) -> List[Trade]:
    out: List[Trade] = []
    for r in raw_rows:
        if not isinstance(r, dict):
            continue
        ts = as_int(r.get("timestamp"), 0)
        side = str(r.get("side") or "").strip().upper()
        outcome = str(r.get("outcome") or "").strip()
        price = as_float(r.get("price"), math.nan)
        size = as_float(r.get("size"), math.nan)
        condition_id = str(r.get("conditionId") or "").strip()
        slug = str(r.get("slug") or "").strip()
        title = str(r.get("title") or "").strip()
        if ts <= 0:
            continue
        if side not in {"BUY", "SELL"}:
            continue
        if not outcome:
            continue
        if not math.isfinite(price) or not math.isfinite(size):
            continue
        if price <= 0.0 or size <= 0.0:
            continue
        out.append(
            Trade(
                timestamp=ts,
                side=side,
                outcome=outcome,
                price=price,
                size=size,
                condition_id=condition_id,
                slug=slug,
                title=title,
            )
        )
    out.sort(key=lambda x: x.timestamp)
    return out


def _group_key(t: Trade) -> Tuple[str, str]:
    cond = t.condition_id if t.condition_id else (t.slug if t.slug else t.title)
    return cond, t.outcome


def build_roundtrips(trades: List[Trade], min_hold_sec: float) -> Tuple[List[Roundtrip], dict]:
    grouped: Dict[Tuple[str, str], List[Trade]] = defaultdict(list)
    for t in trades:
        grouped[_group_key(t)].append(t)

    out: List[Roundtrip] = []
    unmatched_sell_shares = 0.0
    open_shares = 0.0
    for key, rows in grouped.items():
        rows.sort(key=lambda x: x.timestamp)
        lots: Deque[OpenLot] = deque()
        cond_key, outcome = key
        for t in rows:
            if t.side == "BUY":
                lots.append(OpenLot(timestamp=t.timestamp, price=t.price, size_left=t.size))
                open_shares += t.size
                continue

            remain = t.size
            while remain > 1e-12 and lots:
                lot = lots[0]
                take = min(remain, lot.size_left)
                hold_sec = max(0.0, float(t.timestamp - lot.timestamp))
                if hold_sec >= min_hold_sec:
                    out.append(
                        Roundtrip(
                            condition_id=t.condition_id or cond_key,
                            slug=t.slug,
                            title=t.title,
                            outcome=outcome,
                            entry_ts=lot.timestamp,
                            exit_ts=t.timestamp,
                            hold_sec=hold_sec,
                            size=take,
                            entry_price=lot.price,
                            exit_price=t.price,
                        )
                    )
                lot.size_left -= take
                remain -= take
                open_shares -= take
                if lot.size_left <= 1e-12:
                    lots.popleft()
            if remain > 1e-12:
                unmatched_sell_shares += remain

    out.sort(key=lambda x: x.exit_ts)
    meta = {
        "roundtrip_count": len(out),
        "unmatched_sell_shares": unmatched_sell_shares,
        "open_shares_unclosed": max(0.0, open_shares),
    }
    return out, meta


def _clamp01(x: float) -> float:
    if not math.isfinite(x):
        return math.nan
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _adjust_price(price: float, total_bps: float, worse_for_buyer: bool) -> float:
    if not math.isfinite(price):
        return math.nan
    frac = max(0.0, float(total_bps)) / 10000.0
    if worse_for_buyer:
        return _clamp01(price * (1.0 + frac))
    return _clamp01(price * (1.0 - frac))


def _p50(values: List[float]) -> float:
    if not values:
        return 0.0
    return float(statistics.median(values))


def _calc_max_drawdown(curve: List[float]) -> float:
    if not curve:
        return 0.0
    peak = curve[0]
    worst = 0.0
    for v in curve:
        if v > peak:
            peak = v
        dd = v - peak
        if dd < worst:
            worst = dd
    return abs(worst)


def simulate_bucket(
    roundtrips: List[Roundtrip],
    latency_sec: float,
    entry_slippage_bps: float,
    exit_slippage_bps: float,
    per_sec_slippage_bps: float,
    taker_fee_rate: float,
) -> dict:
    total_entry = 0.0
    total_exit = 0.0
    total_fee = 0.0
    total_net = 0.0
    total_gross = 0.0
    wins = 0
    losses = 0
    holds: List[float] = []
    cum = 0.0
    curve: List[float] = []
    traded_markets = set()

    bps_shift = max(0.0, float(per_sec_slippage_bps)) * max(0.0, float(latency_sec))
    for rt in roundtrips:
        in_px = _adjust_price(rt.entry_price, entry_slippage_bps + bps_shift, worse_for_buyer=True)
        out_px = _adjust_price(rt.exit_price, exit_slippage_bps + bps_shift, worse_for_buyer=False)
        if not (math.isfinite(in_px) and math.isfinite(out_px)):
            continue

        entry_notional = rt.size * in_px
        exit_notional = rt.size * out_px
        fee_cost = (entry_notional + exit_notional) * max(0.0, taker_fee_rate)
        gross = exit_notional - entry_notional
        net = gross - fee_cost

        total_entry += entry_notional
        total_exit += exit_notional
        total_fee += fee_cost
        total_gross += gross
        total_net += net
        holds.append(rt.hold_sec)
        traded_markets.add(rt.condition_id or rt.slug or rt.title)
        if net > 1e-12:
            wins += 1
        elif net < -1e-12:
            losses += 1
        cum += net
        curve.append(cum)

    n = len(holds)
    win_rate = (100.0 * wins / n) if n > 0 else 0.0
    roi_pct = (100.0 * total_net / total_entry) if total_entry > 0 else 0.0
    return {
        "latency_sec": float(latency_sec),
        "roundtrip_count": n,
        "market_count": len(traded_markets),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate,
        "total_entry_notional_usd": total_entry,
        "total_exit_notional_usd": total_exit,
        "gross_pnl_usd": total_gross,
        "fee_cost_usd": total_fee,
        "net_pnl_usd": total_net,
        "roi_pct_on_entry_notional": roi_pct,
        "net_pnl_per_roundtrip_usd": (total_net / n) if n > 0 else 0.0,
        "avg_hold_sec": (statistics.mean(holds) if holds else 0.0),
        "p50_hold_sec": _p50(holds),
        "max_drawdown_usd": _calc_max_drawdown(curve),
    }


def parse_latency_buckets(raw: str) -> List[float]:
    out: List[float] = []
    for s in str(raw or "").split(","):
        t = s.strip()
        if not t:
            continue
        try:
            v = float(t)
        except Exception:
            continue
        if not math.isfinite(v) or v < 0:
            continue
        out.append(v)
    uniq = sorted(set(round(x, 6) for x in out))
    return uniq if uniq else [0.0, 1.0, 2.0, 5.0, 10.0]


def print_summary_table(rows: List[dict]) -> None:
    if not rows:
        print("No simulation rows.")
        return
    print("")
    print("COPY LATENCY SIMULATION (observe-only)")
    print("-" * 94)
    print(f"{'lat(s)':>7} {'rt':>6} {'mkts':>6} {'win%':>7} {'net$':>12} {'roi%':>8} {'mdd$':>10} {'avgHold(s)':>11}")
    print("-" * 94)
    for r in rows:
        print(
            f"{r['latency_sec']:>7.1f} "
            f"{int(r['roundtrip_count']):>6d} "
            f"{int(r['market_count']):>6d} "
            f"{r['win_rate_pct']:>7.2f} "
            f"{r['net_pnl_usd']:>12.2f} "
            f"{r['roi_pct_on_entry_notional']:>8.2f} "
            f"{r['max_drawdown_usd']:>10.2f} "
            f"{r['avg_hold_sec']:>11.1f}"
        )


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Observe-only delayed copy simulation from wallet trades")
    p.add_argument(
        "source",
        help="Wallet/@handle/profile URL, or saved trades JSON path",
    )
    p.add_argument("--market", default="", help="Optional conditionId filter when source is a user identifier")
    p.add_argument("--limit", type=int, default=500, help="Data API page size")
    p.add_argument("--max-trades", type=int, default=2000, help="Max trades to fetch")
    p.add_argument("--sleep-sec", type=float, default=0.0, help="Optional sleep between Data API pages")
    p.add_argument(
        "--latency-sec-buckets",
        default="0,1,2,5,10",
        help="Comma-separated latency buckets in seconds",
    )
    p.add_argument("--entry-slippage-bps", type=float, default=5.0, help="Base entry slippage in bps")
    p.add_argument("--exit-slippage-bps", type=float, default=5.0, help="Base exit slippage in bps")
    p.add_argument(
        "--per-sec-slippage-bps",
        type=float,
        default=0.6,
        help="Additional slippage bps per second of latency (applied to entry and exit)",
    )
    p.add_argument("--taker-fee-rate", type=float, default=0.0, help="Optional per-leg taker fee rate")
    p.add_argument("--min-hold-sec", type=float, default=0.0, help="Ignore roundtrips with hold time below this")
    p.add_argument("--out", default="", help="Output JSON path (simple filename goes under logs/)")
    p.add_argument("--pretty", action="store_true", help="Pretty-print output JSON")
    return p


def main() -> int:
    args = build_arg_parser().parse_args()
    source = str(args.source or "").strip()
    if not source:
        print("source is required")
        return 2

    src_path = Path(source)
    raw_rows: List[dict]
    meta_in: dict = {}
    source_meta: dict = {"source_input": source}
    if src_path.exists() and src_path.is_file():
        raw_rows, meta_in = _load_json_rows(src_path)
        source_meta["source_type"] = "file"
        source_meta["source_path"] = str(src_path)
    else:
        wallet, resolve_meta = resolve_user_identifier(source)
        if not wallet:
            print("Could not resolve wallet from source. Use 0xWallet/@handle/profile URL or JSON path.")
            return 2
        raw_rows = fetch_user_trades(
            user=wallet,
            market=str(args.market or "").strip(),
            limit=max(1, min(500, int(args.limit))),
            max_trades=max(1, int(args.max_trades)),
            sleep_sec=max(0.0, float(args.sleep_sec)),
        )
        source_meta.update(
            {
                "source_type": "data_api_fetch",
                "resolved_wallet": wallet,
                "resolve_meta": resolve_meta,
                "fetched_trade_count": len(raw_rows),
                "market_filter": str(args.market or "").strip(),
            }
        )

    trades = normalize_rows(raw_rows)
    if not trades:
        print("No valid trades after normalization.")
        return 1

    roundtrips, pairing_meta = build_roundtrips(trades, min_hold_sec=max(0.0, float(args.min_hold_sec)))
    if not roundtrips:
        print("No BUY->SELL roundtrips found after pairing.")
        return 1

    latencies = parse_latency_buckets(args.latency_sec_buckets)
    rows = []
    for lat in latencies:
        rows.append(
            simulate_bucket(
                roundtrips=roundtrips,
                latency_sec=lat,
                entry_slippage_bps=max(0.0, float(args.entry_slippage_bps)),
                exit_slippage_bps=max(0.0, float(args.exit_slippage_bps)),
                per_sec_slippage_bps=max(0.0, float(args.per_sec_slippage_bps)),
                taker_fee_rate=max(0.0, float(args.taker_fee_rate)),
            )
        )

    print(f"Input normalized trades: {len(trades)}")
    print(
        "Paired roundtrips: "
        f"{pairing_meta.get('roundtrip_count', 0)} "
        f"(unmatched_sell_shares={pairing_meta.get('unmatched_sell_shares', 0.0):.4f}, "
        f"open_shares_unclosed={pairing_meta.get('open_shares_unclosed', 0.0):.4f})"
    )
    print_summary_table(rows)

    out_obj = {
        "meta": {
            "generated_at_utc": now_utc_iso(),
            "observe_only": True,
            "tool": "simulate_wallet_copy_latency.py",
            "source_meta": source_meta,
            "input_meta": meta_in,
            "params": {
                "latency_sec_buckets": latencies,
                "entry_slippage_bps": float(args.entry_slippage_bps),
                "exit_slippage_bps": float(args.exit_slippage_bps),
                "per_sec_slippage_bps": float(args.per_sec_slippage_bps),
                "taker_fee_rate": float(args.taker_fee_rate),
                "min_hold_sec": float(args.min_hold_sec),
            },
            "input_trade_count_raw": len(raw_rows),
            "input_trade_count_normalized": len(trades),
            "pairing_meta": pairing_meta,
        },
        "scenarios": rows,
    }
    out_path = resolve_output_path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(out_obj, ensure_ascii=True, indent=2 if args.pretty else None),
        encoding="utf-8",
    )
    print(f"Saved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
