#!/usr/bin/env python3
"""
Replay CLOB arb monitor metrics and estimate fractional Kelly scales (observe-only).
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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def as_float(value, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    s = str(value or "").strip().lower()
    return s in {"1", "true", "yes", "y", "on"}


def clamp(x: float, lo: float, hi: float) -> float:
    if not math.isfinite(x):
        return lo
    if x <= lo:
        return lo
    if x >= hi:
        return hi
    return float(x)


def percentile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    arr = sorted(float(x) for x in values)
    if len(arr) == 1:
        return arr[0]
    qq = clamp(float(q), 0.0, 1.0)
    idx = qq * (len(arr) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return arr[lo]
    frac = idx - lo
    return arr[lo] * (1.0 - frac) + arr[hi] * frac


def parse_ts_ms(row: dict) -> int:
    ts_ms = as_int(row.get("ts_ms"), 0)
    if ts_ms > 0:
        return ts_ms
    ts = str(row.get("ts") or "").strip()
    if not ts:
        return 0
    try:
        return int(dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp() * 1000.0)
    except ValueError:
        return 0


def parse_scales(raw: str) -> List[float]:
    out: List[float] = []
    for tok in (raw or "").split(","):
        s = tok.strip()
        if not s:
            continue
        try:
            v = float(s)
        except ValueError:
            continue
        if v > 0:
            out.append(v)
    if not out:
        out = [0.25, 0.50, 1.00]
    return out


def resolve_metric_files(metrics_file: str, metrics_glob: str) -> List[str]:
    paths: List[str] = []
    base = (metrics_file or "").strip()
    if base:
        for p in base.split(","):
            pp = p.strip()
            if pp and os.path.exists(pp):
                paths.append(pp)
    if (metrics_glob or "").strip():
        paths.extend(glob.glob(metrics_glob.strip()))
    uniq = sorted({os.path.abspath(p) for p in paths if os.path.exists(p)})
    return uniq


def iter_metric_rows(path: str) -> Iterable[dict]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = (line or "").strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if isinstance(o, dict):
                yield o


@dataclass(frozen=True)
class ReplaySample:
    ts_ms: int
    edge: float
    cost: float
    fill_ratio: float
    worst_stale_sec: float
    proxy_return: float


def _pick_edge_cost(row: dict, edge_mode: str) -> tuple[float, float]:
    if edge_mode == "exec":
        edge = as_float(row.get("net_edge_exec_est"), math.nan)
        cost = as_float(row.get("basket_cost_exec_est"), math.nan)
        if not math.isfinite(edge):
            edge = as_float(row.get("net_edge_raw"), math.nan)
        if not math.isfinite(cost) or cost <= 0:
            cost = as_float(row.get("basket_cost_observed"), math.nan)
        return edge, cost

    edge = as_float(row.get("net_edge_raw"), math.nan)
    cost = as_float(row.get("basket_cost_observed"), math.nan)
    if not math.isfinite(edge):
        edge = as_float(row.get("net_edge_exec_est"), math.nan)
    if not math.isfinite(cost) or cost <= 0:
        cost = as_float(row.get("basket_cost_exec_est"), math.nan)
    return edge, cost


def _pick_fill_ratio(row: dict, mode: str) -> float:
    if mode == "none":
        return 1.0
    if mode == "avg":
        return clamp(as_float(row.get("fill_ratio_avg"), 1.0), 0.0, 1.0)
    return clamp(as_float(row.get("fill_ratio_min"), 1.0), 0.0, 1.0)


def _dedupe_key(row: dict) -> str:
    return (
        str(row.get("event_key") or "").strip()
        or str(row.get("market_id") or "").strip()
        or str(row.get("event_id") or "").strip()
        or str(row.get("title") or "").strip()
    )


def load_samples(files: List[str], args) -> tuple[List[ReplaySample], int]:
    out: List[ReplaySample] = []
    rows_read = 0

    cutoff_ms = 0
    if float(args.hours or 0.0) > 0:
        cutoff_ms = int((time.time() - float(args.hours) * 3600.0) * 1000.0)
    min_gap_ms = max(0, int(args.min_gap_ms_per_event or 0))
    last_ts_by_key: dict[str, int] = {}

    for path in files:
        for row in iter_metric_rows(path):
            rows_read += 1

            ts_ms = parse_ts_ms(row)
            if cutoff_ms > 0 and ts_ms > 0 and ts_ms < cutoff_ms:
                continue
            if min_gap_ms > 0 and ts_ms > 0:
                key = _dedupe_key(row)
                if key:
                    last_ts = int(last_ts_by_key.get(key, 0) or 0)
                    if last_ts > 0 and (ts_ms - last_ts) < min_gap_ms:
                        continue
                    last_ts_by_key[key] = ts_ms
            if args.require_threshold_pass and not as_bool(row.get("passes_raw_threshold")):
                continue

            edge, cost = _pick_edge_cost(row, args.edge_mode)
            if not math.isfinite(edge) or not math.isfinite(cost) or cost <= 0:
                continue
            if edge < float(args.min_edge_usd):
                continue

            fill_ratio = _pick_fill_ratio(row, args.fill_ratio_mode)
            if fill_ratio < float(args.min_fill_ratio):
                continue
            worst_stale_sec = as_float(row.get("worst_book_stale_sec"), 0.0)
            if not math.isfinite(worst_stale_sec):
                worst_stale_sec = 0.0
            if float(args.max_worst_stale_sec) > 0 and worst_stale_sec > float(args.max_worst_stale_sec):
                continue

            gross_ret = edge / cost
            proxy_ret = gross_ret * fill_ratio
            proxy_ret -= float(args.miss_penalty) * (1.0 - fill_ratio)
            stale_excess = max(0.0, worst_stale_sec - float(args.stale_grace_sec))
            proxy_ret -= float(args.stale_penalty_per_sec) * stale_excess
            proxy_ret = clamp(proxy_ret, -0.99, 5.0)

            out.append(
                ReplaySample(
                    ts_ms=ts_ms,
                    edge=float(edge),
                    cost=float(cost),
                    fill_ratio=float(fill_ratio),
                    worst_stale_sec=float(worst_stale_sec),
                    proxy_return=float(proxy_ret),
                )
            )

    out.sort(key=lambda x: x.ts_ms)
    if int(args.max_samples or 0) > 0 and len(out) > int(args.max_samples):
        out = out[-int(args.max_samples) :]
    return out, rows_read


def estimate_full_kelly(returns: List[float], max_full: float) -> tuple[float, float, float]:
    if not returns:
        return 0.0, 0.0, 0.0
    mu = float(statistics.mean(returns))
    var = float(statistics.pvariance(returns)) if len(returns) >= 2 else 0.0
    if var <= 1e-12:
        full = max_full if mu > 0 else 0.0
    else:
        full = mu / var
    full = clamp(full, 0.0, max_full)
    return full, mu, var


def expected_log_growth(returns: List[float], fraction: float) -> float:
    if not returns:
        return float("-inf")
    f = max(0.0, float(fraction))
    vals: List[float] = []
    for r in returns:
        x = 1.0 + (f * float(r))
        if x <= 0:
            return float("-inf")
        vals.append(math.log(x))
    return float(statistics.mean(vals)) if vals else float("-inf")


def bootstrap_growth(
    returns: List[float],
    fraction: float,
    iters: int,
    draw_n: int,
    rng: random.Random,
) -> dict:
    n = len(returns)
    if n <= 0 or iters <= 0:
        return {
            "iterations": 0,
            "sample_size": 0,
            "mean_log_growth": None,
            "p05_log_growth": None,
            "p50_log_growth": None,
            "p95_log_growth": None,
            "prob_negative_log_growth": None,
        }

    m = max(1, int(draw_n or n))
    gs: List[float] = []
    for _ in range(int(iters)):
        sample = [returns[rng.randrange(n)] for _ in range(m)]
        g = expected_log_growth(sample, fraction)
        if math.isfinite(g):
            gs.append(g)

    if not gs:
        return {
            "iterations": int(iters),
            "sample_size": int(m),
            "mean_log_growth": None,
            "p05_log_growth": None,
            "p50_log_growth": None,
            "p95_log_growth": None,
            "prob_negative_log_growth": None,
        }

    neg = sum(1 for x in gs if x < 0.0)
    return {
        "iterations": int(iters),
        "sample_size": int(m),
        "mean_log_growth": float(statistics.mean(gs)),
        "p05_log_growth": percentile(gs, 0.05),
        "p50_log_growth": percentile(gs, 0.50),
        "p95_log_growth": percentile(gs, 0.95),
        "prob_negative_log_growth": float(neg) / float(len(gs)),
    }


def default_metrics_path() -> str:
    root = Path(__file__).resolve().parents[1]
    return str(root / "logs" / "clob-arb-monitor-metrics.jsonl")


def default_out_path() -> str:
    root = Path(__file__).resolve().parents[1]
    return str(root / "logs" / "clob-arb-kelly-replay-summary.json")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Replay CLOB arb metrics and estimate fractional Kelly scales")
    p.add_argument("--metrics-file", default=default_metrics_path(), help="Metrics JSONL path (comma-separated allowed)")
    p.add_argument("--metrics-glob", default="", help="Optional glob for additional metrics files")
    p.add_argument("--hours", type=float, default=0.0, help="Only use rows from the last N hours (0=all)")
    p.add_argument("--edge-mode", choices=("raw", "exec"), default="exec", help="Edge/cost series to replay")
    p.add_argument(
        "--fill-ratio-mode",
        choices=("min", "avg", "none"),
        default="min",
        help="Fillability proxy used in return adjustment",
    )
    p.add_argument("--miss-penalty", type=float, default=0.002, help="Penalty applied to unfilled fraction of each sample")
    p.add_argument("--min-fill-ratio", type=float, default=0.0, help="Skip samples with fill ratio below this value")
    p.add_argument("--stale-grace-sec", type=float, default=2.0, help="No stale penalty inside this grace window (seconds)")
    p.add_argument("--stale-penalty-per-sec", type=float, default=0.0, help="Per-second penalty applied to worst stale-book lag beyond grace")
    p.add_argument("--max-worst-stale-sec", type=float, default=0.0, help="Skip samples whose worst stale-book lag exceeds this (0=disabled)")
    p.add_argument("--min-edge-usd", type=float, default=-1e9, help="Skip samples below this edge (USD)")
    p.add_argument("--min-gap-ms-per-event", type=int, default=0, help="Optional dedupe: keep at most one sample per event key within this interval")
    p.add_argument("--require-threshold-pass", action="store_true", help="Only use rows that passed raw alert threshold")
    p.add_argument("--max-samples", type=int, default=0, help="Keep only most recent N samples (0=all)")
    p.add_argument("--max-full-kelly", type=float, default=1.0, help="Clamp for estimated full Kelly fraction")
    p.add_argument("--scales", default="0.25,0.50,1.00", help="Comma-separated fractions of full Kelly to evaluate")
    p.add_argument("--bootstrap-iters", type=int, default=2000, help="Bootstrap iterations per scale (0=disable)")
    p.add_argument("--bootstrap-sample-size", type=int, default=0, help="Bootstrap draw size per iteration (0=use sample size)")
    p.add_argument("--seed", type=int, default=42, help="Random seed for bootstrap")
    p.add_argument("--out-json", default=default_out_path(), help="Output summary JSON path")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    files = resolve_metric_files(args.metrics_file, args.metrics_glob)
    if not files:
        print(
            f"No metrics files found. --metrics-file={args.metrics_file} --metrics-glob={args.metrics_glob}",
        )
        return 2

    samples, rows_read = load_samples(files, args)
    if not samples:
        print(f"No usable samples. rows_read={rows_read} files={len(files)}")
        return 1

    returns = [s.proxy_return for s in samples]
    full_kelly, mu, var = estimate_full_kelly(returns, max_full=float(args.max_full_kelly))
    stdev = math.sqrt(var) if var > 0 else 0.0

    rng = random.Random(int(args.seed))
    scales = parse_scales(args.scales)

    scale_rows = []
    draw_n = int(args.bootstrap_sample_size or 0)
    for scale in scales:
        f = float(scale) * float(full_kelly)
        exp_g = expected_log_growth(returns, f)
        boot = bootstrap_growth(
            returns=returns,
            fraction=f,
            iters=int(args.bootstrap_iters or 0),
            draw_n=draw_n,
            rng=rng,
        )
        scale_rows.append(
            {
                "scale_of_full_kelly": float(scale),
                "effective_fraction": float(f),
                "expected_log_growth": float(exp_g) if math.isfinite(exp_g) else None,
                "bootstrap": boot,
            }
        )

    summary = {
        "generated_at": now_utc().isoformat(),
        "config": {
            "metrics_file": args.metrics_file,
            "metrics_glob": args.metrics_glob,
            "hours": float(args.hours),
            "edge_mode": args.edge_mode,
            "fill_ratio_mode": args.fill_ratio_mode,
            "miss_penalty": float(args.miss_penalty),
            "min_fill_ratio": float(args.min_fill_ratio),
            "stale_grace_sec": float(args.stale_grace_sec),
            "stale_penalty_per_sec": float(args.stale_penalty_per_sec),
            "max_worst_stale_sec": float(args.max_worst_stale_sec),
            "min_edge_usd": float(args.min_edge_usd),
            "min_gap_ms_per_event": int(args.min_gap_ms_per_event),
            "require_threshold_pass": bool(args.require_threshold_pass),
            "max_samples": int(args.max_samples),
            "max_full_kelly": float(args.max_full_kelly),
            "scales": scales,
            "bootstrap_iters": int(args.bootstrap_iters),
            "bootstrap_sample_size": int(args.bootstrap_sample_size),
            "seed": int(args.seed),
        },
        "data": {
            "files": files,
            "rows_read": int(rows_read),
            "sample_count": len(samples),
            "first_ts_ms": int(samples[0].ts_ms),
            "last_ts_ms": int(samples[-1].ts_ms),
        },
        "returns": {
            "mean": float(mu),
            "stdev": float(stdev),
            "variance": float(var),
            "min": float(min(returns)),
            "max": float(max(returns)),
            "p05": percentile(returns, 0.05),
            "p50": percentile(returns, 0.50),
            "p95": percentile(returns, 0.95),
            "worst_stale_sec_p50": percentile([s.worst_stale_sec for s in samples], 0.50),
            "worst_stale_sec_p95": percentile([s.worst_stale_sec for s in samples], 0.95),
        },
        "kelly": {
            "full_fraction_estimate": float(full_kelly),
            "scales": scale_rows,
        },
    }

    out_path = Path(str(args.out_json))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        if args.pretty:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        else:
            json.dump(summary, f, separators=(",", ":"), ensure_ascii=False)
            f.write("\n")

    print(f"Files: {len(files)} | rows_read={rows_read} | samples={len(samples)}")
    print(
        f"Return proxy: mean={mu:.6f} stdev={stdev:.6f} "
        f"p05={summary['returns']['p05']:.6f} p50={summary['returns']['p50']:.6f} p95={summary['returns']['p95']:.6f}"
    )
    print(f"Estimated full Kelly: {full_kelly:.4f}")
    for row in scale_rows:
        sc = row["scale_of_full_kelly"]
        eff = row["effective_fraction"]
        g = row["expected_log_growth"]
        boot = row["bootstrap"]
        g_s = f"{g:.8f}" if isinstance(g, float) else "NA"
        p50 = boot.get("p50_log_growth")
        p05 = boot.get("p05_log_growth")
        pneg = boot.get("prob_negative_log_growth")
        p50_s = f"{float(p50):.8f}" if p50 is not None else "NA"
        p05_s = f"{float(p05):.8f}" if p05 is not None else "NA"
        pneg_s = f"{float(pneg):.2%}" if pneg is not None else "NA"
        print(
            f"  scale={sc:.2f} effective={eff:.4f} "
            f"E[log]={g_s} boot_p50={p50_s} boot_p05={p05_s} boot_neg={pneg_s}"
        )
    print(f"Saved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
