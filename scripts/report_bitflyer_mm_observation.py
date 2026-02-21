#!/usr/bin/env python3
"""
Summarize bitFlyer MM observe simulation for a time window.

This reads (best-effort):
- Metrics JSONL (written by bitflyer_mm_observe.py)
- Event log (optional): simulated fills, halts, errors
- State JSON (optional): current inventory + pnl snapshot
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
FILL_BUY_RE = re.compile(rf"^\[{TS_RE}\] fill BUY ")
FILL_SELL_RE = re.compile(rf"^\[{TS_RE}\] fill SELL ")
HALT_RE = re.compile(rf"^\[{TS_RE}\] HALT: ")
WARN_RE = re.compile(rf"^\[{TS_RE}\] warn: ")
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
            "User-Agent": "bitflyer-mm-observe-report/1.0",
        },
        method="POST",
    )
    with urlopen(req, timeout=timeout_sec) as _:
        return


def _user_env_from_registry(name: str) -> str:
    """Best-effort read of HKCU\\Environment for cases where process env isn't populated (Task Scheduler quirks)."""
    if not sys.platform.startswith("win"):
        return ""
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            v, _t = winreg.QueryValueEx(k, name)
            return str(v or "").strip()
    except Exception:
        return ""


def _discord_url() -> str:
    return (
        os.getenv("CLOBBOT_DISCORD_WEBHOOK_URL")
        or _user_env_from_registry("CLOBBOT_DISCORD_WEBHOOK_URL")
        or os.getenv("DISCORD_WEBHOOK_URL")
        or _user_env_from_registry("DISCORD_WEBHOOK_URL")
        or ""
    ).strip()


def _mean(xs: list[float]) -> float:
    return float(statistics.mean(xs)) if xs else 0.0


def _p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    if len(xs) < 20:
        return float(max(xs))
    return float(statistics.quantiles(xs, n=20)[-1])


@dataclass(frozen=True)
class MetricRow:
    ts: dt.datetime
    ts_ms: int
    product_code: str
    best_bid_jpy: float
    best_ask_jpy: float
    mid_jpy: float
    spread_jpy: float
    quote_bid_jpy: float
    quote_ask_jpy: float
    inventory_btc: float
    avg_entry_jpy: float
    realized_pnl_jpy: float
    total_pnl_jpy: float
    day_pnl_jpy: float
    fills_buy: int
    fills_sell: int
    halted: bool


