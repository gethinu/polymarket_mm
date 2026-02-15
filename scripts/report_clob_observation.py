#!/usr/bin/env python3
"""
Summarize clob-arb monitor "summary(XXs)" log lines for a time window.

Intended usage (Windows):
  python C:\\Users\\stair\\clawd\\scripts\\report_clob_observation.py --hours 24
  python C:\\Users\\stair\\clawd\\scripts\\report_clob_observation.py --hours 24 --discord
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
from typing import Iterable, Optional
from urllib.request import Request, urlopen
from pathlib import Path


SUMMARY_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] "
    r"summary\((?P<window>\d+)s\): "
    r"candidates=(?P<candidates>\d+) \| "
    r"EDGE \$?(?P<edge>-?\d+(?:\.\d+)?) "
    r"\((?P<edge_pct>-?\d+(?:\.\d+)?)%\) \| "
    r"cost \$?(?P<cost>\d+(?:\.\d+)?) \| "
    r"payout \$?(?P<payout>\d+(?:\.\d+)?) \| "
    r"legs=(?P<legs>\d+) \| "
    r"(?P<title>.*)$"
)


def _parse_ts(s: str) -> dt.datetime:
    # Log timestamps are local time with no TZ.
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _fmt_usd(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):.4f}"


def _post_json(url: str, payload: dict, timeout_sec: float = 7.0) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "clob-observe-report/1.0",
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


@dataclass(frozen=True)
class SummaryRow:
    ts: dt.datetime
    window_sec: int
    candidates: int
    edge: float
    edge_pct: float
    cost: float
    payout: float
    legs: int
    title: str


def iter_rows(lines: Iterable[str]) -> Iterable[SummaryRow]:
    for line in lines:
        m = SUMMARY_RE.match(line.strip())
        if not m:
            continue
        try:
            yield SummaryRow(
                ts=_parse_ts(m.group("ts")),
                window_sec=int(m.group("window")),
                candidates=int(m.group("candidates")),
                edge=float(m.group("edge")),
                edge_pct=float(m.group("edge_pct")) / 100.0,
                cost=float(m.group("cost")),
                payout=float(m.group("payout")),
                legs=int(m.group("legs")),
                title=m.group("title"),
            )
        except Exception:
            continue


def _thresholds_from_arg(s: str) -> list[float]:
    out: list[float] = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part) / 100.0)  # cents -> USD
    return out


def build_report(rows: list[SummaryRow], thresholds_usd: list[float]) -> str:
    if not rows:
        return "No summary lines found for the selected window."

    rows = sorted(rows, key=lambda r: r.ts)
    edges = [r.edge for r in rows]
    candidates = [r.candidates for r in rows]

    ts0 = rows[0].ts
    ts1 = rows[-1].ts
    window_sec = rows[-1].window_sec
    expected = int((ts1 - ts0).total_seconds() / max(window_sec, 1))

    best = max(rows, key=lambda r: r.edge)
    worst = min(rows, key=lambda r: r.edge)

    med = statistics.median(edges)
    p95 = statistics.quantiles(edges, n=20)[-1] if len(edges) >= 20 else max(edges)
    p05 = statistics.quantiles(edges, n=20)[0] if len(edges) >= 20 else min(edges)

    lines: list[str] = []
    lines.append(f"Window: {ts0:%Y-%m-%d %H:%M:%S} -> {ts1:%Y-%m-%d %H:%M:%S} (local)")
    lines.append(f"Samples: {len(rows)} (expected~{expected}, summary_every={window_sec}s)")
    lines.append(
        "Edge($): "
        f"min {_fmt_usd(worst.edge)} | p05 {_fmt_usd(p05)} | median {_fmt_usd(med)} | "
        f"p95 {_fmt_usd(p95)} | max {_fmt_usd(best.edge)}"
    )
    lines.append(f"Candidates/min: avg {statistics.mean(candidates):.0f} | max {max(candidates)}")
    if thresholds_usd:
        lines.append("Opportunities (best edge per minute):")
        for t in thresholds_usd:
            hit = sum(1 for e in edges if e >= t)
            pct = (hit / len(edges)) if edges else 0.0
            lines.append(f"  >= {_fmt_usd(t)} : {hit} / {len(edges)} ({pct:.1%})")

    lines.append("Best sample:")
    lines.append(f"  {best.ts:%Y-%m-%d %H:%M:%S} edge={_fmt_usd(best.edge)} cost=${best.cost:.4f} payout=${best.payout:.4f}")
    lines.append(f"  {best.title[:180]}")

    lines.append("Worst sample:")
    lines.append(f"  {worst.ts:%Y-%m-%d %H:%M:%S} edge={_fmt_usd(worst.edge)} cost=${worst.cost:.4f} payout=${worst.payout:.4f}")
    lines.append(f"  {worst.title[:180]}")

    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Summarize clob-arb monitor observation logs")
    p.add_argument(
        "--log-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "clob-arb-monitor.log"),
        help="Path to clob-arb monitor log file",
    )
    p.add_argument("--hours", type=float, default=24.0, help="Lookback window in hours")
    p.add_argument("--since", default="", help='Start timestamp "YYYY-MM-DD HH:MM:SS" (overrides --hours)')
    p.add_argument("--until", default="", help='End timestamp "YYYY-MM-DD HH:MM:SS" (default: now)')
    p.add_argument(
        "--thresholds-cents",
        default="0,1,2,3,5,10",
        help="Comma-separated thresholds in cents for hit counts (best edge per minute)",
    )
    p.add_argument("--discord", action="store_true", help="Post the report to Discord webhook (if configured)")
    args = p.parse_args()

    if not os.path.exists(args.log_file):
        print(f"Log file not found: {args.log_file}", file=sys.stderr)
        return 2

    now = dt.datetime.now()
    until: dt.datetime = _parse_ts(args.until) if args.until else now
    since: dt.datetime
    if args.since:
        since = _parse_ts(args.since)
    else:
        since = until - dt.timedelta(hours=float(args.hours))

    thresholds_usd = _thresholds_from_arg(args.thresholds_cents)

    with open(args.log_file, "r", encoding="utf-8", errors="replace") as f:
        rows = [r for r in iter_rows(f) if (since <= r.ts <= until)]

    report = build_report(rows, thresholds_usd)
    print(report)

    if args.discord:
        url = _discord_url()
        if not url:
            print("\nDiscord webhook not configured (CLOBBOT_DISCORD_WEBHOOK_URL).", file=sys.stderr)
            return 3
        # Keep under Discord 2000 char limit.
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
