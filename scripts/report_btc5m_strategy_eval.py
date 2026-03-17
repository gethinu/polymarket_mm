#!/usr/bin/env python3
"""
Offline evaluation of BTC short-window strategy metrics (observe-only).

Reads metrics JSONL and state JSON files produced by the lag/panic observe
scripts and computes comprehensive statistics including fee-adjusted PnL.

Supported modes:
- `lag`   -> legacy BTC 5m lag observer
- `lag15` -> BTC 15m lag observer
- `panic` -> BTC short-window panic observer

No orders are placed. No network calls are made.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = REPO_ROOT / "logs"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from lib.btc5m_eval_helpers import (
    compute_fee_adjusted_pnl,
    compute_max_drawdown,
    compute_stale_gap_seconds,
    compute_trade_stats,
)


def _as_float(x, d: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else d
    except Exception:
        return d


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def load_metrics(path: Path) -> List[dict]:
    if not path.exists():
        return []
    rows = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        rows.append(obj)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return rows


def extract_trades_from_log(log_path: Path) -> List[dict]:
    """Parse paper ENTER + settle pairs from log file.
    
    Log format:
      [ts] paper ENTER side=DOWN shares=25.00 entry=0.0300 ...
      [ts] settle WIN side=UP entry=0.0600 shares=25.00 ...
    """
    import re
    if not log_path.exists():
        return []

    enter_re = re.compile(
        r"paper ENTER side=(\w+) shares=([\d.]+) "
        r"entry=([\d.]+)"
    )
    settle_re = re.compile(
        r"settle (\w+) side=(\w+) entry=([\d.]+) "
        r"shares=([\d.]+)"
    )

    trades = []
    pending = None
    try:
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                m = enter_re.search(line)
                if m:
                    pending = {
                        "side": m.group(1),
                        "shares": float(m.group(2)),
                        "entry_price": float(m.group(3)),
                    }
                    continue
                m = settle_re.search(line)
                if m:
                    outcome = m.group(1).upper()
                    side = m.group(2)
                    entry = float(m.group(3))
                    shares = float(m.group(4))
                    trade = {
                        "side": side,
                        "shares": shares,
                        "entry_price": entry,
                        "outcome": outcome,
                    }
                    trades.append(trade)
                    pending = None
    except Exception:
        pass
    return trades


def normalize_trade_side_filter(value: str) -> str:
    s = str(value or "").strip().lower()
    if s in {"down", "down-only", "short"}:
        return "down"
    if s in {"up", "up-only", "long"}:
        return "up"
    return "both"


def filter_trades_by_side(trades: List[dict], trade_side: str) -> List[dict]:
    mode = normalize_trade_side_filter(trade_side)
    if mode == "both":
        return list(trades)
    wanted = "UP" if mode == "up" else "DOWN"
    filtered = []
    for trade in trades:
        side = str(trade.get("side") or "").strip().upper()
        if side == wanted:
            filtered.append(trade)
    return filtered


def default_paths(mode: str) -> tuple:
    mode = mode.lower()
    if mode == "panic":
        return (
            LOGS_DIR / "btc5m-panic-observe.log",
            LOGS_DIR / "btc5m_panic_observe_state.json",
            LOGS_DIR / "btc5m-panic-observe-metrics.jsonl",
        )
    if mode == "lag15":
        return (
            LOGS_DIR / "btc15m-lag-observe.log",
            LOGS_DIR / "btc15m_lag_observe_state.json",
            LOGS_DIR / "btc15m-lag-observe-metrics.jsonl",
        )
    else:
        return (
            LOGS_DIR / "btc5m-lag-observe.log",
            LOGS_DIR / "btc5m_lag_observe_state.json",
            LOGS_DIR / "btc5m-lag-observe-metrics.jsonl",
        )


def build_report(
    mode: str,
    state: dict,
    metrics: List[dict],
    trades: List[dict],
    taker_fee_rate: float,
    slippage_cents: float,
    trade_side: str = "both",
) -> dict:
    """Build the offline evaluation report."""
    trade_side = normalize_trade_side_filter(trade_side)
    filtered_trades = filter_trades_by_side(trades, trade_side)
    # Raw state metrics (before fee adjustment)
    raw_pnl = _as_float(state.get("pnl_total_usd"))
    raw_wins = int(state.get("wins") or 0)
    raw_losses = int(state.get("losses") or 0)
    raw_pushes = int(state.get("pushes") or 0)
    raw_closed = int(state.get("trades_closed") or 0)
    raw_wr = (
        raw_wins / max(1, raw_wins + raw_losses)
        if (raw_wins + raw_losses) > 0
        else 0.0
    )

    # Fee-adjusted stats from trade log
    adj_stats = compute_trade_stats(
        filtered_trades, taker_fee_rate=taker_fee_rate,
        slippage_cents=slippage_cents,
    )

    # Stale-data analysis from metrics
    ts_list = [
        int(m.get("ts_ms") or 0)
        for m in metrics if int(m.get("ts_ms") or 0) > 0
    ]
    stale = compute_stale_gap_seconds(ts_list)

    # PnL series from metrics
    pnl_series = [
        _as_float(m.get("pnl_total_usd")) for m in metrics
    ]
    raw_dd = compute_max_drawdown(pnl_series)

    # Opportunity frequency
    if len(ts_list) >= 2:
        span_hours = (max(ts_list) - min(ts_list)) / 3600000.0
    else:
        span_hours = 0.0
    opps_per_day = (
        (len(filtered_trades) / span_hours * 24.0)
        if span_hours > 0 else 0.0
    )

    return {
        "mode": mode,
        "trade_side_filter": trade_side,
        "taker_fee_rate": taker_fee_rate,
        "slippage_cents": slippage_cents,
        "raw_state": {
            "pnl_total_usd": raw_pnl,
            "trades_closed": raw_closed,
            "wins": raw_wins,
            "losses": raw_losses,
            "pushes": raw_pushes,
            "win_rate": raw_wr,
        },
        "fee_adjusted": {
            "trade_count": adj_stats["count"],
            "wins": adj_stats["wins"],
            "losses": adj_stats["losses"],
            "pushes": adj_stats["pushes"],
            "win_rate": adj_stats["win_rate"],
            "gross_pnl": adj_stats["gross_pnl"],
            "net_pnl": adj_stats["net_pnl"],
            "avg_pnl_per_trade": adj_stats["avg_pnl_per_trade"],
            "max_drawdown": adj_stats["max_drawdown"],
        },
        "by_side": adj_stats.get("by_side", {}),
        "by_bucket": adj_stats.get("by_bucket", {}),
        "stale_data": stale,
        "metrics_rows": len(metrics),
        "metrics_span_hours": round(span_hours, 2),
        "raw_pnl_drawdown": raw_dd,
        "opportunity_frequency_per_day": round(opps_per_day, 2),
    }


def format_text_report(r: dict) -> str:
    lines = []
    lines.append(f"=== BTC {r['mode'].upper()} Strategy Evaluation ===")
    lines.append(f"Fee: {r['taker_fee_rate']*100:.1f}%  "
                 f"Slippage: {r['slippage_cents']:.1f}c")
    if r.get("trade_side_filter", "both") != "both":
        lines.append(f"Trade filter: {str(r.get('trade_side_filter')).upper()} only")
    lines.append("")

    raw = r["raw_state"]
    lines.append("RAW STATE (no fee adjustment):")
    lines.append(f"  PnL: ${raw['pnl_total_usd']:+.2f}  "
                 f"W/L/P: {raw['wins']}/{raw['losses']}/{raw['pushes']}  "
                 f"WR: {raw['win_rate']*100:.1f}%")
    lines.append("")

    adj = r["fee_adjusted"]
    lines.append(f"FEE-ADJUSTED ({r['taker_fee_rate']*100:.0f}% + "
                 f"{r['slippage_cents']}c slip):")
    lines.append(f"  Gross PnL: ${adj['gross_pnl']:+.2f}")
    lines.append(f"  Net PnL:   ${adj['net_pnl']:+.2f}")
    lines.append(f"  Avg/trade: ${adj['avg_pnl_per_trade']:+.4f}")
    lines.append(f"  W/L/P: {adj['wins']}/{adj['losses']}/{adj['pushes']}  "
                 f"WR: {adj['win_rate']*100:.1f}%")
    lines.append(f"  Max DD:    ${adj['max_drawdown']:.2f}")
    lines.append("")

    lines.append("BY SIDE:")
    for side, data in sorted(r.get("by_side", {}).items()):
        lines.append(f"  {side}: N={data['count']}  "
                     f"WR={data.get('win_rate', 0)*100:.1f}%  "
                     f"PnL=${data['net_pnl']:+.2f}")

    lines.append("")
    lines.append("BY ENTRY BUCKET:")
    for bucket, data in sorted(r.get("by_bucket", {}).items()):
        lines.append(f"  {bucket}: N={data['count']}  "
                     f"WR={data.get('win_rate', 0)*100:.1f}%  "
                     f"PnL=${data['net_pnl']:+.2f}")

    lines.append("")
    stale = r.get("stale_data", {})
    lines.append(f"DATA QUALITY: "
                 f"rows={r['metrics_rows']}  "
                 f"span={r['metrics_span_hours']:.1f}h  "
                 f"max_gap={stale.get('max_gap_sec', 0):.0f}s  "
                 f"stale_gaps={stale.get('stale_count', 0)}")
    lines.append(f"OPPORTUNITY FREQ: "
                 f"{r['opportunity_frequency_per_day']:.1f} trades/day")

    # Judgement
    lines.append("")
    lines.append("--- JUDGEMENT ---")
    net = adj["net_pnl"]
    count = adj["trade_count"]
    wr = adj["win_rate"]
    if count < 20:
        lines.append("INSUFFICIENT DATA: <20 fee-adjusted trades. "
                      "Continue observing.")
    elif net < 0 and wr < 0.35:
        lines.append(f"REJECT: Net PnL ${net:+.2f} with "
                      f"{wr*100:.1f}% WR over {count} trades. "
                      "No evidence of edge.")
    elif net > 0 and wr > 0.25:
        lines.append(f"PROMISING: Net PnL ${net:+.2f} with "
                      f"{wr*100:.1f}% WR. "
                      "Continue observing, tighten filters.")
    else:
        lines.append(f"MARGINAL: Net PnL ${net:+.2f}, "
                      f"WR {wr*100:.1f}%. "
                      "Needs more data or parameter tuning.")

    return "\n".join(lines)


def parse_args():
    p = argparse.ArgumentParser(
        description="Offline BTC strategy evaluation (observe-only)"
    )
    p.add_argument(
        "--mode", choices=("lag", "lag15", "panic"),
        default="panic", help="Strategy to evaluate"
    )
    p.add_argument(
        "--taker-fee-rate", type=float, default=0.02,
        help="Taker fee rate for adjustment"
    )
    p.add_argument(
        "--slippage-cents", type=float, default=0.5,
        help="Slippage per share in cents"
    )
    p.add_argument(
        "--trade-side", choices=("both", "down", "up"),
        default="both",
        help="Optional side filter for fee-adjusted trade stats"
    )
    p.add_argument(
        "--log-file", default="",
        help="Log file path"
    )
    p.add_argument(
        "--state-file", default="",
        help="State JSON path"
    )
    p.add_argument(
        "--metrics-file", default="",
        help="Metrics JSONL path"
    )
    p.add_argument(
        "--out-json", default="",
        help="Output JSON path"
    )
    p.add_argument("--pretty", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    d_log, d_state, d_metrics = default_paths(args.mode)
    log_path = Path(args.log_file) if args.log_file else d_log
    state_path = Path(args.state_file) if args.state_file else d_state
    metrics_path = (
        Path(args.metrics_file) if args.metrics_file else d_metrics
    )

    state = load_state(state_path)
    metrics = load_metrics(metrics_path)
    trades = extract_trades_from_log(log_path)

    report = build_report(
        mode=args.mode,
        state=state,
        metrics=metrics,
        trades=trades,
        taker_fee_rate=float(args.taker_fee_rate),
        slippage_cents=float(args.slippage_cents),
        trade_side=str(args.trade_side),
    )

    text = format_text_report(report)
    print(text)

    out_path = (
        Path(args.out_json) if args.out_json
        else LOGS_DIR / (
            "btc15m_strategy_eval_latest.json"
            if args.mode == "lag15"
            else "btc5m_strategy_eval_latest.json"
        )
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        if args.pretty:
            json.dump(report, f, indent=2, ensure_ascii=False)
        else:
            json.dump(report, f, ensure_ascii=False)
        f.write("\n")
    print(f"\nSaved: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
