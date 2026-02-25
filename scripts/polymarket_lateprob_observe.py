#!/usr/bin/env python3
"""
Observe-only late-resolution high-probability validator for Polymarket.

Modes:
- screen: scan active markets near resolution that are already high-probability
- backtest: validate the same fixed rule on closed markets using prices-history
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
USER_AGENT = "Mozilla/5.0 (compatible; lateprob-observe/1.0)"
DEFAULT_EXCLUDE_KEYWORDS = ["draft", "inflation", "unemployment", "interest rate", "fed "]
DEFAULT_WEATHER_INCLUDE_REGEX = r"weather|temperature|precipitation|forecast|\brain\b|\bsnow\b|\bwind\b|humidity"


def safe_print(msg: str) -> None:
    try:
        print(msg)
    except UnicodeEncodeError:
        try:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))
        except Exception:
            pass


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_day() -> str:
    return now_utc().strftime("%Y-%m-%d")


def utc_tag() -> str:
    return now_utc().strftime("%Y%m%d_%H%M%S")


def as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def parse_list_field(value) -> List:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            return []
    return []


def parse_iso_ts(value: str) -> Optional[int]:
    if not value:
        return None
    try:
        return int(dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def fetch_json(url: str, timeout_sec: float = 30.0, retries: int = 3) -> Optional[object]:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for i in range(retries):
        try:
            with urlopen(req, timeout=timeout_sec) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError):
            if i >= retries - 1:
                return None
            time.sleep(0.25 * (i + 1))
    return None


def fetch_markets_page(params: Dict[str, str]) -> List[dict]:
    q = urlencode(params)
    data = fetch_json(f"{GAMMA_API_BASE}/markets?{q}", timeout_sec=35.0, retries=4)
    return data if isinstance(data, list) else []


def extract_yes_no(market: dict) -> Optional[Tuple[str, float]]:
    outcomes = [str(x).strip().lower() for x in parse_list_field(market.get("outcomes"))]
    token_ids = [str(x) for x in parse_list_field(market.get("clobTokenIds"))]
    prices = [as_float(x, -1.0) for x in parse_list_field(market.get("outcomePrices"))]
    if len(outcomes) != 2 or len(token_ids) != 2 or len(prices) != 2:
        return None
    if "yes" not in outcomes or "no" not in outcomes:
        return None
    yes_i = outcomes.index("yes")
    yes_price = prices[yes_i]
    if not (0.0 <= yes_price <= 1.0):
        return None
    return token_ids[yes_i], yes_price


def compile_regex(pattern: str) -> Optional[re.Pattern]:
    p = (pattern or "").strip()
    if not p:
        return None
    return re.compile(p, re.IGNORECASE)


def parse_keywords(raw: str) -> List[str]:
    out: List[str] = []
    for token in (raw or "").split(","):
        t = token.strip().lower()
        if t:
            out.append(t)
    return out


def question_allowed(
    question: str,
    include_rx: Optional[re.Pattern],
    exclude_rx: Optional[re.Pattern],
    exclude_keywords: List[str],
) -> bool:
    q = (question or "").strip()
    ql = q.lower()
    if include_rx and not include_rx.search(q):
        return False
    if exclude_rx and exclude_rx.search(q):
        return False
    return not any(kw in ql for kw in exclude_keywords)


def decide_side(
    yes_price: float,
    side_mode: str,
    yes_high_min: float,
    yes_high_max: float,
    yes_low_min: float,
    yes_low_max: float,
) -> Optional[Tuple[str, float]]:
    if side_mode in {"both", "yes-only"} and yes_high_min <= yes_price <= yes_high_max:
        return ("yes", yes_price)
    if side_mode in {"both", "no-only"} and yes_low_min <= yes_price <= yes_low_max:
        return ("no", 1.0 - yes_price)
    return None


@dataclass
class ScreenRow:
    market_id: str
    question: str
    category: str
    end_iso: str
    hours_to_end: float
    yes_price: float
    side: str
    entry_price: float
    liquidity_num: float
    volume_24h: float


def run_screen(args: argparse.Namespace) -> int:
    include_rx = compile_regex(args.include_regex)
    exclude_rx = compile_regex(args.exclude_regex)
    exclude_keywords = parse_keywords(args.exclude_keywords)
    now_ts = int(now_utc().timestamp())
    rows: List[ScreenRow] = []
    dropped_stale_active = 0

    for page in range(args.max_pages):
        params = {"active": "true", "closed": "false", "limit": str(args.page_size), "offset": str(page * args.page_size)}
        markets = fetch_markets_page(params)
        if not markets:
            break
        for m in markets:
            yn = extract_yes_no(m)
            if not yn:
                continue
            _yes_token, yes_price = yn
            end_iso = str(m.get("endDate") or "")
            end_ts = parse_iso_ts(end_iso)
            if end_ts is None:
                continue
            hours_to_end = (end_ts - now_ts) / 3600.0
            if args.max_active_stale_hours >= 0 and hours_to_end < (-1.0 * args.max_active_stale_hours):
                dropped_stale_active += 1
                continue
            if hours_to_end < args.min_hours_to_end:
                continue
            if args.max_hours_to_end > 0 and hours_to_end > args.max_hours_to_end:
                continue
            question = str(m.get("question") or "")
            if not question_allowed(question, include_rx, exclude_rx, exclude_keywords):
                continue
            liq = as_float(m.get("liquidityNum"), as_float(m.get("liquidity"), 0.0))
            vol24 = as_float(m.get("volume24hr"), 0.0)
            if liq < args.min_liquidity or vol24 < args.min_volume_24h:
                continue
            decision = decide_side(
                yes_price=yes_price,
                side_mode=args.side_mode,
                yes_high_min=args.yes_high_min,
                yes_high_max=args.yes_high_max,
                yes_low_min=args.yes_low_min,
                yes_low_max=args.yes_low_max,
            )
            if decision is None:
                continue
            side, entry_price = decision
            rows.append(
                ScreenRow(
                    market_id=str(m.get("id") or ""),
                    question=question,
                    category=str(m.get("category") or ""),
                    end_iso=end_iso,
                    hours_to_end=float(hours_to_end),
                    yes_price=float(yes_price),
                    side=side,
                    entry_price=float(entry_price),
                    liquidity_num=float(liq),
                    volume_24h=float(vol24),
                )
            )

    rows.sort(key=lambda r: (r.hours_to_end, r.entry_price))
    safe_print(
        f"[screen] candidates={len(rows)} side={args.side_mode} "
        f"dropped_stale_active={dropped_stale_active} max_stale_h={args.max_active_stale_hours:g}"
    )
    for r in rows[: args.top_n]:
        max_profit = 1.0 - r.entry_price
        net_est = max_profit - args.per_trade_cost
        safe_print(
            f"side={r.side:3s} entry={r.entry_price:0.4f} max={max_profit:0.4f} net~={net_est:+0.4f} "
            f"yes={r.yes_price:0.4f} h2e={r.hours_to_end:6.2f} liq={r.liquidity_num:10.0f} vol24h={r.volume_24h:10.0f} | "
            f"{r.question[:96]}"
        )

    out_csv = args.out_csv or f"logs/lateprob_screen_{utc_tag()}.csv"
    out_json = args.out_json or f"logs/lateprob_screen_{utc_tag()}.json"
    Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
    Path(out_json).parent.mkdir(parents=True, exist_ok=True)
    with Path(out_csv).open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(ScreenRow.__dataclass_fields__.keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))
    summary = {
        "generated_utc": now_utc().isoformat(),
        "settings": vars(args),
        "count": len(rows),
        "dropped_stale_active": dropped_stale_active,
        "top": [asdict(r) for r in rows[: args.top_n]],
        "artifacts": {"csv": out_csv},
    }
    Path(out_json).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    safe_print(f"[screen] wrote {out_csv}")
    safe_print(f"[screen] wrote {out_json}")
    return 0


@dataclass
class ClosedSample:
    market_id: str
    question: str
    category: str
    end_ts: int
    end_iso: str
    yes_token: str
    yes_won: int
    cutoff_ts: int
    entry_ts: int
    stale_h: float
    target_hours_before_end: float
    actual_hours_before_end: float
    timing_abs_error_h: float
    entry_yes_price: float
    side: str
    entry_price: float
    pnl_per_share: float
    won: int
    liquidity_num: float
    volume_24h: float
    history_points: int


def iter_offsets(args: argparse.Namespace) -> List[int]:
    if args.sampling_mode == "contiguous":
        return [i * args.page_size for i in range(args.max_pages)]
    return list(range(args.offset_start, args.max_offset + 1, args.offset_step))


def build_closed_candidates(args: argparse.Namespace) -> List[dict]:
    now_ts = int(now_utc().timestamp())
    rows: List[dict] = []
    seen = set()
    for i, offset in enumerate(iter_offsets(args), 1):
        params = {
            "closed": "true",
            "end_date_min": args.date_min,
            "end_date_max": args.date_max,
            "order": "endDate",
            "ascending": "false",
            "limit": str(args.page_size),
            "offset": str(offset),
        }
        markets = fetch_markets_page(params)
        if not markets:
            if args.sampling_mode == "contiguous":
                break
            continue
        if i % args.progress_every == 0:
            safe_print(f"[backtest] fetched {i} offsets | raw_rows={len(rows)}")
        for m in markets:
            market_id = str(m.get("id") or "")
            if not market_id or market_id in seen:
                continue
            yn = extract_yes_no(m)
            if not yn:
                continue
            yes_token, yes_settle = yn
            yes_won = 1 if yes_settle >= 0.99 else (0 if yes_settle <= 0.01 else -1)
            if yes_won < 0:
                continue
            end_iso = str(m.get("endDate") or "")
            end_ts = parse_iso_ts(end_iso)
            if end_ts is None or end_ts > now_ts:
                continue
            cutoff_ts = end_ts - int(args.hours_before_end * 3600.0)
            if cutoff_ts <= 0:
                continue
            seen.add(market_id)
            rows.append(
                {
                    "market_id": market_id,
                    "question": str(m.get("question") or ""),
                    "category": str(m.get("category") or ""),
                    "end_ts": int(end_ts),
                    "end_iso": end_iso,
                    "yes_token": yes_token,
                    "yes_won": yes_won,
                    "cutoff_ts": int(cutoff_ts),
                    "liquidity_num": as_float(m.get("liquidityNum"), as_float(m.get("liquidity"), 0.0)),
                    "volume_24h": as_float(m.get("volume24hr"), 0.0),
                }
            )
            if args.max_candidates > 0 and len(rows) >= args.max_candidates:
                return rows
    return rows


def fetch_entry_sample(candidate: dict, args: argparse.Namespace) -> Optional[ClosedSample]:
    start_ts = int(candidate["cutoff_ts"] - args.lookback_hours * 3600.0)
    end_ts = int(candidate["cutoff_ts"])
    q = urlencode({"market": str(candidate["yes_token"]), "startTs": str(start_ts), "endTs": str(end_ts), "fidelity": str(args.history_fidelity)})
    data = fetch_json(f"{CLOB_API_BASE}/prices-history?{q}", timeout_sec=28.0, retries=3)
    if not isinstance(data, dict):
        return None
    hist = data.get("history")
    if not isinstance(hist, list) or not hist:
        return None
    last = hist[-1] or {}
    entry_ts = int(as_float(last.get("t"), -1))
    entry_yes = as_float(last.get("p"), -1.0)
    if entry_ts <= 0 or not (0.0 <= entry_yes <= 1.0):
        return None
    stale_h = float(candidate["cutoff_ts"] - entry_ts) / 3600.0
    if stale_h > args.max_stale_hours:
        return None
    target_h = float(args.hours_before_end)
    actual_h = float(candidate["end_ts"] - entry_ts) / 3600.0
    timing_abs_err_h = abs(actual_h - target_h)
    decision = decide_side(
        yes_price=entry_yes,
        side_mode=args.side_mode,
        yes_high_min=args.yes_high_min,
        yes_high_max=args.yes_high_max,
        yes_low_min=args.yes_low_min,
        yes_low_max=args.yes_low_max,
    )
    if decision is None:
        return None
    side, entry_price = decision
    yes_won = int(candidate["yes_won"])
    payout = 1.0 if ((side == "yes" and yes_won == 1) or (side == "no" and yes_won == 0)) else 0.0
    pnl = payout - entry_price
    return ClosedSample(
        market_id=str(candidate["market_id"]),
        question=str(candidate["question"]),
        category=str(candidate["category"]),
        end_ts=int(candidate["end_ts"]),
        end_iso=str(candidate["end_iso"]),
        yes_token=str(candidate["yes_token"]),
        yes_won=yes_won,
        cutoff_ts=int(candidate["cutoff_ts"]),
        entry_ts=entry_ts,
        stale_h=float(stale_h),
        target_hours_before_end=target_h,
        actual_hours_before_end=actual_h,
        timing_abs_error_h=timing_abs_err_h,
        entry_yes_price=float(entry_yes),
        side=side,
        entry_price=float(entry_price),
        pnl_per_share=float(pnl),
        won=1 if payout > 0.5 else 0,
        liquidity_num=float(candidate["liquidity_num"]),
        volume_24h=float(candidate["volume_24h"]),
        history_points=len(hist),
    )


def apply_risk_caps(rows: List[ClosedSample], max_open_positions: int, max_open_per_category: int) -> List[ClosedSample]:
    ordered = sorted(rows, key=lambda r: (r.entry_ts, r.end_ts, r.market_id))
    if max_open_positions <= 0 and max_open_per_category <= 0:
        return ordered
    active: List[ClosedSample] = []
    kept: List[ClosedSample] = []
    for r in ordered:
        active = [x for x in active if x.end_ts > r.entry_ts]
        if max_open_positions > 0 and len(active) >= max_open_positions:
            continue
        if max_open_per_category > 0 and sum(1 for x in active if x.category == r.category) >= max_open_per_category:
            continue
        active.append(r)
        kept.append(r)
    return kept


def metrics(rows: Iterable[ClosedSample], per_trade_cost: float) -> dict:
    xs = list(rows)
    if not xs:
        return {"n": 0, "win_rate": 0.0, "capital_return": 0.0, "avg_pnl_per_trade": 0.0}
    gross_pnl = sum(r.pnl_per_share for r in xs)
    total_entry = sum(r.entry_price for r in xs)
    total_fee = per_trade_cost * len(xs)
    net_pnl = gross_pnl - total_fee
    cost = total_entry + total_fee
    return {
        "n": len(xs),
        "wins": sum(1 for r in xs if r.won == 1),
        "losses": sum(1 for r in xs if r.won == 0),
        "win_rate": sum(1 for r in xs if r.won == 1) / len(xs),
        "capital_return": (net_pnl / cost) if cost > 1e-12 else 0.0,
        "avg_pnl_per_trade": net_pnl / len(xs),
        "median_pnl_per_trade": statistics.median(r.pnl_per_share for r in xs),
        "worst_loss": min(r.pnl_per_share for r in xs),
        "best_trade": max(r.pnl_per_share for r in xs),
    }


def _percentile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    if q <= 0.0:
        return float(xs[0])
    if q >= 1.0:
        return float(xs[-1])
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return float(xs[lo] * (1.0 - frac) + xs[hi] * frac)


def timing_quality(rows: Iterable[ClosedSample], target_hours_before_end: float) -> dict:
    xs = list(rows)
    if not xs:
        return {
            "n": 0,
            "target_hours_before_end": float(target_hours_before_end),
            "actual_hours_before_end": {"min": 0.0, "median": 0.0, "p90": 0.0, "p99": 0.0, "max": 0.0},
            "abs_error_hours": {"median": 0.0, "p90": 0.0, "p99": 0.0, "max": 0.0},
            "within_5m_ratio": 0.0,
            "within_15m_ratio": 0.0,
            "early_entry_ratio": 0.0,
            "late_entry_ratio": 0.0,
        }

    actual = [float(r.actual_hours_before_end) for r in xs]
    abs_err = [float(r.timing_abs_error_h) for r in xs]
    n = len(xs)
    eps = 1.0 / 60.0
    early = sum(1 for x in actual if x > float(target_hours_before_end) + eps)
    late = sum(1 for x in actual if x < float(target_hours_before_end) - eps)

    return {
        "n": n,
        "target_hours_before_end": float(target_hours_before_end),
        "actual_hours_before_end": {
            "min": min(actual),
            "median": statistics.median(actual),
            "p90": _percentile(actual, 0.90),
            "p99": _percentile(actual, 0.99),
            "max": max(actual),
        },
        "abs_error_hours": {
            "median": statistics.median(abs_err),
            "p90": _percentile(abs_err, 0.90),
            "p99": _percentile(abs_err, 0.99),
            "max": max(abs_err),
        },
        "within_5m_ratio": sum(1 for e in abs_err if e <= (5.0 / 60.0)) / n,
        "within_15m_ratio": sum(1 for e in abs_err if e <= (15.0 / 60.0)) / n,
        "early_entry_ratio": early / n,
        "late_entry_ratio": late / n,
    }


def run_backtest(args: argparse.Namespace) -> int:
    include_rx = compile_regex(args.include_regex)
    exclude_rx = compile_regex(args.exclude_regex)
    exclude_keywords = parse_keywords(args.exclude_keywords)
    candidates = build_closed_candidates(args)
    safe_print(f"[backtest] candidates={len(candidates)}")
    rows: List[ClosedSample] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(fetch_entry_sample, c, args) for c in candidates]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r is not None and question_allowed(r.question, include_rx, exclude_rx, exclude_keywords):
                if r.liquidity_num >= args.min_liquidity and r.volume_24h >= args.min_volume_24h:
                    rows.append(r)
            if i % args.progress_every == 0:
                safe_print(f"[backtest] priced {i}/{len(futs)} | usable={len(rows)}")
    rows = apply_risk_caps(rows, args.max_open_positions, args.max_open_per_category)
    rows.sort(key=lambda x: x.end_ts)

    out_samples = args.out_samples_csv or f"logs/lateprob_backtest_samples_{utc_tag()}.csv"
    Path(out_samples).parent.mkdir(parents=True, exist_ok=True)
    with Path(out_samples).open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(ClosedSample.__dataclass_fields__.keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))

    overall = metrics(rows, per_trade_cost=args.per_trade_cost)
    overall_timing = timing_quality(rows, target_hours_before_end=args.hours_before_end)
    by_side = {
        "yes": metrics([r for r in rows if r.side == "yes"], per_trade_cost=args.per_trade_cost),
        "no": metrics([r for r in rows if r.side == "no"], per_trade_cost=args.per_trade_cost),
    }
    timing_by_side = {
        "yes": timing_quality([r for r in rows if r.side == "yes"], target_hours_before_end=args.hours_before_end),
        "no": timing_quality([r for r in rows if r.side == "no"], target_hours_before_end=args.hours_before_end),
    }
    by_quarter: Dict[str, dict] = {}
    for r in rows:
        d = dt.datetime.fromtimestamp(r.end_ts, tz=dt.timezone.utc)
        q = (d.month - 1) // 3 + 1
        k = f"{d.year}-Q{q}"
        by_quarter.setdefault(k, {"rows": []})["rows"].append(r)
    for k, v in by_quarter.items():
        v["metrics"] = metrics(v["rows"], per_trade_cost=args.per_trade_cost)
        v["timing_quality"] = timing_quality(v["rows"], target_hours_before_end=args.hours_before_end)
        v.pop("rows", None)

    summary = {
        "generated_utc": now_utc().isoformat(),
        "settings": vars(args),
        "overall": overall,
        "timing_quality": overall_timing,
        "by_side": by_side,
        "timing_quality_by_side": timing_by_side,
        "by_quarter": by_quarter,
        "artifacts": {"samples_csv": out_samples},
    }
    out_json = args.out_summary_json or f"logs/lateprob_backtest_summary_{utc_tag()}.json"
    Path(out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(out_json).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    safe_print(f"[backtest] n={overall['n']} ret={overall['capital_return']:+.4%} win={overall['win_rate']:.2%}")
    safe_print(
        f"[backtest] timing target={args.hours_before_end:g}h "
        f"actual_median={overall_timing['actual_hours_before_end']['median']:.3f}h "
        f"abs_err_med={overall_timing['abs_error_hours']['median']*60.0:.1f}m "
        f"within15m={overall_timing['within_15m_ratio']:.1%}"
    )
    safe_print(f"[backtest] wrote {out_samples}")
    safe_print(f"[backtest] wrote {out_json}")
    return 0 if rows else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Observe-only late-resolution high-probability validator (Polymarket).")
    sub = p.add_subparsers(dest="mode", required=True)

    ps = sub.add_parser("screen", help="Active market screener for late high-probability entries.")
    ps.add_argument("--max-pages", type=int, default=4)
    ps.add_argument("--page-size", type=int, default=500)
    ps.add_argument("--min-hours-to-end", type=float, default=0.0)
    ps.add_argument("--max-hours-to-end", type=float, default=0.5)
    ps.add_argument("--max-active-stale-hours", type=float, default=6.0)
    ps.add_argument("--side-mode", choices=["both", "yes-only", "no-only"], default="both")
    ps.add_argument("--yes-high-min", type=float, default=0.90)
    ps.add_argument("--yes-high-max", type=float, default=0.99)
    ps.add_argument("--yes-low-min", type=float, default=0.01)
    ps.add_argument("--yes-low-max", type=float, default=0.10)
    ps.add_argument("--min-liquidity", type=float, default=0.0)
    ps.add_argument("--min-volume-24h", type=float, default=0.0)
    ps.add_argument("--per-trade-cost", type=float, default=0.0)
    ps.add_argument("--exclude-keywords", default=",".join(DEFAULT_EXCLUDE_KEYWORDS))
    ps.add_argument("--include-regex", default=DEFAULT_WEATHER_INCLUDE_REGEX)
    ps.add_argument("--exclude-regex", default="")
    ps.add_argument("--top-n", type=int, default=30)
    ps.add_argument("--out-csv", default="")
    ps.add_argument("--out-json", default="")

    pb = sub.add_parser("backtest", help="Closed market backtest for fixed late high-probability rule.")
    pb.add_argument("--date-min", default="2024-01-01")
    pb.add_argument("--date-max", default=utc_day())
    pb.add_argument("--sampling-mode", choices=["stratified", "contiguous"], default="stratified")
    pb.add_argument("--page-size", type=int, default=500)
    pb.add_argument("--max-pages", type=int, default=120)
    pb.add_argument("--offset-start", type=int, default=0)
    pb.add_argument("--offset-step", type=int, default=5000)
    pb.add_argument("--max-offset", type=int, default=425000)
    pb.add_argument("--max-candidates", type=int, default=0)
    pb.add_argument("--hours-before-end", type=float, default=0.25)
    pb.add_argument("--lookback-hours", type=float, default=12.0)
    pb.add_argument("--max-stale-hours", type=float, default=0.5)
    pb.add_argument("--history-fidelity", type=int, default=10)
    pb.add_argument("--workers", type=int, default=16)
    pb.add_argument("--progress-every", type=int, default=100)
    pb.add_argument("--side-mode", choices=["both", "yes-only", "no-only"], default="both")
    pb.add_argument("--yes-high-min", type=float, default=0.90)
    pb.add_argument("--yes-high-max", type=float, default=0.99)
    pb.add_argument("--yes-low-min", type=float, default=0.01)
    pb.add_argument("--yes-low-max", type=float, default=0.10)
    pb.add_argument("--per-trade-cost", type=float, default=0.002)
    pb.add_argument("--min-liquidity", type=float, default=0.0)
    pb.add_argument("--min-volume-24h", type=float, default=0.0)
    pb.add_argument("--max-open-positions", type=int, default=0)
    pb.add_argument("--max-open-per-category", type=int, default=0)
    pb.add_argument("--exclude-keywords", default=",".join(DEFAULT_EXCLUDE_KEYWORDS))
    pb.add_argument("--include-regex", default="")
    pb.add_argument("--exclude-regex", default="")
    pb.add_argument("--out-samples-csv", default="")
    pb.add_argument("--out-summary-json", default="")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.mode == "screen":
        return run_screen(args)
    return run_backtest(args)


if __name__ == "__main__":
    raise SystemExit(main())
