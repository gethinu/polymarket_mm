#!/usr/bin/env python3
"""
Summarize CLOB MM observation metrics for a time window.

Intended usage (Windows):
  python C:\\Users\\stair\\clawd\\scripts\\report_clob_mm_observation.py --hours 24
  python C:\\Users\\stair\\clawd\\scripts\\report_clob_mm_observation.py --hours 24 --discord

This reads:
- Metrics JSONL (written by polymarket_clob_mm.py): mid/spread samples
- Event log (optional): fill / halt counts
- State JSON (optional): current inventory + last mid + quote update counters
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
from typing import Dict, Iterable, Optional
from urllib.request import Request, urlopen
from pathlib import Path


FILL_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] fill ")
HALT_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] HALT: ")


def _parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _post_json(url: str, payload: dict, timeout_sec: float = 7.0) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "clob-mm-observe-report/1.0",
        },
        method="POST",
    )
    with urlopen(req, timeout=timeout_sec) as _:
        return


def _discord_url() -> str:
    return (os.getenv("CLOBBOT_DISCORD_WEBHOOK_URL") or os.getenv("DISCORD_WEBHOOK_URL") or "").strip()


def _fmt_usd(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):.4f}"


def _mean(xs: list[float]) -> float:
    return float(statistics.mean(xs)) if xs else 0.0


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


@dataclass(frozen=True)
class MetricRow:
    ts: dt.datetime
    ts_ms: int
    token_id: str
    label: str
    best_bid: float
    best_ask: float
    mid: float
    spread: float
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
                token_id=str(o.get("token_id") or "").strip(),
                label=str(o.get("label") or "").strip(),
                best_bid=float(o.get("best_bid") or 0.0),
                best_ask=float(o.get("best_ask") or 0.0),
                mid=float(o.get("mid") or 0.0),
                spread=float(o.get("spread") or 0.0),
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
    out = {"fills": 0, "halts": 0}
    if not log_file or not os.path.exists(log_file):
        return out
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = FILL_RE.match(line)
                if m:
                    ts = _parse_ts(m.group("ts"))
                    if since <= ts <= until:
                        out["fills"] += 1
                    continue
                m = HALT_RE.match(line)
                if m:
                    ts = _parse_ts(m.group("ts"))
                    if since <= ts <= until:
                        out["halts"] += 1
    except Exception:
        return out
    return out


def build_report(
    rows: list[MetricRow],
    state: dict,
    since: dt.datetime,
    until: dt.datetime,
) -> str:
    lines: list[str] = []
    lines.append(f"Window: {since:%Y-%m-%d %H:%M:%S} -> {until:%Y-%m-%d %H:%M:%S} (local)")
    if not rows:
        lines.append("No metrics samples found for the selected window.")
        return "\n".join(lines)

    rows = sorted(rows, key=lambda r: (r.token_id, r.ts_ms))
    tokens = sorted({r.token_id for r in rows})
    lines.append(f"Samples: {len(rows)} | tokens: {len(tokens)}")

    ts0 = min(r.ts for r in rows)
    ts1 = max(r.ts for r in rows)
    lines.append(f"Observed: {ts0:%Y-%m-%d %H:%M:%S} -> {ts1:%Y-%m-%d %H:%M:%S}")

    ts_state = (state.get("token_states") or {}) if isinstance(state, dict) else {}
    day_key = str(state.get("day_key") or "")
    day_anchor = float(state.get("day_pnl_anchor") or 0.0)

    # Per-token stats.
    lines.append("Per token:")
    for tid in tokens:
        rts = [r for r in rows if r.token_id == tid]
        mids = [r.mid for r in rts if r.mid > 0]
        spreads = [r.spread for r in rts if r.spread >= 0]
        label = (rts[-1].label if rts else "").strip()
        s = ts_state.get(tid) or {}
        tick = float(s.get("tick_size") or 0.01)

        mid_changes = 0
        prev_mid: Optional[float] = None
        for r in rts:
            if prev_mid is not None and abs(r.mid - prev_mid) >= max(tick, 1e-9):
                mid_changes += 1
            prev_mid = r.mid

        # Current snapshot from state (if present).
        inv = float(s.get("inventory_shares") or 0.0)
        avg = float(s.get("avg_cost") or 0.0)
        last_mid = float(s.get("last_mid") or 0.0)
        realized = float(s.get("realized_pnl") or 0.0)
        unreal = (last_mid - avg) * inv if inv > 0 and last_mid > 0 else 0.0
        total = realized + unreal

        lines.append(
            f"- {label[:70]} ({tid[:10]}...)\n"
            f"  samples={len(rts)} mid(mean/min/max)={_mean(mids):.4f}/{min(mids) if mids else 0:.4f}/{max(mids) if mids else 0:.4f} "
            f"spread(mean/min/max)={_mean(spreads):.4f}/{min(spreads) if spreads else 0:.4f}/{max(spreads) if spreads else 0:.4f}\n"
            f"  mid_changes(>=1tick)={mid_changes} tick={tick:g} | inv={inv:g} avg={avg:.4f} last_mid={last_mid:.4f} pnl={total:+.4f}"
        )

    # Totals (mark-to-mid based on state, best-effort).
    total_pnl = 0.0
    if isinstance(ts_state, dict):
        for s in ts_state.values():
            inv = float((s or {}).get("inventory_shares") or 0.0)
            avg = float((s or {}).get("avg_cost") or 0.0)
            last_mid = float((s or {}).get("last_mid") or 0.0)
            realized = float((s or {}).get("realized_pnl") or 0.0)
            unreal = (last_mid - avg) * inv if inv > 0 and last_mid > 0 else 0.0
            total_pnl += realized + unreal
    if day_key:
        lines.append(f"State: day_key={day_key} pnl_today~{(total_pnl - day_anchor):+.4f} total_pnl~{total_pnl:+.4f}")
    else:
        lines.append(f"State: total_pnl~{total_pnl:+.4f}")

    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Summarize CLOB MM observation metrics")
    p.add_argument(
        "--metrics-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "clob-mm-metrics.jsonl"),
        help="Path to JSONL metrics file written by polymarket_clob_mm.py",
    )
    p.add_argument(
        "--log-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "clob-mm.log"),
        help="Path to MM event log (for fills/halts counts)",
    )
    p.add_argument(
        "--state-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "clob_mm_state.json"),
        help="Path to MM state JSON (inventory/PnL snapshot)",
    )
    p.add_argument("--hours", type=float, default=24.0, help="Lookback window in hours")
    p.add_argument("--since", default="", help='Start timestamp "YYYY-MM-DD HH:MM:SS" (overrides --hours)')
    p.add_argument("--until", default="", help='End timestamp "YYYY-MM-DD HH:MM:SS" (default: now)')
    p.add_argument("--discord", action="store_true", help="Post the report to Discord webhook (if configured)")
    args = p.parse_args()

    now = dt.datetime.now()
    until: dt.datetime = _parse_ts(args.until) if args.until else now
    since: dt.datetime = _parse_ts(args.since) if args.since else (until - dt.timedelta(hours=float(args.hours)))

    if not os.path.exists(args.metrics_file):
        print(f"Metrics file not found: {args.metrics_file}", file=sys.stderr)
        return 2

    with open(args.metrics_file, "r", encoding="utf-8", errors="replace") as f:
        rows = [r for r in iter_metrics(f) if (since <= r.ts <= until)]

    state = _load_state(args.state_file)
    report = build_report(rows, state, since=since, until=until)

    counts = _count_events(args.log_file, since=since, until=until)
    report2 = report + f"\nEvents: fills={counts['fills']} halts={counts['halts']}"

    _safe_print(report2)

    if args.discord:
        url = _discord_url()
        if not url:
            print("\nDiscord webhook not configured (CLOBBOT_DISCORD_WEBHOOK_URL).", file=sys.stderr)
            return 3
        content = report2
        if len(content) > 1900:
            content = content[:1900] + "\n...(truncated)"
        _post_json(url, {"content": f"```text\n{content}\n```"})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
