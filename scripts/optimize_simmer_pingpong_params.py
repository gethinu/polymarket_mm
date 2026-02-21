#!/usr/bin/env python3
"""
Optimize Simmer ping-pong parameters by replaying observe metrics.

Observe-only tool:
- grid/random/hybrid candidate generation
- optional walk-forward robustness ranking
- variable risk scaling (inverse_vol)
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import math
import os
import random
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from report_simmer_observation import iter_metrics


def _parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _flist(raw: str) -> list[float]:
    out: list[float] = []
    for t in (raw or "").split(","):
        s = t.strip()
        if s:
            out.append(float(s))
    return out


def _ilist(raw: str) -> list[int]:
    out: list[int] = []
    for t in (raw or "").split(","):
        s = t.strip()
        if s:
            out.append(int(float(s)))
    return out


def _slist(raw: str) -> list[str]:
    out: list[str] = []
    for t in (raw or "").split(","):
        s = t.strip().lower()
        if s:
            out.append(s)
    return out


def _mean(xs: list[float]) -> float:
    return float(statistics.mean(xs)) if xs else 0.0


def _stdev(xs: Iterable[float]) -> float:
    vals = [float(x) for x in xs]
    if len(vals) < 2:
        return 0.0
    try:
        return float(statistics.pstdev(vals))
    except Exception:
        return 0.0


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _resolve_metric_files(metrics_file: str, metrics_glob: str) -> list[str]:
    paths: list[str] = []
    for p in (metrics_file or "").split(","):
        pp = p.strip()
        if pp and os.path.exists(pp):
            paths.append(pp)
    if metrics_glob.strip():
        paths.extend(sorted(glob.glob(metrics_glob.strip())))
    return sorted({os.path.abspath(p) for p in paths if os.path.exists(p)})


@dataclass(frozen=True)
class Sample:
    ts_ms: int
    market_id: str
    p_yes: float


@dataclass
class MarketState:
    inv: float = 0.0
    avg_cost: float = 0.0
    realized: float = 0.0
    buy_target: float = 0.0
    sell_target: float = 0.0
    last_quote_ts: float = 0.0
    last_price: float = 0.0
    returns: list[float] = field(default_factory=list)
    last_unrealized: float = 0.0
    cycle_open_ts: float = 0.0
    cycle_start_realized: float = 0.0
    last_fill_ts: float = 0.0


@dataclass(frozen=True)
class Param:
    spread_cents: float
    quote_refresh_sec: float
    trade_shares: float
    max_inventory_shares: float
    max_hold_sec: float
    sell_decay_cpm: float
    risk_mode: str
    vol_lookback_samples: int
    target_volatility: float
    min_size_scale: float
    max_size_scale: float


@dataclass(frozen=True)
class SimResult:
    param: Param
    fills_buy: int
    fills_sell: int
    closed_cycles: int
    win_rate: float
    expectancy: float
    total_pnl: float
    max_drawdown: float
    sharpe_like: float
    score: float


@dataclass(frozen=True)
class Candidate:
    param: Param
    full: SimResult
    robust_score: float
    wf_mean_score: float
    wf_std_score: float
    wf_profitable_ratio: float
    pass_constraints: bool


def _param_key(p: Param) -> tuple:
    return (
        round(p.spread_cents, 12),
        round(p.quote_refresh_sec, 12),
        round(p.trade_shares, 12),
        round(p.max_inventory_shares, 12),
        round(p.max_hold_sec, 12),
        round(p.sell_decay_cpm, 12),
        p.risk_mode,
        int(p.vol_lookback_samples),
        round(p.target_volatility, 12),
    )


def _size_mult(st: MarketState, p: Param) -> float:
    if p.risk_mode != "inverse_vol":
        return 1.0
    lb = max(2, int(p.vol_lookback_samples))
    vol = _stdev(st.returns[-lb:])
    if vol <= 1e-12:
        return max(1.0, p.max_size_scale)
    raw = p.target_volatility / vol
    return _clamp(raw, p.min_size_scale, p.max_size_scale)


def _simulate(
    samples: list[Sample],
    p: Param,
    dd_penalty: float,
    sharpe_weight: float,
    expectancy_weight: float,
    per_share_fee: float,
    slippage_cents: float,
    entry_prob_min: float,
    entry_prob_max: float,
    seed_interval_sec: float,
) -> SimResult:
    states: dict[str, MarketState] = {}
    fills_buy, fills_sell = 0, 0
    cycle_pnls: list[float] = []
    equity_deltas: list[float] = []
    prev_equity: float | None = None
    portfolio_realized = 0.0
    portfolio_unrealized = 0.0
    peak, max_dd = 0.0, 0.0

    half = max(0.0, p.spread_cents) / 200.0
    slip = max(0.0, slippage_cents) / 100.0
    fee = max(0.0, per_share_fee)
    pmin, pmax = _clamp(entry_prob_min, 0.0, 1.0), _clamp(entry_prob_max, 0.0, 1.0)
    if pmin > pmax:
        pmin, pmax = pmax, pmin

    for row in samples:
        py = float(row.p_yes)
        if py <= 0.0 or py >= 1.0:
            continue
        st = states.get(row.market_id)
        if st is None:
            st = MarketState()
            states[row.market_id] = st
        now = float(row.ts_ms) / 1000.0

        if st.last_price > 0:
            st.returns.append(py - st.last_price)
            if len(st.returns) > max(20, p.vol_lookback_samples * 4):
                del st.returns[:-max(20, p.vol_lookback_samples * 4)]
        st.last_price = py

        portfolio_unrealized -= st.last_unrealized
        realized_before = st.realized

        targets_unset = st.buy_target <= 0.0 and st.sell_target <= 0.0
        flat = st.inv <= 1e-9
        if targets_unset or (flat and (now - st.last_quote_ts) >= max(1.0, p.quote_refresh_sec)):
            st.buy_target = max(0.001, py - half)
            st.sell_target = min(0.999, py + half)
            st.last_quote_ts = now

        size = max(0.0, p.trade_shares * _size_mult(st, p))
        remaining = max(0.0, p.max_inventory_shares - st.inv)
        seed_trigger = False
        if seed_interval_sec > 0 and st.inv <= 1e-9 and pmin <= py <= pmax:
            seed_trigger = (st.last_fill_ts <= 0.0) or ((now - st.last_fill_ts) >= float(seed_interval_sec))

        if remaining > 1e-9 and pmin <= py <= pmax and (py <= st.buy_target or seed_trigger) and size > 1e-9:
            buy_shares = min(size, remaining)
            fill = _clamp(py + slip, 0.001, 0.999)
            unit_cost = fill + fee
            inv_before = st.inv
            total_cost = st.avg_cost * st.inv + buy_shares * unit_cost
            st.inv += buy_shares
            st.avg_cost = total_cost / st.inv if st.inv > 0 else 0.0
            fills_buy += 1
            st.buy_target = max(0.001, py - half)
            st.sell_target = min(0.999, py + half)
            st.last_quote_ts = now
            st.last_fill_ts = now
            if inv_before <= 1e-9 and st.inv > 1e-9:
                st.cycle_open_ts = now
                st.cycle_start_realized = st.realized

        if st.inv > 1e-9 and size > 1e-9:
            hold = max(0.0, now - (st.cycle_open_ts or now))
            sell_target = st.sell_target
            if p.sell_decay_cpm > 0 and hold > 0:
                sell_target = max(0.001, sell_target - (p.sell_decay_cpm / 100.0) * (hold / 60.0))
            force = p.max_hold_sec > 0 and hold >= p.max_hold_sec
            if force or py >= sell_target:
                sell_shares = min(size, st.inv)
                fill = _clamp(py - slip, 0.001, 0.999)
                pnl = ((fill - fee) - st.avg_cost) * sell_shares
                st.realized += pnl
                st.inv = max(0.0, st.inv - sell_shares)
                if st.inv <= 1e-9:
                    st.inv = 0.0
                    st.avg_cost = 0.0
                fills_sell += 1
                st.buy_target = max(0.001, py - half)
                st.sell_target = min(0.999, py + half)
                st.last_quote_ts = now
                st.last_fill_ts = now
                if st.inv <= 1e-9 and st.cycle_open_ts > 0:
                    cycle_pnls.append(st.realized - st.cycle_start_realized)
                    st.cycle_open_ts = 0.0
                    st.cycle_start_realized = st.realized

        portfolio_realized += st.realized - realized_before
        st.last_unrealized = (py - st.avg_cost) * st.inv if st.inv > 1e-9 and st.avg_cost > 0 else 0.0
        portfolio_unrealized += st.last_unrealized

        equity = portfolio_realized + portfolio_unrealized
        if prev_equity is not None:
            equity_deltas.append(equity - prev_equity)
        prev_equity = equity
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    closed = len(cycle_pnls)
    win_rate = (sum(1 for x in cycle_pnls if x > 0) / closed) if closed > 0 else 0.0
    expectancy = _mean(cycle_pnls)
    total = portfolio_realized + portfolio_unrealized
    sharpe = 0.0
    if len(equity_deltas) >= 2:
        mu, sig = _mean(equity_deltas), _stdev(equity_deltas)
        if sig > 1e-12:
            sharpe = (mu / sig) * math.sqrt(float(len(equity_deltas)))
    score = total - dd_penalty * max_dd + sharpe_weight * sharpe + expectancy_weight * expectancy
    return SimResult(p, fills_buy, fills_sell, closed, win_rate, expectancy, total, max_dd, sharpe, score)


def _grid_params(args) -> list[Param]:
    spreads = [x for x in _flist(args.spreads_cents) if x > 0]
    refreshes = [x for x in _flist(args.quote_refresh_secs) if x > 0]
    sizes = [x for x in _flist(args.trade_shares) if x > 0]
    max_invs = [x for x in _flist(args.max_inventory_shares) if x > 0]
    holds = [x for x in _flist(args.max_hold_secs) if x >= 0]
    decays = [x for x in _flist(args.sell_decay_cpm) if x >= 0]
    modes = _slist(args.risk_modes) or ["static"]
    lbs = [x for x in _ilist(args.vol_lookback_samples) if x >= 2] or [60]
    tgts = [x for x in _flist(args.target_volatilities) if x > 0] or [0.0025]
    for m in modes:
        if m not in {"static", "inverse_vol"}:
            raise SystemExit(f"Unsupported risk mode: {m}")
    if not spreads or not refreshes or not sizes or not max_invs or not holds or not decays:
        raise SystemExit("Parameter grid is empty.")
    mn = max(0.0, float(args.min_size_scale))
    mx = max(mn, float(args.max_size_scale))
    out: list[Param] = []
    for sp in spreads:
        for rf in refreshes:
            for sz in sizes:
                for mi in max_invs:
                    for ho in holds:
                        for dc in decays:
                            for md in modes:
                                if md == "inverse_vol":
                                    for lb in lbs:
                                        for tg in tgts:
                                            out.append(Param(sp, rf, sz, mi, ho, dc, md, lb, tg, mn, mx))
                                else:
                                    out.append(Param(sp, rf, sz, mi, ho, dc, md, lbs[0], tgts[0], mn, mx))
    return out


def _random_params(args, seen: set[tuple]) -> list[Param]:
    n = max(0, int(args.random_candidates))
    if n == 0:
        return []
    base = _grid_params(args)
    spreads = sorted({p.spread_cents for p in base})
    refreshes = sorted({p.quote_refresh_sec for p in base})
    sizes = sorted({p.trade_shares for p in base})
    max_invs = sorted({p.max_inventory_shares for p in base})
    holds = sorted({p.max_hold_sec for p in base})
    decays = sorted({p.sell_decay_cpm for p in base})
    modes = sorted({p.risk_mode for p in base})
    lbs = sorted({p.vol_lookback_samples for p in base})
    tgts = sorted({p.target_volatility for p in base})
    mn, mx = base[0].min_size_scale, base[0].max_size_scale
    rng = random.Random(int(args.random_seed))
    out: list[Param] = []
    attempts = 0
    while len(out) < n and attempts < n * 50 + 100:
        attempts += 1
        md = modes[rng.randrange(len(modes))]
        lb = lbs[rng.randrange(len(lbs))] if md == "inverse_vol" else lbs[0]
        tg = round(rng.uniform(min(tgts), max(tgts)), 6) if md == "inverse_vol" else tgts[0]
        sp = round(rng.uniform(min(spreads), max(spreads)), 4)
        rf = round(rng.uniform(min(refreshes), max(refreshes)), 3)
        sz = round(rng.uniform(min(sizes), max(sizes)), 4)
        mi = round(rng.uniform(min(max_invs), max(max_invs)), 4)
        ho = round(rng.uniform(min(holds), max(holds)), 3)
        dc = round(rng.uniform(min(decays), max(decays)), 4)
        prm = Param(
            sp,
            rf,
            sz,
            mi,
            ho,
            dc,
            md,
            lb,
            tg,
            mn,
            mx,
        )
        k = _param_key(prm)
        if k in seen:
            continue
        seen.add(k)
        out.append(prm)
    return out


def _segments(samples: list[Sample], n: int) -> list[list[Sample]]:
    if n <= 1:
        return [samples]
    out: list[list[Sample]] = []
    total = len(samples)
    for i in range(n):
        a = int((i * total) / n)
        b = int(((i + 1) * total) / n)
        seg = samples[a:b]
        if seg:
            out.append(seg)
    return out or [samples]


def _evaluate(prm: Param, samples: list[Sample], segs: list[list[Sample]], args) -> Candidate:
    full = _simulate(
        samples,
        prm,
        args.dd_penalty,
        args.sharpe_weight,
        args.expectancy_weight,
        args.per_share_fee,
        args.slippage_cents,
        args.entry_prob_min,
        args.entry_prob_max,
        args.seed_interval_sec,
    )
    seg_scores: list[float] = []
    seg_pnls: list[float] = []
    for seg in segs:
        r = _simulate(
            seg,
            prm,
            args.dd_penalty,
            args.sharpe_weight,
            args.expectancy_weight,
            args.per_share_fee,
            args.slippage_cents,
            args.entry_prob_min,
            args.entry_prob_max,
            args.seed_interval_sec,
        )
        seg_scores.append(r.score)
        seg_pnls.append(r.total_pnl)
    wf_mean, wf_std = _mean(seg_scores), _stdev(seg_scores)
    wf_prof = (sum(1 for x in seg_pnls if x > 0) / len(seg_pnls)) if seg_pnls else 0.0
    robust = wf_mean - float(args.wf_std_penalty) * wf_std if len(segs) > 1 else full.score
    ok = True
    if full.closed_cycles < int(args.min_closed_cycles):
        ok = False
    if full.win_rate < float(args.min_win_rate):
        ok = False
    if float(args.max_drawdown) > 0 and full.max_drawdown > float(args.max_drawdown):
        ok = False
    if full.total_pnl < float(args.min_total_pnl):
        ok = False
    return Candidate(prm, full, robust, wf_mean, wf_std, wf_prof, ok)


def _probe_cmd(p: Param) -> str:
    return (
        "python scripts/simmer_pingpong_mm.py "
        f"--paper-trades --spread-cents {p.spread_cents:g} --quote-refresh-sec {p.quote_refresh_sec:g} "
        f"--trade-shares {p.trade_shares:g} --max-inventory-shares {p.max_inventory_shares:g} "
        f"--max-hold-sec {p.max_hold_sec:g} --sell-target-decay-cents-per-min {p.sell_decay_cpm:g}"
    )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    p = argparse.ArgumentParser(description="Optimize Simmer ping-pong parameters by replaying observe metrics")
    p.add_argument("--metrics-file", default=str(root / "logs" / "simmer-pingpong-metrics.jsonl"))
    p.add_argument("--metrics-glob", default="")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--since", default="")
    p.add_argument("--until", default="")
    p.add_argument("--min-samples", type=int, default=200)
    p.add_argument("--min-markets", type=int, default=1)
    p.add_argument("--sample-step", type=int, default=1, help="Keep every Nth sample after sorting (1=no downsample)")
    p.add_argument("--max-samples", type=int, default=0, help="Cap sample count after downsample (0=no cap)")
    p.add_argument("--spreads-cents", default="0.8,1.0,1.2")
    p.add_argument("--quote-refresh-secs", default="30,60")
    p.add_argument("--trade-shares", default="3,5")
    p.add_argument("--max-inventory-shares", default="6,10")
    p.add_argument("--max-hold-secs", default="0,20,60")
    p.add_argument("--sell-decay-cpm", default="0.0,0.2")
    p.add_argument("--risk-modes", default="static,inverse_vol")
    p.add_argument("--vol-lookback-samples", default="60")
    p.add_argument("--target-volatilities", default="0.0025")
    p.add_argument("--min-size-scale", type=float, default=0.5)
    p.add_argument("--max-size-scale", type=float, default=2.0)
    p.add_argument("--search-mode", choices=["grid", "random", "hybrid"], default="grid")
    p.add_argument("--random-candidates", type=int, default=0)
    p.add_argument("--random-seed", type=int, default=42)
    p.add_argument("--max-candidates", type=int, default=5000)
    p.add_argument("--walkforward-splits", type=int, default=1)
    p.add_argument("--walkforward-min-segment-samples", type=int, default=50)
    p.add_argument("--rank-by", choices=["auto", "score", "robust"], default="auto")
    p.add_argument("--wf-std-penalty", type=float, default=0.5)
    p.add_argument("--entry-prob-min", type=float, default=0.0)
    p.add_argument("--entry-prob-max", type=float, default=1.0)
    p.add_argument("--seed-interval-sec", type=float, default=0.0)
    p.add_argument("--per-share-fee", type=float, default=0.0)
    p.add_argument("--slippage-cents", type=float, default=0.0)
    p.add_argument("--dd-penalty", type=float, default=0.5)
    p.add_argument("--sharpe-weight", type=float, default=0.2)
    p.add_argument("--expectancy-weight", type=float, default=0.0)
    p.add_argument("--min-closed-cycles", type=int, default=0)
    p.add_argument("--min-win-rate", type=float, default=0.0)
    p.add_argument("--max-drawdown", type=float, default=0.0)
    p.add_argument("--min-total-pnl", type=float, default=-1e18)
    p.add_argument("--top-n", type=int, default=12)
    p.add_argument("--out-json", default=str(root / "logs" / "simmer-pingpong-optimize-latest.json"))
    p.add_argument("--out-commands", default="")
    args = p.parse_args()

    until = _parse_ts(args.until) if args.until else dt.datetime.now()
    since = _parse_ts(args.since) if args.since else until - dt.timedelta(hours=float(args.hours))
    files = _resolve_metric_files(args.metrics_file, args.metrics_glob)
    if not files:
        print("No metrics files found.")
        return 2

    samples: list[Sample] = []
    for mf in files:
        with open(mf, "r", encoding="utf-8", errors="replace") as f:
            for r in iter_metrics(f):
                if since <= r.ts <= until and r.market_id and 0.0 < float(r.p_yes or 0.0) < 1.0:
                    samples.append(Sample(int(r.ts_ms), str(r.market_id), float(r.p_yes)))
    if len(samples) < int(args.min_samples):
        print(f"Not enough samples: {len(samples)} < {int(args.min_samples)}")
        return 3
    samples.sort(key=lambda x: (x.ts_ms, x.market_id))
    step = max(1, int(args.sample_step))
    if step > 1:
        samples = samples[::step]
    cap = max(0, int(args.max_samples))
    if cap > 0 and len(samples) > cap:
        samples = samples[-cap:]
    if len(samples) < int(args.min_samples):
        print(f"Not enough samples after downsample: {len(samples)} < {int(args.min_samples)}")
        return 3
    markets = sorted({s.market_id for s in samples})
    if len(markets) < int(args.min_markets):
        print(f"Not enough markets: {len(markets)} < {int(args.min_markets)}")
        return 4

    params: list[Param] = []
    if args.search_mode in {"grid", "hybrid"}:
        params.extend(_grid_params(args))
    seen = {_param_key(x) for x in params}
    if args.search_mode in {"random", "hybrid"}:
        params.extend(_random_params(args, seen))
    if not params:
        print("No candidates generated.")
        return 5
    if int(args.max_candidates) > 0 and len(params) > int(args.max_candidates):
        rng = random.Random(int(args.random_seed))
        idx = list(range(len(params)))
        rng.shuffle(idx)
        keep = sorted(idx[: int(args.max_candidates)])
        params = [params[i] for i in keep]

    segs = _segments(samples, max(1, int(args.walkforward_splits)))
    if any(len(seg) < int(args.walkforward_min_segment_samples) for seg in segs):
        sizes = ",".join(str(len(seg)) for seg in segs)
        print(f"Walk-forward segments too small: [{sizes}]")
        return 6

    ranked_by = args.rank_by
    if ranked_by == "auto":
        ranked_by = "robust" if len(segs) > 1 else "score"

    evaluated = [_evaluate(prm, samples, segs, args) for prm in params]
    filtered = [c for c in evaluated if c.pass_constraints]
    pool = filtered if filtered else evaluated
    if ranked_by == "robust":
        pool.sort(key=lambda c: (c.robust_score, c.full.score, c.full.total_pnl, -c.full.max_drawdown, c.full.closed_cycles), reverse=True)
    else:
        pool.sort(key=lambda c: (c.full.score, c.full.total_pnl, -c.full.max_drawdown, c.full.sharpe_like, c.full.closed_cycles), reverse=True)
    top = pool[: max(1, int(args.top_n))]

    print(f"Window: {since:%Y-%m-%d %H:%M:%S} -> {until:%Y-%m-%d %H:%M:%S}")
    print(f"Data: samples={len(samples)} markets={len(markets)} files={len(files)} segments={len(segs)}")
    print(f"Search: mode={args.search_mode} candidates={len(params)} passing={len(filtered)} rank_by={ranked_by}")
    print("rank  spread refresh size inv hold decay risk        cycles win%   pnl_total dd_max score/robust wf_mean wf_std")
    for i, c in enumerate(top, 1):
        s = c.full
        r = c.robust_score if ranked_by == "robust" else s.score
        print(
            f"{i:>4d}  {c.param.spread_cents:>6.2f} {c.param.quote_refresh_sec:>7.0f} {c.param.trade_shares:>4.1f} "
            f"{c.param.max_inventory_shares:>3.0f} {c.param.max_hold_sec:>4.0f} {c.param.sell_decay_cpm:>5.2f} "
            f"{c.param.risk_mode:<10s} {s.closed_cycles:>6d} {100.0*s.win_rate:>4.1f} "
            f"{s.total_pnl:>9.4f} {s.max_drawdown:>6.4f} {r:>11.4f} {c.wf_mean_score:>7.4f} {c.wf_std_score:>6.4f}"
        )

    payload = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": {"since": since.strftime("%Y-%m-%d %H:%M:%S"), "until": until.strftime("%Y-%m-%d %H:%M:%S")},
        "data": {"metrics_files": files, "samples": len(samples), "markets": len(markets), "segment_sizes": [len(x) for x in segs]},
        "search": {"mode": args.search_mode, "candidates": len(params), "passing": len(filtered), "rank_by": ranked_by},
        "top": [
            {
                "rank": i + 1,
                "param": vars(c.param),
                "full": {
                    "fills_buy": c.full.fills_buy,
                    "fills_sell": c.full.fills_sell,
                    "closed_cycles": c.full.closed_cycles,
                    "win_rate": c.full.win_rate,
                    "expectancy": c.full.expectancy,
                    "total_pnl": c.full.total_pnl,
                    "max_drawdown": c.full.max_drawdown,
                    "sharpe_like": c.full.sharpe_like,
                    "score": c.full.score,
                },
                "robust_score": c.robust_score,
                "wf_mean_score": c.wf_mean_score,
                "wf_std_score": c.wf_std_score,
                "wf_profitable_ratio": c.wf_profitable_ratio,
                "probe_command": _probe_cmd(c.param),
            }
            for i, c in enumerate(top)
        ],
    }
    out_json = str(args.out_json or "").strip()
    if out_json:
        op = Path(out_json)
        op.parent.mkdir(parents=True, exist_ok=True)
        op.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote: {op}")

    out_cmd = str(args.out_commands or "").strip()
    if out_cmd:
        cp = Path(out_cmd)
        cp.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# Generated: {dt.datetime.now():%Y-%m-%d %H:%M:%S}", f"# rank_by={ranked_by}", ""]
        for i, c in enumerate(top, 1):
            lines.append(f"# {i} total={c.full.total_pnl:+.4f} dd={c.full.max_drawdown:.4f} cycles={c.full.closed_cycles}")
            lines.append(_probe_cmd(c.param))
            lines.append("")
        cp.write_text("\n".join(lines), encoding="utf-8")
        print(f"Wrote: {cp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
