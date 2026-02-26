from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Set, Tuple

from lib.clob_arb_eval import apply_gamma_wallet_signals, score_gamma_basket
from lib.clob_arb_models import EventBasket, Leg
from lib.clob_arb_sports import (
    DEFAULT_ESPN_SCOREBOARD_PATHS,
    build_sports_feed_snapshot,
    gamma_first_event,
    infer_sports_market_type,
    is_in_sports_live_window,
    market_matchup_pair,
    parse_sports_market_type_filter,
)
from lib.runtime_common import parse_iso_or_epoch_to_ms
from polymarket_clob_arb_scanner import (
    OutcomeLeg,
    as_float,
    buckets_look_exhaustive,
    build_markets_from_simmer_weather,
    event_key_for_market,
    event_title_for_market,
    extract_yes_no_token_ids,
    extract_yes_token_id,
    fetch_active_markets,
    fetch_gamma_event_by_slug,
    fetch_simmer_weather_markets,
    is_weather_bucket_market,
    parse_bucket_bounds,
    parse_json_string_field,
)


def build_event_baskets(limit: int, min_outcomes: int, workers: int, strategy: str) -> List[EventBasket]:
    markets = build_markets_from_simmer_weather(limit=limit, workers=workers)
    weather = [m for m in markets if is_weather_bucket_market(m)]
    need_buckets = strategy in {"buckets", "both"}
    need_yes_no = strategy in {"yes-no", "both"}

    simmer_rows = fetch_simmer_weather_markets(limit=max(limit, 500))
    condition_to_simmer_id: Dict[str, str] = {}
    for row in simmer_rows:
        cond = str(row.get("polymarket_id") or "").strip().lower()
        smid = str(row.get("id") or "").strip()
        if cond and smid:
            condition_to_simmer_id[cond] = smid

    baskets: List[EventBasket] = []

    if need_buckets:
        grouped: Dict[str, List[dict]] = {}
        for m in weather:
            k = event_key_for_market(m)
            grouped.setdefault(k, []).append(m)

        for k, ms in grouped.items():
            if len(ms) < min_outcomes:
                continue

            legs: List[Leg] = []
            shape_check_legs: List[OutcomeLeg] = []
            for m in ms:
                token_id = extract_yes_token_id(m)
                if not token_id:
                    continue
                label = str(m.get("groupItemTitle") or m.get("question") or m.get("slug") or "outcome")
                condition_id = str(m.get("conditionId") or "").strip().lower()
                leg = Leg(
                    market_id=str(m.get("id", "")),
                    question=str(m.get("question", "")),
                    label=label,
                    token_id=str(token_id),
                    simmer_market_id=condition_to_simmer_id.get(condition_id, ""),
                    side="yes",
                    condition_id=condition_id,
                )
                legs.append(leg)
                shape_check_legs.append(
                    OutcomeLeg(
                        market_id=leg.market_id,
                        question=leg.question,
                        label=leg.label,
                        token_id=leg.token_id,
                        side=leg.side,
                        ask_cost=0.0,
                    )
                )

            if len(legs) < min_outcomes:
                continue
            if not buckets_look_exhaustive(shape_check_legs):
                continue

            baskets.append(
                EventBasket(
                    key=k,
                    title=event_title_for_market(ms[0]),
                    legs=legs,
                    strategy="buckets",
                )
            )

    if need_yes_no:
        for m in weather:
            yes_tid, no_tid = extract_yes_no_token_ids(m)
            if not yes_tid or not no_tid:
                continue

            market_id = str(m.get("id") or "").strip()
            if not market_id:
                continue

            question = str(m.get("question") or "weather market")
            condition_id = str(m.get("conditionId") or "").strip().lower()
            simmer_market_id = condition_to_simmer_id.get(condition_id, "")
            legs = [
                Leg(
                    market_id=market_id,
                    question=question,
                    label="YES",
                    token_id=str(yes_tid),
                    simmer_market_id=simmer_market_id,
                    side="yes",
                    condition_id=condition_id,
                ),
                Leg(
                    market_id=market_id,
                    question=question,
                    label="NO",
                    token_id=str(no_tid),
                    simmer_market_id=simmer_market_id,
                    side="no",
                    condition_id=condition_id,
                ),
            ]
            baskets.append(
                EventBasket(
                    key=f"yn:{market_id}",
                    title=f"{question} [YES+NO]",
                    legs=legs,
                    strategy="yes-no",
                )
            )

    return baskets


