#!/usr/bin/env python3
"""
Grid-search candidates for polymarket_clob_fade_observe.py from metrics replay.
Observe-only utility. Never places real orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import math
import os
import glob
import statistics
from dataclasses import dataclass, field
from pathlib import Path


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    try:
        return float(statistics.pstdev(xs))
    except Exception:
        return 0.0


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


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


def apush(xs: list[float], x: float, n: int) -> None:
    xs.append(float(x))
    if len(xs) > n:
        del xs[:-n]


@dataclass(frozen=True)
class Row:
    ts: dt.datetime
    ts_ms: int
    token_id: str
    label: str
    mid: float
    bid: float
    ask: float
    spread: float
    dbid: float
    dask: float
    score: float
    cside: int
    cagree: int
    bz: float
    bv: float
    bi: float


@dataclass
class Tok:
    token_id: str
    label: str = ""
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0
    spread: float = 0.0
    dbid: float = 0.0
    dask: float = 0.0
    mids: list[float] = field(default_factory=list)
    rets: list[float] = field(default_factory=list)
    side: int = 0
    size: float = 0.0
    entry_px: float = 0.0
    entry_ts_ms: int = 0
    tp: float = 0.0
    sl: float = 0.0
    cooldown_until: int = 0
    disabled_until: int = 0
    realized: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0


@dataclass(frozen=True)
class Param:
    min_score: float
    min_agree: int
    min_nonext_agree: int
    tp_c: float
    sl_c: float
    hold_s: float
    edge_c: float
    ratio: float


@dataclass(frozen=True)
class Res:
    p: Param
    score: float
    total: float
    realized: float
    unreal: float
    dd: float
    trades: int
    wins: int
    losses: int
    wr: float
    pf: float
    avg: float
    entries: int
    exits: int
    signals: int
    halts: int
    disables: int


def iter_rows(path: str):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = (line or "").strip()
            if not line:
                continue
            try:
                o = json.loads(line)
                ts_raw = str(o.get("ts") or "").strip()
                ts_ms = int(o.get("ts_ms") or 0)
                if ts_raw:
                    ts = parse_ts(ts_raw)
                    if ts_ms <= 0:
                        ts_ms = int(ts.timestamp() * 1000.0)
                else:
                    if ts_ms <= 0:
                        continue
                    ts = dt.datetime.fromtimestamp(ts_ms / 1000.0)
                tid = str(o.get("token_id") or "").strip()
                if not tid:
                    continue
                bid = float(o.get("best_bid") or 0.0)
                ask = float(o.get("best_ask") or 0.0)
                mid = float(o.get("mid") or 0.0)
                if mid <= 0 and bid > 0 and ask > 0 and ask >= bid:
                    mid = (bid + ask) / 2.0
                if mid <= 0:
                    continue
                spr = float(o.get("spread") or 0.0)
                if spr <= 0 and bid > 0 and ask > 0 and ask >= bid:
                    spr = ask - bid
                yield Row(
                    ts=ts,
                    ts_ms=ts_ms,
                    token_id=tid,
                    label=str(o.get("label") or "")[:180],
                    mid=mid,
                    bid=bid,
                    ask=ask,
                    spread=max(0.0, spr),
                    dbid=max(0.0, float(o.get("depth_bid") or 0.0)),
                    dask=max(0.0, float(o.get("depth_ask") or 0.0)),
                    score=float(o.get("consensus_score") or 0.0),
                    cside=int(o.get("consensus_side") or 0),
                    cagree=int(o.get("consensus_agree") or 0),
                    bz=float(o.get("bot_zscore") or 0.0),
                    bv=float(o.get("bot_velocity") or 0.0),
                    bi=float(o.get("bot_imbalance") or 0.0),
                )
            except Exception:
                continue


def resolve_metric_files(metrics_file: str, metrics_glob: str) -> list[str]:
    paths: list[str] = []
    base = (metrics_file or "").strip()
    if base:
        for p in base.split(","):
            pp = p.strip()
            if not pp:
                continue
            if os.path.exists(pp):
                paths.append(pp)
    pat = (metrics_glob or "").strip()
    if pat:
        paths.extend(sorted(glob.glob(pat)))
    uniq = sorted({os.path.abspath(p) for p in paths if os.path.exists(p)})
    return uniq


def group_rows(rows: list[Row]) -> list[tuple[int, list[Row]]]:
    rows = sorted(rows, key=lambda r: (r.ts_ms, r.token_id))
    if not rows:
        return []
    out: list[tuple[int, list[Row]]] = []
    cur = rows[0].ts_ms
    buf: list[Row] = []
    for r in rows:
        if r.ts_ms != cur:
            out.append((cur, buf))
            cur, buf = r.ts_ms, [r]
        else:
            buf.append(r)
    out.append((cur, buf))
    return out


def tpnl(tokens: dict[str, Tok]) -> tuple[float, float, float]:
    realized = sum(t.realized for t in tokens.values())
    unreal = 0.0
    for t in tokens.values():
        if t.side != 0 and t.size > 0 and t.mid > 0:
            unreal += t.side * (t.mid - t.entry_px) * t.size
    return realized + unreal, realized, unreal


def n_nonext(side: int, r: Row) -> int:
    return sum(1 for v in (r.bz, r.bv, r.bi) if sign(v) == side)


def regime_ok(t: Tok, args) -> bool:
    if t.mid <= 0:
        return False
    if t.mid < args.min_mid_prob or t.mid > args.max_mid_prob:
        return False
    if args.max_spread_cents > 0 and t.spread > (args.max_spread_cents / 100.0):
        return False
    if min(t.dbid, t.dask) < args.min_depth_shares:
        return False
    lb = max(5, int(args.vol_lookback))
    vol = stdev(t.rets[-lb:]) if len(t.rets) >= lb else stdev(t.rets)
    max_vol = max(0.0, float(args.max_volatility_cents)) / 100.0
    min_vol = max(0.0, float(args.min_volatility_cents)) / 100.0
    if max_vol > 0 and vol > max_vol:
        return False
    if min_vol > 0 and vol < min_vol:
        return False
    return True


def rt_cost(t: Tok, args) -> float:
    tick = max(0.0001, args.tick_size)
    spr = max(t.spread, tick)
    if args.execution_mode == "mid":
        spr *= (1.0 - clamp(args.maker_spread_capture, 0.0, 1.0))
    slip = 2.0 * max(0.0, args.slippage_ticks) * tick
    fee = 2.0 * max(0.0, t.mid) * (max(0.0, args.fee_bps) / 10000.0)
    return spr + slip + fee


def exp_move(t: Tok, score: float, args) -> float:
    lb = max(5, int(args.vol_lookback))
    vol = stdev(t.rets[-lb:]) if len(t.rets) >= lb else stdev(t.rets)
    if vol <= 1e-9:
        vol = max(t.spread * 0.5, max(args.tick_size, 0.0001))
    return max(0.0, abs(score) * vol * max(0.1, args.expected_move_vol_mult))


def ent_px(t: Tok, side: int, args) -> float:
    tick = max(0.0001, args.tick_size)
    slip = max(0.0, args.slippage_ticks) * tick
    if args.execution_mode == "mid":
        base = t.mid if t.mid > 0 else (t.ask if side > 0 else t.bid)
        return max(0.001, base + (slip * 0.5 if side > 0 else -slip * 0.5))
    if side > 0:
        return max(0.001, (t.ask if t.ask > 0 else t.mid) + slip)
    return max(0.001, (t.bid if t.bid > 0 else t.mid) - slip)


def ex_px(t: Tok, side: int, args) -> float:
    tick = max(0.0001, args.tick_size)
    slip = max(0.0, args.slippage_ticks) * tick
    if args.execution_mode == "mid":
        base = t.mid if t.mid > 0 else (t.bid if side > 0 else t.ask)
        return max(0.001, base + (-slip * 0.5 if side > 0 else slip * 0.5))
    if side > 0:
        return max(0.001, (t.bid if t.bid > 0 else t.mid) - slip)
    return max(0.001, (t.ask if t.ask > 0 else t.mid) + slip)


def close_pos(t: Tok, p: Param, args, ts_ms: int, reason: str, force: bool = False) -> float | None:
    if t.side == 0 or t.size <= 0:
        t.side, t.size = 0, 0.0
        return None
    if (not force) and t.mid <= 0:
        return None
    side, size = t.side, t.size
    px = ex_px(t, side, args)
    gross = side * (px - t.entry_px) * size
    notional = abs(t.entry_px * size) + abs(px * size)
    fees = notional * (max(0.0, args.fee_bps) / 10000.0)
    pnl = gross - fees
    t.realized += pnl
    t.trades += 1
    if pnl > 1e-12:
        t.wins += 1
    else:
        t.losses += 1
    t.side, t.size, t.entry_px, t.entry_ts_ms, t.tp, t.sl = 0, 0.0, 0.0, 0, 0.0, 0.0
    t.cooldown_until = int(ts_ms + max(0.0, args.cooldown_sec) * 1000.0)
    if reason in ("tp", "sl", "timeout") and t.trades >= max(1, int(args.token_loss_min_trades)):
        wr = t.wins / max(1, t.trades)
        disable = False
        if args.token_loss_cut_usd > 0 and t.realized <= -args.token_loss_cut_usd:
            disable = True
        elif wr < clamp(args.token_min_winrate, 0.0, 1.0):
            disable = True
        if disable:
            t.disabled_until = int(ts_ms + max(0.0, args.token_disable_sec) * 1000.0)
    _ = p
    return pnl


def maybe_close(t: Tok, p: Param, args, ts_ms: int) -> float | None:
    if t.side == 0 or t.mid <= 0:
        return None
    per = t.side * (t.mid - t.entry_px)
    tp = max(p.tp_c / 100.0, t.tp)
    sl = max(p.sl_c / 100.0, t.sl)
    hold = (ts_ms - t.entry_ts_ms) / 1000.0
    if tp > 0 and per >= tp:
        return close_pos(t, p, args, ts_ms, "tp")
    if sl > 0 and per <= -sl:
        return close_pos(t, p, args, ts_ms, "sl")
    if p.hold_s > 0 and hold >= p.hold_s:
        return close_pos(t, p, args, ts_ms, "timeout")
    return None


def simulate(batches: list[tuple[int, list[Row]]], p: Param, args) -> Res:
    toks: dict[str, Tok] = {}
    entries = exits = signals = halts = disables = 0
    halted = False
    day_key = ""
    day_anchor = 0.0
    peak = dd = 0.0
    gwin = gloss = 0.0
    for ts_ms, bucket in batches:
        cur_day = bucket[0].ts.astimezone().strftime("%Y-%m-%d")
        if not day_key:
            day_key = cur_day
        elif cur_day != day_key:
            tot, _, _ = tpnl(toks)
            day_key, day_anchor, halted = cur_day, tot, False

        for r in bucket:
            t = toks.get(r.token_id)
            if t is None:
                t = Tok(token_id=r.token_id, label=r.label)
                toks[r.token_id] = t
            prev = t.mid
            t.label = r.label or t.label
            t.bid, t.ask, t.mid = r.bid, r.ask, r.mid
            t.spread, t.dbid, t.dask = r.spread, r.dbid, r.dask
            if t.mid > 0:
                apush(t.mids, t.mid, max(20, int(args.history_size)))
            if prev > 0 and t.mid > 0:
                apush(t.rets, t.mid - prev, max(20, int(args.history_size)))

        for t in toks.values():
            pnl = maybe_close(t, p, args, ts_ms)
            if pnl is None:
                continue
            exits += 1
            if pnl > 0:
                gwin += pnl
            elif pnl < 0:
                gloss += abs(pnl)
            if t.disabled_until > ts_ms:
                disables += 1

        tot, _, _ = tpnl(toks)
        day = tot - day_anchor
        if args.daily_loss_limit_usd > 0 and day <= -args.daily_loss_limit_usd:
            if not halted:
                halted = True
                halts += 1
            for t in toks.values():
                if t.side == 0:
                    continue
                pnl = close_pos(t, p, args, ts_ms, "daily_loss_guard", force=True)
                if pnl is None:
                    continue
                exits += 1
                if pnl > 0:
                    gwin += pnl
                elif pnl < 0:
                    gloss += abs(pnl)

        open_n = sum(1 for t in toks.values() if t.side != 0)
        slots = max(0, int(args.max_open_positions) - open_n)
        cands: list[tuple[float, str, int, float, float]] = []
        if (not halted) and slots > 0:
            for r in bucket:
                t = toks[r.token_id]
                if t.side != 0 or ts_ms < t.cooldown_until:
                    continue
                if t.disabled_until > 0:
                    if ts_ms < t.disabled_until:
                        continue
                    t.disabled_until = 0
                side = r.cside if r.cside != 0 else sign(r.score)
                ss = sign(r.score)
                if ss != 0 and side != ss:
                    side = ss
                if side == 0 or abs(r.score) < p.min_score or r.cagree < p.min_agree:
                    continue
                if n_nonext(side, r) < p.min_nonext_agree:
                    continue
                if not regime_ok(t, args):
                    continue
                signals += 1
                cost = rt_cost(t, args)
                em = exp_move(t, r.score, args)
                if em < (cost * max(0.1, p.ratio)):
                    continue
                edge = em - cost
                if edge < (max(0.0, p.edge_c) / 100.0):
                    continue
                tp = max(p.tp_c / 100.0, cost * max(0.1, args.tp_cost_mult))
                sl = max(p.sl_c / 100.0, cost * max(0.1, args.sl_cost_mult))
                cands.append((abs(r.score), r.token_id, side, tp, sl))
            for _, tid, side, tpv, slv in sorted(cands, key=lambda x: x[0], reverse=True):
                if slots <= 0:
                    break
                t = toks[tid]
                if t.side != 0:
                    continue
                t.side = side
                t.size = max(1.0, args.position_size_shares)
                t.entry_px = ent_px(t, side, args)
                t.entry_ts_ms = ts_ms
                t.tp, t.sl = tpv, slv
                entries += 1
                slots -= 1

        tot, _, _ = tpnl(toks)
        peak = max(peak, tot)
        dd = max(dd, peak - tot)

    last_ts = batches[-1][0] if batches else 0
    for t in toks.values():
        if t.side == 0:
            continue
        pnl = close_pos(t, p, args, last_ts, "end", force=True)
        if pnl is None:
            continue
        exits += 1
        if pnl > 0:
            gwin += pnl
        elif pnl < 0:
            gloss += abs(pnl)

    total, realized, unreal = tpnl(toks)
    trades = sum(t.trades for t in toks.values())
    wins = sum(t.wins for t in toks.values())
    losses = sum(t.losses for t in toks.values())
    wr = wins / max(1, trades)
    pf = (gwin / gloss) if gloss > 1e-12 else (float("inf") if gwin > 0 else 0.0)
    avg = realized / max(1, trades)
    rank_score = total - (max(0.0, args.dd_penalty) * dd)
    if trades < max(0, int(args.min_trades)):
        rank_score -= max(0.0, args.undertrade_penalty) * (int(args.min_trades) - trades)
    return Res(
        p=p,
        score=rank_score,
        total=total,
        realized=realized,
        unreal=unreal,
        dd=dd,
        trades=trades,
        wins=wins,
        losses=losses,
        wr=wr,
        pf=pf,
        avg=avg,
        entries=entries,
        exits=exits,
        signals=signals,
        halts=halts,
        disables=disables,
    )


def fmt_table(rows: list[Res]) -> str:
    h = "rank score    pnl    dd trades win%   pf   avgT scoreThr ag nonEx tp sl hold edge ratio"
    out = [h]
    for i, r in enumerate(rows, start=1):
        pf = "inf" if math.isinf(r.pf) else f"{r.pf:.2f}"
        p = r.p
        out.append(
            f"{i:>4d} {r.score:>6.3f} {r.total:>6.3f} {r.dd:>5.3f} {r.trades:>6d} "
            f"{(100*r.wr):>5.1f}% {pf:>5s} {r.avg:>6.4f} "
            f"{p.min_score:>7.2f} {p.min_agree:>2d} {p.min_nonext_agree:>5d} "
            f"{p.tp_c:>4.1f} {p.sl_c:>4.1f} {p.hold_s:>4.0f} {p.edge_c:>4.2f} {p.ratio:>5.2f}"
        )
    return "\n".join(out)


def rec_cmd(r: Res) -> str:
    p = r.p
    return (
        "python scripts/polymarket_clob_fade_observe.py "
        f"--consensus-min-score {p.min_score:.3f} "
        f"--consensus-min-agree {p.min_agree:d} "
        f"--min-non-extreme-agree {p.min_nonext_agree:d} "
        f"--take-profit-cents {p.tp_c:.3f} "
        f"--stop-loss-cents {p.sl_c:.3f} "
        f"--max-hold-sec {p.hold_s:.0f} "
        f"--min-expected-edge-cents {p.edge_c:.3f} "
        f"--expected-move-cost-ratio {p.ratio:.3f}"
    )


def main() -> int:
    dflt_metrics = str(Path(__file__).resolve().parents[1] / "logs" / "clob-fade-observe-profit-live-v2-metrics.jsonl")
    dflt_out = str(Path(__file__).resolve().parents[1] / "logs" / "clob-fade-optimize-latest.json")
    p = argparse.ArgumentParser(description="Optimize CLOB fade params from observe metrics replay")
    p.add_argument("--metrics-file", default=dflt_metrics)
    p.add_argument("--metrics-glob", default="", help="Optional glob to load multiple metrics files")
    p.add_argument("--hours", type=float, default=6.0)
    p.add_argument("--since", default="")
    p.add_argument("--until", default="")
    p.add_argument("--min-samples", type=int, default=400)
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--strict-min-trades", action="store_true")
    p.add_argument("--out-json", default=dflt_out, help="Set empty to disable")

    p.add_argument("--execution-mode", choices=("taker", "mid"), default="mid")
    p.add_argument("--tick-size", type=float, default=0.001)
    p.add_argument("--slippage-ticks", type=float, default=0.5)
    p.add_argument("--maker-spread-capture", type=float, default=0.4)
    p.add_argument("--fee-bps", type=float, default=0.0)
    p.add_argument("--tp-cost-mult", type=float, default=1.8)
    p.add_argument("--sl-cost-mult", type=float, default=1.2)
    p.add_argument("--expected-move-vol-mult", type=float, default=1.6)
    p.add_argument("--max-spread-cents", type=float, default=2.5)
    p.add_argument("--min-depth-shares", type=float, default=100.0)
    p.add_argument("--vol-lookback", type=int, default=30)
    p.add_argument("--max-volatility-cents", type=float, default=1.4)
    p.add_argument("--min-volatility-cents", type=float, default=0.0)
    p.add_argument("--min-mid-prob", type=float, default=0.05)
    p.add_argument("--max-mid-prob", type=float, default=0.95)
    p.add_argument("--position-size-shares", type=float, default=12.0)
    p.add_argument("--max-open-positions", type=int, default=3)
    p.add_argument("--cooldown-sec", type=float, default=45.0)
    p.add_argument("--daily-loss-limit-usd", type=float, default=8.0)
    p.add_argument("--history-size", type=int, default=300)
    p.add_argument("--token-loss-cut-usd", type=float, default=0.60)
    p.add_argument("--token-loss-min-trades", type=int, default=5)
    p.add_argument("--token-min-winrate", type=float, default=0.20)
    p.add_argument("--token-disable-sec", type=float, default=1800.0)
    p.add_argument("--dd-penalty", type=float, default=0.25)
    p.add_argument("--min-trades", type=int, default=12)
    p.add_argument("--undertrade-penalty", type=float, default=0.02)

    p.add_argument("--consensus-min-scores", default="1.2,1.45,1.8,2.2")
    p.add_argument("--consensus-min-agrees", default="1,2,3")
    p.add_argument("--min-non-extreme-agrees", default="0,1,2")
    p.add_argument("--take-profit-cents-grid", default="0.8,1.2,1.6,2.0")
    p.add_argument("--stop-loss-cents-grid", default="0.8,1.2,1.6")
    p.add_argument("--max-hold-secs", default="120,180,240,360")
    p.add_argument("--min-expected-edge-cents-grid", default="0.00,0.05,0.10,0.20")
    p.add_argument("--expected-move-cost-ratios", default="1.2,1.4,1.8")
    args = p.parse_args()

    metric_files = resolve_metric_files(args.metrics_file, args.metrics_glob)
    if not metric_files:
        print(f"No metrics files found. --metrics-file={args.metrics_file} --metrics-glob={args.metrics_glob}")
        return 2
    now = dt.datetime.now()
    until = parse_ts(args.until) if args.until else now
    since = parse_ts(args.since) if args.since else (until - dt.timedelta(hours=float(args.hours)))
    rows: list[Row] = []
    for mf in metric_files:
        rows.extend(r for r in iter_rows(mf) if since <= r.ts <= until)
    if len(rows) < int(args.min_samples):
        print(f"Not enough samples: {len(rows)} < {int(args.min_samples)}")
        return 3
    batches = group_rows(rows)
    if not batches:
        print("No batches.")
        return 4

    score_g = flist(args.consensus_min_scores)
    agree_g = ilist(args.consensus_min_agrees)
    nonext_g = ilist(args.min_non_extreme_agrees)
    tp_g = flist(args.take_profit_cents_grid)
    sl_g = flist(args.stop_loss_cents_grid)
    hold_g = flist(args.max_hold_secs)
    edge_g = flist(args.min_expected_edge_cents_grid)
    ratio_g = flist(args.expected_move_cost_ratios)
    if not (score_g and agree_g and nonext_g and tp_g and sl_g and hold_g and edge_g and ratio_g):
        print("Empty grid.")
        return 5

    params: list[Param] = []
    for s, a, n, tp, sl, h, e, rr in itertools.product(score_g, agree_g, nonext_g, tp_g, sl_g, hold_g, edge_g, ratio_g):
        if s <= 0 or a < 1 or n < 0 or n > 3 or tp <= 0 or sl <= 0 or h <= 0 or e < 0 or rr <= 0:
            continue
        params.append(Param(float(s), int(a), int(n), float(tp), float(sl), float(h), float(e), float(rr)))
    if not params:
        print("No valid combinations.")
        return 6

    print(
        f"Window: {since:%Y-%m-%d %H:%M:%S} -> {until:%Y-%m-%d %H:%M:%S} "
        f"| files={len(metric_files)} rows={len(rows)} batches={len(batches)} combos={len(params)}"
    )
    res: list[Res] = []
    n_all = len(params)
    for i, pp in enumerate(params, start=1):
        res.append(simulate(batches, pp, args))
        if n_all >= 80 and i % max(1, n_all // 10) == 0:
            print(f"progress {i}/{n_all}")

    ranked = sorted(res, key=lambda r: (r.score, r.total, -r.dd, r.wr, r.trades), reverse=True)
    if args.strict_min_trades:
        flt = [r for r in ranked if r.trades >= int(args.min_trades)]
        if flt:
            ranked = flt
    top = ranked[: max(1, int(args.top_n))]
    print(fmt_table(top))

    best = top[0]
    cmd = rec_cmd(best)
    print("")
    print(
        f"Best: score={best.score:.4f} total={best.total:+.4f} realized={best.realized:+.4f} "
        f"dd={best.dd:.4f} trades={best.trades} win={100*best.wr:.1f}% "
        f"pf={('inf' if math.isinf(best.pf) else f'{best.pf:.2f}')}"
    )
    print("Recommended observe command:")
    print(cmd)

    if args.out_json:
        out = Path(args.out_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "window": {"since": since.strftime("%Y-%m-%d %H:%M:%S"), "until": until.strftime("%Y-%m-%d %H:%M:%S")},
            "metrics_file": args.metrics_file,
            "metrics_glob": args.metrics_glob,
            "metrics_files": metric_files,
            "rows": len(rows),
            "batches": len(batches),
            "combos": len(params),
            "best": {
                "score": best.score,
                "total": best.total,
                "realized": best.realized,
                "unreal": best.unreal,
                "dd": best.dd,
                "trades": best.trades,
                "wins": best.wins,
                "losses": best.losses,
                "win_rate": best.wr,
                "profit_factor": best.pf,
                "avg": best.avg,
                "params": {
                    "min_score": best.p.min_score,
                    "min_agree": best.p.min_agree,
                    "min_nonext_agree": best.p.min_nonext_agree,
                    "tp_c": best.p.tp_c,
                    "sl_c": best.p.sl_c,
                    "hold_s": best.p.hold_s,
                    "edge_c": best.p.edge_c,
                    "ratio": best.p.ratio,
                },
            },
            "recommended_command": cmd,
        }
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
