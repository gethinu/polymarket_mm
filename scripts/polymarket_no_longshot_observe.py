#!/usr/bin/env python3
"""
Observe-only longshot-NO screener and walk-forward validator for Polymarket.

Modes:
- screen: active markets filter (no execution)
- walkforward: closed markets backtest + out-of-sample walk-forward validation
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from polymarket_clob_arb_scanner import parse_bucket_bounds


GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
USER_AGENT = "Mozilla/5.0 (compatible; no-longshot-observe/1.0)"

DEFAULT_EXCLUDE_KEYWORDS = [
    "draft",
    "inflation",
    "unemployment",
    "interest rate",
    "fed ",
    "sentenced",
    "prison",
]
ANNUALIZED_MIN_CONFIDENCE_DAYS = 90.0


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


def compile_regex(pattern: str) -> Optional[re.Pattern]:
    p = (pattern or "").strip()
    if not p:
        return None
    return re.compile(p, re.IGNORECASE)


def parse_keywords(raw: str) -> List[str]:
    if not raw:
        return []
    out: List[str] = []
    for token in raw.split(","):
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
    for kw in exclude_keywords:
        if kw in ql:
            return False
    return True


def period_key(ts: int, frequency: str) -> str:
    d = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
    if frequency == "year":
        return f"{d.year}"
    if frequency == "quarter":
        q = (d.month - 1) // 3 + 1
        return f"{d.year}-Q{q}"
    h = 1 if d.month <= 6 else 2
    return f"{d.year}-H{h}"


def period_sort_key(key: str, frequency: str) -> Tuple[int, int]:
    if frequency == "year":
        return (int(key), 0)
    if frequency == "quarter":
        a, b = key.split("-Q")
        return (int(a), int(b))
    a, b = key.split("-H")
    return (int(a), int(b))


def parse_float_grid(raw: str) -> List[float]:
    out: List[float] = []
    for t in (raw or "").split(","):
        s = t.strip()
        if not s:
            continue
        try:
            out.append(float(s))
        except ValueError:
            continue
    return sorted(set(out))


def extract_yes_no(market: dict) -> Optional[Tuple[str, float, float]]:
    outcomes = [str(x).strip().lower() for x in parse_list_field(market.get("outcomes"))]
    token_ids = [str(x) for x in parse_list_field(market.get("clobTokenIds"))]
    prices_raw = parse_list_field(market.get("outcomePrices"))
    prices: List[float] = []
    for x in prices_raw:
        try:
            prices.append(float(x))
        except Exception:
            return None

    if len(outcomes) != 2 or len(token_ids) != 2 or len(prices) != 2:
        return None
    if "yes" not in outcomes or "no" not in outcomes:
        return None

    yes_i = outcomes.index("yes")
    return token_ids[yes_i], float(prices[yes_i]), float(prices[1 - yes_i])


def fetch_markets_page(params: Dict[str, str]) -> List[dict]:
    q = urlencode(params)
    url = f"{GAMMA_API_BASE}/markets?{q}"
    data = fetch_json(url, timeout_sec=35.0, retries=4)
    if isinstance(data, list):
        return data
    return []


@dataclass
class ScreenRow:
    market_id: str
    question: str
    end_iso: str
    days_to_end: float
    hours_to_end: float
    yes_price: float
    no_price: float
    gross_yield_on_no: float
    net_yield_on_no: float
    gross_yield_per_day: float
    net_yield_per_day: float
    liquidity_num: float
    volume_24h: float
    category: str


@dataclass
class GapMarket:
    market_id: str
    question: str
    event_key: str
    event_title: str
    end_iso: str
    end_ts: int
    days_to_end: float
    hours_to_end: float
    yes_price: float
    no_price: float
    liquidity_num: float
    volume_24h: float
    category: str
    bound_lo: float
    bound_hi: float
    logic_signature: str


@dataclass
class GapCandidate:
    relation: str
    action: str
    event_key: str
    event_title: str
    market_a_id: str
    market_a_question: str
    market_a_yes: float
    market_a_no: float
    market_a_bounds: str
    market_a_end_iso: str
    market_b_id: str
    market_b_question: str
    market_b_yes: float
    market_b_no: float
    market_b_bounds: str
    market_b_end_iso: str
    basket_cost: float
    payout_floor: float
    payout_ceiling: float
    gross_edge: float
    net_edge: float
    gross_edge_cents: float
    net_edge_cents: float
    days_to_end_min: float
    liquidity_sum: float
    volume_24h_sum: float


def _as_events(value) -> List[dict]:
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [x for x in parsed if isinstance(x, dict)]
        except Exception:
            return []
    return []


def _event_key_title(market: dict) -> Tuple[str, str]:
    events = _as_events(market.get("events"))
    if events:
        e0 = events[0]
        e_id = str(e0.get("id") or "").strip()
        e_slug = str(e0.get("slug") or "").strip()
        e_title = str(e0.get("title") or "").strip()
        if e_id:
            return f"event:{e_id}", (e_title or str(market.get("question") or ""))
        if e_slug:
            return f"slug:{e_slug}", (e_title or str(market.get("question") or ""))
        if e_title:
            return f"title:{e_title.lower()[:160]}", e_title

    for key_name in ("negRiskMarketID", "questionID"):
        v = str(market.get(key_name) or "").strip()
        if v:
            return f"{key_name}:{v}", str(market.get("question") or "")

    q = str(market.get("question") or "")
    return f"q:{q.lower()[:160]}", q


def _looks_numeric_condition_question(question: str) -> bool:
    q = (question or "").lower()
    if not q:
        return False
    hints = (
        "between",
        "at least",
        "at most",
        "less than",
        "more than",
        "or above",
        "or below",
        "under ",
        "over ",
        "above ",
        "below ",
        ">",
        "<",
    )
    if any(h in q for h in hints):
        return True
    if re.search(r"\b\d+(?:\.\d+)?\s*(k|m|b|t|%|usd|dollars?)\b", q):
        return True
    return False


def _logic_signature(question: str) -> str:
    q = (question or "").lower()
    if not q:
        return ""

    q = q.replace("−", "-")
    q = re.sub(r"\b(at least|at most|less than|more than|or above|or below)\b", " ", q)
    q = re.sub(r"\b(under|over|above|below|between|and|to)\b", " ", q)
    q = re.sub(r"\b\d+(?:\.\d+)?\s*[kmbt]?\b", " <n> ", q)
    q = re.sub(r"[^a-z0-9<>\s]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def _bounds_to_text(lo: float, hi: float) -> str:
    def _fmt(x: float) -> str:
        if math.isinf(x):
            return "-inf" if x < 0 else "+inf"
        if abs(x) >= 1000:
            return f"{x:.0f}"
        return f"{x:.4g}"

    return f"[{_fmt(lo)}, {_fmt(hi)}]"


def _is_strict_subset(a_lo: float, a_hi: float, b_lo: float, b_hi: float, tol: float = 1e-12) -> bool:
    if a_lo < (b_lo - tol):
        return False
    if a_hi > (b_hi + tol):
        return False
    strict = (a_lo > (b_lo + tol)) or (a_hi < (b_hi - tol))
    return strict


def _is_strict_disjoint(a_lo: float, a_hi: float, b_lo: float, b_hi: float, tol: float = 1e-12) -> bool:
    return (a_hi < (b_lo - tol)) or (b_hi < (a_lo - tol))


def run_gap(args: argparse.Namespace) -> int:
    include_rx = compile_regex(args.include_regex)
    exclude_rx = compile_regex(args.exclude_regex)
    exclude_keywords = parse_keywords(args.exclude_keywords)
    now_ts = int(now_utc().timestamp())

    markets_total = 0
    active_interval: List[GapMarket] = []
    for page in range(args.max_pages):
        params = {
            "active": "true",
            "closed": "false",
            "limit": str(args.page_size),
            "offset": str(page * args.page_size),
        }
        markets = fetch_markets_page(params)
        if not markets:
            break
        markets_total += len(markets)

        for m in markets:
            yn = extract_yes_no(m)
            if not yn:
                continue
            _yes_token, yes_price, _no_price_raw = yn
            if not (args.yes_min <= yes_price <= args.yes_max):
                continue

            end_iso = str(m.get("endDate") or "")
            end_ts = parse_iso_ts(end_iso)
            if end_ts is None:
                continue
            days_to_end = (end_ts - now_ts) / 86400.0
            hours_to_end = (end_ts - now_ts) / 3600.0
            if hours_to_end <= 0:
                continue
            if days_to_end < args.min_days_to_end:
                continue
            if args.max_days_to_end > 0 and days_to_end > args.max_days_to_end:
                continue
            if hours_to_end < args.min_hours_to_end:
                continue
            if args.max_hours_to_end > 0 and hours_to_end > args.max_hours_to_end:
                continue

            question = str(m.get("question") or "")
            if not question_allowed(question, include_rx, exclude_rx, exclude_keywords):
                continue
            if not _looks_numeric_condition_question(question):
                continue

            bounds = parse_bucket_bounds(question)
            if bounds is None:
                continue
            bound_lo, bound_hi = bounds
            if not (math.isfinite(bound_lo) or math.isfinite(bound_hi)):
                continue

            liquidity_num = as_float(m.get("liquidityNum"), as_float(m.get("liquidity"), 0.0))
            volume_24h = as_float(m.get("volume24hr"), 0.0)
            if liquidity_num < args.min_liquidity:
                continue
            if volume_24h < args.min_volume_24h:
                continue

            event_key, event_title = _event_key_title(m)
            no_price = max(0.0, min(1.0, 1.0 - yes_price))
            active_interval.append(
                GapMarket(
                    market_id=str(m.get("id") or ""),
                    question=question,
                    event_key=event_key,
                    event_title=event_title,
                    end_iso=end_iso,
                    end_ts=int(end_ts),
                    days_to_end=float(days_to_end),
                    hours_to_end=float(hours_to_end),
                    yes_price=float(yes_price),
                    no_price=float(no_price),
                    liquidity_num=float(liquidity_num),
                    volume_24h=float(volume_24h),
                    category=str(m.get("category") or ""),
                    bound_lo=float(bound_lo),
                    bound_hi=float(bound_hi),
                    logic_signature=_logic_signature(question),
                )
            )

    by_event: Dict[str, List[GapMarket]] = {}
    for m in active_interval:
        by_event.setdefault(m.event_key, []).append(m)

    min_gross_edge = args.min_gross_edge_cents / 100.0
    min_net_edge = args.min_net_edge_cents / 100.0
    per_leg_cost = float(args.per_leg_cost or 0.0)
    pairs_scanned = 0
    event_considered = 0
    candidates: List[GapCandidate] = []

    for event_key, markets in by_event.items():
        if len(markets) < 2:
            continue
        event_considered += 1
        local_rows: List[GapCandidate] = []
        for i in range(len(markets)):
            a = markets[i]
            for j in range(i + 1, len(markets)):
                b = markets[j]
                pairs_scanned += 1

                if args.max_end_diff_hours > 0:
                    max_diff_sec = float(args.max_end_diff_hours) * 3600.0
                    if abs(float(a.end_ts) - float(b.end_ts)) > max_diff_sec:
                        continue
                if args.require_same_signature and a.logic_signature != b.logic_signature:
                    continue

                if args.relation in {"both", "subset"}:
                    if _is_strict_subset(a.bound_lo, a.bound_hi, b.bound_lo, b.bound_hi):
                        overpriced_subset = a
                        superset = b
                    elif _is_strict_subset(b.bound_lo, b.bound_hi, a.bound_lo, a.bound_hi):
                        overpriced_subset = b
                        superset = a
                    else:
                        overpriced_subset = None
                        superset = None

                    if overpriced_subset is not None and superset is not None:
                        gross_edge = overpriced_subset.yes_price - superset.yes_price
                        net_edge = gross_edge - (2.0 * per_leg_cost)
                        if gross_edge >= min_gross_edge and net_edge >= min_net_edge:
                            basket_cost = (1.0 - overpriced_subset.yes_price) + superset.yes_price
                            local_rows.append(
                                GapCandidate(
                                    relation="subset_inversion",
                                    action="BUY NO(A) + YES(B)",
                                    event_key=event_key,
                                    event_title=overpriced_subset.event_title,
                                    market_a_id=overpriced_subset.market_id,
                                    market_a_question=overpriced_subset.question,
                                    market_a_yes=overpriced_subset.yes_price,
                                    market_a_no=overpriced_subset.no_price,
                                    market_a_bounds=_bounds_to_text(overpriced_subset.bound_lo, overpriced_subset.bound_hi),
                                    market_a_end_iso=overpriced_subset.end_iso,
                                    market_b_id=superset.market_id,
                                    market_b_question=superset.question,
                                    market_b_yes=superset.yes_price,
                                    market_b_no=superset.no_price,
                                    market_b_bounds=_bounds_to_text(superset.bound_lo, superset.bound_hi),
                                    market_b_end_iso=superset.end_iso,
                                    basket_cost=float(basket_cost),
                                    payout_floor=1.0,
                                    payout_ceiling=2.0,
                                    gross_edge=float(gross_edge),
                                    net_edge=float(net_edge),
                                    gross_edge_cents=float(gross_edge * 100.0),
                                    net_edge_cents=float(net_edge * 100.0),
                                    days_to_end_min=float(min(overpriced_subset.days_to_end, superset.days_to_end)),
                                    liquidity_sum=float(overpriced_subset.liquidity_num + superset.liquidity_num),
                                    volume_24h_sum=float(overpriced_subset.volume_24h + superset.volume_24h),
                                )
                            )

                if args.relation in {"both", "disjoint"} and _is_strict_disjoint(
                    a.bound_lo, a.bound_hi, b.bound_lo, b.bound_hi
                ):
                    gross_edge = (a.yes_price + b.yes_price) - 1.0
                    net_edge = gross_edge - (2.0 * per_leg_cost)
                    if gross_edge >= min_gross_edge and net_edge >= min_net_edge:
                        basket_cost = (1.0 - a.yes_price) + (1.0 - b.yes_price)
                        local_rows.append(
                            GapCandidate(
                                relation="disjoint_overlap",
                                action="BUY NO(A) + NO(B)",
                                event_key=event_key,
                                event_title=a.event_title,
                                market_a_id=a.market_id,
                                market_a_question=a.question,
                                market_a_yes=a.yes_price,
                                market_a_no=a.no_price,
                                market_a_bounds=_bounds_to_text(a.bound_lo, a.bound_hi),
                                market_a_end_iso=a.end_iso,
                                market_b_id=b.market_id,
                                market_b_question=b.question,
                                market_b_yes=b.yes_price,
                                market_b_no=b.no_price,
                                market_b_bounds=_bounds_to_text(b.bound_lo, b.bound_hi),
                                market_b_end_iso=b.end_iso,
                                basket_cost=float(basket_cost),
                                payout_floor=1.0,
                                payout_ceiling=2.0,
                                gross_edge=float(gross_edge),
                                net_edge=float(net_edge),
                                gross_edge_cents=float(gross_edge * 100.0),
                                net_edge_cents=float(net_edge * 100.0),
                                days_to_end_min=float(min(a.days_to_end, b.days_to_end)),
                                liquidity_sum=float(a.liquidity_num + b.liquidity_num),
                                volume_24h_sum=float(a.volume_24h + b.volume_24h),
                            )
                        )

        local_rows.sort(key=lambda r: (r.net_edge, r.gross_edge, r.liquidity_sum), reverse=True)
        if args.max_pairs_per_event > 0:
            local_rows = local_rows[: args.max_pairs_per_event]
        candidates.extend(local_rows)

    candidates.sort(key=lambda r: (r.net_edge, r.gross_edge, r.liquidity_sum), reverse=True)

    safe_print(
        f"[gap] markets={markets_total} interval_markets={len(active_interval)} events={event_considered} "
        f"pairs={pairs_scanned} candidates={len(candidates)} | relation={args.relation}"
    )
    for r in candidates[: args.top_n]:
        safe_print(
            f"{r.relation} net={r.net_edge_cents:+6.2f}c gross={r.gross_edge_cents:+6.2f}c "
            f"cost={r.basket_cost:.4f} dte={r.days_to_end_min:5.2f} | {r.market_a_yes:.4f}/{r.market_b_yes:.4f} | "
            f"{r.market_a_question[:70]} || {r.market_b_question[:70]}"
        )

    out_csv = args.out_csv or f"logs/no_longshot_gap_{utc_tag()}.csv"
    out_json = args.out_json or f"logs/no_longshot_gap_{utc_tag()}.json"
    Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
    Path(out_json).parent.mkdir(parents=True, exist_ok=True)

    with Path(out_csv).open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=list(asdict(candidates[0]).keys()) if candidates else list(GapCandidate.__dataclass_fields__.keys()),
        )
        writer.writeheader()
        for r in candidates:
            writer.writerow(asdict(r))

    summary = {
        "generated_utc": now_utc().isoformat(),
        "settings": {
            "max_pages": args.max_pages,
            "page_size": args.page_size,
            "relation": args.relation,
            "yes_min": args.yes_min,
            "yes_max": args.yes_max,
            "min_days_to_end": args.min_days_to_end,
            "max_days_to_end": args.max_days_to_end,
            "min_hours_to_end": args.min_hours_to_end,
            "max_hours_to_end": args.max_hours_to_end,
            "max_end_diff_hours": args.max_end_diff_hours,
            "require_same_signature": args.require_same_signature,
            "min_liquidity": args.min_liquidity,
            "min_volume_24h": args.min_volume_24h,
            "min_gross_edge_cents": args.min_gross_edge_cents,
            "min_net_edge_cents": args.min_net_edge_cents,
            "per_leg_cost": args.per_leg_cost,
            "max_pairs_per_event": args.max_pairs_per_event,
            "exclude_keywords": exclude_keywords,
            "include_regex": args.include_regex,
            "exclude_regex": args.exclude_regex,
        },
        "counts": {
            "markets_total": markets_total,
            "interval_markets": len(active_interval),
            "events_considered": event_considered,
            "pairs_scanned": pairs_scanned,
            "candidates": len(candidates),
        },
        "top": [asdict(r) for r in candidates[: args.top_n]],
        "artifacts": {
            "csv": out_csv,
        },
    }
    Path(out_json).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    safe_print(f"[gap] wrote {out_csv}")
    safe_print(f"[gap] wrote {out_json}")
    return 0


def run_screen(args: argparse.Namespace) -> int:
    include_rx = compile_regex(args.include_regex)
    exclude_rx = compile_regex(args.exclude_regex)
    exclude_keywords = parse_keywords(args.exclude_keywords)
    now_ts = int(now_utc().timestamp())

    rows: List[ScreenRow] = []
    for page in range(args.max_pages):
        params = {
            "active": "true",
            "closed": "false",
            "limit": str(args.page_size),
            "offset": str(page * args.page_size),
        }
        markets = fetch_markets_page(params)
        if not markets:
            break

        for m in markets:
            yn = extract_yes_no(m)
            if not yn:
                continue
            _yes_token, yes_price, _no_price_raw = yn
            if not (args.yes_min <= yes_price <= args.yes_max):
                continue

            end_iso = str(m.get("endDate") or "")
            end_ts = parse_iso_ts(end_iso)
            if end_ts is None:
                continue
            days_to_end = (end_ts - now_ts) / 86400.0
            hours_to_end = (end_ts - now_ts) / 3600.0
            if hours_to_end <= 0:
                continue
            if days_to_end < args.min_days_to_end:
                continue
            if args.max_days_to_end > 0 and days_to_end > args.max_days_to_end:
                continue
            if hours_to_end < args.min_hours_to_end:
                continue
            if args.max_hours_to_end > 0 and hours_to_end > args.max_hours_to_end:
                continue

            question = str(m.get("question") or "")
            if not question_allowed(question, include_rx, exclude_rx, exclude_keywords):
                continue

            liquidity_num = as_float(m.get("liquidityNum"), as_float(m.get("liquidity"), 0.0))
            volume_24h = as_float(m.get("volume24hr"), 0.0)
            if liquidity_num < args.min_liquidity:
                continue
            if volume_24h < args.min_volume_24h:
                continue

            no_price = max(0.0, min(1.0, 1.0 - yes_price))
            if no_price <= 1e-12:
                continue
            gross_yield_on_no = yes_price / no_price
            net_profit = yes_price - args.per_trade_cost
            net_capital = no_price + args.per_trade_cost
            net_yield_on_no = net_profit / net_capital if net_capital > 1e-12 else -1.0
            hold_days = max(days_to_end, 1.0 / 24.0)
            gross_yield_per_day = gross_yield_on_no / hold_days
            net_yield_per_day = net_yield_on_no / hold_days
            if args.min_net_yield_per_day > 0 and net_yield_per_day < args.min_net_yield_per_day:
                continue

            rows.append(
                ScreenRow(
                    market_id=str(m.get("id")),
                    question=question,
                    end_iso=end_iso,
                    days_to_end=days_to_end,
                    hours_to_end=hours_to_end,
                    yes_price=yes_price,
                    no_price=no_price,
                    gross_yield_on_no=gross_yield_on_no,
                    net_yield_on_no=net_yield_on_no,
                    gross_yield_per_day=gross_yield_per_day,
                    net_yield_per_day=net_yield_per_day,
                    liquidity_num=liquidity_num,
                    volume_24h=volume_24h,
                    category=str(m.get("category") or ""),
                )
            )

    if args.sort_by == "days_asc":
        rows.sort(key=lambda r: (r.days_to_end, -r.yes_price, -r.liquidity_num, -r.volume_24h))
    elif args.sort_by == "gross_yield_per_day_desc":
        rows.sort(key=lambda r: (r.gross_yield_per_day, r.liquidity_num, r.volume_24h), reverse=True)
    elif args.sort_by == "net_yield_per_day_desc":
        rows.sort(key=lambda r: (r.net_yield_per_day, r.liquidity_num, r.volume_24h), reverse=True)
    elif args.sort_by == "liquidity_desc":
        rows.sort(key=lambda r: (r.liquidity_num, r.volume_24h, r.yes_price), reverse=True)
    else:
        rows.sort(key=lambda r: (r.yes_price, r.liquidity_num, r.volume_24h), reverse=True)

    max_days_txt = f"{args.max_days_to_end:g}" if args.max_days_to_end > 0 else "inf"
    max_hours_txt = f"{args.max_hours_to_end:g}" if args.max_hours_to_end > 0 else "inf"

    safe_print(
        f"[screen] candidates={len(rows)} | range=[{args.yes_min:.4f},{args.yes_max:.4f}] | "
        f"days=[{args.min_days_to_end:g},{max_days_txt}] | hours=[{args.min_hours_to_end:g},{max_hours_txt}] | "
        f"sort={args.sort_by}"
    )
    for r in rows[: args.top_n]:
        safe_print(
            f"YES={r.yes_price:0.4f} NO={r.no_price:0.4f} days={r.days_to_end:6.1f} hrs={r.hours_to_end:6.1f} "
            f"net/day={r.net_yield_per_day:8.3%} net={r.net_yield_on_no:7.2%} "
            f"liq={r.liquidity_num:10.0f} vol24h={r.volume_24h:10.0f} | {r.question[:110]}"
        )

    out_csv = args.out_csv or f"logs/no_longshot_screen_{utc_tag()}.csv"
    out_json = args.out_json or f"logs/no_longshot_screen_{utc_tag()}.json"
    Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
    Path(out_json).parent.mkdir(parents=True, exist_ok=True)

    with Path(out_csv).open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=list(asdict(rows[0]).keys()) if rows else list(ScreenRow.__dataclass_fields__.keys()),
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))

    summary = {
        "generated_utc": now_utc().isoformat(),
        "settings": {
            "max_pages": args.max_pages,
            "page_size": args.page_size,
            "yes_min": args.yes_min,
            "yes_max": args.yes_max,
            "min_days_to_end": args.min_days_to_end,
            "max_days_to_end": args.max_days_to_end,
            "min_hours_to_end": args.min_hours_to_end,
            "max_hours_to_end": args.max_hours_to_end,
            "min_liquidity": args.min_liquidity,
            "min_volume_24h": args.min_volume_24h,
            "per_trade_cost": args.per_trade_cost,
            "min_net_yield_per_day": args.min_net_yield_per_day,
            "sort_by": args.sort_by,
            "exclude_keywords": exclude_keywords,
            "include_regex": args.include_regex,
            "exclude_regex": args.exclude_regex,
        },
        "counts": {
            "candidates": len(rows),
        },
        "top": [asdict(r) for r in rows[: args.top_n]],
        "artifacts": {
            "csv": out_csv,
        },
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
    created_ts: int
    end_ts: int
    end_iso: str
    duration_days: float
    yes_token: str
    yes_settle: float
    cutoff_ts: int
    entry_ts: int
    entry_yes_price: float
    stale_h: float
    no_entry_price: float
    pnl_per_no_share: float
    no_won: int
    liquidity_num: float
    volume_24h: float
    history_points: int
    source_offset: int


def iter_offsets(args: argparse.Namespace) -> List[int]:
    if args.sampling_mode == "contiguous":
        return [i * args.page_size for i in range(args.max_pages)]
    out = list(range(args.offset_start, args.max_offset + 1, args.offset_step))
    return out


def build_closed_candidates(args: argparse.Namespace) -> Tuple[List[dict], Dict[str, int]]:
    now_ts = int(now_utc().timestamp())
    offsets = iter_offsets(args)

    stats = {
        "offsets_planned": len(offsets),
        "offsets_used": 0,
        "raw_markets": 0,
        "raw_candidates": 0,
        "duplicates_removed": 0,
        "deduped_candidates": 0,
    }

    raw_rows: List[dict] = []
    for i, offset in enumerate(offsets, 1):
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

        stats["offsets_used"] += 1
        stats["raw_markets"] += len(markets)
        if i % args.progress_every == 0:
            safe_print(f"[walkforward] fetched {i}/{len(offsets)} offsets | raw_markets={stats['raw_markets']}")

        for m in markets:
            yn = extract_yes_no(m)
            if not yn:
                continue
            yes_token, yes_settle, _no_settle = yn

            if not (yes_settle <= 0.01 or yes_settle >= 0.99):
                continue

            created_ts = parse_iso_ts(str(m.get("createdAt") or ""))
            end_iso = str(m.get("endDate") or "")
            end_ts = parse_iso_ts(end_iso)
            if created_ts is None or end_ts is None:
                continue
            if end_ts > now_ts:
                continue

            duration_days = (end_ts - created_ts) / 86400.0
            if duration_days < args.min_duration_days:
                continue

            cutoff_ts = end_ts - int(args.hours_before_end * 3600)
            if cutoff_ts <= 0:
                continue

            raw_rows.append(
                {
                    "market_id": str(m.get("id")),
                    "question": str(m.get("question") or ""),
                    "category": str(m.get("category") or ""),
                    "created_ts": created_ts,
                    "end_ts": end_ts,
                    "end_iso": end_iso,
                    "duration_days": duration_days,
                    "yes_token": yes_token,
                    "yes_settle": yes_settle,
                    "cutoff_ts": cutoff_ts,
                    "liquidity_num": as_float(m.get("liquidityNum"), as_float(m.get("liquidity"), 0.0)),
                    "volume_24h": as_float(m.get("volume24hr"), 0.0),
                    "source_offset": offset,
                }
            )
            if args.max_candidates > 0 and len(raw_rows) >= args.max_candidates:
                break
        if args.max_candidates > 0 and len(raw_rows) >= args.max_candidates:
            break

    stats["raw_candidates"] = len(raw_rows)

    seen = set()
    deduped: List[dict] = []
    for r in raw_rows:
        key = r["market_id"]
        if key in seen:
            stats["duplicates_removed"] += 1
            continue
        seen.add(key)
        deduped.append(r)

    stats["deduped_candidates"] = len(deduped)
    return deduped, stats


def fetch_entry_point(candidate: dict, args: argparse.Namespace) -> Optional[ClosedSample]:
    start_ts = int(candidate["cutoff_ts"] - args.lookback_hours * 3600)
    end_ts = int(candidate["cutoff_ts"])
    q = urlencode(
        {
            "market": str(candidate["yes_token"]),
            "startTs": str(start_ts),
            "endTs": str(end_ts),
            "fidelity": str(args.history_fidelity),
        }
    )
    url = f"{CLOB_API_BASE}/prices-history?{q}"
    data = fetch_json(url, timeout_sec=28.0, retries=3)
    if not isinstance(data, dict):
        return None
    hist = data.get("history")
    if not isinstance(hist, list) or not hist:
        return None
    history_points = len(hist)

    last = hist[-1] or {}
    try:
        entry_ts = int(last.get("t"))
        entry_yes = float(last.get("p"))
    except Exception:
        return None
    if not (0.0 <= entry_yes <= 1.0):
        return None

    stale_h = float(candidate["cutoff_ts"] - entry_ts) / 3600.0
    if stale_h > args.max_stale_hours:
        return None

    no_cost = 1.0 - entry_yes
    pnl = entry_yes if float(candidate["yes_settle"]) <= 0.01 else -(1.0 - entry_yes)
    return ClosedSample(
        market_id=str(candidate["market_id"]),
        question=str(candidate["question"]),
        category=str(candidate["category"]),
        created_ts=int(candidate["created_ts"]),
        end_ts=int(candidate["end_ts"]),
        end_iso=str(candidate["end_iso"]),
        duration_days=float(candidate["duration_days"]),
        yes_token=str(candidate["yes_token"]),
        yes_settle=float(candidate["yes_settle"]),
        cutoff_ts=int(candidate["cutoff_ts"]),
        entry_ts=entry_ts,
        entry_yes_price=entry_yes,
        stale_h=stale_h,
        no_entry_price=no_cost,
        pnl_per_no_share=pnl,
        no_won=1 if float(candidate["yes_settle"]) <= 0.01 else 0,
        liquidity_num=float(candidate.get("liquidity_num", 0.0) or 0.0),
        volume_24h=float(candidate.get("volume_24h", 0.0) or 0.0),
        history_points=int(history_points),
        source_offset=int(candidate["source_offset"]),
    )


def load_samples_csv(path: str) -> List[ClosedSample]:
    rows: List[ClosedSample] = []
    with Path(path).open("r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append(
                    ClosedSample(
                        market_id=str(r["market_id"]),
                        question=str(r["question"]),
                        category=str(r.get("category", "")),
                        created_ts=int(float(r["created_ts"])),
                        end_ts=int(float(r["end_ts"])),
                        end_iso=str(r["end_iso"]),
                        duration_days=float(r["duration_days"]),
                        yes_token=str(r["yes_token"]),
                        yes_settle=float(r["yes_settle"]),
                        cutoff_ts=int(float(r["cutoff_ts"])),
                        entry_ts=int(float(r["entry_ts"])),
                        entry_yes_price=float(r["entry_yes_price"]),
                        stale_h=float(r["stale_h"]),
                        no_entry_price=float(r["no_entry_price"]),
                        pnl_per_no_share=float(r["pnl_per_no_share"]),
                        no_won=int(float(r["no_won"])),
                        liquidity_num=float(r.get("liquidity_num", 0.0) or 0.0),
                        volume_24h=float(r.get("volume_24h", 0.0) or 0.0),
                        history_points=int(float(r.get("history_points", 0) or 0)),
                        source_offset=int(float(r.get("source_offset", 0))),
                    )
                )
            except Exception:
                continue
    return rows


def save_samples_csv(path: str, rows: List[ClosedSample]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fields = list(ClosedSample.__dataclass_fields__.keys())
    with Path(path).open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))


def subset_rows(
    rows: Iterable[ClosedSample],
    yes_min: float,
    yes_max: float,
    include_rx: Optional[re.Pattern],
    exclude_rx: Optional[re.Pattern],
    exclude_keywords: List[str],
    min_liquidity: float,
    min_volume_24h: float,
    min_history_points: int,
    max_stale_hours: float,
) -> List[ClosedSample]:
    out: List[ClosedSample] = []
    for r in rows:
        if not (yes_min <= r.entry_yes_price <= yes_max):
            continue
        if float(r.liquidity_num) < float(min_liquidity):
            continue
        if float(r.volume_24h) < float(min_volume_24h):
            continue
        if int(r.history_points) < int(min_history_points):
            continue
        if float(r.stale_h) > float(max_stale_hours):
            continue
        if not question_allowed(r.question, include_rx, exclude_rx, exclude_keywords):
            continue
        out.append(r)
    return out


def apply_risk_caps(
    rows: List[ClosedSample],
    max_open_positions: int,
    max_open_per_category: int,
) -> Tuple[List[ClosedSample], Dict[str, int]]:
    ordered = sorted(rows, key=lambda r: (r.entry_ts, r.end_ts, r.market_id))
    stats = {
        "input_n": len(ordered),
        "kept_n": 0,
        "dropped_max_open": 0,
        "dropped_category_open": 0,
    }
    if max_open_positions <= 0 and max_open_per_category <= 0:
        stats["kept_n"] = len(ordered)
        return ordered, stats

    active: List[ClosedSample] = []
    kept: List[ClosedSample] = []
    for r in ordered:
        if active:
            active = [x for x in active if x.end_ts > r.entry_ts]
        if max_open_positions > 0 and len(active) >= max_open_positions:
            stats["dropped_max_open"] += 1
            continue
        if max_open_per_category > 0:
            cat_open = 0
            for x in active:
                if x.category == r.category:
                    cat_open += 1
            if cat_open >= max_open_per_category:
                stats["dropped_category_open"] += 1
                continue
        active.append(r)
        kept.append(r)

    stats["kept_n"] = len(kept)
    return kept, stats


def metrics(rows: List[ClosedSample], per_trade_cost: float = 0.0) -> dict:
    if not rows:
        return {
            "n": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "total_cost": 0.0,
            "capital_return": 0.0,
            "avg_pnl_per_trade": 0.0,
            "median_pnl_per_trade": 0.0,
            "worst_loss": 0.0,
        }

    pnl_values = [r.pnl_per_no_share for r in rows]
    total_pnl = float(sum(pnl_values))
    total_cost = float(sum(r.no_entry_price for r in rows))
    n = len(rows)
    adj_pnl = total_pnl - per_trade_cost * n
    adj_cost = total_cost + per_trade_cost * n
    wins = sum(1 for v in pnl_values if v > 0.0)
    losses = n - wins
    cap_ret = (adj_pnl / adj_cost) if adj_cost > 1e-12 else 0.0
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / n if n else 0.0,
        "total_pnl": adj_pnl,
        "total_cost": adj_cost,
        "capital_return": cap_ret,
        "avg_pnl_per_trade": adj_pnl / n if n else 0.0,
        "median_pnl_per_trade": float(statistics.median(pnl_values)) if pnl_values else 0.0,
        "worst_loss": float(min(pnl_values)) if pnl_values else 0.0,
    }


def performance_window(rows: List[ClosedSample], capital_return: float) -> dict:
    if not rows:
        return {
            "n": 0,
            "min_end_iso": None,
            "max_end_iso": None,
            "span_days": 0.0,
            "annualized_return": None,
            "annualized_min_confidence_days": ANNUALIZED_MIN_CONFIDENCE_DAYS,
            "annualized_low_confidence": True,
        }

    ordered = sorted(rows, key=lambda r: r.end_ts)
    min_row = ordered[0]
    max_row = ordered[-1]
    span_days = float(max(0, max_row.end_ts - min_row.end_ts)) / 86400.0
    annualized_return: Optional[float] = None
    if span_days > 0.0 and (1.0 + capital_return) > 0.0:
        annualized_return = (1.0 + capital_return) ** (365.0 / span_days) - 1.0
    low_confidence = span_days < ANNUALIZED_MIN_CONFIDENCE_DAYS

    return {
        "n": len(rows),
        "min_end_iso": min_row.end_iso,
        "max_end_iso": max_row.end_iso,
        "span_days": span_days,
        "annualized_return": annualized_return,
        "annualized_min_confidence_days": ANNUALIZED_MIN_CONFIDENCE_DAYS,
        "annualized_low_confidence": low_confidence,
    }


def run_walkforward(args: argparse.Namespace) -> int:
    include_rx = compile_regex(args.include_regex)
    exclude_rx = compile_regex(args.exclude_regex)
    exclude_keywords = parse_keywords(args.exclude_keywords)

    if args.input_csv:
        rows = load_samples_csv(args.input_csv)
        ingest_stats = {
            "loaded_from_csv": True,
            "input_csv": args.input_csv,
            "raw_candidates": len(rows),
            "deduped_candidates": len(rows),
            "usable_with_price": len(rows),
        }
        safe_print(f"[walkforward] loaded {len(rows)} samples from {args.input_csv}")
    else:
        candidates, ingest_stats = build_closed_candidates(args)
        safe_print(
            f"[walkforward] candidates raw={ingest_stats['raw_candidates']} "
            f"deduped={ingest_stats['deduped_candidates']}"
        )
        rows = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(fetch_entry_point, c, args) for c in candidates]
            for i, f in enumerate(as_completed(futs), 1):
                r = f.result()
                if r is not None:
                    rows.append(r)
                if i % args.progress_every == 0:
                    safe_print(f"[walkforward] priced {i}/{len(futs)} | usable={len(rows)}")

        ingest_stats["loaded_from_csv"] = False
        ingest_stats["usable_with_price"] = len(rows)
        rows.sort(key=lambda x: x.end_ts)

        out_samples = args.out_samples_csv or f"logs/no_longshot_walkforward_samples_{utc_tag()}.csv"
        save_samples_csv(out_samples, rows)
        safe_print(f"[walkforward] wrote {out_samples}")
        args.out_samples_csv = out_samples

    if not rows:
        safe_print("[walkforward] no usable rows")
        return 2

    rows.sort(key=lambda x: x.end_ts)
    yes_min_grid = parse_float_grid(args.yes_min_grid)
    yes_max_grid = parse_float_grid(args.yes_max_grid)
    if not yes_min_grid or not yes_max_grid:
        safe_print("[walkforward] invalid yes grid")
        return 2

    fixed_rows_raw = subset_rows(
        rows,
        args.yes_min,
        args.yes_max,
        include_rx,
        exclude_rx,
        exclude_keywords,
        args.min_liquidity,
        args.min_volume_24h,
        args.min_history_points,
        args.max_stale_hours,
    )
    fixed_rows, fixed_risk_caps = apply_risk_caps(
        fixed_rows_raw,
        args.max_open_positions,
        args.max_open_per_category,
    )
    fixed_metrics = metrics(fixed_rows, per_trade_cost=args.per_trade_cost)
    fixed_window = performance_window(fixed_rows, fixed_metrics["capital_return"])

    period_rows: Dict[str, List[ClosedSample]] = {}
    for r in rows:
        k = period_key(r.end_ts, args.period_frequency)
        period_rows.setdefault(k, []).append(r)
    periods = sorted(period_rows.keys(), key=lambda x: period_sort_key(x, args.period_frequency))

    folds: List[dict] = []
    oos_rows: List[ClosedSample] = []
    for i in range(args.min_train_periods, len(periods)):
        train_keys = periods[:i]
        test_key = periods[i]
        train_rows = [r for k in train_keys for r in period_rows.get(k, [])]
        test_pool = period_rows.get(test_key, [])

        best: Optional[Tuple[float, float, dict]] = None
        for y0 in yes_min_grid:
            for y1 in yes_max_grid:
                if y0 > y1:
                    continue
                train_subset = subset_rows(
                    train_rows,
                    y0,
                    y1,
                    include_rx,
                    exclude_rx,
                    exclude_keywords,
                    args.min_liquidity,
                    args.min_volume_24h,
                    args.min_history_points,
                    args.max_stale_hours,
                )
                train_subset, _train_risk_caps = apply_risk_caps(
                    train_subset,
                    args.max_open_positions,
                    args.max_open_per_category,
                )
                m = metrics(train_subset, per_trade_cost=args.per_trade_cost)
                if m["n"] < args.min_train_n:
                    continue
                score = float(m["capital_return"])
                if best is None:
                    best = (y0, y1, m)
                else:
                    _, _, bm = best
                    if (score > bm["capital_return"]) or (
                        math.isclose(score, bm["capital_return"], abs_tol=1e-12) and m["n"] > bm["n"]
                    ):
                        best = (y0, y1, m)

        if best is None:
            folds.append(
                {
                    "test_period": test_key,
                    "train_periods": train_keys,
                    "selected_rule": None,
                    "train_metrics": None,
                    "test_metrics": metrics([], per_trade_cost=args.per_trade_cost),
                    "note": "no rule met min_train_n",
                }
            )
            continue

        y0, y1, train_m = best
        test_subset_raw = subset_rows(
            test_pool,
            y0,
            y1,
            include_rx,
            exclude_rx,
            exclude_keywords,
            args.min_liquidity,
            args.min_volume_24h,
            args.min_history_points,
            args.max_stale_hours,
        )
        test_subset, test_risk_caps = apply_risk_caps(
            test_subset_raw,
            args.max_open_positions,
            args.max_open_per_category,
        )
        test_m = metrics(test_subset, per_trade_cost=args.per_trade_cost)
        fold_payload = {
            "test_period": test_key,
            "train_periods": train_keys,
            "selected_rule": {"yes_min": y0, "yes_max": y1},
            "train_metrics": train_m,
            "test_metrics": test_m,
            "risk_caps": test_risk_caps,
        }
        if len(test_subset) < args.min_test_n:
            fold_payload["excluded_from_oos"] = True
            fold_payload["note"] = f"test sample n={len(test_subset)} below min_test_n={args.min_test_n}"
            folds.append(fold_payload)
            continue

        oos_rows.extend(test_subset)
        fold_payload["excluded_from_oos"] = False
        folds.append(fold_payload)

    oos_metrics = metrics(oos_rows, per_trade_cost=args.per_trade_cost)
    oos_window = performance_window(oos_rows, oos_metrics["capital_return"])

    worst_rows = sorted(fixed_rows, key=lambda r: r.pnl_per_no_share)[:15]
    worst_payload = [
        {
            "market_id": r.market_id,
            "question": r.question,
            "entry_yes_price": r.entry_yes_price,
            "pnl_per_no_share": r.pnl_per_no_share,
            "end_iso": r.end_iso,
            "duration_days": r.duration_days,
        }
        for r in worst_rows
    ]

    summary = {
        "generated_utc": now_utc().isoformat(),
        "settings": {
            "date_min": args.date_min,
            "date_max": args.date_max,
            "sampling_mode": args.sampling_mode,
            "page_size": args.page_size,
            "max_pages": args.max_pages,
            "offset_start": args.offset_start,
            "offset_step": args.offset_step,
            "max_offset": args.max_offset,
            "hours_before_end": args.hours_before_end,
            "lookback_hours": args.lookback_hours,
            "max_stale_hours": args.max_stale_hours,
            "history_fidelity": args.history_fidelity,
            "min_duration_days": args.min_duration_days,
            "workers": args.workers,
            "per_trade_cost": args.per_trade_cost,
            "max_open_positions": args.max_open_positions,
            "max_open_per_category": args.max_open_per_category,
            "min_liquidity": args.min_liquidity,
            "min_volume_24h": args.min_volume_24h,
            "min_history_points": args.min_history_points,
            "yes_min": args.yes_min,
            "yes_max": args.yes_max,
            "yes_min_grid": yes_min_grid,
            "yes_max_grid": yes_max_grid,
            "min_train_n": args.min_train_n,
            "min_test_n": args.min_test_n,
            "min_train_periods": args.min_train_periods,
            "period_frequency": args.period_frequency,
            "exclude_keywords": exclude_keywords,
            "include_regex": args.include_regex,
            "exclude_regex": args.exclude_regex,
        },
        "ingest": ingest_stats,
        "date_span": {
            "min_end_iso": rows[0].end_iso if rows else None,
            "max_end_iso": rows[-1].end_iso if rows else None,
            "periods": periods,
        },
        "fixed_rule": {
            "yes_min": args.yes_min,
            "yes_max": args.yes_max,
            "raw_n_before_risk_caps": len(fixed_rows_raw),
            "risk_caps": fixed_risk_caps,
            "window": fixed_window,
            "metrics": fixed_metrics,
        },
        "walkforward_oos": oos_metrics,
        "walkforward_oos_window": oos_window,
        "folds": folds,
        "worst_fixed_15": worst_payload,
        "artifacts": {
            "samples_csv": args.out_samples_csv if getattr(args, "out_samples_csv", "") else "",
        },
    }

    out_json = args.out_summary_json or f"logs/no_longshot_walkforward_summary_{utc_tag()}.json"
    Path(out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(out_json).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    safe_print(
        f"[walkforward] fixed n={fixed_metrics['n']} ret={fixed_metrics['capital_return']:+.4%} "
        f"win={fixed_metrics['win_rate']:.2%}"
    )
    safe_print(
        f"[walkforward] oos n={oos_metrics['n']} ret={oos_metrics['capital_return']:+.4%} "
        f"win={oos_metrics['win_rate']:.2%}"
    )
    safe_print(f"[walkforward] wrote {out_json}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Observe-only longshot NO screener and walk-forward validator (Polymarket)."
    )
    sub = p.add_subparsers(dest="mode", required=True)

    ps = sub.add_parser("screen", help="Scan active markets and output candidate list (observe-only).")
    ps.add_argument("--max-pages", type=int, default=6, help="Active markets pages to scan.")
    ps.add_argument("--page-size", type=int, default=500, help="Rows per page.")
    ps.add_argument("--yes-min", type=float, default=0.005, help="Minimum YES price filter.")
    ps.add_argument("--yes-max", type=float, default=0.02, help="Maximum YES price filter.")
    ps.add_argument("--min-days-to-end", type=float, default=14.0, help="Minimum days remaining to end.")
    ps.add_argument("--max-days-to-end", type=float, default=0.0, help="Maximum days remaining to end (0=disabled).")
    ps.add_argument("--min-hours-to-end", type=float, default=0.0, help="Minimum hours remaining to end.")
    ps.add_argument("--max-hours-to-end", type=float, default=0.0, help="Maximum hours remaining to end (0=disabled).")
    ps.add_argument("--min-liquidity", type=float, default=0.0, help="Minimum liquidityNum/liquidity.")
    ps.add_argument("--min-volume-24h", type=float, default=0.0, help="Minimum 24h volume.")
    ps.add_argument(
        "--per-trade-cost",
        type=float,
        default=0.0,
        help="Flat per-trade cost in share-price units for net yield estimates.",
    )
    ps.add_argument(
        "--min-net-yield-per-day",
        type=float,
        default=0.0,
        help="Drop rows below this net-yield/day threshold (0=disabled).",
    )
    ps.add_argument(
        "--sort-by",
        choices=[
            "yes_desc",
            "days_asc",
            "gross_yield_per_day_desc",
            "net_yield_per_day_desc",
            "liquidity_desc",
        ],
        default="yes_desc",
        help="Sort key for screen output.",
    )
    ps.add_argument(
        "--exclude-keywords",
        default=",".join(DEFAULT_EXCLUDE_KEYWORDS),
        help="Comma-separated question keyword blacklist.",
    )
    ps.add_argument("--include-regex", default="", help="Keep only questions matching this regex.")
    ps.add_argument("--exclude-regex", default="", help="Drop questions matching this regex.")
    ps.add_argument("--top-n", type=int, default=30, help="How many candidates to print.")
    ps.add_argument("--out-csv", default="", help="Output CSV path (default: logs/...).")
    ps.add_argument("--out-json", default="", help="Output summary JSON path (default: logs/...).")

    pg = sub.add_parser(
        "gap",
        help="Scan active markets for numeric-logic pricing gaps (subset/disjoint contradictions).",
    )
    pg.add_argument("--max-pages", type=int, default=6, help="Active markets pages to scan.")
    pg.add_argument("--page-size", type=int, default=500, help="Rows per page.")
    pg.add_argument("--relation", choices=["both", "subset", "disjoint"], default="both")
    pg.add_argument("--yes-min", type=float, default=0.0, help="Minimum YES price filter.")
    pg.add_argument("--yes-max", type=float, default=1.0, help="Maximum YES price filter.")
    pg.add_argument("--min-days-to-end", type=float, default=0.0, help="Minimum days remaining to end.")
    pg.add_argument("--max-days-to-end", type=float, default=0.0, help="Maximum days remaining to end (0=disabled).")
    pg.add_argument("--min-hours-to-end", type=float, default=0.0, help="Minimum hours remaining to end.")
    pg.add_argument("--max-hours-to-end", type=float, default=0.0, help="Maximum hours remaining to end (0=disabled).")
    pg.add_argument(
        "--max-end-diff-hours",
        type=float,
        default=48.0,
        help="Pair only markets whose end times differ by <= this many hours (0=disabled).",
    )
    pg.add_argument(
        "--require-same-signature",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Compare only pairs with the same normalized question signature.",
    )
    pg.add_argument("--min-liquidity", type=float, default=0.0, help="Minimum liquidityNum/liquidity.")
    pg.add_argument("--min-volume-24h", type=float, default=0.0, help="Minimum 24h volume.")
    pg.add_argument("--min-gross-edge-cents", type=float, default=0.5, help="Minimum gross edge to keep candidate.")
    pg.add_argument("--min-net-edge-cents", type=float, default=0.0, help="Minimum net edge to keep candidate.")
    pg.add_argument("--per-leg-cost", type=float, default=0.0, help="Flat cost per leg in share-price units.")
    pg.add_argument("--max-pairs-per-event", type=int, default=20, help="Keep at most N best candidates per event.")
    pg.add_argument(
        "--exclude-keywords",
        default=",".join(DEFAULT_EXCLUDE_KEYWORDS),
        help="Comma-separated question keyword blacklist.",
    )
    pg.add_argument("--include-regex", default="", help="Keep only questions matching this regex.")
    pg.add_argument("--exclude-regex", default="", help="Drop questions matching this regex.")
    pg.add_argument("--top-n", type=int, default=30, help="How many candidates to print.")
    pg.add_argument("--out-csv", default="", help="Output CSV path (default: logs/...).")
    pg.add_argument("--out-json", default="", help="Output summary JSON path (default: logs/...).")

    pw = sub.add_parser(
        "walkforward",
        help="Build closed-market sample and run walk-forward validation.",
    )
    pw.add_argument("--input-csv", default="", help="Use existing sample CSV instead of refetching.")
    pw.add_argument("--out-samples-csv", default="", help="Where to write built samples CSV.")
    pw.add_argument("--out-summary-json", default="", help="Where to write summary JSON.")
    pw.add_argument("--date-min", default="2024-01-01", help="Closed market end-date minimum (YYYY-MM-DD).")
    pw.add_argument("--date-max", default=utc_day(), help="Closed market end-date maximum (YYYY-MM-DD).")
    pw.add_argument("--sampling-mode", choices=["stratified", "contiguous"], default="stratified")
    pw.add_argument("--page-size", type=int, default=500, help="Rows per markets page.")
    pw.add_argument("--max-pages", type=int, default=120, help="Used when --sampling-mode contiguous.")
    pw.add_argument("--offset-start", type=int, default=0, help="Used when --sampling-mode stratified.")
    pw.add_argument("--offset-step", type=int, default=5000, help="Used when --sampling-mode stratified.")
    pw.add_argument("--max-offset", type=int, default=425000, help="Used when --sampling-mode stratified.")
    pw.add_argument("--max-candidates", type=int, default=0, help="Optional hard cap after candidate extraction.")
    pw.add_argument("--hours-before-end", type=float, default=24.0, help="Entry timing before market end.")
    pw.add_argument("--lookback-hours", type=float, default=72.0, help="History query lookback window.")
    pw.add_argument("--max-stale-hours", type=float, default=24.0, help="Maximum staleness from cutoff.")
    pw.add_argument("--history-fidelity", type=int, default=60, help="prices-history fidelity parameter.")
    pw.add_argument("--min-duration-days", type=float, default=14.0, help="Minimum market lifetime.")
    pw.add_argument("--workers", type=int, default=16, help="Parallel workers for prices-history fetch.")
    pw.add_argument("--min-liquidity", type=float, default=0.0, help="Minimum liquidityNum/liquidity filter.")
    pw.add_argument("--min-volume-24h", type=float, default=0.0, help="Minimum volume24hr filter.")
    pw.add_argument("--min-history-points", type=int, default=0, help="Minimum prices-history points in lookback window.")
    pw.add_argument("--yes-min", type=float, default=0.005, help="Fixed-rule yes_min.")
    pw.add_argument("--yes-max", type=float, default=0.02, help="Fixed-rule yes_max.")
    pw.add_argument(
        "--yes-min-grid",
        default="0,0.003,0.005,0.0075,0.01",
        help="Walk-forward train grid yes_min list.",
    )
    pw.add_argument(
        "--yes-max-grid",
        default="0.01,0.015,0.02",
        help="Walk-forward train grid yes_max list.",
    )
    pw.add_argument("--min-train-n", type=int, default=100, help="Minimum train samples per candidate rule.")
    pw.add_argument("--min-test-n", type=int, default=30, help="Minimum test samples to include a fold in OOS aggregate.")
    pw.add_argument(
        "--min-train-periods",
        type=int,
        default=1,
        help="Minimum prior periods before first OOS fold.",
    )
    pw.add_argument("--period-frequency", choices=["year", "halfyear", "quarter"], default="halfyear")
    pw.add_argument(
        "--per-trade-cost",
        type=float,
        default=0.0,
        help="Flat per-trade cost in share-price units.",
    )
    pw.add_argument(
        "--max-open-positions",
        type=int,
        default=0,
        help="Max concurrent open positions (0 = unlimited).",
    )
    pw.add_argument(
        "--max-open-per-category",
        type=int,
        default=0,
        help="Max concurrent open positions per category (0 = unlimited).",
    )
    pw.add_argument(
        "--exclude-keywords",
        default=",".join(DEFAULT_EXCLUDE_KEYWORDS),
        help="Comma-separated question keyword blacklist.",
    )
    pw.add_argument("--include-regex", default="", help="Keep only questions matching this regex.")
    pw.add_argument("--exclude-regex", default="", help="Drop questions matching this regex.")
    pw.add_argument("--progress-every", type=int, default=10, help="Progress log frequency.")

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.mode == "screen":
        return run_screen(args)
    if args.mode == "gap":
        return run_gap(args)
    if args.mode == "walkforward":
        return run_walkforward(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
