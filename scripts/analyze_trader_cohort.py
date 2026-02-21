#!/usr/bin/env python3
"""
Analyze multiple Polymarket accounts and extract cohort-level mimic hints (observe-only).

Examples:
  python scripts/analyze_trader_cohort.py --user @meropi --user @1pixel
  python scripts/analyze_trader_cohort.py --user-file logs/target_users.txt --max-trades 4000 --pretty
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from fetch_trades import fetch_user_trades, resolve_user_identifier


DEFAULT_WEATHER_KEYWORDS: Tuple[str, ...] = (
    "weather",
    "temperature",
    "highest temperature",
    "lowest temperature",
    "precipitation",
    "rain",
    "snow",
    "wind",
    "humidity",
    "forecast",
)


STOPWORDS = {
    "the",
    "and",
    "for",
    "will",
    "with",
    "from",
    "that",
    "this",
    "have",
    "has",
    "had",
    "was",
    "are",
    "been",
    "yes",
    "no",
    "highest",
    "lowest",
    "temperature",
    "between",
    "below",
    "above",
    "higher",
    "lower",
    "than",
    "under",
    "over",
    "city",
    "daily",
    "today",
    "tomorrow",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}


@dataclass(frozen=True)
class TradePoint:
    timestamp: int
    side: str
    price: float
    size: float
    condition_id: str
    outcome: str
    title: str
    slug: str
    event_slug: str


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_tag() -> str:
    return now_utc().strftime("%Y%m%d_%H%M%S")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


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


def parse_keywords(raw: str) -> List[str]:
    if not raw.strip():
        return [k.lower() for k in DEFAULT_WEATHER_KEYWORDS]
    parts = re.split(r"[,\n]+", raw)
    out: List[str] = []
    for p in parts:
        item = p.strip().lower()
        if item:
            out.append(item)
    return out if out else [k.lower() for k in DEFAULT_WEATHER_KEYWORDS]


def normalize_trade_rows(raw_rows: Sequence[dict]) -> List[TradePoint]:
    out: List[TradePoint] = []
    for row in raw_rows:
        ts = as_int(row.get("timestamp"), 0)
        side = str(row.get("side") or "").strip().upper()
        price = as_float(row.get("price"))
        size = as_float(row.get("size"))
        condition_id = str(row.get("conditionId") or "").strip()
        outcome = str(row.get("outcome") or "").strip()
        title = str(row.get("title") or "").strip()
        slug = str(row.get("slug") or "").strip()
        event_slug = str(row.get("eventSlug") or "").strip()
        if ts <= 0:
            continue
        if side not in {"BUY", "SELL"}:
            continue
        if not math.isfinite(price) or not math.isfinite(size):
            continue
        if price <= 0 or size <= 0:
            continue
        if not condition_id:
            continue
        out.append(
            TradePoint(
                timestamp=ts,
                side=side,
                price=price,
                size=size,
                condition_id=condition_id,
                outcome=outcome,
                title=title,
                slug=slug,
                event_slug=event_slug,
            )
        )
    out.sort(key=lambda x: x.timestamp)
    return out


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    arr = sorted(float(x) for x in values)
    if len(arr) == 1:
        return arr[0]
    qq = max(0.0, min(1.0, float(q)))
    idx = qq * (len(arr) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return arr[lo]
    frac = idx - lo
    return arr[lo] * (1.0 - frac) + arr[hi] * frac


def safe_mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return statistics.mean(values)


def safe_median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return statistics.median(values)


def to_pct(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return (numerator / denominator) * 100.0


def is_weather_trade(tp: TradePoint, keywords: Sequence[str]) -> bool:
    text = f"{tp.title} {tp.slug} {tp.event_slug}".lower()
    return any(k in text for k in keywords)


def tokenize_text(text: str) -> Iterable[str]:
    for token in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(token) < 3:
            continue
        if token in STOPWORDS:
            continue
        yield token


def price_bucket(price: float) -> str:
    if price < 0.02:
        return "<0.02"
    if price < 0.05:
        return "0.02-0.05"
    if price < 0.15:
        return "0.05-0.15"
    if price < 0.50:
        return "0.15-0.50"
    if price < 0.85:
        return "0.50-0.85"
    return ">=0.85"


def analyze_wallet_trades(
    raw_rows: Sequence[dict],
    weather_keywords: Sequence[str],
    low_price_max: float,
    high_price_min: float,
    top_markets: int,
) -> dict:
    rows = normalize_trade_rows(raw_rows)
    if not rows:
        return {"ok": False, "reason": "no_valid_trades", "trade_count": 0}

    buy_prices: List[float] = []
    buy_sizes: List[float] = []
    buy_notionals: List[float] = []
    all_intervals_sec: List[float] = []
    weather_buy_prices: List[float] = []
    weather_token_counter: Counter[str] = Counter()
    buy_price_bucket_counter: Counter[str] = Counter()

    buy_count = 0
    sell_count = 0
    buy_notional = 0.0
    sell_notional = 0.0
    weather_trade_count = 0
    weather_trade_notional = 0.0

    market_stats: Dict[str, dict] = {}
    outcome_position_qty: Dict[Tuple[str, str], float] = defaultdict(float)
    outcome_position_cost: Dict[Tuple[str, str], float] = defaultdict(float)
    outcome_last_price: Dict[Tuple[str, str], float] = {}
    key_side_counter: Dict[Tuple[str, str], Counter[str]] = defaultdict(Counter)

    realized_pnl = 0.0
    realized_cost_basis = 0.0
    closed_shares = 0.0
    close_leg_count = 0
    close_win_count = 0
    close_loss_count = 0

    prev_ts: Optional[int] = None

    for tp in rows:
        key = (tp.condition_id, tp.outcome.lower())
        notional = tp.price * tp.size
        is_weather = is_weather_trade(tp, weather_keywords)

        stat = market_stats.get(tp.condition_id)
        if stat is None:
            stat = {
                "condition_id": tp.condition_id,
                "title": tp.title,
                "slug": tp.slug,
                "event_slug": tp.event_slug,
                "trade_count": 0,
                "buy_count": 0,
                "sell_count": 0,
                "notional": 0.0,
                "weather_trade_count": 0,
            }
            market_stats[tp.condition_id] = stat
        stat["trade_count"] += 1
        stat["notional"] += notional
        if is_weather:
            stat["weather_trade_count"] += 1

        if prev_ts is not None and tp.timestamp >= prev_ts:
            all_intervals_sec.append(float(tp.timestamp - prev_ts))
        prev_ts = tp.timestamp

        outcome_last_price[key] = tp.price
        key_side_counter[key][tp.side] += 1

        if tp.side == "BUY":
            buy_count += 1
            buy_notional += notional
            stat["buy_count"] += 1
            buy_prices.append(tp.price)
            buy_sizes.append(tp.size)
            buy_notionals.append(notional)
            buy_price_bucket_counter[price_bucket(tp.price)] += 1

            outcome_position_qty[key] += tp.size
            outcome_position_cost[key] += notional

            if is_weather:
                weather_buy_prices.append(tp.price)
                text_for_tokens = f"{tp.title} {tp.slug} {tp.event_slug}"
                for tok in tokenize_text(text_for_tokens):
                    weather_token_counter[tok] += 1
        else:
            sell_count += 1
            sell_notional += notional
            stat["sell_count"] += 1

            open_qty = outcome_position_qty.get(key, 0.0)
            open_cost = outcome_position_cost.get(key, 0.0)
            if open_qty > 1e-12:
                close_qty = min(tp.size, open_qty)
                avg_cost = open_cost / open_qty if open_qty > 0 else 0.0
                pnl = close_qty * (tp.price - avg_cost)
                realized_pnl += pnl
                realized_cost_basis += close_qty * avg_cost
                closed_shares += close_qty
                close_leg_count += 1
                if pnl >= 0:
                    close_win_count += 1
                else:
                    close_loss_count += 1

                remain_qty = open_qty - close_qty
                remain_cost = open_cost - close_qty * avg_cost
                if remain_qty <= 1e-12:
                    remain_qty = 0.0
                    remain_cost = 0.0
                outcome_position_qty[key] = remain_qty
                outcome_position_cost[key] = remain_cost

        if is_weather:
            weather_trade_count += 1
            weather_trade_notional += notional

    open_positions = 0
    open_shares = 0.0
    open_cost_basis = 0.0
    unrealized_pnl = 0.0
    for key, qty in outcome_position_qty.items():
        if qty <= 1e-12:
            continue
        cost = outcome_position_cost.get(key, 0.0)
        if cost <= 0:
            continue
        mark = outcome_last_price.get(key, 0.0)
        avg_cost = cost / qty
        open_positions += 1
        open_shares += qty
        open_cost_basis += cost
        unrealized_pnl += qty * (mark - avg_cost)

    keys_with_both_sides = 0
    for key, side_counts in key_side_counter.items():
        if side_counts.get("BUY", 0) > 0 and side_counts.get("SELL", 0) > 0:
            keys_with_both_sides += 1

    low_buy_count = sum(1 for p in buy_prices if p <= low_price_max)
    high_buy_count = sum(1 for p in buy_prices if p >= high_price_min)

    top_market_rows = sorted(
        market_stats.values(),
        key=lambda x: (float(x.get("notional") or 0.0), int(x.get("trade_count") or 0)),
        reverse=True,
    )[: max(1, int(top_markets))]

    intervals_burst_15 = sum(1 for x in all_intervals_sec if x <= 15.0)
    intervals_burst_60 = sum(1 for x in all_intervals_sec if x <= 60.0)

    total_notional = buy_notional + sell_notional
    weather_market_count = sum(1 for m in market_stats.values() if int(m.get("weather_trade_count") or 0) > 0)
    close_win_rate = to_pct(close_win_count, close_leg_count)
    realized_roi_pct = to_pct(realized_pnl, realized_cost_basis)
    mtm_total_pnl = realized_pnl + unrealized_pnl
    mtm_roi_pct = to_pct(mtm_total_pnl, open_cost_basis + realized_cost_basis)

    return {
        "ok": True,
        "trade_count": len(rows),
        "market_count": len(market_stats),
        "first_trade_ts": rows[0].timestamp,
        "last_trade_ts": rows[-1].timestamp,
        "duration_hours": max(0.0, float(rows[-1].timestamp - rows[0].timestamp) / 3600.0),
        "activity": {
            "buy_count": buy_count,
            "sell_count": sell_count,
            "buy_share_pct": to_pct(buy_count, len(rows)),
            "buy_notional": buy_notional,
            "sell_notional": sell_notional,
            "total_notional": total_notional,
            "avg_interval_sec": safe_mean(all_intervals_sec),
            "median_interval_sec": safe_median(all_intervals_sec),
            "burst_15sec_share_pct": to_pct(intervals_burst_15, len(all_intervals_sec)),
            "burst_60sec_share_pct": to_pct(intervals_burst_60, len(all_intervals_sec)),
        },
        "pricing": {
            "buy_price_p10": percentile(buy_prices, 0.10),
            "buy_price_p50": percentile(buy_prices, 0.50),
            "buy_price_p90": percentile(buy_prices, 0.90),
            "low_price_threshold": low_price_max,
            "high_price_threshold": high_price_min,
            "low_price_buy_share_pct": to_pct(low_buy_count, len(buy_prices)),
            "high_price_buy_share_pct": to_pct(high_buy_count, len(buy_prices)),
            "buy_price_buckets": dict(buy_price_bucket_counter),
            "weather_buy_price_p10": percentile(weather_buy_prices, 0.10),
            "weather_buy_price_p50": percentile(weather_buy_prices, 0.50),
            "weather_buy_price_p90": percentile(weather_buy_prices, 0.90),
        },
        "sizing": {
            "buy_size_p50": percentile(buy_sizes, 0.50),
            "buy_size_p90": percentile(buy_sizes, 0.90),
            "buy_notional_p50": percentile(buy_notionals, 0.50),
            "buy_notional_p90": percentile(buy_notionals, 0.90),
        },
        "weather_focus": {
            "keywords": list(weather_keywords),
            "weather_trade_count": weather_trade_count,
            "weather_trade_share_pct": to_pct(weather_trade_count, len(rows)),
            "weather_notional": weather_trade_notional,
            "weather_notional_share_pct": to_pct(weather_trade_notional, total_notional),
            "weather_market_count": weather_market_count,
            "top_weather_tokens": [
                {"token": tok, "count": int(cnt)}
                for tok, cnt in weather_token_counter.most_common(15)
            ],
        },
        "pnl_approx": {
            "realized_pnl": realized_pnl,
            "realized_cost_basis": realized_cost_basis,
            "realized_roi_pct": realized_roi_pct,
            "closed_shares": closed_shares,
            "close_leg_count": close_leg_count,
            "close_win_rate_pct": close_win_rate,
            "close_loss_count": close_loss_count,
            "open_positions": open_positions,
            "open_shares": open_shares,
            "open_cost_basis": open_cost_basis,
            "unrealized_pnl_marked_last_trade": unrealized_pnl,
            "mtm_total_pnl_marked_last_trade": mtm_total_pnl,
            "mtm_roi_pct_marked_last_trade": mtm_roi_pct,
        },
        "roundtrip": {
            "keys_with_both_buy_sell": keys_with_both_sides,
            "keys_total": len(key_side_counter),
            "roundtrip_key_share_pct": to_pct(keys_with_both_sides, len(key_side_counter)),
        },
        "top_markets": top_market_rows,
    }


def cohort_style_label(
    weather_share_pct: float,
    low_buy_share_pct: float,
    high_buy_share_pct: float,
    avg_interval_sec: float,
) -> dict:
    if weather_share_pct >= 70.0:
        market_style = "WEATHER_SPECIALIST"
    elif weather_share_pct >= 40.0:
        market_style = "WEATHER_HEAVY"
    else:
        market_style = "MULTI_THEME"

    if high_buy_share_pct >= 55.0:
        price_style = "HIGH_CONFIDENCE_NEAR_RESOLUTION"
    elif low_buy_share_pct >= 55.0:
        price_style = "LOW_PRICE_LONGSHOT_ACCUMULATION"
    elif low_buy_share_pct >= 30.0 and high_buy_share_pct >= 30.0:
        price_style = "BARBELL_LOW_AND_HIGH"
    else:
        price_style = "MID_BAND_ACCUMULATION"

    if avg_interval_sec <= 20.0:
        speed_style = "BOT_LIKE_HIGH_FREQUENCY"
    elif avg_interval_sec <= 120.0:
        speed_style = "HIGH_FREQUENCY"
    elif avg_interval_sec <= 900.0:
        speed_style = "MEDIUM_FREQUENCY"
    else:
        speed_style = "LOW_FREQUENCY"

    return {
        "market_style": market_style,
        "price_style": price_style,
        "speed_style": speed_style,
    }


def build_cohort_summary(user_rows: Sequence[dict], weather_keywords: Sequence[str]) -> dict:
    valid = [u for u in user_rows if isinstance(u.get("analysis"), dict) and u["analysis"].get("ok")]
    if not valid:
        return {
            "ok": False,
            "reason": "no_valid_users",
            "style": {},
            "mimic_template": {},
        }

    trade_weight_total = 0.0
    weighted_weather_trade_share = 0.0
    weighted_low_price_share = 0.0
    weighted_high_price_share = 0.0
    weighted_interval_sec = 0.0

    pooled_buy_prices: List[float] = []
    pooled_weather_buy_prices: List[float] = []
    pooled_buy_notionals: List[float] = []
    weather_token_counter: Counter[str] = Counter()

    user_weather_share: List[float] = []
    user_low_share: List[float] = []
    user_high_share: List[float] = []
    user_interval_sec: List[float] = []
    user_realized_roi: List[float] = []

    for row in valid:
        analysis = row["analysis"]
        trade_count = float(analysis.get("trade_count") or 0.0)
        if trade_count <= 0:
            continue
        trade_weight_total += trade_count

        wf = analysis.get("weather_focus") or {}
        pr = analysis.get("pricing") or {}
        act = analysis.get("activity") or {}
        sz = analysis.get("sizing") or {}
        pnl = analysis.get("pnl_approx") or {}

        w_share = float(wf.get("weather_trade_share_pct") or 0.0)
        low_share = float(pr.get("low_price_buy_share_pct") or 0.0)
        high_share = float(pr.get("high_price_buy_share_pct") or 0.0)
        avg_int = float(act.get("avg_interval_sec") or 0.0)

        weighted_weather_trade_share += trade_count * w_share
        weighted_low_price_share += trade_count * low_share
        weighted_high_price_share += trade_count * high_share
        weighted_interval_sec += trade_count * avg_int

        user_weather_share.append(w_share)
        user_low_share.append(low_share)
        user_high_share.append(high_share)
        user_interval_sec.append(avg_int)
        user_realized_roi.append(float(pnl.get("realized_roi_pct") or 0.0))

        for key in ("buy_price_p10", "buy_price_p50", "buy_price_p90"):
            v = pr.get(key)
            if isinstance(v, (int, float)) and math.isfinite(float(v)):
                pooled_buy_prices.append(float(v))
        for key in ("weather_buy_price_p10", "weather_buy_price_p50", "weather_buy_price_p90"):
            v = pr.get(key)
            if isinstance(v, (int, float)) and math.isfinite(float(v)):
                pooled_weather_buy_prices.append(float(v))
        for key in ("buy_notional_p50", "buy_notional_p90"):
            v = sz.get(key)
            if isinstance(v, (int, float)) and math.isfinite(float(v)):
                pooled_buy_notionals.append(float(v))

        tokens = wf.get("top_weather_tokens") or []
        if isinstance(tokens, list):
            for token_row in tokens:
                if not isinstance(token_row, dict):
                    continue
                tok = str(token_row.get("token") or "").strip().lower()
                cnt = int(token_row.get("count") or 0)
                if tok and cnt > 0:
                    weather_token_counter[tok] += cnt

    if trade_weight_total <= 0:
        return {
            "ok": False,
            "reason": "no_trade_weight",
            "style": {},
            "mimic_template": {},
        }

    weather_share_weighted = weighted_weather_trade_share / trade_weight_total
    low_share_weighted = weighted_low_price_share / trade_weight_total
    high_share_weighted = weighted_high_price_share / trade_weight_total
    interval_weighted = weighted_interval_sec / trade_weight_total

    style = cohort_style_label(
        weather_share_pct=weather_share_weighted,
        low_buy_share_pct=low_share_weighted,
        high_buy_share_pct=high_share_weighted,
        avg_interval_sec=interval_weighted,
    )

    pooled_price_p10 = percentile(pooled_buy_prices, 0.10)
    pooled_price_p50 = percentile(pooled_buy_prices, 0.50)
    pooled_price_p90 = percentile(pooled_buy_prices, 0.90)
    pooled_weather_price_p50 = percentile(pooled_weather_buy_prices, 0.50)
    pooled_buy_notional_p50 = percentile(pooled_buy_notionals, 0.50)
    pooled_buy_notional_p90 = percentile(pooled_buy_notionals, 0.90)

    base_focus_keywords = ["weather", "temperature", "precipitation", "rain", "snow"]
    extra_focus_tokens: List[str] = []
    for tok, _cnt in weather_token_counter.most_common(30):
        if tok in STOPWORDS:
            continue
        if re.fullmatch(r"\d{4}", tok):
            continue
        if tok in base_focus_keywords:
            continue
        extra_focus_tokens.append(tok)
        if len(extra_focus_tokens) >= 6:
            break
    include_tokens = [*base_focus_keywords, *extra_focus_tokens]
    if not include_tokens:
        include_tokens = list(weather_keywords)
    include_regex = "|".join(re.escape(tok) for tok in include_tokens[:12])

    mimic_template = {
        "observe_only": True,
        "market_filter": {
            "market_style": style["market_style"],
            "include_regex": include_regex,
            "focus_keywords": include_tokens[:10],
        },
        "entry_price_profile": {
            "price_style": style["price_style"],
            "buy_price_p10": pooled_price_p10,
            "buy_price_p50": pooled_price_p50,
            "buy_price_p90": pooled_price_p90,
            "weather_buy_price_p50": pooled_weather_price_p50,
            "weighted_low_price_buy_share_pct": low_share_weighted,
            "weighted_high_price_buy_share_pct": high_share_weighted,
        },
        "execution_profile": {
            "speed_style": style["speed_style"],
            "weighted_avg_interval_sec": interval_weighted,
            "median_user_interval_sec": safe_median(user_interval_sec),
            "suggestion": "Start with observe/paper mode and enforce cooldown + per-market cap.",
        },
        "sizing_profile": {
            "buy_notional_p50": pooled_buy_notional_p50,
            "buy_notional_p90": pooled_buy_notional_p90,
            "suggestion": "Mirror median size first, then widen gradually only after stable paper performance.",
        },
        "risk_note": (
            "PnL fields are approximate (fill-based, marked to each outcome's last observed trade). "
            "Validate with paper/observe logs before any live deployment."
        ),
    }

    return {
        "ok": True,
        "valid_user_count": len(valid),
        "weighted_weather_trade_share_pct": weather_share_weighted,
        "weighted_low_price_buy_share_pct": low_share_weighted,
        "weighted_high_price_buy_share_pct": high_share_weighted,
        "weighted_avg_interval_sec": interval_weighted,
        "median_user_weather_share_pct": safe_median(user_weather_share),
        "median_user_low_price_share_pct": safe_median(user_low_share),
        "median_user_high_price_share_pct": safe_median(user_high_share),
        "median_user_interval_sec": safe_median(user_interval_sec),
        "median_user_realized_roi_pct_approx": safe_median(user_realized_roi),
        "style": style,
        "mimic_template": mimic_template,
    }


def load_users_from_file(raw_path: str) -> List[str]:
    if not raw_path.strip():
        return []
    p = Path(raw_path)
    if not p.is_absolute():
        p = repo_root() / p
    if not p.exists():
        raise FileNotFoundError(f"user file not found: {p}")
    out: List[str] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.append(s)
    return out


def dedupe_preserve(values: Sequence[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for v in values:
        x = v.strip()
        if not x:
            continue
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def resolve_output_path(raw_out: str) -> Path:
    logs_dir = repo_root() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    if raw_out:
        p = Path(raw_out)
        if p.is_absolute():
            return p
        if len(p.parts) == 1:
            return logs_dir / p.name
        return repo_root() / p
    return logs_dir / f"trader_cohort_{utc_tag()}.json"


def print_user_summary_rows(rows: Sequence[dict]) -> None:
    print("-" * 116)
    print(
        f"{'user':<28} {'trades':>7} {'mkts':>6} {'weather%':>9} "
        f"{'lowBuy%':>9} {'highBuy%':>9} {'avgInt(s)':>10} {'realized$':>12}"
    )
    print("-" * 116)
    for row in rows:
        label = str(row.get("display_user") or "")[:28]
        analysis = row.get("analysis") if isinstance(row.get("analysis"), dict) else {}
        if not analysis or not analysis.get("ok"):
            print(f"{label:<28} {'-':>7} {'-':>6} {'-':>9} {'-':>9} {'-':>9} {'-':>10} {'-':>12}")
            continue
        activity = analysis.get("activity") or {}
        pricing = analysis.get("pricing") or {}
        weather = analysis.get("weather_focus") or {}
        pnl = analysis.get("pnl_approx") or {}
        trade_count = int(analysis.get("trade_count") or 0)
        market_count = int(analysis.get("market_count") or 0)
        weather_pct = float(weather.get("weather_trade_share_pct") or 0.0)
        low_pct = float(pricing.get("low_price_buy_share_pct") or 0.0)
        high_pct = float(pricing.get("high_price_buy_share_pct") or 0.0)
        avg_int = float(activity.get("avg_interval_sec") or 0.0)
        realized = float(pnl.get("realized_pnl") or 0.0)
        print(
            f"{label:<28} {trade_count:>7} {market_count:>6} {weather_pct:>8.1f}% "
            f"{low_pct:>8.1f}% {high_pct:>8.1f}% {avg_int:>10.1f} {realized:>12.2f}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze multiple Polymarket accounts (observe-only)")
    parser.add_argument(
        "--user",
        action="append",
        default=[],
        help="Wallet address, @handle, or profile URL. Repeatable.",
    )
    parser.add_argument("--user-file", default="", help="Text file with one user identifier per line")
    parser.add_argument("--limit", type=int, default=500, help="Data API pagination page size")
    parser.add_argument("--max-trades", type=int, default=4000, help="Maximum trades fetched per user")
    parser.add_argument("--sleep-sec", type=float, default=0.0, help="Sleep between paginated fetches")
    parser.add_argument(
        "--weather-keywords",
        default=",".join(DEFAULT_WEATHER_KEYWORDS),
        help="Comma-separated keywords for weather-market classification",
    )
    parser.add_argument(
        "--min-low-price",
        type=float,
        default=0.15,
        help="BUY price <= this is counted as low-price entry",
    )
    parser.add_argument(
        "--max-high-price",
        type=float,
        default=0.85,
        help="BUY price >= this is counted as high-price entry",
    )
    parser.add_argument("--top-markets", type=int, default=8, help="Top markets to keep per user")
    parser.add_argument("--out", default="", help="Output JSON path (simple filename goes under logs/)")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print output JSON")
    args = parser.parse_args()

    try:
        from_file = load_users_from_file(args.user_file) if args.user_file else []
    except FileNotFoundError as exc:
        print(str(exc))
        return 2

    input_users = dedupe_preserve([*(args.user or []), *from_file])
    if not input_users:
        print("No users provided. Use --user and/or --user-file.")
        return 2

    weather_keywords = parse_keywords(args.weather_keywords)
    if args.min_low_price < 0 or args.max_high_price > 1 or args.min_low_price >= args.max_high_price:
        print("Invalid thresholds: require 0 <= --min-low-price < --max-high-price <= 1")
        return 2

    user_rows: List[dict] = []
    failed_rows: List[dict] = []
    for raw_user in input_users:
        wallet, resolve_meta = resolve_user_identifier(raw_user)
        if not wallet:
            failed_rows.append(
                {
                    "input_user": raw_user,
                    "reason": "resolve_failed",
                }
            )
            user_rows.append(
                {
                    "input_user": raw_user,
                    "display_user": raw_user,
                    "resolved_wallet": "",
                    "resolve_meta": resolve_meta,
                    "analysis": {"ok": False, "reason": "resolve_failed"},
                }
            )
            continue

        rows = fetch_user_trades(
            user=wallet,
            market="",
            limit=args.limit,
            max_trades=args.max_trades,
            sleep_sec=args.sleep_sec,
        )
        analysis = analyze_wallet_trades(
            rows,
            weather_keywords=weather_keywords,
            low_price_max=float(args.min_low_price),
            high_price_min=float(args.max_high_price),
            top_markets=int(args.top_markets),
        )
        display_user = resolve_meta.get("profile_handle") or wallet
        user_rows.append(
            {
                "input_user": raw_user,
                "display_user": display_user,
                "resolved_wallet": wallet,
                "resolve_meta": resolve_meta,
                "fetch_meta": {
                    "trade_count": len(rows),
                    "limit": int(args.limit),
                    "max_trades": int(args.max_trades),
                },
                "analysis": analysis,
            }
        )

    cohort_summary = build_cohort_summary(user_rows, weather_keywords=weather_keywords)
    out_path = resolve_output_path(args.out)
    payload = {
        "meta": {
            "generated_at_utc": now_utc().isoformat(),
            "source": "data-api.polymarket.com/trades",
            "observe_only": True,
            "input_user_count": len(input_users),
            "resolved_user_count": sum(1 for r in user_rows if r.get("resolved_wallet")),
            "failed_user_count": len(failed_rows),
            "limit": int(args.limit),
            "max_trades": int(args.max_trades),
            "sleep_sec": float(args.sleep_sec),
            "weather_keywords": weather_keywords,
            "min_low_price": float(args.min_low_price),
            "max_high_price": float(args.max_high_price),
            "top_markets": int(args.top_markets),
        },
        "cohort": cohort_summary,
        "users": user_rows,
        "failed_users": failed_rows,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        if args.pretty:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        else:
            json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)

    print_user_summary_rows(user_rows)
    print()
    if cohort_summary.get("ok"):
        style = cohort_summary.get("style") or {}
        print(
            "Cohort style:",
            style.get("market_style", "-"),
            "/",
            style.get("price_style", "-"),
            "/",
            style.get("speed_style", "-"),
        )
    else:
        print(f"Cohort style: unavailable ({cohort_summary.get('reason', 'unknown')})")
    print(f"Saved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