def build_gamma_yes_no_baskets(
    gamma_limit: int,
    gamma_offset: int,
    min_liquidity: float,
    min_volume24hr: float,
    scan_max_markets: int = 5000,
    max_days_to_end: float = 0.0,
    include_re: Optional[re.Pattern] = None,
    exclude_re: Optional[re.Pattern] = None,
    sports_live_only: bool = False,
    sports_live_prestart_min: float = 10.0,
    sports_live_postend_min: float = 30.0,
    sports_market_types_allow: Optional[Set[str]] = None,
    sports_market_types_exclude: Optional[Set[str]] = None,
    sports_require_matchup: bool = False,
    sports_feed_snapshot: Optional[dict] = None,
    sports_feed_live_buffer_sec: float = 90.0,
    sports_feed_strict: bool = False,
) -> List[EventBasket]:
    baskets: List[EventBasket] = []
    seen: Set[str] = set()
    now_ms = int(time.time() * 1000)
    allow_types: Set[str] = set(sports_market_types_allow or set())
    exclude_types: Set[str] = set(sports_market_types_exclude or set())
    max_end_ms: Optional[int] = None
    if float(max_days_to_end or 0.0) > 0:
        max_end_ms = now_ms + int(float(max_days_to_end) * 86400000.0)

    offset = max(0, int(gamma_offset or 0))
    want = max(0, int(gamma_limit or 0))
    scan_cap = max(0, int(scan_max_markets or 0))
    scanned = 0
    page_size = 500

    while len(baskets) < want and (scan_cap <= 0 or scanned < scan_cap):
        batch = page_size
        if scan_cap > 0:
            batch = min(batch, scan_cap - scanned)
            if batch <= 0:
                break

        markets = fetch_active_markets(limit=batch, offset=offset)
        if not markets:
            break
        offset += batch
        scanned += len(markets)

        for m in markets:
            if len(baskets) >= want:
                break

            if m.get("enableOrderBook") is False:
                continue
            if m.get("feesEnabled") is True:
                continue
            market_type = infer_sports_market_type(m)
            if sports_live_only:
                if sports_require_matchup and market_matchup_pair(m) is None:
                    continue
                if allow_types and market_type not in allow_types:
                    continue
                if exclude_types and market_type in exclude_types:
                    continue
            elif allow_types or exclude_types:
                if allow_types and market_type not in allow_types:
                    continue
                if exclude_types and market_type in exclude_types:
                    continue
            if sports_live_only and not is_in_sports_live_window(
                m,
                now_ms=now_ms,
                prestart_min=sports_live_prestart_min,
                postend_min=sports_live_postend_min,
                sports_feed_snapshot=sports_feed_snapshot,
                sports_feed_live_buffer_sec=sports_feed_live_buffer_sec,
                sports_feed_strict=sports_feed_strict,
            ):
                continue

            liq = as_float(m.get("liquidityNum", m.get("liquidity", 0.0)), 0.0)
            vol24 = as_float(m.get("volume24hr", 0.0), 0.0)
            spread = as_float(m.get("spread", 0.0), 0.0)
            one_day_change = as_float(m.get("oneDayPriceChange", 0.0), 0.0)
            end_ms = parse_iso_or_epoch_to_ms(m.get("endDate") or m.get("endDateIso"))
            if max_end_ms is not None and end_ms is not None and end_ms > max_end_ms:
                continue
            if liq < float(min_liquidity or 0.0):
                continue
            if vol24 < float(min_volume24hr or 0.0):
                continue

            token_ids = m.get("clobTokenIds")
            outcomes = m.get("outcomes")
            token_list = parse_json_string_field(token_ids)
            outcomes_list = parse_json_string_field(outcomes)

            if len(token_list) != 2:
                continue
            if outcomes_list and len(outcomes_list) != 2:
                continue

            yes_tid, no_tid = token_list[0], token_list[1]
            if not yes_tid or not no_tid:
                continue

            market_id = str(m.get("id") or "").strip()
            if not market_id or market_id in seen:
                continue
            seen.add(market_id)
            condition_id = str(m.get("conditionId") or "").strip().lower()

            question = str(m.get("question") or f"market {market_id}").strip()
            event_slug = str(gamma_first_event(m).get("slug") or "").strip()
            hay = f"{question}\n{event_slug}\n{market_id}"
            if include_re and not include_re.search(hay):
                continue
            if exclude_re and exclude_re.search(hay):
                continue
            label_a = "YES"
            label_b = "NO"
            if outcomes_list and len(outcomes_list) == 2:
                label_a = str(outcomes_list[0]).strip() or label_a
                label_b = str(outcomes_list[1]).strip() or label_b
            legs = [
                Leg(
                    market_id=market_id,
                    question=question,
                    label=label_a,
                    token_id=str(yes_tid),
                    side="yes",
                    condition_id=condition_id,
                ),
                Leg(
                    market_id=market_id,
                    question=question,
                    label=label_b,
                    token_id=str(no_tid),
                    side="no",
                    condition_id=condition_id,
                ),
            ]
            baskets.append(
                EventBasket(
                    key=f"yn:{market_id}",
                    title=f"{question} [YES+NO]",
                    legs=legs,
                    strategy="yes-no",
                    market_id=market_id,
                    event_id=str(gamma_first_event(m).get("id") or "").strip(),
                    event_slug=event_slug,
                    liquidity_num=float(liq),
                    volume24hr=float(vol24),
                    spread=float(spread),
                    one_day_price_change=float(one_day_change),
                    end_ms=end_ms,
                    sports_market_type=market_type,
                    min_order_size=as_float(m.get("orderMinSize"), 0.0),
                    price_tick_size=as_float(m.get("orderPriceMinTickSize"), 0.0),
                )
            )

    return baskets


