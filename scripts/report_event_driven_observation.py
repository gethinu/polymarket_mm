#!/usr/bin/env python3
"""
Summarize event-driven observe signals/metrics for a selected time window.
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
    try:
        print(s)
    except UnicodeEncodeError:
        try:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(s.encode(enc, errors="replace").decode(enc, errors="replace"))
        except Exception:
            pass


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
        t = part.strip()
        if not t:
            continue
        out.append(float(t))
    return out


def _post_json(url: str, payload: dict, timeout_sec: float = 7.0) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "event-driven-observe-report/1.0",
        },
        method="POST",
    )
    with urlopen(req, timeout=timeout_sec) as _:
        return


def _user_env_from_registry(name: str) -> str:
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
class SignalRow:
    ts: dt.datetime
    run_id: str
    market_id: str
    event_slug: str
    question: str
    event_class: str
    side: str
    selected_price: float
    p_market_yes: float
    p_model_yes: float
    edge_cents: float
    kelly_fractional: float
    suggested_stake_usd: float
    confidence: float
    fragility: float
    ambiguity: float
    liquidity_num: float
    volume_24h: float
    days_to_end: float


@dataclass(frozen=True)
class MetricRow:
    ts: dt.datetime
    run_id: str
    scanned: int
    binary_count: int
    eligible_count: int
    event_count: int
    candidate_count: int
    top_written: int
    runtime_sec: float


def iter_signals(lines: Iterable[str]) -> Iterable[SignalRow]:
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        try:
            o = json.loads(line)
            ts = _parse_ts(str(o.get("ts") or ""))
            dte_raw = o.get("days_to_end")
            dte = float(dte_raw) if dte_raw is not None else float("nan")
            yield SignalRow(
                ts=ts,
                run_id=str(o.get("run_id") or "").strip(),
                market_id=str(o.get("market_id") or "").strip(),
                event_slug=str(o.get("event_slug") or "").strip(),
                question=str(o.get("question") or "").strip(),
                event_class=str(o.get("event_class") or "").strip(),
                side=str(o.get("side") or "").strip(),
                selected_price=float(o.get("selected_price") or 0.0),
                p_market_yes=float(o.get("p_market_yes") or 0.0),
                p_model_yes=float(o.get("p_model_yes") or 0.0),
                edge_cents=float(o.get("edge_cents") or 0.0),
                kelly_fractional=float(o.get("kelly_fractional") or 0.0),
                suggested_stake_usd=float(o.get("suggested_stake_usd") or 0.0),
                confidence=float(o.get("confidence") or 0.0),
                fragility=float(o.get("fragility") or 0.0),
                ambiguity=float(o.get("ambiguity") or 0.0),
                liquidity_num=float(o.get("liquidity_num") or 0.0),
                volume_24h=float(o.get("volume_24h") or 0.0),
                days_to_end=dte,
            )
        except Exception:
            continue


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
                run_id=str(o.get("run_id") or "").strip(),
                scanned=int(o.get("scanned") or 0),
                binary_count=int(o.get("binary_count") or 0),
                eligible_count=int(o.get("eligible_count") or 0),
                event_count=int(o.get("event_count") or 0),
                candidate_count=int(o.get("candidate_count") or 0),
                top_written=int(o.get("top_written") or 0),
                runtime_sec=float(o.get("runtime_sec") or 0.0),
            )
        except Exception:
            continue


def _fmt_class_counts(rows: list[SignalRow], top_n: int = 6) -> str:
    buckets: dict[str, int] = {}
    for r in rows:
        key = r.event_class or "unclassified"
        buckets[key] = buckets.get(key, 0) + 1
    ranked = sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = ranked[: max(1, top_n)]
    return ", ".join([f"{k}:{v}" for k, v in shown]) if shown else "n/a"


def build_report(
    signals: list[SignalRow],
    metrics: list[MetricRow],
    since: dt.datetime,
    until: dt.datetime,
    thresholds_cents: list[float],
) -> str:
    lines: list[str] = []
    lines.append(f"Window: {since:%Y-%m-%d %H:%M:%S} -> {until:%Y-%m-%d %H:%M:%S} (local)")

    if metrics:
        metrics = sorted(metrics, key=lambda x: x.ts)
        lines.append(
            "Runs: "
            f"{len(metrics)} | scanned(sum/avg)={sum(x.scanned for x in metrics)}/{_mean([float(x.scanned) for x in metrics]):.1f} "
            f"eligible(avg)={_mean([float(x.eligible_count) for x in metrics]):.1f} "
            f"event_matched(avg)={_mean([float(x.event_count) for x in metrics]):.1f} "
            f"candidates(avg)={_mean([float(x.candidate_count) for x in metrics]):.1f} "
            f"runtime(avg)={_mean([x.runtime_sec for x in metrics]):.2f}s"
        )
    else:
        lines.append("Runs: no metrics rows in selected window.")

    if not signals:
        lines.append("Signals: no signal rows in selected window.")
        return "\n".join(lines)

    signals = sorted(signals, key=lambda x: x.ts)
    lines.append(f"Signals: {len(signals)}")
    lines.append(f"Observed: {signals[0].ts:%Y-%m-%d %H:%M:%S} -> {signals[-1].ts:%Y-%m-%d %H:%M:%S}")

    unique_runs = len({r.run_id for r in signals if r.run_id})
    unique_markets = len({r.market_id for r in signals if r.market_id})
    unique_events = len({r.event_slug for r in signals if r.event_slug})
    yes_count = sum(1 for r in signals if r.side.upper() == "YES")
    no_count = sum(1 for r in signals if r.side.upper() == "NO")
    lines.append(
        f"Coverage: runs={unique_runs} markets={unique_markets} events={unique_events} side_yes={yes_count} side_no={no_count}"
    )
    lines.append(f"Classes: {_fmt_class_counts(signals)}")

    edges = [r.edge_cents for r in signals]
    prices = [r.selected_price for r in signals if r.selected_price > 0.0]
    conf = [r.confidence for r in signals]
    kelly = [r.kelly_fractional for r in signals]
    stakes = [r.suggested_stake_usd for r in signals if r.suggested_stake_usd > 0.0]
    liq = [r.liquidity_num for r in signals if r.liquidity_num > 0]
    vol = [r.volume_24h for r in signals if r.volume_24h >= 0]
    dte = [r.days_to_end for r in signals if r.days_to_end == r.days_to_end]

    lines.append(
        "Edge(c): "
        f"mean={_mean(edges):+.2f} median={_median(edges):+.2f} p95={_p95(edges):+.2f} max={max(edges):+.2f}"
    )
    lines.append(
        "Model: "
        f"confidence(mean/median)={_mean(conf):.3f}/{_median(conf):.3f} "
        f"kelly(mean/median/p95)={_mean(kelly):.4f}/{_median(kelly):.4f}/{_p95(kelly):.4f}"
    )
    lines.append(
        "Execution proxy: "
        f"price(mean/median)={_mean(prices):.3f}/{_median(prices):.3f} "
        f"stake(mean/median/max)={_mean(stakes):.2f}/{_median(stakes):.2f}/{max(stakes) if stakes else 0.0:.2f} USD"
    )
    lines.append(
        "Market quality: "
        f"liq(mean/median)={_mean(liq):.0f}/{_median(liq):.0f} "
        f"vol24h(mean/median)={_mean(vol):.0f}/{_median(vol):.0f} "
        f"dte(mean/median)={_mean(dte):.1f}/{_median(dte):.1f}d"
    )

    if thresholds_cents:
        lines.append("Edge threshold hits:")
        total = len(edges)
        for t in thresholds_cents:
            hit = sum(1 for x in edges if x >= float(t))
            pct = (hit / total) if total > 0 else 0.0
            lines.append(f"  >= {t:.2f}c : {hit} / {total} ({pct:.1%})")

    best = sorted(signals, key=lambda x: x.edge_cents, reverse=True)[:3]
    lines.append("Top signals:")
    for i, row in enumerate(best, start=1):
        lines.append(
            f"  {i}. {row.ts:%Y-%m-%d %H:%M:%S} {row.side} edge={row.edge_cents:+.2f}c "
            f"class={row.event_class or 'unclassified'} conf={row.confidence:.2f} "
            f"stake=${row.suggested_stake_usd:.2f}"
        )
        lines.append(f"     {row.question[:150]}")

    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Summarize event-driven observe signals/metrics")
    p.add_argument(
        "--signals-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "event-driven-observe-signals.jsonl"),
        help="Path to event-driven signal JSONL",
    )
    p.add_argument(
        "--metrics-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "event-driven-observe-metrics.jsonl"),
        help="Path to event-driven metrics JSONL",
    )
    p.add_argument("--hours", type=float, default=24.0, help="Lookback window in hours")
    p.add_argument("--since", default="", help='Start timestamp "YYYY-MM-DD HH:MM:SS" (overrides --hours)')
    p.add_argument("--until", default="", help='End timestamp "YYYY-MM-DD HH:MM:SS" (default: now)')
    p.add_argument(
        "--thresholds-cents",
        default="1,2,3,5,8,10",
        help="Comma-separated edge thresholds in cents",
    )
    p.add_argument("--discord", action="store_true", help="Post report to Discord webhook")
    args = p.parse_args()

    now = dt.datetime.now()
    until: dt.datetime = _parse_ts(args.until) if args.until else now
    since: dt.datetime = _parse_ts(args.since) if args.since else (until - dt.timedelta(hours=float(args.hours)))
    thresholds_cents = _thresholds_from_arg(args.thresholds_cents)

    signals: list[SignalRow] = []
    if os.path.exists(args.signals_file):
        with open(args.signals_file, "r", encoding="utf-8", errors="replace") as f:
            signals = [r for r in iter_signals(f) if (since <= r.ts <= until)]

    metrics: list[MetricRow] = []
    if os.path.exists(args.metrics_file):
        with open(args.metrics_file, "r", encoding="utf-8", errors="replace") as f:
            metrics = [r for r in iter_metrics(f) if (since <= r.ts <= until)]

    if not signals and not metrics:
        print(
            f"No rows found in window; checked signals={args.signals_file} metrics={args.metrics_file}",
            file=sys.stderr,
        )
        return 2

    report = build_report(signals, metrics, since=since, until=until, thresholds_cents=thresholds_cents)
    _safe_print(report)

    if args.discord:
        url = _discord_url()
        if not url:
            print("\nDiscord webhook not configured (CLOBBOT_DISCORD_WEBHOOK_URL).", file=sys.stderr)
            return 3
        content = report if len(report) <= 1900 else report[:1900] + "\n...(truncated)"
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
