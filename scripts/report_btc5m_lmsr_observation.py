#!/usr/bin/env python3
"""
Summarize BTC 5m LMSR observe signals for a time window.

This reads metrics JSONL written by:
  scripts/polymarket_btc5m_lmsr_observe.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.request import Request, urlopen


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
            "User-Agent": "btc5m-lmsr-observe-report/1.0",
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


def _median(xs: list[float]) -> float:
    return float(statistics.median(xs)) if xs else 0.0


def _p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    if len(xs) < 20:
        return float(max(xs))
    return float(statistics.quantiles(xs, n=20)[-1])


def _thresholds_from_arg(s: str) -> list[float]:
    out: list[float] = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    return out


@dataclass(frozen=True)
class SignalRow:
    ts: dt.datetime
    event_slug: str
    market_id: str
    title: str
    side: str
    side_label: str
    edge: float
    p_obs: float
    p_post: float
    ask_price: float
    kelly_full: float
    kelly_fractional: float
    stake_usd: float
    shares: float
    time_to_end_sec: float


def iter_metrics(lines: Iterable[str]) -> Iterable[SignalRow]:
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        try:
            o = json.loads(line)
            ts = _parse_ts(str(o.get("ts") or ""))
            tte = o.get("time_to_end_sec")
            tte_f = float(tte) if tte is not None else float("nan")
            yield SignalRow(
                ts=ts,
                event_slug=str(o.get("event_slug") or "").strip(),
                market_id=str(o.get("market_id") or "").strip(),
                title=str(o.get("title") or "").strip(),
                side=str(o.get("side") or "").strip(),
                side_label=str(o.get("side_label") or "").strip(),
                edge=float(o.get("edge") or 0.0),
                p_obs=float(o.get("p_obs") or 0.0),
                p_post=float(o.get("p_post") or 0.0),
                ask_price=float(o.get("ask_price") or 0.0),
                kelly_full=float(o.get("kelly_full") or 0.0),
                kelly_fractional=float(o.get("kelly_fractional") or 0.0),
                stake_usd=float(o.get("stake_usd") or 0.0),
                shares=float(o.get("shares") or 0.0),
                time_to_end_sec=tte_f,
            )
        except Exception:
            continue


def build_report(rows: list[SignalRow], since: dt.datetime, until: dt.datetime, thresholds_cents: list[float]) -> str:
    lines: list[str] = []
    lines.append(f"Window: {since:%Y-%m-%d %H:%M:%S} -> {until:%Y-%m-%d %H:%M:%S} (local)")

    if not rows:
        lines.append("No signal rows found for the selected window.")
        return "\n".join(lines)

    rows = sorted(rows, key=lambda r: r.ts)
    ts0 = rows[0].ts
    ts1 = rows[-1].ts
    lines.append(f"Signals: {len(rows)}")
    lines.append(f"Observed: {ts0:%Y-%m-%d %H:%M:%S} -> {ts1:%Y-%m-%d %H:%M:%S}")

    unique_slugs = len({r.event_slug for r in rows if r.event_slug})
    unique_markets = len({r.market_id for r in rows if r.market_id})
    side_a = sum(1 for r in rows if r.side.upper() == "A")
    side_b = sum(1 for r in rows if r.side.upper() == "B")
    lines.append(f"Coverage: slugs={unique_slugs} markets={unique_markets} sideA={side_a} sideB={side_b}")

    edges_c = [r.edge * 100.0 for r in rows]
    stakes = [r.stake_usd for r in rows if r.stake_usd > 0]
    shares = [r.shares for r in rows if r.shares > 0]
    kelly_f = [r.kelly_fractional for r in rows if r.kelly_fractional >= 0]
    ttes = [r.time_to_end_sec for r in rows if r.time_to_end_sec == r.time_to_end_sec and r.time_to_end_sec >= 0]
    prices = [r.ask_price for r in rows if r.ask_price > 0]

    lines.append(
        "Edge(c): "
        f"mean={_mean(edges_c):+.3f} median={_median(edges_c):+.3f} "
        f"p95={_p95(edges_c):+.3f} max={max(edges_c):+.3f}"
    )
    lines.append(
        "Sizing: "
        f"stake(mean/median/max)={_mean(stakes):.2f}/{_median(stakes):.2f}/{max(stakes) if stakes else 0.0:.2f} USD "
        f"shares(mean/median/max)={_mean(shares):.2f}/{_median(shares):.2f}/{max(shares) if shares else 0.0:.2f}"
    )
    lines.append(
        "Model: "
        f"kelly_frac(mean/median/p95)={_mean(kelly_f):.4f}/{_median(kelly_f):.4f}/{_p95(kelly_f):.4f} "
        f"ask(mean)={_mean(prices):.4f}"
    )
    if ttes:
        lines.append(
            "Time-to-end(sec): "
            f"mean={_mean(ttes):.1f} median={_median(ttes):.1f} min={min(ttes):.1f} max={max(ttes):.1f}"
        )

    if thresholds_cents:
        lines.append("Edge threshold hits:")
        total = len(edges_c)
        for t in thresholds_cents:
            hit = sum(1 for x in edges_c if x >= float(t))
            pct = (hit / total) if total > 0 else 0.0
            lines.append(f"  >= {t:.2f}c : {hit} / {total} ({pct:.1%})")

    best = max(rows, key=lambda r: r.edge)
    lines.append("Best signal:")
    lines.append(
        f"  {best.ts:%Y-%m-%d %H:%M:%S} edge={best.edge * 100.0:+.3f}c "
        f"side={best.side_label} stake=${best.stake_usd:.2f} price={best.ask_price:.4f}"
    )
    lines.append(f"  {best.title[:180]}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Summarize BTC 5m LMSR observe metrics")
    p.add_argument(
        "--metrics-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "btc5m-lmsr-observe-metrics.jsonl"),
        help="Path to JSONL metrics file written by polymarket_btc5m_lmsr_observe.py",
    )
    p.add_argument("--hours", type=float, default=24.0, help="Lookback window in hours")
    p.add_argument("--since", default="", help='Start timestamp "YYYY-MM-DD HH:MM:SS" (overrides --hours)')
    p.add_argument("--until", default="", help='End timestamp "YYYY-MM-DD HH:MM:SS" (default: now)')
    p.add_argument(
        "--thresholds-cents",
        default="0.6,1.0,1.5,2.0",
        help="Comma-separated edge thresholds in cents",
    )
    p.add_argument("--discord", action="store_true", help="Post the report to Discord webhook (if configured)")
    args = p.parse_args()

    if not os.path.exists(args.metrics_file):
        print(f"Metrics file not found: {args.metrics_file}", file=sys.stderr)
        return 2

    now = dt.datetime.now()
    until: dt.datetime = _parse_ts(args.until) if args.until else now
    since: dt.datetime = _parse_ts(args.since) if args.since else (until - dt.timedelta(hours=float(args.hours)))
    thresholds_cents = _thresholds_from_arg(args.thresholds_cents)

    with open(args.metrics_file, "r", encoding="utf-8", errors="replace") as f:
        rows = [r for r in iter_metrics(f) if (since <= r.ts <= until)]

    report = build_report(rows, since=since, until=until, thresholds_cents=thresholds_cents)
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

