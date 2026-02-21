#!/usr/bin/env python3
"""
Observe-only BTC 5m signal monitor for Polymarket.

Approach:
1) Load rolling BTC up/down 5-minute markets by deterministic event slug.
2) Read YES/NO top-of-book from CLOB.
3) Build an LMSR-style implied probability from both sides of the book.
4) Apply Bayesian shrinkage to a configurable prior.
5) Size a hypothetical trade using fractional Kelly.

This script never places orders. It only logs "would BUY ..." signals.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from polymarket_clob_arb_scanner import as_float, fetch_gamma_event_by_slug, fetch_json, parse_json_string_field

CLOB_API_BASE = "https://clob.polymarket.com"


def now_ts() -> float:
    return time.time()


def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clamp(x: float, lo: float, hi: float) -> float:
    if not math.isfinite(x):
        return lo
    if x < lo:
        return lo
    if x > hi:
        return hi
    return float(x)


def parse_iso_or_epoch_to_ms(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        x = float(value)
        if x <= 0:
            return None
        if x > 10_000_000_000:
            return int(x)
        return int(x * 1000.0)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return parse_iso_or_epoch_to_ms(float(s))
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000.0)
        except Exception:
            return None
    return None


class Logger:
    def __init__(self, log_file: str):
        self.log_file = Path(log_file)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def info(self, msg: str) -> None:
        print(msg)
        with self.log_file.open("a", encoding="utf-8", newline="\n") as f:
            f.write(msg + "\n")


@dataclass
class MarketPair:
    key: str
    slug: str
    title: str
    market_id: str
    token_a: str
    token_b: str
    label_a: str
    label_b: str
    end_ms: Optional[int]


@dataclass
class Signal:
    ts: str
    event_slug: str
    market_id: str
    title: str
    side: str
    side_label: str
    edge: float
    p_obs: float
    p_post: float
    ask_price: float
    kelly_full: float
    kelly_fractional: float
    stake_usd: float
    shares: float
    time_to_end_sec: Optional[float]
    spread_a: float
    spread_b: float
    ask_a: float
    ask_b: float
    bid_a: float
    bid_b: float


@dataclass
class WindowStats:
    started_at: float
    loops: int = 0
    markets_seen: int = 0
    markets_evaluated: int = 0
    signals: int = 0
    edge_sum: float = 0.0


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")


def fetch_book(token_id: str) -> Optional[dict]:
    url = f"{CLOB_API_BASE}/book?token_id={token_id}"
    data = fetch_json(url, timeout=20)
    if isinstance(data, dict):
        return data
    return None


def best_ask_bid(book: dict) -> Tuple[Optional[float], Optional[float]]:
    asks = book.get("asks") if isinstance(book, dict) else None
    bids = book.get("bids") if isinstance(book, dict) else None

    best_ask: Optional[float] = None
    best_bid: Optional[float] = None

    if isinstance(asks, list):
        for x in asks:
            if not isinstance(x, dict):
                continue
            p = as_float(x.get("price"), math.nan)
            sz = as_float(x.get("size"), 0.0)
            if not math.isfinite(p) or sz <= 0 or p <= 0 or p >= 1:
                continue
            if best_ask is None or p < best_ask:
                best_ask = p

    if isinstance(bids, list):
        for x in bids:
            if not isinstance(x, dict):
                continue
            p = as_float(x.get("price"), math.nan)
            sz = as_float(x.get("size"), 0.0)
            if not math.isfinite(p) or sz <= 0 or p <= 0 or p >= 1:
                continue
            if best_bid is None or p > best_bid:
                best_bid = p

    return best_ask, best_bid


def mid_price(ask: Optional[float], bid: Optional[float]) -> Optional[float]:
    if ask is not None and bid is not None and ask >= bid and ask > 0 and bid > 0:
        return 0.5 * (ask + bid)
    if ask is not None and ask > 0:
        return float(ask)
    if bid is not None and bid > 0:
        return float(bid)
    return None


def kelly_binary(prob_win: float, price: float) -> float:
    denom = 1.0 - price
    if denom <= 1e-9:
        return 0.0
    f = (prob_win - price) / denom
    return clamp(f, 0.0, 1.0)


def choose_binary_indices(token_ids: List[str], outcomes: List[str]) -> Tuple[int, int]:
    if len(token_ids) < 2:
        return -1, -1
    yes_i: Optional[int] = None
    no_i: Optional[int] = None
    for i, out in enumerate(outcomes[: len(token_ids)]):
        o = str(out).strip().lower()
        if o == "yes":
            yes_i = i
        elif o == "no":
            no_i = i
    if yes_i is not None and no_i is not None and yes_i != no_i:
        return yes_i, no_i
    return 0, 1


def build_btc5m_pairs(windows_back: int, windows_forward: int) -> List[MarketPair]:
    back = max(0, int(windows_back or 0))
    fwd = max(0, int(windows_forward or 0))
    now_s = int(time.time())
    start_s = (now_s // 300) * 300

    pairs: List[MarketPair] = []
    seen: set[str] = set()

    for i in range(-back, fwd + 1):
        slug = f"btc-updown-5m-{start_s + (i * 300)}"
        ev = fetch_gamma_event_by_slug(slug)
        if not isinstance(ev, dict):
            continue

        event_title = str(ev.get("title") or slug).strip()
        rows = ev.get("markets") or []
        if not isinstance(rows, list):
            continue

        for m in rows:
            if not isinstance(m, dict):
                continue
            if m.get("enableOrderBook") is False:
                continue

            token_ids = parse_json_string_field(m.get("clobTokenIds"))
            outcomes = [str(x) for x in parse_json_string_field(m.get("outcomes"))]
            a_i, b_i = choose_binary_indices(token_ids, outcomes)
            if a_i < 0 or b_i < 0:
                continue

            market_id = str(m.get("id") or "").strip()
            if not market_id:
                continue
            key = f"{slug}:{market_id}"
            if key in seen:
                continue
            seen.add(key)

            label_a = outcomes[a_i].strip() if a_i < len(outcomes) else "A"
            label_b = outcomes[b_i].strip() if b_i < len(outcomes) else "B"
            if not label_a:
                label_a = "A"
            if not label_b:
                label_b = "B"

            end_ms = parse_iso_or_epoch_to_ms(
                m.get("endDate") or m.get("endDateIso") or ev.get("endDate") or ev.get("endDateIso")
            )
            title = str(m.get("question") or event_title or slug).strip()

            pairs.append(
                MarketPair(
                    key=key,
                    slug=slug,
                    title=title,
                    market_id=market_id,
                    token_a=token_ids[a_i],
                    token_b=token_ids[b_i],
                    label_a=label_a,
                    label_b=label_b,
                    end_ms=end_ms,
                )
            )

    return pairs


def evaluate_market(pair: MarketPair, args, ts_now: float) -> Optional[Signal]:
    book_a = fetch_book(pair.token_a)
    book_b = fetch_book(pair.token_b)
    if not book_a or not book_b:
        return None

    ask_a, bid_a = best_ask_bid(book_a)
    ask_b, bid_b = best_ask_bid(book_b)
    if ask_a is None or ask_b is None:
        return None

    mid_a = mid_price(ask_a, bid_a)
    mid_b = mid_price(ask_b, bid_b)
    if mid_a is None or mid_b is None:
        return None

    time_to_end_sec: Optional[float] = None
    if pair.end_ms is not None and pair.end_ms > 0:
        time_to_end_sec = (float(pair.end_ms) / 1000.0) - ts_now
        if time_to_end_sec < float(args.min_seconds_to_end):
            return None
        max_tte = float(args.max_seconds_to_end or 0.0)
        if max_tte > 0 and time_to_end_sec > max_tte:
            return None

    spread_a = max(0.0, float(ask_a) - float(bid_a or ask_a))
    spread_b = max(0.0, float(ask_b) - float(bid_b or ask_b))
    weight_floor = max(1e-6, float(args.weight_spread_floor))
    w_a = 1.0 / max(spread_a, weight_floor)
    w_b = 1.0 / max(spread_b, weight_floor)

    p_a_from_a = clamp(float(mid_a), 0.001, 0.999)
    p_a_from_b = clamp(1.0 - float(mid_b), 0.001, 0.999)
    p_obs = clamp(((w_a * p_a_from_a) + (w_b * p_a_from_b)) / (w_a + w_b), 0.001, 0.999)

    prior_prob = clamp(float(args.prior_prob), 0.001, 0.999)
    prior_w = max(0.0, float(args.prior_strength))
    obs_w = max(0.0, float(args.obs_strength))
    if prior_w + obs_w <= 1e-9:
        p_post = p_obs
    else:
        p_post = clamp(((prior_w * prior_prob) + (obs_w * p_obs)) / (prior_w + obs_w), 0.001, 0.999)

    fee = max(0.0, float(args.taker_fee_rate))
    edge_a = p_post - float(ask_a) - fee
    edge_b = (1.0 - p_post) - float(ask_b) - fee

    if edge_a >= edge_b:
        side = "A"
        side_label = pair.label_a
        edge = edge_a
        ask_price = float(ask_a)
        prob_win = p_post
    else:
        side = "B"
        side_label = pair.label_b
        edge = edge_b
        ask_price = float(ask_b)
        prob_win = 1.0 - p_post

    min_edge = float(args.min_edge_cents) / 100.0
    if edge < min_edge:
        return None

    k_full = kelly_binary(prob_win=prob_win, price=ask_price)
    k_frac = clamp(float(args.kelly_fraction), 0.0, 1.0) * k_full
    bankroll = max(0.0, float(args.bankroll_usd))
    stake = bankroll * k_frac
    max_bet = max(0.0, float(args.max_bet_usd))
    if max_bet > 0:
        stake = min(stake, max_bet)
    min_bet = max(0.0, float(args.min_bet_usd))
    if stake < min_bet:
        return None

    shares = stake / ask_price if ask_price > 0 else 0.0
    if shares <= 0 or not math.isfinite(shares):
        return None

    return Signal(
        ts=iso_now(),
        event_slug=pair.slug,
        market_id=pair.market_id,
        title=pair.title,
        side=side,
        side_label=side_label,
        edge=float(edge),
        p_obs=float(p_obs),
        p_post=float(p_post),
        ask_price=float(ask_price),
        kelly_full=float(k_full),
        kelly_fractional=float(k_frac),
        stake_usd=float(stake),
        shares=float(shares),
        time_to_end_sec=float(time_to_end_sec) if time_to_end_sec is not None else None,
        spread_a=float(spread_a),
        spread_b=float(spread_b),
        ask_a=float(ask_a),
        ask_b=float(ask_b),
        bid_a=float(bid_a or 0.0),
        bid_b=float(bid_b or 0.0),
    )


def parse_args():
    p = argparse.ArgumentParser(description="Observe-only BTC 5m LMSR/Bayes/Kelly signal monitor")
    p.add_argument("--windows-back", type=int, default=1, help="How many prior 5m windows to include")
    p.add_argument("--windows-forward", type=int, default=1, help="How many future 5m windows to include")
    p.add_argument("--min-seconds-to-end", type=float, default=20.0, help="Skip markets resolving too soon")
    p.add_argument("--max-seconds-to-end", type=float, default=600.0, help="Skip markets too far out (0=disabled)")

    p.add_argument("--prior-prob", type=float, default=0.50, help="Bayesian prior probability for side A")
    p.add_argument("--prior-strength", type=float, default=8.0, help="Bayesian prior pseudo-count")
    p.add_argument("--obs-strength", type=float, default=20.0, help="Observed-price pseudo-count")
    p.add_argument(
        "--weight-spread-floor",
        type=float,
        default=0.005,
        help="Spread floor for blending A/B implied probabilities (price units)",
    )

    p.add_argument("--min-edge-cents", type=float, default=0.6, help="Signal threshold in cents")
    p.add_argument("--taker-fee-rate", type=float, default=0.0, help="Per-share fee rate assumption")
    p.add_argument("--kelly-fraction", type=float, default=0.25, help="Fraction of full Kelly (0..1)")
    p.add_argument("--bankroll-usd", type=float, default=200.0, help="Reference bankroll for sizing")
    p.add_argument("--min-bet-usd", type=float, default=1.0, help="Minimum hypothetical stake")
    p.add_argument("--max-bet-usd", type=float, default=25.0, help="Maximum hypothetical stake (0=unlimited)")

    p.add_argument("--poll-sec", type=float, default=2.0, help="Loop interval seconds")
    p.add_argument("--signal-cooldown-sec", type=float, default=15.0, help="Suppress repeated same-side signals")
    p.add_argument("--summary-every-sec", type=float, default=60.0, help="Summary cadence (0=disabled)")
    p.add_argument("--run-seconds", type=int, default=0, help="Auto-exit after N seconds (0=run forever)")

    p.add_argument("--log-file", default="", help="Log output file path")
    p.add_argument("--metrics-file", default="", help="JSONL signal output path")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    script_dir = Path(__file__).resolve().parent
    if not args.log_file:
        args.log_file = str(script_dir.parent / "logs" / "btc5m-lmsr-observe.log")
    if not args.metrics_file:
        args.metrics_file = str(script_dir.parent / "logs" / "btc5m-lmsr-observe-metrics.jsonl")

    logger = Logger(str(args.log_file))
    metrics_path = Path(str(args.metrics_file))
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.touch(exist_ok=True)

    logger.info(f"[{iso_now()}] Mode: observe-only (no order placement)")
    logger.info(
        f"[{iso_now()}] Universe: btc-5m back={int(args.windows_back)} forward={int(args.windows_forward)} "
        f"tte=[{float(args.min_seconds_to_end):.0f}s,{float(args.max_seconds_to_end):.0f}s]"
    )
    logger.info(
        f"[{iso_now()}] Pricing: prior={float(args.prior_prob):.3f} prior_strength={float(args.prior_strength):.1f} "
        f"obs_strength={float(args.obs_strength):.1f} min_edge={float(args.min_edge_cents):.2f}c"
    )
    logger.info(
        f"[{iso_now()}] Sizing: kelly_fraction={float(args.kelly_fraction):.2f} bankroll=${float(args.bankroll_usd):.2f} "
        f"stake=[${float(args.min_bet_usd):.2f},${float(args.max_bet_usd):.2f}]"
    )

    started = now_ts()
    last_signal_ts: Dict[str, float] = {}
    stats = WindowStats(started_at=now_ts())

    while True:
        ts_loop = now_ts()
        if int(args.run_seconds or 0) > 0 and (ts_loop - started) >= int(args.run_seconds):
            logger.info(f"[{iso_now()}] run_seconds reached -> exiting")
            break

        try:
            pairs = build_btc5m_pairs(
                windows_back=int(args.windows_back),
                windows_forward=int(args.windows_forward),
            )
        except Exception as e:
            logger.info(f"[{iso_now()}] warn: failed to build btc-5m universe: {type(e).__name__}")
            time.sleep(max(0.2, float(args.poll_sec)))
            continue

        stats.loops += 1
        stats.markets_seen += len(pairs)

        if not pairs:
            logger.info(f"[{iso_now()}] warn: no btc-5m markets found")
        else:
            for pair in pairs:
                stats.markets_evaluated += 1
                signal = evaluate_market(pair, args, ts_now=ts_loop)
                if signal is None:
                    continue

                dedupe_key = f"{pair.key}:{signal.side}"
                next_ok = float(last_signal_ts.get(dedupe_key, 0.0))
                if ts_loop < next_ok:
                    continue
                last_signal_ts[dedupe_key] = ts_loop + float(args.signal_cooldown_sec)

                stats.signals += 1
                stats.edge_sum += float(signal.edge)

                tte = "n/a"
                if signal.time_to_end_sec is not None:
                    tte = f"{max(0.0, float(signal.time_to_end_sec)):.0f}s"
                logger.info(
                    f"[{signal.ts}] would BUY {signal.side_label} | edge={signal.edge * 100.0:+.2f}c "
                    f"p_obs={signal.p_obs:.3f} p_post={signal.p_post:.3f} price={signal.ask_price:.3f} "
                    f"kelly={signal.kelly_full:.3f} frac={signal.kelly_fractional:.3f} "
                    f"stake=${signal.stake_usd:.2f} shares={signal.shares:.2f} tte={tte} | {signal.title[:120]}"
                )
                append_jsonl(metrics_path, asdict(signal))

        if float(args.summary_every_sec or 0.0) > 0:
            elapsed = ts_loop - stats.started_at
            if elapsed >= float(args.summary_every_sec):
                avg_edge_c = 0.0
                if stats.signals > 0:
                    avg_edge_c = (stats.edge_sum / float(stats.signals)) * 100.0
                logger.info(
                    f"[{iso_now()}] summary({int(elapsed)}s): loops={stats.loops} seen={stats.markets_seen} "
                    f"eval_ok={stats.markets_evaluated} signals={stats.signals} avg_edge={avg_edge_c:+.2f}c"
                )
                stats = WindowStats(started_at=now_ts())

        time.sleep(max(0.2, float(args.poll_sec)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