def iter_metrics(lines: Iterable[str]) -> Iterable[MetricRow]:
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        try:
            o = json.loads(line)
            ts_raw = str(o.get("ts") or "").strip()
            if ts_raw:
                ts = _parse_ts(ts_raw)
                ts_ms = int(o.get("ts_ms") or int(ts.timestamp() * 1000.0))
            else:
                ts_ms = int(o.get("ts_ms") or 0)
                if ts_ms <= 0:
                    continue
                ts = dt.datetime.fromtimestamp(ts_ms / 1000.0)
            yield MetricRow(
                ts=ts,
                ts_ms=ts_ms,
                product_code=str(o.get("product_code") or "").strip(),
                best_bid_jpy=float(o.get("best_bid_jpy") or 0.0),
                best_ask_jpy=float(o.get("best_ask_jpy") or 0.0),
                mid_jpy=float(o.get("mid_jpy") or 0.0),
                spread_jpy=float(o.get("spread_jpy") or 0.0),
                quote_bid_jpy=float(o.get("quote_bid_jpy") or 0.0),
                quote_ask_jpy=float(o.get("quote_ask_jpy") or 0.0),
                inventory_btc=float(o.get("inventory_btc") or 0.0),
                avg_entry_jpy=float(o.get("avg_entry_jpy") or 0.0),
                realized_pnl_jpy=float(o.get("realized_pnl_jpy") or 0.0),
                total_pnl_jpy=float(o.get("total_pnl_jpy") or 0.0),
                day_pnl_jpy=float(o.get("day_pnl_jpy") or 0.0),
                fills_buy=int(o.get("fills_buy") or 0),
                fills_sell=int(o.get("fills_sell") or 0),
                halted=bool(o.get("halted") or False),
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
    out = {"fill_buys": 0, "fill_sells": 0, "halts": 0, "warns": 0, "errors": 0}
    if not log_file or not os.path.exists(log_file):
        return out
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
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
                m = WARN_RE.match(line)
                if m:
                    ts = _parse_ts(m.group("ts"))
                    if since <= ts <= until:
                        out["warns"] += 1
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


def _state_total_pnl_jpy(state: dict) -> float:
    inv = float(state.get("inventory_btc") or 0.0)
    avg = float(state.get("avg_entry_jpy") or 0.0)
    last_mid = float(state.get("last_mid_jpy") or 0.0)
    realized = float(state.get("realized_pnl_jpy") or 0.0)
    unrealized = (last_mid - avg) * inv if (inv > 0 and avg > 0 and last_mid > 0) else 0.0
    return float(realized + unrealized)


def build_report(rows: list[MetricRow], state: dict, counts: dict, since: dt.datetime, until: dt.datetime) -> str:
    lines: list[str] = []
    lines.append(f"Window: {since:%Y-%m-%d %H:%M:%S} -> {until:%Y-%m-%d %H:%M:%S} (local)")

    if rows:
        rows = sorted(rows, key=lambda r: r.ts_ms)
        ts0 = rows[0].ts
        ts1 = rows[-1].ts
        products = sorted({r.product_code for r in rows if r.product_code})
        mids = [r.mid_jpy for r in rows if r.mid_jpy > 0]
        spreads = [r.spread_jpy for r in rows if r.spread_jpy >= 0]

        buy_touches = sum(1 for r in rows if r.quote_bid_jpy > 0 and r.best_ask_jpy > 0 and r.best_ask_jpy <= r.quote_bid_jpy)
        sell_touches = sum(1 for r in rows if r.quote_ask_jpy > 0 and r.best_bid_jpy > 0 and r.best_bid_jpy >= r.quote_ask_jpy)
        half_spreads: list[float] = []
        quote_crossed = 0
        for r in rows:
            if r.mid_jpy <= 0 or r.quote_bid_jpy <= 0 or r.quote_ask_jpy <= 0 or r.quote_ask_jpy <= r.quote_bid_jpy:
                continue
            d_bid = r.mid_jpy - r.quote_bid_jpy
            d_ask = r.quote_ask_jpy - r.mid_jpy
            if d_bid < 0 or d_ask < 0:
                quote_crossed += 1
                continue
            half_spreads.append(min(d_bid, d_ask))

        lines.append(f"Samples: {len(rows)} | products: {','.join(products) if products else '-'}")
        lines.append(f"Observed: {ts0:%Y-%m-%d %H:%M:%S} -> {ts1:%Y-%m-%d %H:%M:%S}")
        lines.append(
            "Market: "
            f"mid(mean/min/max)={_mean(mids):.1f}/{min(mids) if mids else 0:.1f}/{max(mids) if mids else 0:.1f} "
            f"spread(mean/p95/max)={_mean(spreads):.1f}/{_p95(spreads):.1f}/{max(spreads) if spreads else 0:.1f} JPY"
        )
        lines.append(
            "Quote/touch: "
            f"half_spread(mean)={_mean(half_spreads):.1f} JPY "
            f"buy_touch={buy_touches} sell_touch={sell_touches} quote_crossed={quote_crossed}"
        )

        latest = rows[-1]
        lines.append(
            "Latest(metrics): "
            f"inv={latest.inventory_btc:.6f} BTC avg={latest.avg_entry_jpy:.1f} "
            f"realized={latest.realized_pnl_jpy:+.1f} total={latest.total_pnl_jpy:+.1f} "
            f"day={latest.day_pnl_jpy:+.1f} fills={latest.fills_buy + latest.fills_sell} halted={latest.halted}"
        )
    else:
        lines.append("Samples: 0 (metrics missing or disabled)")

    if state:
        day_key = str(state.get("day_key") or "")
        day_anchor = float(state.get("day_anchor_total_pnl_jpy") or 0.0)
        total = _state_total_pnl_jpy(state)
        day = total - day_anchor if day_key else 0.0
        lines.append(
            "State: "
            f"inv={float(state.get('inventory_btc') or 0.0):.6f} BTC "
            f"avg={float(state.get('avg_entry_jpy') or 0.0):.1f} "
            f"realized={float(state.get('realized_pnl_jpy') or 0.0):+.1f} "
            f"total={total:+.1f}"
            + (f" day={day:+.1f} day_key={day_key}" if day_key else "")
            + f" halted={bool(state.get('halted') or False)}"
        )
        halt_reason = str(state.get("halt_reason") or "")
        if bool(state.get("halted") or False) and halt_reason:
            lines.append(f"Halt reason: {halt_reason}")
    else:
        lines.append("State: not found")

    lines.append(
        "Events(log): "
        f"fills_buy={counts.get('fill_buys', 0)} fills_sell={counts.get('fill_sells', 0)} "
        f"halts={counts.get('halts', 0)} warns={counts.get('warns', 0)} errors={counts.get('errors', 0)}"
    )
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Summarize bitFlyer MM observe metrics/log/state")
    p.add_argument(
        "--metrics-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "bitflyer-mm-observe-metrics.jsonl"),
        help="Path to metrics JSONL",
    )
    p.add_argument(
        "--state-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "bitflyer_mm_observe_state.json"),
        help="Path to state JSON",
    )
    p.add_argument(
        "--log-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "bitflyer-mm-observe.log"),
        help="Path to log file",
    )
    p.add_argument("--hours", type=float, default=24.0, help="Lookback window in hours")
    p.add_argument("--since", default="", help='Start timestamp "YYYY-MM-DD HH:MM:SS" (overrides --hours)')
    p.add_argument("--until", default="", help='End timestamp "YYYY-MM-DD HH:MM:SS" (default: now)')
    p.add_argument("--discord", action="store_true", help="Post report to Discord webhook (if configured)")
    args = p.parse_args()

    now = dt.datetime.now()
    until: dt.datetime = _parse_ts(args.until) if args.until else now
    since: dt.datetime = _parse_ts(args.since) if args.since else (until - dt.timedelta(hours=float(args.hours)))

    rows: list[MetricRow] = []
    if os.path.exists(args.metrics_file):
        with open(args.metrics_file, "r", encoding="utf-8", errors="replace") as f:
            rows = [r for r in iter_metrics(f) if (since <= r.ts <= until)]

    state = _load_state(args.state_file)
    counts = _count_events(args.log_file, since, until)

    report = build_report(rows, state, counts, since, until)
    _safe_print(report)

    if args.discord:
        url = _discord_url()
        if not url:
            print("\nDiscord webhook not configured (CLOBBOT_DISCORD_WEBHOOK_URL).", file=sys.stderr)
            return 3
        content = report
        if len(content) > 1900:
            content = content[:1900] + "\n...(truncated)"
        try:
            _post_json(url, {"content": f"```text\n{content}\n```"})
        except Exception as e:
            code = getattr(e, "code", None)
            if isinstance(code, int):
                print(f"\nDiscord post failed: HTTP {code}", file=sys.stderr)
            else:
                print(f"\nDiscord post failed: {type(e).__name__}", file=sys.stderr)
            return 4

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
