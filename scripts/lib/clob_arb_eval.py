from __future__ import annotations

import json
import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from lib.clob_arb_models import Candidate, EventBasket, Leg, LocalBook
from lib.runtime_common import iso_now, now_ts
from polymarket_clob_arb_scanner import as_float, order_cost_for_shares


def _q_cents_up(x: float) -> float:
    if not math.isfinite(x):
        return 0.0
    d = Decimal(str(x))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def _q_cents_down(x: float) -> float:
    if not math.isfinite(x):
        return 0.0
    d = Decimal(str(x))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_FLOOR))


def _estimate_exec_cost_clob(candidate: Candidate, slippage_bps: float) -> float:
    slip = max(0.0, float(slippage_bps or 0.0)) / 10000.0
    total = 0.0
    for _, observed_cost in candidate.leg_costs:
        est_price = float(observed_cost) / max(float(candidate.shares_per_leg), 1e-9)
        px = min(0.999, est_price * (1.0 + slip))
        px = max(0.001, _q_cents_up(px))
        total += px * float(candidate.shares_per_leg)
    return total


def _clamp01(x: float) -> float:
    if not math.isfinite(x):
        return 0.0
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    return float(x)


def _clamp(x: float, lo: float, hi: float) -> float:
    if not math.isfinite(x):
        return lo
    if x <= lo:
        return lo
    if x >= hi:
        return hi
    return float(x)


def score_gamma_basket(basket: EventBasket, now_ms: int, args) -> float:
    days_to_end = 365.0
    if basket.end_ms:
        raw_days = float(basket.end_ms - now_ms) / 86400000.0
        if raw_days < -1.0:
            return -1.0
        days_to_end = max(0.0, raw_days)

    max_days = float(getattr(args, "gamma_max_days_to_end", 0.0) or 0.0)
    if max_days > 0 and days_to_end > max_days:
        return -1.0

    halflife = float(getattr(args, "gamma_score_halflife_days", 30.0) or 30.0)
    halflife = max(1.0, halflife)
    time_score = math.exp(-days_to_end / halflife)

    liq = max(0.0, float(basket.liquidity_num or 0.0))
    vol = max(0.0, float(basket.volume24hr or 0.0))
    spr = max(0.0, float(basket.spread or 0.0))
    volat = abs(float(basket.one_day_price_change or 0.0))

    liq_score = _clamp01(math.log1p(liq) / math.log1p(50_000.0))
    vol_score = _clamp01(math.log1p(vol) / math.log1p(50_000.0))
    spread_score = _clamp01(spr / 0.02)
    volat_score = _clamp01(volat / 0.02)

    base = 0.40 * liq_score + 0.35 * vol_score + 0.15 * spread_score + 0.10 * volat_score
    return float(base * time_score)


def _leg_condition_id(leg: Leg) -> str:
    return str(getattr(leg, "condition_id", "") or "").strip().lower()


def _wallet_quality_score(summary: dict) -> float:
    if not isinstance(summary, dict) or not summary.get("ok"):
        return 0.0

    timeline = summary.get("timeline") if isinstance(summary.get("timeline"), dict) else {}
    trading = summary.get("trading_behavior") if isinstance(summary.get("trading_behavior"), dict) else {}
    inventory = summary.get("inventory") if isinstance(summary.get("inventory"), dict) else {}

    prof_pct = as_float(timeline.get("time_profitable_pct"), 50.0)
    score = (prof_pct - 50.0) / 50.0

    classification = str(summary.get("classification") or "").strip().upper()
    if classification == "SNIPER_ARBITRAGE":
        score += 0.30
    elif classification == "SNIPER_TIMING":
        score += 0.20
    elif classification == "HIGH_DRAWDOWN_STYLE":
        score -= 0.30

    hedge_status = str(inventory.get("hedge_status") or "").strip().upper()
    if hedge_status == "ARBITRAGE_CANDIDATE":
        score += 0.20
    elif hedge_status == "NEGATIVE_EDGE":
        score -= 0.20

    hedge_edge_pct = as_float(inventory.get("hedge_edge_pct"), 0.0)
    score += _clamp(hedge_edge_pct / 20.0, -0.20, 0.20)

    intensity = str(trading.get("intensity") or "").strip().upper()
    if intensity == "BOT_LIKE":
        score += 0.05
    elif intensity == "HIGH":
        score += 0.03
    elif intensity == "LOW":
        score -= 0.02

    trade_count = max(0, int(summary.get("trade_count") or 0))
    confidence = _clamp(math.log1p(trade_count) / math.log1p(50.0), 0.0, 1.0)
    return _clamp(score * confidence, -1.0, 1.0)