def build_gamma_event_pair_baskets(
    gamma_limit: int,
    gamma_offset: int,
    min_liquidity: float,
    min_volume24hr: float,
    max_legs: int,
    scan_max_markets: int = 5000,
    max_days_to_end: float = 0.0,
    include_re: Optional[re.Pattern] = None,
    exclude_re: Optional[re.Pattern] = None,
) -> List[EventBasket]:
    grouped: Dict[str, List[dict]] = {}

    offset = max(0, int(gamma_offset or 0))
    want = max(0, int(gamma_limit or 0))
    scan_cap = max(0, int(scan_max_markets or 0))
    scanned = 0
    page_size = 500
    now_ms = int(time.time() * 1000)
    max_end_ms: Optional[int] = None
    if float(max_days_to_end or 0.0) > 0:
        max_end_ms = now_ms + int(float(max_days_to_end) * 86400000.0)

    while (scan_cap <= 0 or scanned < scan_cap):
        batch = page_size
        if scan_cap > 0:
            batch = min(batch, scan_cap - scanned)
            if batch <= 0:
                break

        markets = fetch_active_markets(limit=batch, offset=offset)
        if not markets:
            break
        offset += batch
        scanned += len(markets)

        for m in markets:
            if m.get("enableOrderBook") is False:
                continue
            if m.get("feesEnabled") is True:
                continue

            neg_risk_id = str(m.get("negRiskMarketID") or "").strip()
            if not neg_risk_id:
                continue

            token_list = parse_json_string_field(m.get("clobTokenIds"))
            outcomes_list = parse_json_string_field(m.get("outcomes"))
            if len(token_list) != 2:
                continue
            if outcomes_list and len(outcomes_list) != 2:
                continue

            label = str(m.get("groupItemTitle") or "").strip()
            if not label:
                continue
            label_lc = label.lower()
            if re.search(r"\d", label_lc):
                continue
            if any(sym in label_lc for sym in ("<", ">", "%", "$", "°", "\"")):
                continue
            if re.search(r"\b(or more|or less|or below|or above|between|under|over|at least|at most)\b", label_lc):
                continue
            if parse_bucket_bounds(label) is not None:
                continue

            yes_tid, no_tid = extract_yes_no_token_ids(m)
            if not yes_tid or not no_tid:
                continue

            grouped.setdefault(neg_risk_id, []).append(
                {
                    "market": m,
                    "label": label,
                    "yes_tid": str(yes_tid),
                    "no_tid": str(no_tid),
                }
            )

    baskets: List[EventBasket] = []
    for neg_risk_id, rows in grouped.items():
        if want > 0 and len(baskets) >= want:
            break

        by_label: Dict[str, dict] = {}
        for row in rows:
            label_key = str(row.get("label") or "").strip().lower()
            if not label_key:
                continue
            cur = by_label.get(label_key)
            if cur is None:
                by_label[label_key] = row
                continue
            cur_liq = as_float((cur.get("market") or {}).get("liquidityNum", 0.0), 0.0)
            new_liq = as_float((row.get("market") or {}).get("liquidityNum", 0.0), 0.0)
            if new_liq > cur_liq:
                by_label[label_key] = row

        clean_rows = list(by_label.values())
        if len(clean_rows) != 2:
            continue
        if max_legs > 0 and 2 > max_legs:
            continue

        clean_rows.sort(key=lambda x: str(x.get("label") or "").lower())

        first_market = (clean_rows[0].get("market") or {}) if clean_rows else {}
        e0 = ((first_market.get("events") or [{}])[0] or {}) if isinstance(first_market, dict) else {}
        event_slug = str(e0.get("slug") or "").strip()
        title = event_title_for_market(first_market) if isinstance(first_market, dict) else neg_risk_id
        labels_joined = " | ".join(str(r.get("label") or "") for r in clean_rows)
        hay = f"{title}\n{event_slug}\n{labels_joined}"
        if include_re and not include_re.search(hay):
            continue
        if exclude_re and exclude_re.search(hay):
            continue

        legs_meet_filters = True
        for row in clean_rows:
            m = row.get("market") or {}
            liq = as_float(m.get("liquidityNum", m.get("liquidity", 0.0)), 0.0)
            vol24 = as_float(m.get("volume24hr", 0.0), 0.0)
            if liq < float(min_liquidity or 0.0) or vol24 < float(min_volume24hr or 0.0):
                legs_meet_filters = False
                break
            end_ms = parse_iso_or_epoch_to_ms(m.get("endDate") or m.get("endDateIso"))
            if end_ms is None:
                legs_meet_filters = False
                break
            if end_ms < (now_ms - 86400000):
                legs_meet_filters = False
                break
            if max_end_ms is not None and end_ms is not None and end_ms > max_end_ms:
                legs_meet_filters = False
                break
        if not legs_meet_filters:
            continue

        yes_legs: List[Leg] = []
        no_legs: List[Leg] = []
        liqs: List[float] = []
        vols: List[float] = []
        spreads: List[float] = []
        changes: List[float] = []
        min_sizes: List[float] = []
        tick_sizes: List[float] = []
        end_ms_vals: List[int] = []

        for row in clean_rows:
            m = row.get("market") or {}
            market_id = str(m.get("id") or "").strip()
            condition_id = str(m.get("conditionId") or "").strip().lower()
            question = str(m.get("question") or "").strip()
            label = str(row.get("label") or "").strip()
            if not market_id or not label:
                continue

            yes_legs.append(
                Leg(
                    market_id=market_id,
                    question=question,
                    label=label,
                    token_id=str(row.get("yes_tid") or ""),
                    side="yes",
                    condition_id=condition_id,
                )
            )
            no_legs.append(
                Leg(
                    market_id=market_id,
                    question=question,
                    label=label,
                    token_id=str(row.get("no_tid") or ""),
                    side="no",
                    condition_id=condition_id,
                )
            )

            liqs.append(as_float(m.get("liquidityNum", m.get("liquidity", 0.0)), 0.0))
            vols.append(as_float(m.get("volume24hr", 0.0), 0.0))
            spreads.append(as_float(m.get("spread", 0.0), 0.0))
            changes.append(as_float(m.get("oneDayPriceChange", 0.0), 0.0))
            min_sizes.append(as_float(m.get("orderMinSize"), 0.0))
            tick_sizes.append(as_float(m.get("orderPriceMinTickSize"), 0.0))
            em = parse_iso_or_epoch_to_ms(m.get("endDate") or m.get("endDateIso"))
            if em:
                end_ms_vals.append(int(em))

        if len(yes_legs) != 2 or len(no_legs) != 2:
            continue

        event_id = str(e0.get("id") or "").strip()
        basket_end_ms = min(end_ms_vals) if end_ms_vals else None
        common_kwargs = {
            "market_id": neg_risk_id,
            "event_id": event_id,
            "event_slug": event_slug,
            "liquidity_num": float(min(liqs) if liqs else 0.0),
            "volume24hr": float(sum(vols) if vols else 0.0),
            "spread": float(max(spreads) if spreads else 0.0),
            "one_day_price_change": float(max((abs(x) for x in changes), default=0.0)),
            "end_ms": basket_end_ms,
            "min_order_size": float(max(min_sizes) if min_sizes else 0.0),
            "price_tick_size": float(max(tick_sizes) if tick_sizes else 0.0),
        }

        baskets.append(
            EventBasket(
                key=f"ey:{neg_risk_id}",
                title=f"{title} [YES+YES]",
                legs=yes_legs,
                strategy="event-yes",
                **common_kwargs,
            )
        )
        baskets.append(
            EventBasket(
                key=f"en:{neg_risk_id}",
                title=f"{title} [NO+NO]",
                legs=no_legs,
                strategy="event-no",
                **common_kwargs,
            )
        )

    return baskets


