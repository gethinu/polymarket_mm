#!/usr/bin/env python3
"""
Summarize Simmer ping-pong observation for a time window.

This reads (best-effort):
- Metrics JSONL (written by simmer_pingpong_mm.py): p_yes/targets/inventory samples
- Event log (optional): would BUY/SELL, fills, halts, errors
- State JSON (optional): current inventory + avg cost + realized pnl
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.request import Request, urlopen


TS_RE = r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
WOULD_BUY_RE = re.compile(rf"^\[{TS_RE}\] would BUY ")
WOULD_SELL_RE = re.compile(rf"^\[{TS_RE}\] would SELL ")
FILL_BUY_RE = re.compile(rf"^\[{TS_RE}\] FILL BUY ")
FILL_SELL_RE = re.compile(rf"^\[{TS_RE}\] FILL SELL ")
HALT_RE = re.compile(rf"^\[{TS_RE}\] HALT: ")
ERROR_RE = re.compile(rf"^\[{TS_RE}\] error: ")


def _parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _safe_print(s: str) -> None:
    # Avoid crashing on Windows consoles with legacy encodings (cp932, etc.).
    try:
        print(s)
    except UnicodeEncodeError:
        try:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(s.encode(enc, errors="replace").decode(enc, errors="replace"))
        except Exception:
            pass


def _post_json(url: str, payload: dict, timeout_sec: float = 7.0) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "simmer-pingpong-observe-report/1.0",
        },
        method="POST",
    )
    with urlopen(req, timeout=timeout_sec) as _:
        return


def _discord_url() -> str:
    return (os.getenv("CLOBBOT_DISCORD_WEBHOOK_URL") or os.getenv("DISCORD_WEBHOOK_URL") or "").strip()


def _mean(xs: list[float]) -> float:
    return float(statistics.mean(xs)) if xs else 0.0


@dataclass(frozen=True)
class MetricRow:
    ts: dt.datetime
    ts_ms: int
    market_id: str
    label: str
    p_yes: float
    buy_target: float
    sell_target: float
    inv: float


def iter_metrics(lines: Iterable[str]) -> Iterable[MetricRow]:
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        try:
            o = json.loads(line)
            ts = _parse_ts(str(o.get("ts") or ""))
            yield MetricRow(
                ts=ts,
                ts_ms=int(o.get("ts_ms") or int(ts.timestamp() * 1000.0)),
                market_id=str(o.get("market_id") or "").strip(),
                label=str(o.get("label") or "").strip(),
                p_yes=float(o.get("p_yes") or 0.0),
                buy_target=float(o.get("buy_target") or 0.0),
                sell_target=float(o.get("sell_target") or 0.0),
                inv=float(o.get("inv") or 0.0),
            )
        except Exception:
            continue


def _load_state(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return {}


def _count_events(log_file: str, since: dt.datetime, until: dt.datetime) -> dict:
    out = {
        "would_buys": 0,
        "would_sells": 0,
        "fill_buys": 0,
        "fill_sells": 0,
        "halts": 0,
        "errors": 0,
    }
    if not log_file or not os.path.exists(log_file):
        return out
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = WOULD_BUY_RE.match(line)
                if m:
                    ts = _parse_ts(m.group("ts"))
                    if since <= ts <= until:
                        out["would_buys"] += 1
                    continue
                m = WOULD_SELL_RE.match(line)
                if m:
                    ts = _parse_ts(m.group("ts"))
                    if since <= ts <= until:
                        out["would_sells"] += 1
                    continue
                m = FILL_BUY_RE.match(line)
                if m:
                    ts = _parse_ts(m.group("ts"))
                    if since <= ts <= until:
                        out["fill_buys"] += 1
                    continue
                m = FILL_SELL_RE.match(line)
                if m:
                    ts = _parse_ts(m.group("ts"))
                    if since <= ts <= until:
                        out["fill_sells"] += 1
                    continue
                m = HALT_RE.match(line)
                if m:
                    ts = _parse_ts(m.group("ts"))
                    if since <= ts <= until:
                        out["halts"] += 1
                    continue
                m = ERROR_RE.match(line)
                if m:
                    ts = _parse_ts(m.group("ts"))
                    if since <= ts <= until:
                        out["errors"] += 1
                    continue
    except Exception:
        return out
    return out


def _compute_total_pnl_from_state(state: dict) -> float:
    total = 0.0
    ms = state.get("market_states") if isinstance(state, dict) else None
    if not isinstance(ms, dict):
        return 0.0
    for s in ms.values():
        if not isinstance(s, dict):
            continue
        p = float(s.get("last_price_yes") or 0.0)
        inv = float(s.get("inventory_yes_shares") or 0.0)
        avg = float(s.get("avg_cost") or 0.0)
        realized = float(s.get("realized_pnl") or 0.0)
        unreal = (p - avg) * inv if (inv > 0 and p > 0 and avg > 0) else 0.0
        total += realized + unreal
    return float(total)


def build_report(rows: list[MetricRow], state: dict, counts: dict, since: dt.datetime, until: dt.datetime) -> str:
    lines: list[str] = []
    lines.append(f"Window: {since:%Y-%m-%d %H:%M:%S} -> {until:%Y-%m-%d %H:%M:%S} (local)")

    # Metrics overview (optional).
    if rows:
        rows = sorted(rows, key=lambda r: (r.market_id, r.ts_ms))
        markets = sorted({r.market_id for r in rows if r.market_id})
        lines.append(f"Samples: {len(rows)} | markets: {len(markets)}")
        ts0 = min(r.ts for r in rows)
        ts1 = max(r.ts for r in rows)
        lines.append(f"Observed: {ts0:%Y-%m-%d %H:%M:%S} -> {ts1:%Y-%m-%d %H:%M:%S}")
    else:
        lines.append("Samples: 0 (metrics missing or disabled)")

    # State snapshot (best-effort).
    day_key = str(state.get("day_key") or "") if isinstance(state, dict) else ""
    day_anchor = float(state.get("day_pnl_anchor") or 0.0) if isinstance(state, dict) else 0.0
    halted = bool(state.get("halted", False)) if isinstance(state, dict) else False
    halt_reason = str(state.get("halt_reason") or "") if isinstance(state, dict) else ""

    total_pnl = _compute_total_pnl_from_state(state)
    if day_key:
        lines.append(f"State: halted={halted} pnl_today~{(total_pnl - day_anchor):+.4f} total_pnl~{total_pnl:+.4f} day_key={day_key}")
    else:
        lines.append(f"State: halted={halted} total_pnl~{total_pnl:+.4f}")
    if halted and halt_reason:
        lines.append(f"Halt reason: {halt_reason}")

    # Per-market section.
    ms = state.get("market_states") if isinstance(state, dict) else None
    ms = ms if isinstance(ms, dict) else {}
    if rows or ms:
        lines.append("Per market:")
        market_ids = sorted(set([r.market_id for r in rows if r.market_id] + list(ms.keys())))
        for mid in market_ids:
            rts = [r for r in rows if r.market_id == mid]
            label = ""
            if rts:
                label = (rts[-1].label or "").strip()
            if not label:
                label = str((ms.get(mid) or {}).get("label") or "").strip()
            label = label or f"market:{mid}"

            ps = [r.p_yes for r in rts if 0.0 < r.p_yes < 1.0]
            buy_touches = sum(1 for r in rts if 0.0 < r.p_yes < 1.0 and r.buy_target > 0 and r.p_yes <= r.buy_target)
            sell_touches = sum(1 for r in rts if 0.0 < r.p_yes < 1.0 and r.sell_target > 0 and r.p_yes >= r.sell_target)

            s = ms.get(mid) if isinstance(ms.get(mid), dict) else {}
            inv = float(s.get("inventory_yes_shares") or 0.0)
            avg = float(s.get("avg_cost") or 0.0)
            realized = float(s.get("realized_pnl") or 0.0)
            last_p = float(s.get("last_price_yes") or 0.0)
            unreal = (last_p - avg) * inv if (inv > 0 and last_p > 0 and avg > 0) else 0.0
            total = realized + unreal
            buy_trades = int(s.get("buy_trades") or 0)
            sell_trades = int(s.get("sell_trades") or 0)

            if ps:
                lines.append(
                    f"- {label[:70]} ({mid[:8]}...)\n"
                    f"  samples={len(rts)} p_yes(mean/min/max)={_mean(ps):.4f}/{min(ps):.4f}/{max(ps):.4f} "
                    f"touches(buy/sell)={buy_touches}/{sell_touches}\n"
                    f"  inv={inv:g} avg={avg:.4f} last_p={last_p:.4f} pnl={total:+.4f} trades(buy/sell)={buy_trades}/{sell_trades}"
                )
            else:
                lines.append(
                    f"- {label[:70]} ({mid[:8]}...)\n"
                    f"  samples={len(rts)} touches(buy/sell)={buy_touches}/{sell_touches}\n"
                    f"  inv={inv:g} avg={avg:.4f} last_p={last_p:.4f} pnl={total:+.4f} trades(buy/sell)={buy_trades}/{sell_trades}"
                )

    lines.append(
        "Events: "
        f"would_buy={int(counts.get('would_buys') or 0)} "
        f"would_sell={int(counts.get('would_sells') or 0)} "
        f"fills_buy={int(counts.get('fill_buys') or 0)} "
        f"fills_sell={int(counts.get('fill_sells') or 0)} "
        f"halts={int(counts.get('halts') or 0)} "
        f"errors={int(counts.get('errors') or 0)}"
    )
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Summarize Simmer ping-pong observation")
    p.add_argument(
        "--metrics-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "simmer-pingpong-metrics.jsonl"),
        help="Path to JSONL metrics file written by simmer_pingpong_mm.py (optional)",
    )
    p.add_argument(
        "--log-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "simmer-pingpong.log"),
        help="Path to bot event log (optional)",
    )
    p.add_argument(
        "--state-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "simmer_pingpong_state.json"),
        help="Path to bot state JSON (optional)",
    )
    p.add_argument("--hours", type=float, default=24.0, help="Lookback window in hours")
    p.add_argument("--since", default="", help='Start timestamp "YYYY-MM-DD HH:MM:SS" (overrides --hours)')
    p.add_argument("--until", default="", help='End timestamp "YYYY-MM-DD HH:MM:SS" (default: now)')
    p.add_argument("--discord", action="store_true", help="Post the report to Discord webhook (if configured)")
    args = p.parse_args()

    now = dt.datetime.now()
    until: dt.datetime = _parse_ts(args.until) if args.until else now
    since: dt.datetime = _parse_ts(args.since) if args.since else (until - dt.timedelta(hours=float(args.hours)))

    rows: list[MetricRow] = []
    if args.metrics_file and os.path.exists(args.metrics_file):
        with open(args.metrics_file, "r", encoding="utf-8", errors="replace") as f:
            rows = [r for r in iter_metrics(f) if (since <= r.ts <= until)]

    state = _load_state(args.state_file)
    counts = _count_events(args.log_file, since=since, until=until)
    report = build_report(rows, state, counts=counts, since=since, until=until)

    _safe_print(report)

    if args.discord:
        url = _discord_url()
        if not url:
            print("\nDiscord webhook not configured (CLOBBOT_DISCORD_WEBHOOK_URL).", file=sys.stderr)
            return 3
        content = report
        if len(content) > 1900:
            content = content[:1900] + "\n...(truncated)"
        _post_json(url, {"content": f"```text\n{content}\n```"})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