def apply_gamma_wallet_signals(baskets: List[EventBasket], args, logger) -> dict:
    out = {
        "conditions_total": 0,
        "conditions_scored": 0,
        "wallets_attempted": 0,
        "wallets_scored": 0,
        "baskets_scored": 0,
    }
    if not baskets:
        return out

    try:
        from analyze_trades import analyze_trades  # type: ignore
        from analyze_user import fetch_market_holders  # type: ignore
        from fetch_trades import fetch_user_trades  # type: ignore
    except Exception as e:
        logger.info(f"[{iso_now()}] wallet-signal unavailable: import failed ({type(e).__name__}: {e})")
        return out

    holders_limit = max(1, int(getattr(args, "wallet_signal_holders_limit", 16) or 16))
    top_wallets = max(1, int(getattr(args, "wallet_signal_top_wallets", 8) or 8))
    min_trades = max(1, int(getattr(args, "wallet_signal_min_trades", 8) or 8))
    max_trades = max(10, int(getattr(args, "wallet_signal_max_trades", 600) or 600))
    page_size = max(1, min(500, int(getattr(args, "wallet_signal_page_size", 200) or 200)))
    max_baskets = max(1, int(getattr(args, "wallet_signal_max_baskets", 80) or 80))

    ranked = sorted((b for b in baskets if b.score >= 0), key=lambda x: (x.score, x.volume24hr), reverse=True)
    target_baskets = ranked[:max_baskets]
    if not target_baskets:
        return out

    condition_ids: List[str] = []
    seen_conditions: Set[str] = set()
    for b in target_baskets:
        for leg in b.legs:
            cid = _leg_condition_id(leg)
            if not cid or cid in seen_conditions:
                continue
            seen_conditions.add(cid)
            condition_ids.append(cid)

    out["conditions_total"] = len(condition_ids)
    if not condition_ids:
        logger.info("[wallet-signal] no condition_id metadata on selected baskets; skipping.")
        return out

    cond_scores: Dict[str, Tuple[float, float]] = {}
    holders_per_cond = max(holders_limit, top_wallets)
    for idx, condition_id in enumerate(condition_ids, start=1):
        try:
            holders = fetch_market_holders(condition_id, limit=holders_per_cond)
        except Exception:
            holders = []
        if not holders:
            continue

        holder_rows = holders[:top_wallets]
        weighted_sum = 0.0
        weight_total = 0.0
        scored_wallets = 0

        for h in holder_rows:
            wallet = str(h.get("wallet") or "").strip().lower()
            if not wallet:
                continue
            out["wallets_attempted"] += 1
            try:
                rows = fetch_user_trades(
                    user=wallet,
                    market=condition_id,
                    limit=page_size,
                    max_trades=max_trades,
                    sleep_sec=0.0,
                )
            except Exception:
                rows = []
            if len(rows) < min_trades:
                continue

            try:
                summary = analyze_trades(rows, market_title="", wallet=wallet)
            except Exception:
                summary = {}
            if not isinstance(summary, dict) or not summary.get("ok"):
                continue
            if int(summary.get("trade_count") or 0) < min_trades:
                continue

            wscore = _wallet_quality_score(summary)
            amount = as_float(h.get("amount_total"), 0.0)
            weight = amount if amount > 0 else 1.0
            weighted_sum += weight * wscore
            weight_total += weight
            scored_wallets += 1
            out["wallets_scored"] += 1

        if weight_total <= 0 or scored_wallets <= 0:
            continue

        cond_score = weighted_sum / weight_total
        cond_conf = _clamp(float(scored_wallets) / float(max(1, len(holder_rows))), 0.0, 1.0)
        cond_scores[condition_id] = (float(cond_score), float(cond_conf))
        out["conditions_scored"] += 1

        if idx % 10 == 0:
            logger.info(
                f"[wallet-signal] progress {idx}/{len(condition_ids)} conditions | "
                f"scored={out['conditions_scored']} wallets={out['wallets_scored']}"
            )

    for b in target_baskets:
        per_cond: List[Tuple[float, float]] = []
        used: Set[str] = set()
        for leg in b.legs:
            cid = _leg_condition_id(leg)
            if not cid or cid in used:
                continue
            used.add(cid)
            v = cond_scores.get(cid)
            if v is not None:
                per_cond.append(v)
        if not per_cond:
            continue
        b.wallet_signal_score = float(sum(x[0] for x in per_cond) / len(per_cond))
        b.wallet_signal_confidence = float(sum(x[1] for x in per_cond) / len(per_cond))
        out["baskets_scored"] += 1

    return out