def build_gamma_bucket_baskets(
    gamma_limit: int,
    gamma_offset: int,
    min_liquidity: float,
    min_volume24hr: float,
    min_outcomes: int,
    max_legs: int,
    scan_max_markets: int = 5000,
    max_days_to_end: float = 0.0,
    include_re: Optional[re.Pattern] = None,
    exclude_re: Optional[re.Pattern] = None,
) -> List[EventBasket]:
    baskets: List[EventBasket] = []

    offset = max(0, int(gamma_offset or 0))
    want = max(0, int(gamma_limit or 0))
    scan_cap = max(0, int(scan_max_markets or 0))
    scanned = 0
    page_size = 500
    now_ms = int(time.time() * 1000)
    max_end_ms: Optional[int] = None
    if float(max_days_to_end or 0.0) > 0:
        max_end_ms = now_ms + int(float(max_days_to_end) * 86400000.0)

    grouped: Dict[str, List[dict]] = {}

    while (scan_cap <= 0 or scanned < scan_cap):
        batch = page_size
        if scan_cap > 0:
            batch = min(batch, scan_cap - scanned)
            if batch <= 0:
                break

        markets = fetch_active_markets(limit=batch, offset=offset)
        if not markets:
            break
        offset += batch
        scanned += len(markets)

        for m in markets:
            if m.get("enableOrderBook") is False:
                continue
            if m.get("feesEnabled") is True:
                continue

            liq = as_float(m.get("liquidityNum", m.get("liquidity", 0.0)), 0.0)
            vol24 = as_float(m.get("volume24hr", 0.0), 0.0)
            if liq < float(min_liquidity or 0.0):
                continue
            if vol24 < float(min_volume24hr or 0.0):
                continue
            end_ms = parse_iso_or_epoch_to_ms(m.get("endDate") or m.get("endDateIso"))
            if max_end_ms is not None and end_ms is not None and end_ms > max_end_ms:
                continue

            question = str(m.get("question") or "").strip()
            event_slug = str(((m.get("events") or [{}])[0] or {}).get("slug") or "").strip()
            hay = f"{question}\n{event_slug}"
            if include_re and not include_re.search(hay):
                continue
            if exclude_re and exclude_re.search(hay):
                continue

            token_list = parse_json_string_field(m.get("clobTokenIds"))
            outcomes_list = parse_json_string_field(m.get("outcomes"))
            if len(token_list) != 2:
                continue
            if outcomes_list and len(outcomes_list) != 2:
                continue

            label = str(m.get("groupItemTitle") or "").strip()
            if not label:
                continue
            if parse_bucket_bounds(label) is None:
                continue

            k = event_key_for_market(m)
            grouped.setdefault(k, []).append(m)

        if want > 0 and len(grouped) >= max(want * 4, want + 50):
            break

    for k, ms in grouped.items():
        if want > 0 and len(baskets) >= want:
            break

        if len(ms) < int(min_outcomes or 0):
            continue

        if max_legs > 0 and len(ms) > max_legs:
            continue

        legs: List[Leg] = []
        shape_check_legs: List[OutcomeLeg] = []
        liqs: List[float] = []
        vols: List[float] = []
        spreads: List[float] = []
        changes: List[float] = []
        min_sizes: List[float] = []
        tick_sizes: List[float] = []
        end_ms: Optional[int] = None

        for m in ms:
            token_id = extract_yes_token_id(m)
            if not token_id:
                continue
            label = str(m.get("groupItemTitle") or "").strip()
            if not label or parse_bucket_bounds(label) is None:
                continue

            market_id = str(m.get("id") or "").strip()
            question = str(m.get("question") or "").strip()
            condition_id = str(m.get("conditionId") or "").strip().lower()
            leg = Leg(
                market_id=market_id,
                question=question,
                label=label,
                token_id=str(token_id),
                side="yes",
                condition_id=condition_id,
            )
            legs.append(leg)
            shape_check_legs.append(
                OutcomeLeg(
                    market_id=market_id,
                    question=question,
                    label=label,
                    token_id=leg.token_id,
                    side=leg.side,
                    ask_cost=0.0,
                )
            )

            liqs.append(as_float(m.get("liquidityNum", m.get("liquidity", 0.0)), 0.0))
            vols.append(as_float(m.get("volume24hr", 0.0), 0.0))
            spreads.append(as_float(m.get("spread", 0.0), 0.0))
            changes.append(as_float(m.get("oneDayPriceChange", 0.0), 0.0))
            min_sizes.append(as_float(m.get("orderMinSize"), 0.0))
            tick_sizes.append(as_float(m.get("orderPriceMinTickSize"), 0.0))
            if end_ms is None:
                end_ms = parse_iso_or_epoch_to_ms(m.get("endDate") or m.get("endDateIso"))

        if len(legs) < int(min_outcomes or 0):
            continue
        if max_legs > 0 and len(legs) > max_legs:
            continue
        if not buckets_look_exhaustive(shape_check_legs):
            continue

        e0 = ((ms[0].get("events") or [{}])[0] or {}) if isinstance(ms[0], dict) else {}
        event_id = str(e0.get("id") or "").strip()
        event_slug = str(e0.get("slug") or "").strip()
        title = event_title_for_market(ms[0])

        baskets.append(
            EventBasket(
                key=k,
                title=title,
                legs=legs,
                strategy="buckets",
                market_id=event_id or str(ms[0].get("id") or "").strip(),
                event_id=event_id,
                event_slug=event_slug,
                liquidity_num=float(min(liqs) if liqs else 0.0),
                volume24hr=float(sum(vols) if vols else 0.0),
                spread=float(max(spreads) if spreads else 0.0),
                one_day_price_change=float(max((abs(x) for x in changes), default=0.0)),
                end_ms=end_ms,
                min_order_size=float(max(min_sizes) if min_sizes else 0.0),
                price_tick_size=float(max(tick_sizes) if tick_sizes else 0.0),
            )
        )

    return baskets


