#!/usr/bin/env python3
"""
Optimize fade entry filters from event logs.

This script reuses real observe-only entry/exit logs and searches thresholds for:
- minimum consensus score
- minimum agree count
- minimum expected edge (cents)
- side mode (both / long / short)

It never places live orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import itertools
import math
import os
import re
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path


TS_RE = r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
ENTRY_RE = re.compile(
    rf"^\[{TS_RE}\] entry (?P<side>LONG|SHORT) (?P<size>[\d.]+) @ (?P<px>[\d.]+) "
    rf"\| score=(?P<score>[+\-]?\d+(?:\.\d+)?) agree=(?P<agree>\d+) "
    rf"\| exp_edge=(?P<edge>[+\-]?\d+(?:\.\d+)?) tp/sl=(?P<tp>[\d.]+)/(?P<sl>[\d.]+) "
    rf"\| (?P<label>.+)$"
)
EXIT_RE = re.compile(
    rf"^\[{TS_RE}\] exit (?P<reason>[a-z_]+) pnl=(?P<pnl>[+\-]?\d+(?:\.\d+)?) "
    rf"realized=(?P<realized>[+\-]?\d+(?:\.\d+)?) trades=(?P<trades>\d+) "
    rf"\| (?P<label>.+)$"
)


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def flist(s: str) -> list[float]:
    out: list[float] = []
    for p in (s or "").split(","):
        p = p.strip()
        if p:
            out.append(float(p))
    return out


def ilist(s: str) -> list[int]:
    out: list[int] = []
    for p in (s or "").split(","):
        p = p.strip()
        if p:
            out.append(int(p))
    return out


@dataclass(frozen=True)
class EntryEvent:
    ts: dt.datetime
    side: int
    score_abs: float
    agree: int
    edge: float
    tp: float
    sl: float
    label: str
    source_file: str


@dataclass(frozen=True)
class Trade:
    entry_ts: dt.datetime
    exit_ts: dt.datetime
    hold_sec: float
    side: int
    score_abs: float
    agree: int
    edge: float
    tp: float
    sl: float
    pnl: float
    reason: str
    label: str
    source_file: str


@dataclass(frozen=True)
class Param:
    min_score: float
    min_agree: int
    min_edge_c: float
    side_mode: str


@dataclass(frozen=True)
class Result:
    p: Param
    trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    avg_pnl: float
    profit_factor: float
    max_drawdown: float
    score: float
    hold_mean_sec: float
    hold_p95_sec: float
    tp_rate: float
    sl_rate: float
    timeout_rate: float


def resolve_files(log_file: str, log_glob: str) -> list[str]:
    out: list[str] = []
    if (log_file or "").strip():
        for p in (log_file or "").split(","):
            p = p.strip()
            if p and os.path.exists(p):
                out.append(os.path.abspath(p))
    pat = (log_glob or "").strip()
    if pat:
        out.extend(os.path.abspath(p) for p in glob.glob(pat) if os.path.exists(p))
    return sorted(set(out))


def parse_trades(files: list[str], since: dt.datetime, until: dt.datetime) -> list[Trade]:
    events: list[tuple[dt.datetime, str, dict]] = []
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m1 = ENTRY_RE.match(line)
                    if m1:
                        ts = parse_ts(m1.group("ts"))
                        if ts < since or ts > until:
                            continue
                        events.append(
                            (
                                ts,
                                "entry",
                                {
                                    "side": 1 if m1.group("side") == "LONG" else -1,
                                    "score_abs": abs(float(m1.group("score"))),
                                    "agree": int(m1.group("agree")),
                                    "edge": float(m1.group("edge")),
                                    "tp": float(m1.group("tp")),
                                    "sl": float(m1.group("sl")),
                                    "label": m1.group("label").strip(),
                                    "source_file": fp,
                                },
                            )
                        )
                        continue
                    m2 = EXIT_RE.match(line)
                    if not m2:
                        continue
                    ts = parse_ts(m2.group("ts"))
                    if ts < since or ts > until:
                        continue
                    events.append(
                        (
                            ts,
                            "exit",
                            {
                                "pnl": float(m2.group("pnl")),
                                "reason": m2.group("reason"),
                                "label": m2.group("label").strip(),
                            },
                        )
                    )
        except Exception:
            continue
    events.sort(key=lambda x: x[0])
    by_label: dict[str, deque[EntryEvent]] = defaultdict(deque)
    out: list[Trade] = []
    for ts, kind, payload in events:
        if kind == "entry":
            by_label[payload["label"]].append(
                EntryEvent(
                    ts=ts,
                    side=int(payload["side"]),
                    score_abs=float(payload["score_abs"]),
                    agree=int(payload["agree"]),
                    edge=float(payload["edge"]),
                    tp=float(payload["tp"]),
                    sl=float(payload["sl"]),
                    label=str(payload["label"]),
                    source_file=str(payload["source_file"]),
                )
            )
            continue
        label = str(payload["label"])
        q = by_label.get(label)
        if not q:
            continue
        ent = q.popleft()
        out.append(
            Trade(
                entry_ts=ent.ts,
                exit_ts=ts,
                hold_sec=max(0.0, (ts - ent.ts).total_seconds()),
                side=ent.side,
                score_abs=ent.score_abs,
                agree=ent.agree,
                edge=ent.edge,
                tp=ent.tp,
                sl=ent.sl,
                pnl=float(payload["pnl"]),
                reason=str(payload["reason"]),
                label=label,
                source_file=ent.source_file,
            )
        )
    out.sort(key=lambda t: t.exit_ts)
    return out


def evaluate(trades: list[Trade], p: Param, dd_penalty: float, min_trades: int, undertrade_penalty: float) -> Result:
    filt: list[Trade] = []
    for t in trades:
        if t.score_abs < p.min_score:
            continue
        if t.agree < p.min_agree:
            continue
        if t.edge < (p.min_edge_c / 100.0):
            continue
        if p.side_mode == "long" and t.side < 0:
            continue
        if p.side_mode == "short" and t.side > 0:
            continue
        filt.append(t)

    if not filt:
        return Result(p, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, -undertrade_penalty * min_trades, 0.0, 0.0, 0.0, 0.0, 0.0)

    tot = 0.0
    peak = 0.0
    dd = 0.0
    wins = losses = 0
    gw = gl = 0.0
    holds: list[float] = []
    tpn = sln = ton = 0
    for t in filt:
        tot += t.pnl
        peak = max(peak, tot)
        dd = max(dd, peak - tot)
        holds.append(t.hold_sec)
        if t.pnl > 1e-12:
            wins += 1
            gw += t.pnl
        else:
            losses += 1
            gl += abs(t.pnl)
        if t.reason == "tp":
            tpn += 1
        elif t.reason == "sl":
            sln += 1
        elif t.reason == "timeout":
            ton += 1

    n = len(filt)
    wr = wins / max(1, n)
    pf = (gw / gl) if gl > 1e-12 else (float("inf") if gw > 0 else 0.0)
    avg = tot / max(1, n)
    hold_mean = statistics.mean(holds) if holds else 0.0
    hold_p95 = max(holds) if len(holds) < 20 else float(statistics.quantiles(holds, n=20)[-1])
    score = tot - (max(0.0, dd_penalty) * dd)
    if n < max(0, min_trades):
        score -= max(0.0, undertrade_penalty) * (int(min_trades) - n)
    return Result(
        p=p,
        trades=n,
        wins=wins,
        losses=losses,
        win_rate=wr,
        total_pnl=tot,
        avg_pnl=avg,
        profit_factor=pf,
        max_drawdown=dd,
        score=score,
        hold_mean_sec=hold_mean,
        hold_p95_sec=hold_p95,
        tp_rate=(tpn / n),
        sl_rate=(sln / n),
        timeout_rate=(ton / n),
    )


def fmt_table(rows: list[Result]) -> str:
    h = "rank score    pnl    dd trades win%   pf   avgT holdM hold95 tp%  sl%  to%  minScore agree edge(c) side"
    lines = [h]
    for i, r in enumerate(rows, start=1):
        pf = "inf" if math.isinf(r.profit_factor) else f"{r.profit_factor:.2f}"
        p = r.p
        lines.append(
            f"{i:>4d} {r.score:>6.3f} {r.total_pnl:>6.3f} {r.max_drawdown:>5.3f} {r.trades:>6d} "
            f"{(100*r.win_rate):>5.1f}% {pf:>5s} {r.avg_pnl:>6.4f} "
            f"{r.hold_mean_sec:>5.0f}s {r.hold_p95_sec:>6.0f}s "
            f"{(100*r.tp_rate):>4.0f}% {(100*r.sl_rate):>4.0f}% {(100*r.timeout_rate):>4.0f}% "
            f"{p.min_score:>8.2f} {p.min_agree:>5d} {p.min_edge_c:>7.3f} {p.side_mode:>5s}"
        )
    return "\n".join(lines)


def recommend_cmd(best: Result) -> str:
    p = best.p
    return (
        "python scripts/polymarket_clob_fade_observe.py "
        f"--consensus-min-score {p.min_score:.3f} "
        f"--consensus-min-agree {p.min_agree:d} "
        f"--min-expected-edge-cents {p.min_edge_c:.3f}"
    )


def main() -> int:
    base = Path(__file__).resolve().parents[1] / "logs"
    dflt_log = str(base / "clob-fade-observe-profit-live-v2.log")
    dflt_glob = str(base / "clob-fade-observe-profit*.log")
    p = argparse.ArgumentParser(description="Optimize CLOB fade entry filters from event logs")
    p.add_argument("--log-file", default=dflt_log)
    p.add_argument("--log-glob", default=dflt_glob)
    p.add_argument("--hours", type=float, default=72.0)
    p.add_argument("--since", default="")
    p.add_argument("--until", default="")
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--min-trades", type=int, default=20)
    p.add_argument("--strict-min-trades", action="store_true")
    p.add_argument("--dd-penalty", type=float, default=0.5)
    p.add_argument("--undertrade-penalty", type=float, default=0.01)
    p.add_argument("--score-thresholds", default="0.6,0.9,1.2,1.45,1.8")
    p.add_argument("--agree-thresholds", default="1,2,3")
    p.add_argument("--edge-threshold-cents", default="0.00,0.05,0.10,0.20")
    p.add_argument("--side-modes", default="both,long,short")
    args = p.parse_args()

    files = resolve_files(args.log_file, args.log_glob)
    if not files:
        print(f"No log files found. --log-file={args.log_file} --log-glob={args.log_glob}")
        return 2
    now = dt.datetime.now()
    until = parse_ts(args.until) if args.until else now
    since = parse_ts(args.since) if args.since else (until - dt.timedelta(hours=float(args.hours)))
    trades = parse_trades(files, since=since, until=until)
    if not trades:
        print("No paired trades found in selected window.")
        return 3

    score_g = flist(args.score_thresholds)
    agree_g = ilist(args.agree_thresholds)
    edge_g = flist(args.edge_threshold_cents)
    side_g = [x.strip().lower() for x in (args.side_modes or "").split(",") if x.strip()]
    side_g = [x for x in side_g if x in {"both", "long", "short"}]
    if not (score_g and agree_g and edge_g and side_g):
        print("Parameter grid is empty.")
        return 4

    params: list[Param] = []
    for s, a, e, side in itertools.product(score_g, agree_g, edge_g, side_g):
        if s <= 0 or a < 1 or e < 0:
            continue
        params.append(Param(min_score=float(s), min_agree=int(a), min_edge_c=float(e), side_mode=side))
    if not params:
        print("No valid parameter combinations.")
        return 5

    print(
        f"Window: {since:%Y-%m-%d %H:%M:%S} -> {until:%Y-%m-%d %H:%M:%S} "
        f"| files={len(files)} trades={len(trades)} combos={len(params)}"
    )
    res = [
        evaluate(
            trades=trades,
            p=pp,
            dd_penalty=float(args.dd_penalty),
            min_trades=int(args.min_trades),
            undertrade_penalty=float(args.undertrade_penalty),
        )
        for pp in params
    ]
    ranked = sorted(res, key=lambda r: (r.score, r.total_pnl, -r.max_drawdown, r.win_rate, r.trades), reverse=True)
    if args.strict_min_trades:
        flt = [r for r in ranked if r.trades >= int(args.min_trades)]
        if flt:
            ranked = flt
    top = ranked[: max(1, int(args.top_n))]
    print(fmt_table(top))
    best = top[0]
    print("")
    print(
        f"Best: score={best.score:.4f} pnl={best.total_pnl:+.4f} dd={best.max_drawdown:.4f} "
        f"trades={best.trades} win={100*best.win_rate:.1f}% "
        f"pf={('inf' if math.isinf(best.profit_factor) else f'{best.profit_factor:.2f}')}"
    )
    print("Recommended observe command (entry filter part):")
    print(recommend_cmd(best))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