def compute_candidate_metrics_row(
    candidate: Candidate,
    basket: EventBasket,
    books: Dict[str, LocalBook],
    args,
) -> dict:
    ts_now = now_ts()
    threshold = float(getattr(args, "min_edge_cents", 0.0) or 0.0) / 100.0
    exec_cost = _estimate_exec_cost_clob(candidate, float(getattr(args, "exec_slippage_bps", 0.0) or 0.0))
    exec_edge = candidate.payout_after_fee - float(exec_cost) - candidate.fixed_cost
    exec_edge_pct = (float(exec_edge) / candidate.payout_after_fee) if candidate.payout_after_fee > 0 else 0.0

    fill_ratios: List[float] = []
    stale_secs: List[float] = []
    synthetic_ask_legs = 0
    missing_book_legs = 0
    shares = max(float(candidate.shares_per_leg), 1e-9)
    slip = max(0.0, float(getattr(args, "exec_slippage_bps", 0.0) or 0.0)) / 10000.0

    for leg, observed_cost in candidate.leg_costs:
        book = books.get(leg.token_id)
        if not book:
            missing_book_legs += 1
            fill_ratios.append(0.0)
            continue

        if float(book.updated_at or 0.0) > 0:
            stale_secs.append(max(0.0, ts_now - float(book.updated_at)))
        if getattr(book, "asks_synthetic", False):
            synthetic_ask_legs += 1

        asks = book.asks
        if (not asks) and book.best_ask:
            asks = [{"price": book.best_ask, "size": 1e9}]

        est_price = float(observed_cost) / shares
        limit_price = min(0.999, est_price * (1.0 + slip))
        limit_price = max(0.001, _q_cents_up(limit_price))

        available = 0.0
        for lvl in asks or []:
            p = as_float(lvl.get("price"), math.inf)
            sz = as_float(lvl.get("size"), 0.0)
            if not math.isfinite(p) or p <= 0 or sz <= 0:
                continue
            if p <= (limit_price + 1e-12):
                available += sz

        fill_ratio = _clamp(available / shares, 0.0, 1.0)
        fill_ratios.append(fill_ratio)

    fill_ratio_min = min(fill_ratios) if fill_ratios else 0.0
    fill_ratio_avg = (sum(fill_ratios) / len(fill_ratios)) if fill_ratios else 0.0
    worst_stale_sec = max(stale_secs) if stale_secs else -1.0

    return {
        "ts": iso_now(),
        "ts_ms": int(ts_now * 1000.0),
        "universe": str(getattr(args, "universe", "") or ""),
        "strategy": candidate.strategy,
        "event_key": candidate.event_key,
        "title": candidate.title,
        "market_id": basket.market_id,
        "event_id": basket.event_id,
        "event_slug": basket.event_slug,
        "sports_market_type": str(getattr(basket, "sports_market_type", "") or ""),
        "leg_count": len(candidate.leg_costs),
        "shares_per_leg": float(candidate.shares_per_leg),
        "payout_after_fee": float(candidate.payout_after_fee),
        "fixed_cost": float(candidate.fixed_cost),
        "basket_cost_observed": float(candidate.basket_cost),
        "basket_cost_exec_est": float(exec_cost),
        "net_edge_raw": float(candidate.net_edge),
        "edge_pct_raw": float(candidate.edge_pct),
        "net_edge_exec_est": float(exec_edge),
        "edge_pct_exec_est": float(exec_edge_pct),
        "edge_threshold_usd": float(threshold),
        "passes_raw_threshold": bool(candidate.net_edge >= threshold),
        "passes_exec_threshold": bool(exec_edge >= threshold),
        "fill_ratio_min": float(fill_ratio_min),
        "fill_ratio_avg": float(fill_ratio_avg),
        "worst_book_stale_sec": float(worst_stale_sec),
        "synthetic_ask_legs": int(synthetic_ask_legs),
        "missing_book_legs": int(missing_book_legs),
        "gamma_base_score": float(getattr(basket, "score", 0.0) or 0.0),
        "wallet_signal_score": float(getattr(basket, "wallet_signal_score", 0.0) or 0.0),
        "wallet_signal_confidence": float(getattr(basket, "wallet_signal_confidence", 0.0) or 0.0),
        "gamma_combined_score": float(getattr(basket, "combined_score", 0.0) or 0.0),
    }


