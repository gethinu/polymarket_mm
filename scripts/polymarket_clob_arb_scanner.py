#!/usr/bin/env python3
"""
Polymarket CLOB weather arbitrage scanner (read-only).

Strategies:
- buckets: buy YES across all mutually-exclusive weather buckets for one event
- yes-no: buy YES and NO in the same binary market (sum-to-one)
- both: run both scanners

This is a scanner only. It does not place trades.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
SIMMER_API_BASE = "https://api.simmer.markets"
USER_AGENT = "Mozilla/5.0 (compatible; clob-arb-scanner/1.0)"


def fetch_json(url: str, timeout: int = 25) -> Optional[object]:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None


def parse_json_string_field(value) -> List[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except Exception:
            return []
    return []


def parse_bucket_bounds(label: str) -> Optional[Tuple[float, float]]:
    """
    Parse a bucket label into numeric bounds (low, high).

    Supports common Polymarket bucket styles beyond temperature:
    - "<250k", "2m+", "1-1.25m", "250-500k", "$100b-$200b", "between 60% and 65%"
    - "34-35°F", "5°C", "10 or below", "20 or above"

    Returns None if the label doesn't look like a numeric bucket.
    """

    s = (label or "").strip().lower()
    if not s:
        return None

    # Normalize common noise.
    s = s.replace(",", "")
    s = s.replace("$", "")
    s = s.replace("%", "")
    s = s.replace("−", "-")  # unicode minus

    # Normalize words to compact suffixes for parsing.
    s = re.sub(r"\b(thousand)\b", "k", s)
    s = re.sub(r"\b(million)\b", "m", s)
    s = re.sub(r"\b(billion|bn)\b", "b", s)
    s = re.sub(r"\b(trillion)\b", "t", s)

    def _has_suffix(tok: str) -> bool:
        return bool(re.search(r"[kmbt]\s*$", (tok or "").strip().lower()))

    def _parse_num(tok: str) -> Optional[float]:
        t = (tok or "").strip().lower()
        if not t:
            return None
        # allow spaces like "250 k"
        t = t.replace(" ", "")
        m = re.match(r"^(-?\d+(?:\.\d+)?)([kmbt])?$", t)
        if not m:
            return None
        v = float(m.group(1))
        suf = m.group(2) or ""
        mult = {"": 1.0, "k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}.get(suf, 1.0)
        return v * mult

    num_token = r"(-?\d+(?:\.\d+)?\s*[kmbt]?)"

    # "<250k" / "<=250k" / "≤250k"
    m = re.match(r"^\s*(?:<=|<|≤)\s*" + num_token + r"\s*$", s)
    if m:
        x = _parse_num(m.group(1))
        return (-math.inf, x) if x is not None else None

    # "2m+"
    m = re.match(r"^\s*" + num_token + r"\s*\+\s*$", s)
    if m:
        x = _parse_num(m.group(1))
        return (x, math.inf) if x is not None else None

    # "10 or below / or less" (+ temperature variants)
    m = re.search(num_token + r"\s*[°]?\s*[fc]?\s*(or below|or less|or under|or fewer)", s)
    if m:
        x = _parse_num(m.group(1))
        return (-math.inf, x) if x is not None else None

    # "10 or above / or more"
    m = re.search(num_token + r"\s*[°]?\s*[fc]?\s*(or higher|or above|or more|or over|or greater)", s)
    if m:
        x = _parse_num(m.group(1))
        return (x, math.inf) if x is not None else None

    # "less than 250k" / "under 250k" / "more than 2m"
    m = re.search(r"(less than|under|below)\s*" + num_token, s)
    if m:
        x = _parse_num(m.group(2))
        return (-math.inf, x) if x is not None else None

    m = re.search(r"(more than|over|above|greater than)\s*" + num_token, s)
    if m:
        x = _parse_num(m.group(2))
        return (x, math.inf) if x is not None else None

    # "between 60 and 65"
    m = re.search(r"between\s*" + num_token + r"\s*(?:and|to)\s*" + num_token, s)
    if m:
        a_tok, b_tok = m.group(1), m.group(2)
        if not _has_suffix(a_tok) and _has_suffix(b_tok):
            a_tok = a_tok.strip() + re.search(r"([kmbt])\s*$", b_tok.strip()).group(1)
        if not _has_suffix(b_tok) and _has_suffix(a_tok):
            b_tok = b_tok.strip() + re.search(r"([kmbt])\s*$", a_tok.strip()).group(1)
        a = _parse_num(a_tok)
        b = _parse_num(b_tok)
        if a is None or b is None:
            return None
        return (min(a, b), max(a, b))

    # Range: "250-500k" / "34-35°F" / "1-1.25m" / "750k-1m"
    m = re.search(num_token + r"\s*[-–to]+\s*" + num_token, s)
    if m:
        a_tok, b_tok = m.group(1), m.group(2)
        if not _has_suffix(a_tok) and _has_suffix(b_tok):
            a_tok = a_tok.strip() + re.search(r"([kmbt])\s*$", b_tok.strip()).group(1)
        if not _has_suffix(b_tok) and _has_suffix(a_tok):
            b_tok = b_tok.strip() + re.search(r"([kmbt])\s*$", a_tok.strip()).group(1)
        a = _parse_num(a_tok)
        b = _parse_num(b_tok)
        if a is None or b is None:
            return None
        return (min(a, b), max(a, b))

    # Single-point bucket (e.g., "5°C")
    m = re.match(r"^\s*" + num_token + r"\s*[°]?\s*[fc]?\s*$", s)
    if m:
        x = _parse_num(m.group(1))
        return (x, x) if x is not None else None

    return None


def buckets_look_exhaustive(legs: List[OutcomeLeg]) -> bool:
    bounds = []
    for leg in legs:
        b = parse_bucket_bounds(leg.label)
        if not b:
            return False
        bounds.append(b)

    has_lower = any(math.isinf(lo) and lo < 0 for lo, _ in bounds)
    has_upper = any(math.isinf(hi) and hi > 0 for _, hi in bounds)
    if not (has_lower and has_upper):
        return False

    finite_ranges = [(lo, hi) for lo, hi in bounds if math.isfinite(lo) and math.isfinite(hi)]
    finite_ranges.sort(key=lambda x: x[0])

    # Ensure no obvious holes between finite buckets (allow overlap).
    for i in range(1, len(finite_ranges)):
        prev_lo, prev_hi = finite_ranges[i - 1]
        curr_lo, curr_hi = finite_ranges[i]
        if curr_lo > prev_hi + 1:
            return False
    return True


def as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def order_cost_for_shares(asks: List[dict], shares: float) -> Optional[float]:
    """
    Compute cost to buy `shares` using asks (cheapest first).
    Returns None if depth is insufficient.
    """
    remaining = shares
    total = 0.0
    levels: List[Tuple[float, float]] = []
    for a in asks or []:
        p = as_float(a.get("price"), math.nan)
        sz = as_float(a.get("size"), 0.0)
        if not math.isfinite(p) or p <= 0 or sz <= 0:
            continue
        levels.append((p, sz))

    levels.sort(key=lambda x: x[0])  # cheapest ask first

    for price, size in levels:
        take = min(remaining, size)
        if take <= 0:
            continue
        total += take * price
        remaining -= take
        if remaining <= 1e-9:
            return total
    return None


@dataclass
class OutcomeLeg:
    market_id: str
    question: str
    label: str
    token_id: str
    side: str
    ask_cost: float


@dataclass
class EventOpportunity:
    kind: str
    event_key: str
    event_title: str
    legs: List[OutcomeLeg]
    basket_cost: float
    payout: float
    gross_edge: float
    edge_pct: float


def fetch_active_markets(limit: int, offset: int = 0) -> List[dict]:
    q = urlencode({"active": "true", "closed": "false", "limit": str(limit), "offset": str(offset)})
    url = f"{GAMMA_API_BASE}/markets?{q}"
    data = fetch_json(url)
    if isinstance(data, list):
        return data
    return []


def fetch_simmer_weather_markets(limit: int) -> List[dict]:
    q = urlencode({"tags": "weather", "status": "active", "limit": str(limit)})
    url = f"{SIMMER_API_BASE}/api/markets?{q}"
    data = fetch_json(url)
    if isinstance(data, dict) and isinstance(data.get("markets"), list):
        return data["markets"]
    return []


def fetch_gamma_market_by_condition_id(condition_id: str) -> Optional[dict]:
    q = urlencode({"condition_ids": condition_id})
    url = f"{GAMMA_API_BASE}/markets?{q}"
    data = fetch_json(url)
    if isinstance(data, list) and data:
        return data[0]
    return None


def fetch_gamma_event_by_slug(slug: str) -> Optional[dict]:
    url = f"{GAMMA_API_BASE}/events/slug/{slug}"
    data = fetch_json(url)
    if isinstance(data, dict):
        return data
    return None


def is_weather_bucket_market(m: dict) -> bool:
    q = str(m.get("question", "")).lower()
    return (
        "highest temperature in" in q
        or "lowest temperature in" in q
        or "high temperature in" in q
        or "low temperature in" in q
    )


def event_key_for_market(m: dict) -> str:
    for k in ("negRiskMarketID", "questionID"):
        v = m.get(k)
        if v:
            return f"{k}:{v}"

    events = m.get("events") or []
    if isinstance(events, list) and events:
        e0 = events[0] or {}
        e_id = e0.get("id")
        if e_id:
            return f"event:{e_id}"
        title = e0.get("title")
        if title:
            return f"title:{title}"
    # fallback: question stem
    return f"q:{m.get('question', '')[:80]}"


def event_title_for_market(m: dict) -> str:
    # For weather bucket markets from event endpoint, derive stable event title
    q = str(m.get("question", ""))
    marker = " be "
    if marker in q.lower():
        # split preserving original casing by finding index in lower string
        idx = q.lower().find(marker)
        if idx > 0:
            return q[:idx + len(marker)].strip() + "..."

    events = m.get("events") or []
    if isinstance(events, list) and events:
        e0 = events[0] or {}
        title = e0.get("title")
        if title:
            return str(title)
    return str(m.get("question", "Unknown event"))


def extract_yes_token_id(m: dict) -> Optional[str]:
    token_ids = parse_json_string_field(m.get("clobTokenIds"))
    outcomes = parse_json_string_field(m.get("outcomes"))
    if not token_ids:
        return None
    if outcomes and len(token_ids) >= len(outcomes):
        for i, out in enumerate(outcomes):
            if str(out).strip().lower() == "yes":
                return token_ids[i]
    # most binary markets return [YES, NO]
    return token_ids[0]


def extract_yes_no_token_ids(m: dict) -> Tuple[Optional[str], Optional[str]]:
    token_ids = parse_json_string_field(m.get("clobTokenIds"))
    outcomes = [str(x).strip().lower() for x in parse_json_string_field(m.get("outcomes"))]
    if not token_ids or len(token_ids) < 2:
        return None, None

    if outcomes and len(outcomes) == len(token_ids):
        yes_i = None
        no_i = None
        for i, out in enumerate(outcomes):
            if out == "yes":
                yes_i = i
            elif out == "no":
                no_i = i
        if yes_i is not None and no_i is not None:
            return token_ids[yes_i], token_ids[no_i]

    # fallback: common ordering [YES, NO]
    return token_ids[0], token_ids[1]


def fetch_book(token_id: str) -> Optional[dict]:
    url = f"{CLOB_API_BASE}/book?token_id={token_id}"
    data = fetch_json(url, timeout=20)
    if isinstance(data, dict):
        return data
    return None


def build_opportunities(
    markets: List[dict],
    shares_per_leg: float,
    min_event_outcomes: int,
    max_workers: int,
    strategy: str,
    winner_fee_rate: float,
    fixed_cost: float,
) -> List[EventOpportunity]:
    weather = [m for m in markets if is_weather_bucket_market(m)]
    grouped: Dict[str, List[dict]] = {}
    for m in weather:
        key = event_key_for_market(m)
        grouped.setdefault(key, []).append(m)

    need_buckets = strategy in {"buckets", "both"}
    need_yes_no = strategy in {"yes-no", "both"}

    # Flatten legs and prefetch orderbooks.
    bucket_legs_raw: List[Tuple[str, dict, str]] = []
    if need_buckets:
        for key, ms in grouped.items():
            if len(ms) < min_event_outcomes:
                continue
            for m in ms:
                token_id = extract_yes_token_id(m)
                if not token_id:
                    continue
                bucket_legs_raw.append((key, m, token_id))

    yes_no_raw: List[Tuple[dict, str, str]] = []
    if need_yes_no:
        for m in weather:
            yes_tid, no_tid = extract_yes_no_token_ids(m)
            if yes_tid and no_tid:
                yes_no_raw.append((m, yes_tid, no_tid))

    books: Dict[str, dict] = {}
    token_ids: Set[str] = set()
    token_ids.update(token for _, _, token in bucket_legs_raw)
    for _, yes_tid, no_tid in yes_no_raw:
        token_ids.add(yes_tid)
        token_ids.add(no_tid)

    token_ids = sorted(token_ids)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_book, tid): tid for tid in token_ids}
        for fut in as_completed(futures):
            tid = futures[fut]
            try:
                b = fut.result()
                if b:
                    books[tid] = b
            except Exception:
                pass

    opportunities: List[EventOpportunity] = []

    if need_buckets:
        by_event: Dict[str, List[OutcomeLeg]] = {}
        title_map: Dict[str, str] = {}
        for key, m, token_id in bucket_legs_raw:
            book = books.get(token_id)
            if not book:
                continue
            asks = book.get("asks") or []
            cost = order_cost_for_shares(asks, shares_per_leg)
            if cost is None:
                continue

            label = str(m.get("groupItemTitle") or m.get("question") or m.get("slug") or "outcome")
            leg = OutcomeLeg(
                market_id=str(m.get("id", "")),
                question=str(m.get("question", "")),
                label=label,
                token_id=token_id,
                side="yes",
                ask_cost=cost,
            )
            by_event.setdefault(key, []).append(leg)
            title_map[key] = event_title_for_market(m)

        for key, legs in by_event.items():
            if len(legs) < min_event_outcomes:
                continue
            if not buckets_look_exhaustive(legs):
                continue
            basket_cost = sum(l.ask_cost for l in legs)
            payout = shares_per_leg * (1.0 - winner_fee_rate)
            gross_edge = payout - basket_cost - fixed_cost
            edge_pct = (gross_edge / payout) if payout > 0 else 0.0
            opportunities.append(
                EventOpportunity(
                    kind="buckets",
                    event_key=key,
                    event_title=title_map.get(key, key),
                    legs=legs,
                    basket_cost=basket_cost,
                    payout=payout,
                    gross_edge=gross_edge,
                    edge_pct=edge_pct,
                )
            )

    if need_yes_no:
        for m, yes_tid, no_tid in yes_no_raw:
            yes_book = books.get(yes_tid)
            no_book = books.get(no_tid)
            if not yes_book or not no_book:
                continue

            yes_cost = order_cost_for_shares(yes_book.get("asks") or [], shares_per_leg)
            no_cost = order_cost_for_shares(no_book.get("asks") or [], shares_per_leg)
            if yes_cost is None or no_cost is None:
                continue

            q = str(m.get("question") or "weather market")
            key = f"yn:{m.get('id', '')}"
            legs = [
                OutcomeLeg(
                    market_id=str(m.get("id", "")),
                    question=q,
                    label="YES",
                    token_id=yes_tid,
                    side="yes",
                    ask_cost=yes_cost,
                ),
                OutcomeLeg(
                    market_id=str(m.get("id", "")),
                    question=q,
                    label="NO",
                    token_id=no_tid,
                    side="no",
                    ask_cost=no_cost,
                ),
            ]
            basket_cost = yes_cost + no_cost
            payout = shares_per_leg * (1.0 - winner_fee_rate)
            gross_edge = payout - basket_cost - fixed_cost
            edge_pct = (gross_edge / payout) if payout > 0 else 0.0
            opportunities.append(
                EventOpportunity(
                    kind="yes-no",
                    event_key=key,
                    event_title=q,
                    legs=legs,
                    basket_cost=basket_cost,
                    payout=payout,
                    gross_edge=gross_edge,
                    edge_pct=edge_pct,
                )
            )

    opportunities.sort(key=lambda o: o.gross_edge, reverse=True)
    return opportunities


def build_markets_from_simmer_weather(limit: int, workers: int) -> List[dict]:
    simmer_markets = fetch_simmer_weather_markets(limit)
    condition_ids = sorted(
        {
            str(m.get("polymarket_id", "")).strip()
            for m in simmer_markets
            if str(m.get("polymarket_id", "")).startswith("0x")
        }
    )
    if not condition_ids:
        return []

    event_slugs = set()
    with ThreadPoolExecutor(max_workers=max(4, workers)) as ex:
        futures = {ex.submit(fetch_gamma_market_by_condition_id, cid): cid for cid in condition_ids}
        for fut in as_completed(futures):
            try:
                m = fut.result()
                if m:
                    events = m.get("events") or []
                    if isinstance(events, list) and events:
                        slug = (events[0] or {}).get("slug")
                        if slug:
                            event_slugs.add(str(slug))
            except Exception:
                pass

    full_markets: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(4, workers)) as ex:
        futures = {ex.submit(fetch_gamma_event_by_slug, slug): slug for slug in sorted(event_slugs)}
        for fut in as_completed(futures):
            try:
                event_obj = fut.result()
                if not event_obj:
                    continue
                for m in event_obj.get("markets") or []:
                    m_id = str(m.get("id", ""))
                    if m_id:
                        full_markets[m_id] = m
            except Exception:
                pass

    return list(full_markets.values())


def build_markets_from_gamma_weather(
    limit: int,
    workers: int,
    page_size: int = 500,
    max_pages: int = 40,
    max_event_slugs: int = 120,
) -> List[dict]:
    """
    Fetch weather markets from Gamma API by paging through active markets and then
    expanding to full event markets by slug.

    This avoids dependency on Simmer's market index (useful for direct CLOB mode).
    """
    weather_markets: List[dict] = []
    event_slugs: Set[str] = set()

    offset = 0
    for _ in range(max_pages):
        rows = fetch_active_markets(limit=min(page_size, 500), offset=offset)
        if not rows:
            break

        for m in rows:
            if not is_weather_bucket_market(m):
                continue
            weather_markets.append(m)
            events = m.get("events") or []
            if isinstance(events, list) and events:
                slug = (events[0] or {}).get("slug")
                if slug:
                    event_slugs.add(str(slug))

        if len(weather_markets) >= limit or len(event_slugs) >= max_event_slugs:
            break

        offset += min(page_size, 500)

    if not event_slugs:
        return []

    full_markets: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(4, workers)) as ex:
        futures = {ex.submit(fetch_gamma_event_by_slug, slug): slug for slug in sorted(event_slugs)}
        for fut in as_completed(futures):
            try:
                event_obj = fut.result()
                if not event_obj:
                    continue
                for m in event_obj.get("markets") or []:
                    m_id = str(m.get("id", ""))
                    if m_id:
                        full_markets[m_id] = m
            except Exception:
                pass

    return list(full_markets.values())


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan Polymarket CLOB weather arbitrage opportunities")
    parser.add_argument(
        "--mode",
        choices=["simmer-weather", "gamma-weather", "gamma-all"],
        default="simmer-weather",
        help="Market source: simmer weather list, gamma weather paging, or global Gamma scan",
    )
    parser.add_argument("--limit", type=int, default=300, help="Max markets to load from source")
    parser.add_argument("--shares", type=float, default=5.0, help="Shares per bucket leg to price")
    parser.add_argument("--min-outcomes", type=int, default=4, help="Min outcomes required per event")
    parser.add_argument("--min-edge-cents", type=float, default=1.0, help="Show events with edge >= this many cents")
    parser.add_argument("--top", type=int, default=15, help="Max rows to print")
    parser.add_argument("--workers", type=int, default=24, help="Parallel workers for CLOB book fetches")
    parser.add_argument("--winner-fee-rate", type=float, default=0.02, help="Winner fee rate (0.02 => 2%)")
    parser.add_argument("--fixed-cost", type=float, default=0.0, help="Per-trade fixed USD cost")
    parser.add_argument(
        "--strategy",
        choices=["buckets", "yes-no", "both"],
        default="both",
        help="Arbitrage strategy to scan",
    )
    args = parser.parse_args()

    t0 = time.time()
    if args.mode == "simmer-weather":
        markets = build_markets_from_simmer_weather(limit=args.limit, workers=max(4, args.workers))
    elif args.mode == "gamma-weather":
        markets = build_markets_from_gamma_weather(limit=args.limit, workers=max(4, args.workers))
    else:
        markets = fetch_active_markets(args.limit)
    if not markets:
        print("No markets fetched from Gamma API.")
        return 1

    ops = build_opportunities(
        markets=markets,
        shares_per_leg=args.shares,
        min_event_outcomes=args.min_outcomes,
        max_workers=max(4, args.workers),
        strategy=args.strategy,
        winner_fee_rate=args.winner_fee_rate,
        fixed_cost=args.fixed_cost,
    )

    min_edge = args.min_edge_cents / 100.0
    filtered = [o for o in ops if o.gross_edge >= min_edge]
    buckets_count = sum(1 for o in ops if o.kind == "buckets")
    yes_no_count = sum(1 for o in ops if o.kind == "yes-no")

    dt = time.time() - t0
    print(f"Scanned {len(markets)} active markets in {dt:.2f}s")
    print(f"Opportunities analyzed: {len(ops)} (buckets={buckets_count}, yes-no={yes_no_count})")
    print(f"Arb candidates (edge >= {args.min_edge_cents:.2f}c): {len(filtered)}")
    print("-" * 110)

    for o in filtered[: args.top]:
        print(
            f"[{o.kind}] EDGE ${o.gross_edge:.4f} ({o.edge_pct:.2%}) | "
            f"cost ${o.basket_cost:.4f} vs payout ${o.payout:.4f} | "
            f"legs {len(o.legs)} | {o.event_title}"
        )
        # Print cheapest few legs for quick sanity.
        sample = sorted(o.legs, key=lambda l: l.ask_cost)[:4]
        for leg in sample:
            print(f"  - {leg.label}: ask_cost=${leg.ask_cost:.4f}")
        if len(o.legs) > 4:
            print(f"  - ... {len(o.legs) - 4} more legs")
        print()

    if not filtered:
        print("No executable basket edge found at configured threshold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
