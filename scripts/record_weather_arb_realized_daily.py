#!/usr/bin/env python3
"""
Observe-only forward realized-PnL tracker for the WEATHER CLOB basket-arb strategy.

This is the *correct* realized ledger for `weather_clob_arb_buckets_observe`. It
reads the strategy's own metrics stream (logs/clob-arb-monitor-metrics.jsonl),
books a paper basket position for each exec-threshold-passing opportunity, and
realizes PnL only when the underlying event actually settles on Gamma.

It deliberately does NOT read the Simmer account snapshot
(logs/clob_arb_realized_daily.jsonl). Sourcing weather realized PnL from that
account balance is the misattribution that produced the phantom "+53.72%"
headline (see materialize_strategy_realized_daily.py guard).

Basket-arb economics: we buy every mutually-exclusive leg, so the payout is
deterministic once the event resolves ( = payout_after_fee for the single
winning bucket ). Realized PnL for a settled basket is therefore the exec-based
edge locked at entry:  payout_after_fee - basket_cost_exec_est.

Outputs (all observe-only, no orders):
- logs/weather_arb_forward_positions.json
- logs/weather_arb_paper_realized_daily.jsonl
- logs/weather_arb_realized_latest.json
- logs/weather_arb_monthly_return_latest.txt

Nothing here places trades or moves funds. Read-only Gamma queries + local files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path
from typing import Dict, List, Optional

# Reuse battle-tested helpers from the mimic recorder (HTTP, gate, monthly math).
import record_weather_mimic_realized_daily as mimic

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
DEFAULT_STRATEGY_ID = "weather_clob_arb_buckets_observe"
DEFAULT_METRICS_FILE = "clob-arb-monitor-metrics.jsonl"
DEFAULT_ASSUMED_BANKROLL_USD = 60.0


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def as_str(v) -> str:
    return str(v or "").strip()


def as_float(v, default: Optional[float] = None) -> Optional[float]:
    try:
        n = float(v)
    except Exception:
        return default
    if not math.isfinite(n):
        return default
    return n


def iter_metric_rows(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def ingest_exec_candidates(
    positions: Dict[str, dict],
    metrics_path: Path,
    universe: str,
    strategy: str,
    require_exec_threshold: bool,
    min_exec_edge_usd: float,
) -> int:
    """Book one paper basket position per event_key at its first qualifying sighting.

    Qualifying = right universe/strategy, passes exec threshold, positive exec edge,
    and carries settlement metadata (leg_condition_ids) so it can actually resolve.
    """
    added = 0
    for row in iter_metric_rows(metrics_path):
        if as_str(row.get("universe")).lower() != universe.lower():
            continue
        if as_str(row.get("strategy")).lower() != strategy.lower():
            continue
        if require_exec_threshold and not bool(row.get("passes_exec_threshold")):
            continue
        exec_edge = as_float(row.get("net_edge_exec_est"), None)
        if exec_edge is None or exec_edge < float(min_exec_edge_usd):
            continue

        event_key = as_str(row.get("event_key"))
        if not event_key or event_key in positions:
            continue

        cond_ids = row.get("leg_condition_ids")
        cond_ids = [as_str(c) for c in cond_ids if as_str(c)] if isinstance(cond_ids, list) else []
        if not cond_ids:
            # Older metrics rows lack settlement metadata; they can never resolve,
            # so we do not book a phantom open position for them.
            continue

        payout = as_float(row.get("payout_after_fee"), None)
        basket_cost = as_float(row.get("basket_cost_exec_est"), None)
        if payout is None or basket_cost is None:
            continue
        locked_pnl = float(payout) - float(basket_cost)
        if locked_pnl <= 0.0:
            continue

        entry_ts = now_utc()
        positions[event_key] = {
            "position_id": event_key,
            "event_key": event_key,
            "title": as_str(row.get("title")),
            "strategy_id": DEFAULT_STRATEGY_ID,
            "entry_utc": entry_ts.isoformat(),
            "entry_day": entry_ts.date().isoformat(),
            "entry_metric_ts": as_str(row.get("ts")),
            "leg_count": int(row.get("leg_count") or len(cond_ids)),
            "shares_per_leg": float(as_float(row.get("shares_per_leg"), 0.0) or 0.0),
            "payout_after_fee": float(payout),
            "basket_cost_exec_est": float(basket_cost),
            "net_edge_exec_est_at_entry": float(exec_edge),
            "locked_pnl_usd": float(locked_pnl),
            "leg_condition_ids": cond_ids,
            "end_ms": (int(row["end_ms"]) if row.get("end_ms") else None),
            "status": "open",
            "resolved_utc": None,
            "resolved_day": None,
            "resolution": None,
            "winners": None,
            "realized_pnl_usd": None,
            "realized_cost_basis_usd": None,
        }
        added += 1
    return added


def fetch_market_by_condition(cond_id: str, timeout_sec: float) -> Optional[dict]:
    cid = as_str(cond_id)
    if not cid:
        return None
    data = mimic.fetch_json(
        f"{GAMMA_API_BASE}/markets?condition_ids={cid}", timeout_sec=timeout_sec, retries=3
    )
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


def try_resolve_basket(pos: dict, timeout_sec: float, win_threshold: float) -> bool:
    """Settle a paper basket once ALL member markets are closed on Gamma.

    Realizes the exec-edge locked at entry when exactly one bucket won (the normal
    MECE weather case). If zero buckets won (void / all-NO), conservatively books
    the loss of the paid basket premium.
    """
    if as_str(pos.get("status")) != "open":
        return False
    cond_ids = pos.get("leg_condition_ids") or []
    if not cond_ids:
        return False

    winners = 0
    closed_all = True
    for cid in cond_ids:
        market = fetch_market_by_condition(cid, timeout_sec=timeout_sec)
        if market is None:
            closed_all = False
            break
        if not bool(market.get("closed")):
            closed_all = False
            break
        yn = mimic.extract_yes_no_prices(market)
        if yn is not None:
            yes_price, _no_price = yn
            if yes_price >= win_threshold:
                winners += 1

    if not closed_all:
        return False

    payout = float(as_float(pos.get("payout_after_fee"), 0.0) or 0.0)
    basket_cost = float(as_float(pos.get("basket_cost_exec_est"), 0.0) or 0.0)
    if winners >= 1:
        realized = payout - basket_cost
        resolution = "WIN" if realized > 0 else "FLAT"
    else:
        # No bucket paid out (void/refund/all-NO): lose the premium paid.
        realized = -basket_cost
        resolution = "VOID_OR_LOSS"

    now = now_utc()
    pos["status"] = "resolved"
    pos["resolved_utc"] = now.isoformat()
    pos["resolved_day"] = now.date().isoformat()
    pos["resolution"] = resolution
    pos["winners"] = int(winners)
    pos["realized_pnl_usd"] = float(realized)
    pos["realized_cost_basis_usd"] = float(basket_cost)
    return True


def aggregate_daily(positions: List[dict], assumed_bankroll_usd: float) -> Dict[str, dict]:
    by_day: Dict[str, dict] = {}
    for p in positions:
        if as_str(p.get("status")) != "resolved":
            continue
        day = as_str(p.get("resolved_day"))
        pnl = as_float(p.get("realized_pnl_usd"), None)
        cost = as_float(p.get("realized_cost_basis_usd"), None)
        if not day or pnl is None or cost is None:
            continue
        rec = by_day.setdefault(
            day,
            {
                "day": day,
                "strategy_id": DEFAULT_STRATEGY_ID,
                "observe_only": True,
                "source": "record_weather_arb_realized_daily.py",
                "series_mode": "daily_realized",
                "realized_pnl_usd": 0.0,
                "realized_cost_basis_usd": 0.0,
                "resolved_trades": 0,
                "bankroll_usd": float(assumed_bankroll_usd),
            },
        )
        rec["realized_pnl_usd"] = float(rec["realized_pnl_usd"]) + float(pnl)
        rec["realized_cost_basis_usd"] = float(rec["realized_cost_basis_usd"]) + float(cost)
        rec["resolved_trades"] = int(rec["resolved_trades"]) + 1

    for rec in by_day.values():
        pnl = float(rec.get("realized_pnl_usd") or 0.0)
        cost = float(rec.get("realized_cost_basis_usd") or 0.0)
        bank = float(rec.get("bankroll_usd") or 0.0)
        rec["realized_return_pct"] = (pnl / cost) if cost > 1e-12 else None
        rec["bankroll_return_pct"] = (pnl / bank) if bank > 1e-12 else None
    return by_day


def main() -> int:
    p = argparse.ArgumentParser(
        description="Observe-only paper realized tracker for weather CLOB basket-arb."
    )
    p.add_argument("--metrics-file", default="", help=f"Arb metrics jsonl (default logs/{DEFAULT_METRICS_FILE})")
    p.add_argument("--positions-json", default="", help="Forward ledger (default logs/weather_arb_forward_positions.json)")
    p.add_argument("--out-daily-jsonl", default="", help="Daily realized jsonl (default logs/weather_arb_paper_realized_daily.jsonl)")
    p.add_argument("--out-latest-json", default="", help="Latest summary json (default logs/weather_arb_realized_latest.json)")
    p.add_argument("--out-monthly-txt", default="", help="Monthly return txt (default logs/weather_arb_monthly_return_latest.txt)")
    p.add_argument("--universe", default="weather")
    p.add_argument("--strategy", default="buckets")
    p.add_argument("--min-exec-edge-usd", type=float, default=0.0, help="Only book positions with net_edge_exec_est >= this")
    p.add_argument("--allow-below-exec-threshold", action="store_true", help="Book even if passes_exec_threshold is false (diagnostic; default off)")
    p.add_argument("--assumed-bankroll-usd", type=float, default=DEFAULT_ASSUMED_BANKROLL_USD)
    p.add_argument("--min-realized-days", type=int, default=30)
    p.add_argument("--win-threshold", type=float, default=0.99)
    p.add_argument("--api-timeout-sec", type=float, default=20.0)
    p.add_argument("--no-resolve", action="store_true", help="Skip Gamma settlement pass (ingest only)")
    p.add_argument("--pretty", action="store_true")
    args = p.parse_args()

    metrics_path = mimic.resolve_path(str(args.metrics_file), DEFAULT_METRICS_FILE)
    positions_json = mimic.resolve_path(str(args.positions_json), "weather_arb_forward_positions.json")
    out_daily = mimic.resolve_path(str(args.out_daily_jsonl), "weather_arb_paper_realized_daily.jsonl")
    out_latest = mimic.resolve_path(str(args.out_latest_json), "weather_arb_realized_latest.json")
    out_monthly = mimic.resolve_path(str(args.out_monthly_txt), "weather_arb_monthly_return_latest.txt")

    existing, ledger_err = mimic.load_positions_ledger(positions_json)
    if ledger_err:
        print(f"[weather-arb-realized] error: {ledger_err}")
        return 2
    positions: Dict[str, dict] = {}
    for row in existing:
        k = as_str(row.get("event_key")) or as_str(row.get("position_id"))
        if k:
            positions[k] = row

    added = ingest_exec_candidates(
        positions=positions,
        metrics_path=metrics_path,
        universe=str(args.universe),
        strategy=str(args.strategy),
        require_exec_threshold=(not bool(args.allow_below_exec_threshold)),
        min_exec_edge_usd=float(args.min_exec_edge_usd),
    )

    resolved_now = 0
    if not bool(args.no_resolve):
        for pos in positions.values():
            if try_resolve_basket(
                pos=pos,
                timeout_sec=max(1.0, float(args.api_timeout_sec)),
                win_threshold=max(0.5, min(1.0, float(args.win_threshold))),
            ):
                resolved_now += 1

    pos_list = sorted(positions.values(), key=lambda x: as_str(x.get("entry_utc")))
    mimic.write_json(
        positions_json,
        {
            "generated_utc": now_utc().isoformat(),
            "meta": {
                "observe_only": True,
                "source": "scripts/record_weather_arb_realized_daily.py",
                "strategy_id": DEFAULT_STRATEGY_ID,
                "metrics_file": str(metrics_path),
            },
            "positions": pos_list,
        },
        pretty=True,
    )

    rows_by_day = aggregate_daily(pos_list, assumed_bankroll_usd=max(0.0, float(args.assumed_bankroll_usd)))
    mimic.write_jsonl_sorted(out_daily, rows_by_day)

    metrics = mimic.compute_metrics(rows_by_day=rows_by_day, positions=pos_list)
    realized_gate = mimic.build_realized_30d_gate(
        rows_by_day=rows_by_day, strategy_id=DEFAULT_STRATEGY_ID, min_days=max(1, int(args.min_realized_days))
    )
    realized_monthly = mimic.summarize_realized_monthly_return(
        rows_by_day=rows_by_day, strategy_id=DEFAULT_STRATEGY_ID, min_days=max(1, int(args.min_realized_days))
    )

    open_positions = sum(1 for x in pos_list if as_str(x.get("status")) == "open")
    resolved_positions = sum(1 for x in pos_list if as_str(x.get("status")) == "resolved")
    summary = {
        "generated_utc": now_utc().isoformat(),
        "meta": {
            "observe_only": True,
            "source": "scripts/record_weather_arb_realized_daily.py",
            "strategy_id": DEFAULT_STRATEGY_ID,
            "metrics_file": str(metrics_path),
        },
        "counts": {
            "positions_added": int(added),
            "positions_resolved_now": int(resolved_now),
            "positions_total": len(pos_list),
            "open_positions": int(open_positions),
            "resolved_positions": int(resolved_positions),
        },
        "metrics": metrics,
        "realized_30d_gate": realized_gate,
        "realized_monthly_return": realized_monthly,
        "artifacts": {
            "metrics_file": str(metrics_path),
            "positions_json": str(positions_json),
            "daily_jsonl": str(out_daily),
            "latest_json": str(out_latest),
            "monthly_txt": str(out_monthly),
        },
    }
    mimic.write_json(out_latest, summary, pretty=bool(args.pretty))
    mimic.write_monthly_txt(out_monthly, realized_monthly)

    print(
        f"[weather-arb-realized] added={added} resolved_now={resolved_now} "
        f"open={open_positions} resolved_total={resolved_positions}"
    )
    print(
        f"[weather-arb-realized] gate={realized_gate.get('decision_3stage')} "
        f"observed_days={realized_gate.get('observed_realized_days')} "
        f"total_realized_usd={realized_gate.get('observed_total_realized_pnl_usd')}"
    )
    print(f"[weather-arb-realized] out_daily_jsonl={out_daily}")
    print(f"[weather-arb-realized] out_latest_json={out_latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