def update_book_from_snapshot(item: dict, books: Dict[str, LocalBook]) -> Optional[str]:
    token_id = str(item.get("asset_id") or item.get("assetId") or "")
    if not token_id:
        return None

    book = books.setdefault(token_id, LocalBook())
    if isinstance(item.get("asks"), list):
        book.asks = item["asks"]
        book.asks_synthetic = False
        if book.asks:
            best_ask = min((as_float(x.get("price"), math.inf) for x in book.asks), default=math.inf)
            book.best_ask = best_ask if math.isfinite(best_ask) else None
    if isinstance(item.get("bids"), list):
        book.bids = item["bids"]
        book.bids_synthetic = False
        if book.bids:
            best_bid = max((as_float(x.get("price"), 0.0) for x in book.bids), default=0.0)
            book.best_bid = best_bid if best_bid > 0 else None

    if item.get("best_ask") is not None:
        b = as_float(item.get("best_ask"), math.inf)
        if math.isfinite(b):
            book.best_ask = b
            if not book.asks:
                book.asks = [{"price": b, "size": 1e9}]
                book.asks_synthetic = True
    if item.get("best_bid") is not None:
        b = as_float(item.get("best_bid"), 0.0)
        if b > 0:
            book.best_bid = b
            if not book.bids:
                book.bids = [{"price": b, "size": 1e9}]
                book.bids_synthetic = True

    book.updated_at = now_ts()
    return token_id


def extract_book_items(payload) -> List[dict]:
    out: List[dict] = []

    def add_if_book(d):
        if isinstance(d, dict) and (d.get("asset_id") or d.get("assetId")):
            if "asks" in d or "bids" in d or "best_ask" in d or "best_bid" in d:
                out.append(d)

    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            event_type = str(item.get("event_type", "")).lower()
            if event_type in {"book", "price_change", "tick_size_change"}:
                add_if_book(item)
                for key in ("changes", "price_changes", "items"):
                    nested = item.get(key)
                    if isinstance(nested, list):
                        for n in nested:
                            add_if_book(n)
            else:
                add_if_book(item)
    elif isinstance(payload, dict):
        add_if_book(payload)
        for key in ("changes", "price_changes", "items", "books"):
            nested = payload.get(key)
            if isinstance(nested, list):
                for n in nested:
                    add_if_book(n)

    return out


def collect_impacted_events_from_payload(
    payload,
    books: Dict[str, LocalBook],
    token_to_events: Dict[str, Set[str]],
) -> Set[str]:
    changed_tokens: Set[str] = set()
    for item in extract_book_items(payload):
        token_id = update_book_from_snapshot(item, books)
        if token_id:
            changed_tokens.add(token_id)
    if not changed_tokens:
        return set()

    impacted_events: Set[str] = set()
    for token in changed_tokens:
        impacted_events.update(token_to_events.get(token, set()))
    return impacted_events