def parse_btc_updown_window_minutes(raw: str) -> List[int]:
    s = str(raw or "").strip()
    if not s:
        return [5]
    out: List[int] = []
    for tok in re.split(r"[\s,]+", s):
        if not tok:
            continue
        try:
            v = int(float(tok))
        except Exception:
            continue
        if v in {5, 15} and v not in out:
            out.append(v)
    return out or [5]


def build_btc_updown_baskets(
    window_minutes_list: List[int],
    windows_back: int = 1,
    windows_forward: int = 1,
) -> List[EventBasket]:
    back = max(0, int(windows_back or 0))
    fwd = max(0, int(windows_forward or 0))
    now_s = int(time.time())

    baskets: List[EventBasket] = []
    seen: Set[str] = set()
    minutes_list = [int(m) for m in (window_minutes_list or []) if int(m) > 0]
    if not minutes_list:
        minutes_list = [5]

    for window_min in minutes_list:
        window_sec = int(window_min) * 60
        start_s = (now_s // window_sec) * window_sec

        for i in range(-back, fwd + 1):
            slug = f"btc-updown-{int(window_min)}m-{start_s + (i * window_sec)}"
            ev = fetch_gamma_event_by_slug(slug)
            if not isinstance(ev, dict):
                continue
            markets = ev.get("markets") or []
            if not isinstance(markets, list):
                continue

            for m in markets:
                if not isinstance(m, dict):
                    continue
                if m.get("enableOrderBook") is False:
                    continue

                token_list = parse_json_string_field(m.get("clobTokenIds"))
                outcomes_list = parse_json_string_field(m.get("outcomes"))
                if len(token_list) < 2:
                    continue

                market_id = str(m.get("id") or "").strip()
                condition_id = str(m.get("conditionId") or "").strip().lower()
                question = str(m.get("question") or ev.get("title") or slug).strip()
                title = str(ev.get("title") or question or slug).strip()
                end_ms = parse_iso_or_epoch_to_ms(m.get("endDate") or m.get("endDateIso") or ev.get("endDate"))

                legs: List[Leg] = []
                for idx, tid in enumerate(token_list):
                    if not tid:
                        continue
                    label = f"OUT{idx + 1}"
                    if outcomes_list and idx < len(outcomes_list):
                        label = str(outcomes_list[idx]).strip() or label
                    legs.append(
                        Leg(
                            market_id=market_id,
                            question=question,
                            label=label,
                            token_id=str(tid),
                            side=("yes" if idx == 0 else "no"),
                            condition_id=condition_id,
                        )
                    )

                if len(legs) < 2:
                    continue

                key = f"slug:{slug}:{market_id}"
                if key in seen:
                    continue
                seen.add(key)
                baskets.append(
                    EventBasket(
                        key=key,
                        title=title,
                        legs=legs,
                        strategy="yes-no",
                        market_id=market_id,
                        event_id=str(ev.get("id") or "").strip(),
                        event_slug=str(ev.get("slug") or slug).strip(),
                        liquidity_num=as_float(m.get("liquidityNum", m.get("liquidity", 0.0)), 0.0),
                        volume24hr=as_float(m.get("volume24hr", 0.0), 0.0),
                        spread=as_float(m.get("spread", 0.0), 0.0),
                        one_day_price_change=abs(as_float(m.get("oneDayPriceChange", 0.0), 0.0)),
                        end_ms=end_ms,
                        min_order_size=as_float(m.get("orderMinSize"), 0.0),
                        price_tick_size=as_float(m.get("orderPriceMinTickSize"), 0.0),
                    )
                )

    return baskets


def build_btc_updown_5m_baskets(windows_back: int = 1, windows_forward: int = 1) -> List[EventBasket]:
    return build_btc_updown_baskets([5], windows_back=windows_back, windows_forward=windows_forward)


def normalize_universe(raw_universe: str) -> str:
    universe = str(raw_universe or "weather").strip().lower()
    if universe not in {"weather", "gamma-active", "btc-5m", "btc-updown"}:
        universe = "weather"
    return universe


def build_universe_baskets(
    args,
    logger,
    include_re: Optional[re.Pattern] = None,
    exclude_re: Optional[re.Pattern] = None,
) -> Tuple[str, List[EventBasket], Optional[str]]:
    universe = normalize_universe(getattr(args, "universe", "weather"))

    if universe == "gamma-active":
        gamma_limit = getattr(args, "gamma_limit", 500)
        gamma_offset = getattr(args, "gamma_offset", 0)
        min_liq = getattr(args, "gamma_min_liquidity", 0.0)
        min_vol24 = getattr(args, "gamma_min_volume24hr", 0.0)
        scan_max = getattr(args, "gamma_scan_max_markets", 5000)
        max_days = float(getattr(args, "gamma_max_days_to_end", 0.0) or 0.0)
        sports_live_only = bool(getattr(args, "sports_live_only", False))
        sports_live_prestart_min = float(getattr(args, "sports_live_prestart_min", 10.0) or 10.0)
        sports_live_postend_min = float(getattr(args, "sports_live_postend_min", 30.0) or 30.0)
        sports_market_types_allow = parse_sports_market_type_filter(str(getattr(args, "sports_market_types", "") or ""))
        sports_market_types_exclude = parse_sports_market_type_filter(
            str(getattr(args, "sports_market_types_exclude", "") or "")
        )
        sports_require_matchup = bool(getattr(args, "sports_require_matchup", False))
        sports_feed_provider = str(getattr(args, "sports_feed_provider", "none") or "none").strip().lower()
        sports_feed_strict = bool(getattr(args, "sports_feed_strict", False))
        sports_feed_timeout_sec = float(getattr(args, "sports_feed_timeout_sec", 5.0) or 5.0)
        sports_feed_live_buffer_sec = float(getattr(args, "sports_feed_live_buffer_sec", 90.0) or 90.0)
        sports_feed_espn_paths = [
            x.strip()
            for x in str(getattr(args, "sports_feed_espn_paths", ",".join(DEFAULT_ESPN_SCOREBOARD_PATHS)) or "").split(",")
            if x.strip()
        ]
        sports_feed_snapshot: dict = {
            "provider": "none",
            "fetched_ms": int(time.time() * 1000),
            "events": [],
            "pair_index": {},
        }
        if args.strategy in {"yes-no", "both", "all"} and sports_feed_provider != "none":
            sports_feed_snapshot = build_sports_feed_snapshot(
                provider=sports_feed_provider,
                espn_paths=sports_feed_espn_paths,
                timeout_sec=sports_feed_timeout_sec,
                logger=logger,
            )
        build_max_legs = int(args.max_legs) if int(args.max_legs or 0) > 0 else 12

        if args.strategy == "yes-no":
            baskets = build_gamma_yes_no_baskets(
                gamma_limit=gamma_limit,
                gamma_offset=gamma_offset,
                min_liquidity=min_liq,
                min_volume24hr=min_vol24,
                scan_max_markets=scan_max,
                max_days_to_end=max_days,
                include_re=include_re,
                exclude_re=exclude_re,
                sports_live_only=sports_live_only,
                sports_live_prestart_min=sports_live_prestart_min,
                sports_live_postend_min=sports_live_postend_min,
                sports_market_types_allow=sports_market_types_allow,
                sports_market_types_exclude=sports_market_types_exclude,
                sports_require_matchup=sports_require_matchup,
                sports_feed_snapshot=sports_feed_snapshot,
                sports_feed_live_buffer_sec=sports_feed_live_buffer_sec,
                sports_feed_strict=sports_feed_strict,
            )
        elif args.strategy == "buckets":
            baskets = build_gamma_bucket_baskets(
                gamma_limit=gamma_limit,
                gamma_offset=gamma_offset,
                min_liquidity=min_liq,
                min_volume24hr=min_vol24,
                min_outcomes=args.min_outcomes,
                max_legs=build_max_legs,
                scan_max_markets=scan_max,
                max_days_to_end=max_days,
                include_re=include_re,
                exclude_re=exclude_re,
            )
        elif args.strategy == "event-pair":
            baskets = build_gamma_event_pair_baskets(
                gamma_limit=gamma_limit,
                gamma_offset=gamma_offset,
                min_liquidity=min_liq,
                min_volume24hr=min_vol24,
                max_legs=build_max_legs,
                scan_max_markets=scan_max,
                max_days_to_end=max_days,
                include_re=include_re,
                exclude_re=exclude_re,
            )
        else:
            baskets = []
            baskets.extend(
                build_gamma_bucket_baskets(
                    gamma_limit=gamma_limit,
                    gamma_offset=gamma_offset,
                    min_liquidity=min_liq,
                    min_volume24hr=min_vol24,
                    min_outcomes=args.min_outcomes,
                    max_legs=build_max_legs,
                    scan_max_markets=scan_max,
                    max_days_to_end=max_days,
                    include_re=include_re,
                    exclude_re=exclude_re,
                )
            )
            baskets.extend(
                build_gamma_yes_no_baskets(
                    gamma_limit=gamma_limit,
                    gamma_offset=gamma_offset,
                    min_liquidity=min_liq,
                    min_volume24hr=min_vol24,
                    scan_max_markets=scan_max,
                    max_days_to_end=max_days,
                    include_re=include_re,
                    exclude_re=exclude_re,
                    sports_live_only=sports_live_only,
                    sports_live_prestart_min=sports_live_prestart_min,
                    sports_live_postend_min=sports_live_postend_min,
                    sports_market_types_allow=sports_market_types_allow,
                    sports_market_types_exclude=sports_market_types_exclude,
                    sports_require_matchup=sports_require_matchup,
                    sports_feed_snapshot=sports_feed_snapshot,
                    sports_feed_live_buffer_sec=sports_feed_live_buffer_sec,
                    sports_feed_strict=sports_feed_strict,
                )
            )
            if args.strategy == "all":
                baskets.extend(
                    build_gamma_event_pair_baskets(
                        gamma_limit=gamma_limit,
                        gamma_offset=gamma_offset,
                        min_liquidity=min_liq,
                        min_volume24hr=min_vol24,
                        max_legs=build_max_legs,
                        scan_max_markets=scan_max,
                        max_days_to_end=max_days,
                        include_re=include_re,
                        exclude_re=exclude_re,
                    )
                )
        if not baskets:
            logger.info("No valid Gamma baskets found.")
            return universe, [], "CLOBBOT: gamma-active universe empty (no baskets). Check filters / Gamma API."
        return universe, baskets, None

    if universe in {"btc-5m", "btc-updown"}:
        window_minutes = parse_btc_updown_window_minutes(str(getattr(args, "btc_updown_window_minutes", "5") or "5"))
        btc_windows_back = max(0, int(getattr(args, "btc_5m_windows_back", 1)))
        btc_windows_forward = max(0, int(getattr(args, "btc_5m_windows_forward", 1)))
        baskets = build_btc_updown_baskets(
            window_minutes_list=window_minutes,
            windows_back=btc_windows_back,
            windows_forward=btc_windows_forward,
        )
        if not baskets:
            mins_txt = ",".join(str(x) for x in window_minutes)
            logger.info(f"No valid BTC up/down baskets found (event slug fetch, minutes={mins_txt}).")
            return universe, [], f"CLOBBOT: btc-updown universe empty (minutes={mins_txt}; could not fetch current events)."
        return universe, baskets, None

    baskets = build_event_baskets(
        limit=args.limit,
        min_outcomes=args.min_outcomes,
        workers=args.workers,
        strategy=args.strategy,
    )
    if not baskets:
        logger.info("No valid weather event baskets found.")
        return universe, [], "CLOBBOT: weather universe empty (no baskets)."
    return universe, baskets, None


def apply_subscription_token_cap(
    baskets: List[EventBasket],
    universe: str,
    args,
    logger,
) -> Tuple[List[EventBasket], Optional[str]]:
    max_tokens = int(getattr(args, "max_subscribe_tokens", 0) or 0)
    if max_tokens <= 0:
        return baskets, None

    selected: List[EventBasket] = []
    tokens: Set[str] = set()

    if universe == "gamma-active":
        now_ms = int(time.time() * 1000)
        for basket in baskets:
            basket.score = score_gamma_basket(basket, now_ms, args)
            basket.wallet_signal_score = 0.0
            basket.wallet_signal_confidence = 0.0
            basket.combined_score = basket.score

        if bool(getattr(args, "wallet_signal_enable", False)):
            wallet_summary = apply_gamma_wallet_signals(baskets, args, logger)
            wallet_weight = float(getattr(args, "wallet_signal_weight", 0.25) or 0.0)
            for basket in baskets:
                if basket.score < 0:
                    basket.combined_score = basket.score
                else:
                    basket.combined_score = (
                        basket.score + (wallet_weight * basket.wallet_signal_score * basket.wallet_signal_confidence)
                    )
            logger.info(
                "[wallet-signal] "
                f"conditions={wallet_summary.get('conditions_scored', 0)}/{wallet_summary.get('conditions_total', 0)} "
                f"wallets={wallet_summary.get('wallets_scored', 0)}/{wallet_summary.get('wallets_attempted', 0)} "
                f"baskets={wallet_summary.get('baskets_scored', 0)} "
                f"weight={wallet_weight:.3f}"
            )

        ranked = [basket for basket in baskets if basket.score >= 0]
        ranked.sort(key=lambda x: (x.combined_score, x.score, x.volume24hr), reverse=True)

        max_per_event = int(getattr(args, "max_markets_per_event", 0) or 0)
        per_event: Dict[str, int] = {}

        for basket in ranked:
            basket_tokens = {leg.token_id for leg in basket.legs if leg.token_id}
            if not basket_tokens:
                continue
            if len(tokens | basket_tokens) > max_tokens:
                continue

            if max_per_event > 0 and basket.event_id:
                used = per_event.get(basket.event_id, 0)
                if used >= max_per_event:
                    continue
                per_event[basket.event_id] = used + 1

            selected.append(basket)
            tokens |= basket_tokens

        logger.info(
            f"Applied scored selection (gamma-active) max_subscribe_tokens={max_tokens}: "
            f"baskets {len(baskets)} -> {len(selected)} | tokens={len(tokens)}"
        )
        if not ranked:
            logger.info(
                "Note: no baskets passed scoring filters (likely stale endDate values, or gamma_max_days_to_end too strict)."
            )
        if selected:
            top = sorted(selected, key=lambda x: (x.combined_score, x.score), reverse=True)[:8]
            for picked in top:
                end_days = None
                if picked.end_ms:
                    end_days = max(0.0, float(picked.end_ms - now_ms) / 86400000.0)
                end_s = f"{end_days:.0f}d" if end_days is not None else "?d"
                logger.info(
                    f"  picked score={picked.score:.3f} combo={picked.combined_score:.3f} "
                    f"ws={picked.wallet_signal_score:+.3f} "
                    f"conf={picked.wallet_signal_confidence:.2f} end={end_s} "
                    f"vol24={picked.volume24hr:.0f} liq={picked.liquidity_num:.0f} "
                    f"spr={picked.spread:.3f} id={picked.market_id} q={picked.title[:80]}"
                )

        if not selected:
            logger.info("No baskets selected after gamma-active scoring/caps.")
            return (
                [],
                "CLOBBOT: gamma-active selection empty (all markets filtered out). "
                "Try increasing CLOBBOT_GAMMA_SCAN_MAX, relaxing min_liquidity/min_volume, "
                "or loosening CLOBBOT_GAMMA_MAX_DAYS_TO_END.",
            )
        return selected, None

    for basket in sorted(baskets, key=lambda x: len({leg.token_id for leg in x.legs})):
        basket_tokens = {leg.token_id for leg in basket.legs if leg.token_id}
        if not basket_tokens:
            continue
        if len(tokens | basket_tokens) > max_tokens:
            continue
        selected.append(basket)
        tokens |= basket_tokens

    if selected and len(selected) != len(baskets):
        logger.info(
            f"Applied max_subscribe_tokens={max_tokens}: baskets {len(baskets)} -> {len(selected)} | "
            f"tokens={len(tokens)}"
        )
        return selected, None
    return baskets, None


def build_subscription_maps(
    baskets: List[EventBasket],
) -> Tuple[Dict[str, Set[str]], Dict[str, EventBasket], List[str]]:
    token_to_events: Dict[str, Set[str]] = {}
    event_map: Dict[str, EventBasket] = {}
    token_ids: List[str] = []
    seen_tokens: Set[str] = set()

    for basket in baskets:
        event_map[basket.key] = basket
        for leg in basket.legs:
            token_to_events.setdefault(leg.token_id, set()).add(basket.key)
            if leg.token_id not in seen_tokens:
                token_ids.append(leg.token_id)
                seen_tokens.add(leg.token_id)

    return token_to_events, event_map, token_ids
