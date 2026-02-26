#!/usr/bin/env python3
"""
Observe-only event-driven mispricing monitor for Polymarket.

What this script does:
1) Scans active binary (YES/NO) Polymarket markets.
2) Filters for event-driven catalyst classes (M&A, regulation, legal, etc.).
3) Builds a lightweight model probability from text-driven priors + market evidence.
4) Computes edge, expected value per share, and fractional-Kelly sizing.
5) Logs ranked paper opportunities under logs/ (never places orders).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

from polymarket_clob_arb_scanner import fetch_json, parse_json_string_field

GAMMA_API_BASE = "https://gamma-api.polymarket.com"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().strftime("%Y-%m-%d %H:%M:%S")


def utc_tag() -> str:
    return now_utc().strftime("%Y%m%d_%H%M%S")


def clamp(x: float, lo: float, hi: float) -> float:
    if not math.isfinite(x):
        return lo
    if x < lo:
        return lo
    if x > hi:
        return hi
    return float(x)


def as_float(value, default: float = math.nan) -> float:
    try:
        return float(value)
    except Exception:
        return default


def parse_iso_ts(value: str) -> Optional[int]:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def compile_regex(pattern: str) -> Optional[re.Pattern]:
    raw = str(pattern or "").strip()
    if not raw:
        return None
    return re.compile(raw, re.IGNORECASE)


def question_allowed(question: str, include_rx: Optional[re.Pattern], exclude_rx: Optional[re.Pattern]) -> bool:
    q = str(question or "")
    if include_rx and not include_rx.search(q):
        return False
    if exclude_rx and exclude_rx.search(q):
        return False
    return True


def text_hits(text: str, terms: Sequence[str]) -> List[str]:
    out: List[str] = []
    t = text.lower()
    for term in terms:
        raw = str(term or "").strip().lower()
        if not raw:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(raw) + r"(?![a-z0-9])"
        if re.search(pattern, t):
            out.append(term)
    return out


def fetch_json_retry(url: str, retries: int, sleep_sec: float) -> Optional[object]:
    for i in range(max(1, retries)):
        data = fetch_json(url, timeout=30)
        if data is not None:
            return data
        if i < retries - 1:
            time.sleep(max(0.0, sleep_sec))
    return None


def append_jsonl(path: Path, row: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")


def load_signal_state(path: Path) -> Dict[str, float]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        obj = raw.get("last_emit_ts_by_key")
        if not isinstance(obj, dict):
            return {}
        out: Dict[str, float] = {}
        for k, v in obj.items():
            key = str(k or "").strip()
            if not key:
                continue
            ts = as_float(v, math.nan)
            if math.isfinite(ts) and ts > 0.0:
                out[key] = float(ts)
        return out
    except Exception:
        return {}


def save_signal_state(path: Path, state: Dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at_utc": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_emit_ts_by_key": state,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    tmp.replace(path)


class Logger:
    def __init__(self, log_file: str):
        self.log_file = Path(log_file)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def info(self, msg: str) -> None:
        try:
            print(msg)
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))
        with self.log_file.open("a", encoding="utf-8", newline="\n") as f:
            f.write(msg + "\n")


@dataclass(frozen=True)
class EventClassRule:
    name: str
    terms: Tuple[str, ...]
    prior_shift: float
    fragility_boost: float


EVENT_RULES: Tuple[EventClassRule, ...] = (
    EventClassRule(
        name="merger_arb",
        terms=(
            "merger",
            "acquire",
            "acquisition",
            "takeover",
            "buyout",
            "deal close",
            "antitrust",
            "doj",
            "ftc",
            "cfius",
        ),
        prior_shift=0.06,
        fragility_boost=0.20,
    ),
    EventClassRule(
        name="restructuring",
        terms=(
            "bankrupt",
            "chapter 11",
            "restructuring",
            "default",
            "insolvency",
            "creditor",
            "debt exchange",
        ),
        prior_shift=-0.08,
        fragility_boost=0.18,
    ),
    EventClassRule(
        name="earnings",
        terms=("earnings", "eps", "guidance", "beat", "miss", "quarter", "q1", "q2", "q3", "q4"),
        prior_shift=0.00,
        fragility_boost=0.08,
    ),
    EventClassRule(
        name="regulatory",
        terms=(
            "approval",
            "approve",
            "permit",
            "license",
            "fda",
            "sec",
            "etf",
            "ban",
            "regulator",
            "compliance",
        ),
        prior_shift=0.00,
        fragility_boost=0.22,
    ),
    EventClassRule(
        name="legal",
        terms=(
            "court",
            "lawsuit",
            "judge",
            "ruling",
            "verdict",
            "appeal",
            "settlement",
            "convict",
            "acquit",
            "injunction",
        ),
        prior_shift=0.00,
        fragility_boost=0.24,
    ),
    EventClassRule(
        name="special_situations",
        terms=(
            "spin-off",
            "spinoff",
            "asset sale",
            "divest",
            "tender offer",
            "reverse split",
            "delist",
        ),
        prior_shift=0.03,
        fragility_boost=0.16,
    ),
    EventClassRule(
        name="index_rebalance",
        terms=(
            "s&p 500",
            "nasdaq-100",
            "russell 2000",
            "index inclusion",
            "index exclusion",
            "rebalance",
            "dow jones",
        ),
        prior_shift=-0.02,
        fragility_boost=0.12,
    ),
)

POSITIVE_TERMS = (
    "approve",
    "approved",
    "pass",
    "passes",
    "close",
    "complete",
    "beat",
    "win",
    "settle",
    "include",
    "uphold",
    "acquit",
)
NEGATIVE_TERMS = (
    "reject",
    "deny",
    "block",
    "fail",
    "miss",
    "lose",
    "delay",
    "bankrupt",
    "default",
    "convict",
    "ban",
    "strike down",
)
FRAGILITY_TERMS = (
    "antitrust",
    "doj",
    "ftc",
    "cfius",
    "injunction",
    "appeal",
    "lawsuit",
    "court",
    "financing",
    "debt",
    "bankrupt",
    "regulator",
    "vote",
    "shareholder",
)
AMBIGUITY_TERMS = (
    "at least",
    "more than",
    "less than",
    "above",
    "below",
    "before",
    "by ",
    "anytime",
    "officially",
    "announced",
    "or",
)


@dataclass
class MarketSnapshot:
    market_id: str
    question: str
    category: str
    end_iso: str
    end_ts: Optional[int]
    days_to_end: Optional[float]
    yes_price: float
    no_price: float
    p_market_yes: float
    vig: float
    liquidity_num: float
    volume_24h: float
    event_slug: str


@dataclass
class Opportunity:
    ts: str
    run_id: str
    market_id: str
    event_slug: str
    question: str
    category: str
    event_class: str
    matched_terms: List[str]
    side: str
    selected_price: float
    yes_price: float
    no_price: float
    p_market_yes: float
    p_prior_yes: float
    p_model_yes: float
    edge: float
    edge_cents: float
    ev_per_share: float
    kelly_full: float
    kelly_fractional: float
    suggested_stake_usd: float
    confidence: float
    fragility: float
    ambiguity: float
    liquidity_num: float
    volume_24h: float
    days_to_end: Optional[float]
    end_iso: str
    vig: float


def detect_event_rule(question: str, category: str) -> Tuple[Optional[EventClassRule], List[str]]:
    text = f"{question} {category}".lower()
    best_rule: Optional[EventClassRule] = None
    best_hits: List[str] = []
    for rule in EVENT_RULES:
        hits = text_hits(text, rule.terms)
        if len(hits) > len(best_hits):
            best_rule = rule
            best_hits = hits
    if not best_hits:
        return None, []
    return best_rule, sorted(set(best_hits))


def directional_score(question: str) -> float:
    q = question.lower()
    pos = len(text_hits(q, POSITIVE_TERMS))
    neg = len(text_hits(q, NEGATIVE_TERMS))
    raw = float(pos - neg)
    return clamp(math.tanh(raw / 3.0), -1.0, 1.0)


def parse_binary_market(m: Dict[str, object]) -> Optional[MarketSnapshot]:
    outcomes = [str(x).strip().lower() for x in parse_json_string_field(m.get("outcomes"))]
    prices_raw = parse_json_string_field(m.get("outcomePrices"))
    prices = [as_float(x) for x in prices_raw]

    if len(outcomes) != 2 or len(prices) != 2:
        return None
    if "yes" not in outcomes or "no" not in outcomes:
        return None

    yes_i = outcomes.index("yes")
    no_i = 1 - yes_i
    yes_price = prices[yes_i]
    no_price = prices[no_i]
    if not (math.isfinite(yes_price) and math.isfinite(no_price)):
        return None
    if yes_price <= 0.0 or no_price <= 0.0 or yes_price >= 1.0 or no_price >= 1.0:
        return None

    p_market_yes = yes_price
    if yes_price + no_price > 1e-9:
        p_market_yes = yes_price / (yes_price + no_price)

    end_iso = str(m.get("endDate") or "")
    end_ts = parse_iso_ts(end_iso)
    dte: Optional[float] = None
    if end_ts is not None:
        dte = (end_ts - int(now_utc().timestamp())) / 86400.0

    events = m.get("events") or []
    event_slug = ""
    if isinstance(events, list) and events:
        e0 = events[0] if isinstance(events[0], dict) else {}
        event_slug = str(e0.get("slug") or "").strip()

    return MarketSnapshot(
        market_id=str(m.get("id") or "").strip(),
        question=str(m.get("question") or "").strip(),
        category=str(m.get("category") or "").strip(),
        end_iso=end_iso,
        end_ts=end_ts,
        days_to_end=dte,
        yes_price=float(yes_price),
        no_price=float(no_price),
        p_market_yes=clamp(p_market_yes, 0.001, 0.999),
        vig=float(yes_price + no_price - 1.0),
        liquidity_num=as_float(m.get("liquidityNum"), 0.0),
        volume_24h=max(as_float(m.get("volume24hr"), 0.0), as_float(m.get("volume24h"), 0.0)),
        event_slug=event_slug,
    )


def fetch_active_markets(args: argparse.Namespace) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for page in range(max(1, int(args.max_pages))):
        q = urlencode(
            {
                "active": "true",
                "closed": "false",
                "limit": str(args.page_size),
                "offset": str(page * args.page_size),
            }
        )
        url = f"{GAMMA_API_BASE}/markets?{q}"
        data = fetch_json_retry(url, retries=args.api_retries, sleep_sec=args.api_retry_sleep_sec)
        if not isinstance(data, list) or not data:
            break
        for item in data:
            if isinstance(item, dict):
                rows.append(item)
        if len(data) < int(args.page_size):
            break
        time.sleep(max(0.0, args.sleep_between_pages_sec))
    return rows


def score_snapshot(
    s: MarketSnapshot,
    rule: Optional[EventClassRule],
    matched_terms: List[str],
    args: argparse.Namespace,
) -> Opportunity:
    rule_shift = rule.prior_shift if rule else 0.0
    rule_fragility = rule.fragility_boost if rule else 0.0
    event_class = rule.name if rule else "unclassified"

    dir_score = directional_score(s.question)
    dir_shift = args.directional_bias_scale * dir_score

    fragility_hits = text_hits(s.question.lower(), FRAGILITY_TERMS)
    fragility = rule_fragility + 0.06 * len(set(fragility_hits))
    if s.days_to_end is not None and s.days_to_end > 45:
        fragility += 0.08
    if s.liquidity_num < 25_000:
        fragility += 0.08
    fragility = clamp(fragility, 0.0, 1.0)

    ambiguity_hits = text_hits(s.question.lower(), AMBIGUITY_TERMS)
    ambiguity = clamp(0.08 * len(set(ambiguity_hits)), 0.0, 1.0)

    p_prior_yes = clamp(0.5 + rule_shift + dir_shift - 0.03 * ambiguity, 0.03, 0.97)

    liq_scale = clamp(math.log10(s.liquidity_num + 10.0) / 5.0, 0.2, 1.6)
    vol_scale = clamp(math.log10(s.volume_24h + 10.0) / 4.0, 0.2, 1.6)
    quality_scale = 0.5 * (liq_scale + vol_scale)

    prior_strength = args.prior_strength * (1.0 + 0.8 * fragility + 0.3 * ambiguity)
    obs_strength = args.obs_strength * quality_scale / (
        1.0 + args.fragility_obs_discount * fragility + args.ambiguity_obs_discount * ambiguity
    )
    denom = max(1e-9, prior_strength + obs_strength)
    p_model_yes = clamp((p_prior_yes * prior_strength + s.p_market_yes * obs_strength) / denom, 0.01, 0.99)

    edge_yes = p_model_yes - s.yes_price
    edge_no = (1.0 - p_model_yes) - s.no_price
    if edge_yes >= edge_no:
        side = "YES"
        price = s.yes_price
        p_win = p_model_yes
        edge = edge_yes
    else:
        side = "NO"
        price = s.no_price
        p_win = 1.0 - p_model_yes
        edge = edge_no

    kelly_full = 0.0
    if edge > 0.0 and price < 0.999:
        kelly_full = clamp((p_win - price) / max(1e-9, 1.0 - price), 0.0, 1.0)
    kelly_fractional = min(args.max_kelly_fraction, kelly_full * args.kelly_fraction)
    suggested_stake = args.bankroll_usd * kelly_fractional
    if suggested_stake < args.min_bet_usd:
        suggested_stake = 0.0
    else:
        suggested_stake = min(suggested_stake, args.max_bet_usd)

    confidence = clamp(1.0 - 0.5 * fragility - 0.35 * ambiguity, 0.05, 0.95)

    return Opportunity(
        ts=now_iso(),
        run_id="",
        market_id=s.market_id,
        event_slug=s.event_slug,
        question=s.question,
        category=s.category,
        event_class=event_class,
        matched_terms=matched_terms,
        side=side,
        selected_price=price,
        yes_price=s.yes_price,
        no_price=s.no_price,
        p_market_yes=s.p_market_yes,
        p_prior_yes=p_prior_yes,
        p_model_yes=p_model_yes,
        edge=edge,
        edge_cents=edge * 100.0,
        ev_per_share=edge,
        kelly_full=kelly_full,
        kelly_fractional=kelly_fractional,
        suggested_stake_usd=suggested_stake,
        confidence=confidence,
        fragility=fragility,
        ambiguity=ambiguity,
        liquidity_num=s.liquidity_num,
        volume_24h=s.volume_24h,
        days_to_end=s.days_to_end,
        end_iso=s.end_iso,
        vig=s.vig,
    )


def shorten(s: str, max_len: int) -> str:
    t = str(s or "").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 3].rstrip() + "..."


def run_once(args: argparse.Namespace, logger: Logger) -> int:
    t0 = time.time()
    run_id = utc_tag()
    include_rx = compile_regex(args.include_regex)
    exclude_rx = compile_regex(args.exclude_regex)

    raw_markets = fetch_active_markets(args)
    scanned = len(raw_markets)

    binary_count = 0
    filtered_count = 0
    event_count = 0
    opportunities: List[Opportunity] = []

    for m in raw_markets:
        s = parse_binary_market(m)
        if not s:
            continue
        binary_count += 1

        if not s.market_id or not s.question:
            continue
        if not question_allowed(s.question, include_rx, exclude_rx):
            continue
        if s.liquidity_num < args.min_liquidity:
            continue
        if s.volume_24h < args.min_volume_24h:
            continue
        if s.days_to_end is not None:
            if s.days_to_end < args.min_days_to_end:
                continue
            if args.max_days_to_end > 0 and s.days_to_end > args.max_days_to_end:
                continue
        filtered_count += 1

        rule, matched_terms = detect_event_rule(s.question, s.category)
        if rule is not None:
            event_count += 1
        elif not args.include_non_event:
            continue

        opp = score_snapshot(s, rule, matched_terms, args)
        if opp.edge_cents < args.min_edge_cents:
            continue
        if opp.selected_price < args.min_leg_price or opp.selected_price > args.max_leg_price:
            continue
        if opp.confidence < args.min_confidence:
            continue
        opp.run_id = run_id
        opportunities.append(opp)

    opportunities.sort(key=lambda x: (x.edge_cents, x.confidence), reverse=True)

    top_raw = opportunities[: max(0, int(args.top_n))]
    top: List[Opportunity] = []
    suppressed_count = 0
    cooldown_sec = max(0.0, float(args.signal_cooldown_sec))
    if cooldown_sec > 0.0:
        state_path = Path(args.signal_state_file)
        last_emit = load_signal_state(state_path)
        now_ts = time.time()
        for opp in top_raw:
            key = f"{opp.market_id}:{opp.side}"
            prev = float(last_emit.get(key) or 0.0)
            if prev > 0.0 and (now_ts - prev) < cooldown_sec:
                suppressed_count += 1
                continue
            last_emit[key] = now_ts
            top.append(opp)
        keep_after = now_ts - max(cooldown_sec * 6.0, 24.0 * 3600.0)
        last_emit = {k: v for k, v in last_emit.items() if v >= keep_after}
        save_signal_state(state_path, last_emit)
    else:
        top = top_raw

    logger.info(
        (
            f"[{now_iso()}] run={run_id} scanned={scanned} binary={binary_count} eligible={filtered_count} "
            f"event_matched={event_count} candidates={len(opportunities)} "
            f"suppressed={suppressed_count} written={len(top)} "
            f"elapsed_sec={(time.time() - t0):.2f}"
        )
    )

    for idx, opp in enumerate(top, start=1):
        dte = "na" if opp.days_to_end is None else f"{opp.days_to_end:.1f}d"
        logger.info(
            (
                f"#{idx:02d} {opp.side} edge={opp.edge_cents:+.2f}c "
                f"yes_mkt={opp.p_market_yes:.3f} yes_model={opp.p_model_yes:.3f} "
                f"kelly={opp.kelly_fractional * 100.0:.2f}% stake=${opp.suggested_stake_usd:.2f} "
                f"class={opp.event_class} conf={opp.confidence:.2f} dte={dte} "
                f"liq={opp.liquidity_num:.0f} vol24h={opp.volume_24h:.0f} "
                f"q=\"{shorten(opp.question, 110)}\""
            )
        )

    signals_path = Path(args.signals_file)
    for opp in top:
        append_jsonl(signals_path, asdict(opp))

    metrics_row = {
        "ts": now_iso(),
        "run_id": run_id,
        "scanned": scanned,
        "binary_count": binary_count,
        "eligible_count": filtered_count,
        "event_count": event_count,
        "candidate_count": len(opportunities),
        "suppressed_count": suppressed_count,
        "top_written": len(top),
        "runtime_sec": round(time.time() - t0, 3),
        "args": {
            "max_pages": args.max_pages,
            "page_size": args.page_size,
            "min_liquidity": args.min_liquidity,
            "min_volume_24h": args.min_volume_24h,
            "min_edge_cents": args.min_edge_cents,
            "min_leg_price": args.min_leg_price,
            "max_leg_price": args.max_leg_price,
            "min_confidence": args.min_confidence,
            "include_non_event": bool(args.include_non_event),
            "signal_cooldown_sec": args.signal_cooldown_sec,
        },
    }
    append_jsonl(Path(args.metrics_file), metrics_row)

    return len(opportunities)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Observe-only event-driven mispricing monitor for Polymarket.")
    p.add_argument("--poll-sec", type=float, default=0.0, help="Run loop interval in seconds (0 = run once).")
    p.add_argument("--max-pages", type=int, default=8, help="Gamma pages to scan.")
    p.add_argument("--page-size", type=int, default=200, help="Markets per page.")
    p.add_argument("--sleep-between-pages-sec", type=float, default=0.12, help="Throttle between page requests.")
    p.add_argument("--api-retries", type=int, default=3, help="Retries per Gamma request.")
    p.add_argument("--api-retry-sleep-sec", type=float, default=0.35, help="Retry backoff sleep (seconds).")

    p.add_argument("--include-regex", default="", help="Only include questions matching this regex.")
    p.add_argument("--exclude-regex", default="", help="Exclude questions matching this regex.")
    p.add_argument("--include-non-event", action="store_true", help="Also score markets not matching event classes.")

    p.add_argument("--min-liquidity", type=float, default=5_000.0, help="Minimum market liquidityNum filter.")
    p.add_argument("--min-volume-24h", type=float, default=250.0, help="Minimum 24h volume filter.")
    p.add_argument("--min-days-to-end", type=float, default=0.5, help="Minimum days until market end.")
    p.add_argument("--max-days-to-end", type=float, default=365.0, help="Maximum days until market end (<=0 disables).")

    p.add_argument("--prior-strength", type=float, default=5.0, help="Base strength of text-derived prior.")
    p.add_argument("--obs-strength", type=float, default=12.0, help="Base strength of market-implied evidence.")
    p.add_argument(
        "--directional-bias-scale",
        type=float,
        default=0.07,
        help="Strength of positive/negative keyword directional shift on YES prior.",
    )
    p.add_argument(
        "--fragility-obs-discount",
        type=float,
        default=1.6,
        help="How strongly fragility reduces trust in market-implied probability.",
    )
    p.add_argument(
        "--ambiguity-obs-discount",
        type=float,
        default=1.0,
        help="How strongly ambiguity reduces trust in market-implied probability.",
    )

    p.add_argument("--min-edge-cents", type=float, default=1.0, help="Minimum edge (cents/share) to emit.")
    p.add_argument(
        "--min-leg-price",
        type=float,
        default=0.05,
        help="Minimum selected-side price to emit (longshot guardrail).",
    )
    p.add_argument(
        "--max-leg-price",
        type=float,
        default=0.95,
        help="Maximum selected-side price to emit (overconfident guardrail).",
    )
    p.add_argument("--min-confidence", type=float, default=0.25, help="Minimum model confidence [0,1].")
    p.add_argument("--top-n", type=int, default=15, help="Top opportunities to print/write per run.")
    p.add_argument(
        "--signal-cooldown-sec",
        type=float,
        default=0.0,
        help="Suppress repeated emits for same market+side within this cooldown (0 = disabled).",
    )
    p.add_argument(
        "--signal-state-file",
        default="logs/event-driven-observe-signal-state.json",
        help="JSON state path for signal cooldown suppression.",
    )

    p.add_argument("--kelly-fraction", type=float, default=0.25, help="Fractional Kelly multiplier.")
    p.add_argument("--max-kelly-fraction", type=float, default=0.20, help="Cap on Kelly fraction of bankroll.")
    p.add_argument("--bankroll-usd", type=float, default=1_000.0, help="Paper bankroll for stake sizing.")
    p.add_argument("--min-bet-usd", type=float, default=10.0, help="Minimum paper stake to show.")
    p.add_argument("--max-bet-usd", type=float, default=150.0, help="Maximum paper stake to show.")

    p.add_argument("--log-file", default="logs/event-driven-observe.log", help="Plain text log path.")
    p.add_argument(
        "--signals-file",
        default="logs/event-driven-observe-signals.jsonl",
        help="JSONL path for emitted candidates.",
    )
    p.add_argument(
        "--metrics-file",
        default="logs/event-driven-observe-metrics.jsonl",
        help="JSONL path for per-run metrics summary.",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    logger = Logger(args.log_file)
    logger.info("[startup] polymarket_event_driven_observe (observe-only, no order placement)")

    poll_sec = max(0.0, args.poll_sec)
    if poll_sec <= 0.0:
        run_once(args, logger)
        return 0

    while True:
        try:
            run_once(args, logger)
            time.sleep(poll_sec)
        except KeyboardInterrupt:
            logger.info("[stop] keyboard interrupt")
            return 0
        except Exception as e:
            logger.info(f"[error] {type(e).__name__}: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