def compute_candidate(
    basket: EventBasket,
    books: Dict[str, LocalBook],
    shares_per_leg: float,
    winner_fee_rate: float,
    fixed_cost: float,
) -> Optional[Candidate]:
    leg_costs: List[Tuple[Leg, float]] = []

    if float(getattr(basket, "min_order_size", 0.0) or 0.0) > 0 and shares_per_leg < float(
        basket.min_order_size
    ):
        return None

    for leg in basket.legs:
        book = books.get(leg.token_id)
        if not book:
            return None

        asks = book.asks
        if not asks and book.best_ask:
            asks = [{"price": book.best_ask, "size": 1e9}]

        cost = order_cost_for_shares(asks, shares_per_leg)
        if cost is None:
            return None
        leg_costs.append((leg, cost))

    basket_cost = sum(c for _, c in leg_costs)
    payout = shares_per_leg * (1.0 - winner_fee_rate)
    net_edge = payout - basket_cost - fixed_cost
    edge_pct = (net_edge / payout) if payout > 0 else 0.0
    return Candidate(
        strategy=basket.strategy,
        event_key=basket.key,
        title=basket.title,
        shares_per_leg=shares_per_leg,
        basket_cost=basket_cost,
        payout_after_fee=payout,
        fixed_cost=fixed_cost,
        net_edge=net_edge,
        edge_pct=edge_pct,
        leg_costs=leg_costs,
    )


def format_candidate(candidate: Candidate) -> str:
    lines = []
    lines.append(
        f"[{iso_now()}] [{candidate.strategy}] EDGE ${candidate.net_edge:.4f} ({candidate.edge_pct:.2%}) | "
        f"cost ${candidate.basket_cost:.4f} | payout ${candidate.payout_after_fee:.4f} | "
        f"fixed ${candidate.fixed_cost:.4f}"
    )
    lines.append(f"  Event: {candidate.title} | legs={len(candidate.leg_costs)}")
    for leg, cost in sorted(candidate.leg_costs, key=lambda x: x[1])[:6]:
        lines.append(f"    - {leg.label}/{leg.side.upper()}: ${cost:.4f} (market_id={leg.market_id})")
    extra = len(candidate.leg_costs) - 6
    if extra > 0:
        lines.append(f"    - ... {extra} more")
    return "\n".join(lines)


def make_signature(candidate: Candidate) -> str:
    rounded = [f"{leg.side}:{c:.4f}" for leg, c in candidate.leg_costs]
    return f"{candidate.strategy}|{candidate.event_key}|{candidate.net_edge:.4f}|{','.join(rounded)}"


def format_candidate_brief(candidate: Candidate) -> str:
    return (
        f"EDGE ${candidate.net_edge:.4f} ({candidate.edge_pct:.2%}) | "
        f"cost ${candidate.basket_cost:.4f} | payout ${candidate.payout_after_fee:.4f} | "
        f"legs={len(candidate.leg_costs)} | {candidate.title}"
    )


def apply_observe_exec_edge_and_log_metrics(
    *,
    candidate: Candidate,
    basket: EventBasket,
    books: Dict[str, LocalBook],
    args,
    metrics_file,
    observe_exec_edge_filter: bool,
    observe_exec_edge_min_usd: float,
    observe_exec_edge_strike_limit: int,
    observe_exec_edge_cooldown_sec: float,
    observe_exec_edge_filter_strategies: Set[str],
    append_jsonl_func: Callable[[object, dict], None],
    logger,
) -> Tuple[Optional[dict], bool]:
    metrics_row: Optional[dict] = None
    if metrics_file or observe_exec_edge_filter:
        metrics_row = compute_candidate_metrics_row(
            candidate=candidate,
            basket=basket,
            books=books,
            args=args,
        )

    observe_exec_filtered = False
    if observe_exec_edge_filter and (
        not observe_exec_edge_filter_strategies
        or str(getattr(basket, "strategy", "") or "").strip().lower() in observe_exec_edge_filter_strategies
    ):
        now_eval = now_ts()
        if float(getattr(basket, "exec_edge_filter_until_ts", 0.0) or 0.0) > now_eval:
            observe_exec_filtered = True
        else:
            exec_edge_est = math.nan
            if isinstance(metrics_row, dict):
                exec_edge_est = as_float(metrics_row.get("net_edge_exec_est"), math.nan)

            if math.isfinite(exec_edge_est):
                if exec_edge_est <= observe_exec_edge_min_usd:
                    basket.exec_edge_neg_streak = int(getattr(basket, "exec_edge_neg_streak", 0) or 0) + 1
                else:
                    basket.exec_edge_neg_streak = 0

                if basket.exec_edge_neg_streak >= observe_exec_edge_strike_limit:
                    basket.exec_edge_filter_until_ts = now_eval + observe_exec_edge_cooldown_sec
                    basket.exec_edge_neg_streak = 0
                    observe_exec_filtered = True
                    logger.info(
                        f"[{iso_now()}] observe exec-edge filter: muted event={basket.key} "
                        f"strategy={basket.strategy} exec_edge={exec_edge_est:.4f} <= "
                        f"{observe_exec_edge_min_usd:.4f} cooldown={observe_exec_edge_cooldown_sec:.0f}s"
                    )
            else:
                basket.exec_edge_neg_streak = 0

    if isinstance(metrics_row, dict):
        if observe_exec_filtered:
            metrics_row["observe_exec_filter_blocked"] = True
            metrics_row["observe_exec_filter_until_ms"] = int(
                float(getattr(basket, "exec_edge_filter_until_ts", 0.0) or 0.0) * 1000.0
            )
            metrics_row["observe_exec_filter_min_usd"] = float(observe_exec_edge_min_usd)
        if metrics_file and (
            bool(getattr(args, "metrics_log_all_candidates", False))
            or metrics_row.get("passes_raw_threshold", False)
        ):
            reason = "threshold" if metrics_row.get("passes_raw_threshold", False) else "candidate"
            if observe_exec_filtered:
                reason = "observe_exec_filter_blocked"
            metrics_row["reason"] = reason
            append_jsonl_func(metrics_file, metrics_row)

    return metrics_row, observe_exec_filtered


async def process_impacted_event(
    *,
    basket: EventBasket,
    books: Dict[str, LocalBook],
    args,
    stats,
    min_eval_interval: float,
    metrics_file,
    observe_exec_edge_filter: bool,
    observe_exec_edge_min_usd: float,
    observe_exec_edge_strike_limit: int,
    observe_exec_edge_cooldown_sec: float,
    observe_exec_edge_filter_strategies: Set[str],
    observe_notify_min_interval: float,
    last_observe_notify_ts: float,
    logger,
    append_jsonl_func: Callable[[object, dict], None],
    notify_func: Callable[[object, str], None],
    live_execution_ctx: Optional[Dict[str, Any]] = None,
) -> float:
    now_eval = now_ts()
    if min_eval_interval > 0 and (now_eval - basket.last_eval_ts) < min_eval_interval:
        return last_observe_notify_ts

    candidate = compute_candidate(
        basket=basket,
        books=books,
        shares_per_leg=args.shares,
        winner_fee_rate=args.winner_fee_rate,
        fixed_cost=args.fixed_cost,
    )
    if not candidate:
        return last_observe_notify_ts
    basket.last_eval_ts = now_eval

    stats.candidates_total += 1
    stats.candidates_window += 1
    if (stats.best_all is None) or (candidate.net_edge > stats.best_all.net_edge):
        stats.best_all = candidate
    if (stats.best_window is None) or (candidate.net_edge > stats.best_window.net_edge):
        stats.best_window = candidate

    _metrics_row, observe_exec_filtered = apply_observe_exec_edge_and_log_metrics(
        candidate=candidate,
        basket=basket,
        books=books,
        args=args,
        metrics_file=metrics_file,
        observe_exec_edge_filter=observe_exec_edge_filter,
        observe_exec_edge_min_usd=observe_exec_edge_min_usd,
        observe_exec_edge_strike_limit=observe_exec_edge_strike_limit,
        observe_exec_edge_cooldown_sec=observe_exec_edge_cooldown_sec,
        observe_exec_edge_filter_strategies=observe_exec_edge_filter_strategies,
        append_jsonl_func=append_jsonl_func,
        logger=logger,
    )

    if candidate.net_edge < (args.min_edge_cents / 100.0):
        return last_observe_notify_ts
    if observe_exec_filtered:
        return last_observe_notify_ts

    sig = make_signature(candidate)
    now_alert = now_ts()
    if sig == basket.last_signature and (now_alert - basket.last_alert_ts) < args.alert_cooldown_sec:
        return last_observe_notify_ts

    logger.info("")
    logger.info(format_candidate(candidate))
    basket.last_signature = sig
    basket.last_alert_ts = now_alert

    if not args.execute and bool(getattr(args, "notify_observe_signals", False)):
        if observe_notify_min_interval <= 0 or (now_alert - last_observe_notify_ts) >= observe_notify_min_interval:
            notify_func(
                logger,
                (
                    f"OBSERVE SIGNAL {candidate.title} | "
                    f"edge {candidate.edge_pct:.2%} (${candidate.net_edge:.4f}) | "
                    f"cost ${candidate.basket_cost:.4f} | legs={len(candidate.leg_costs)}"
                ),
            )
            last_observe_notify_ts = now_alert

    if args.execute and isinstance(live_execution_ctx, dict):
        execute_func = live_execution_ctx.get("execute_func")
        if callable(execute_func):
            await execute_func(
                candidate=candidate,
                basket=basket,
                state=live_execution_ctx.get("state"),
                args=args,
                logger=logger,
                books=books,
                exec_backend=live_execution_ctx.get("exec_backend", "none"),
                client=live_execution_ctx.get("client"),
                simmer_api_key=live_execution_ctx.get("simmer_api_key", ""),
                now_ts_value=now_alert,
                notify_func=notify_func,
                save_state_func=live_execution_ctx.get("save_state_func"),
                state_file=live_execution_ctx.get("state_file"),
                sdk_request_func=live_execution_ctx.get("sdk_request_func"),
                fetch_simmer_positions_func=live_execution_ctx.get("fetch_simmer_positions_func"),
                estimate_exec_cost_func=live_execution_ctx.get("estimate_exec_cost_func"),
            )

    return last_observe_notify_ts


async def process_ws_raw_message(
    *,
    raw: str,
    books: Dict[str, LocalBook],
    token_to_events: Dict[str, Set[str]],
    event_map: Dict[str, EventBasket],
    state,
    args,
    stats,
    min_eval_interval: float,
    metrics_file,
    observe_exec_edge_filter: bool,
    observe_exec_edge_min_usd: float,
    observe_exec_edge_strike_limit: int,
    observe_exec_edge_cooldown_sec: float,
    observe_exec_edge_filter_strategies: Set[str],
    observe_notify_min_interval: float,
    last_observe_notify_ts: float,
    logger,
    append_jsonl_func: Callable[[object, dict], None],
    notify_func: Callable[[object, str], None],
    live_execution_ctx: Optional[Dict[str, Any]] = None,
) -> float:
    try:
        payload = json.loads(raw)
    except Exception:
        return last_observe_notify_ts

    impacted_events = collect_impacted_events_from_payload(
        payload=payload,
        books=books,
        token_to_events=token_to_events,
    )
    if not impacted_events:
        return last_observe_notify_ts

    if isinstance(live_execution_ctx, dict):
        live_execution_ctx["state"] = state

    for event_key in impacted_events:
        basket = event_map[event_key]
        last_observe_notify_ts = await process_impacted_event(
            basket=basket,
            books=books,
            args=args,
            stats=stats,
            min_eval_interval=min_eval_interval,
            metrics_file=metrics_file,
            observe_exec_edge_filter=observe_exec_edge_filter,
            observe_exec_edge_min_usd=observe_exec_edge_min_usd,
            observe_exec_edge_strike_limit=observe_exec_edge_strike_limit,
            observe_exec_edge_cooldown_sec=observe_exec_edge_cooldown_sec,
            observe_exec_edge_filter_strategies=observe_exec_edge_filter_strategies,
            observe_notify_min_interval=observe_notify_min_interval,
            last_observe_notify_ts=last_observe_notify_ts,
            logger=logger,
            append_jsonl_func=append_jsonl_func,
            notify_func=notify_func,
            live_execution_ctx=live_execution_ctx,
        )

    return last_observe_notify_ts
