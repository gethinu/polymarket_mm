#!/usr/bin/env python3
"""
Estimate practical monthly return range from event-driven observe artifacts.

Observe-only analytics helper:
  - parses event-driven signal/metrics JSONL
  - collapses repeated same-market signals into episodes
  - projects monthly return under configurable capture ratios
  - outputs deterministic JSON/TXT artifacts under logs/
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

DEFAULT_ASSUMED_BANKROLL_USD = 60.0


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def now_local() -> dt.datetime:
    return dt.datetime.now()


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def as_optional_float(value) -> Optional[float]:
    try:
        n = float(value)
    except Exception:
        return None
    if not math.isfinite(n):
        return None
    return float(n)


def as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def safe_mean(values: List[float], default: float = 0.0) -> float:
    if not values:
        return float(default)
    return float(sum(values)) / float(len(values))


def safe_median(values: List[float], default: float = 0.0) -> float:
    if not values:
        return float(default)
    return float(statistics.median(values))


def percentile(values: List[float], p: float, default: float = 0.0) -> float:
    if not values:
        return float(default)
    xs = sorted(values)
    q = max(0.0, min(1.0, float(p)))
    idx = q * float(len(xs) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(xs[lo])
    w = idx - float(lo)
    return float(xs[lo] * (1.0 - w) + xs[hi] * w)


def parse_float_csv(raw: str) -> List[float]:
    out: List[float] = []
    for tok in str(raw or "").split(","):
        s = tok.strip()
        if not s:
            continue
        out.append(float(s))
    return out


def parse_thresholds_cents(raw: str) -> List[float]:
    xs = parse_float_csv(raw)
    vals = sorted(set(max(0.0, float(x)) for x in xs))
    return vals


def parse_capture_ratios(raw: str) -> List[float]:
    xs = parse_float_csv(raw)
    vals = sorted(set(max(0.0, min(1.0, float(x))) for x in xs))
    return vals


@dataclass(frozen=True)
class SignalRow:
    ts: dt.datetime
    run_id: str
    market_id: str
    event_slug: str
    question: str
    event_class: str
    side: str
    selected_price: float
    edge_cents: float
    confidence: float
    suggested_stake_usd: float
    days_to_end: float


@dataclass(frozen=True)
class MetricRow:
    ts: dt.datetime
    run_id: str
    candidate_count: int
    top_written: int
    suppressed_count: int
    event_count: int
    scanned: int


@dataclass(frozen=True)
class Episode:
    key: str
    market_id: str
    event_slug: str
    question: str
    event_class: str
    side: str
    start_ts: dt.datetime
    end_ts: dt.datetime
    samples: int
    run_count: int
    edge_cents_max: float
    edge_cents_median: float
    confidence_mean: float
    selected_price_median: float
    stake_usd_median: float
    ev_usd_median: float
    dte_median: float


class EpisodeBuilder:
    def __init__(self, row: SignalRow):
        self.key = self._key_of(row)
        self.market_id = row.market_id
        self.event_slug = row.event_slug
        self.question = row.question
        self.event_class = row.event_class
        self.side = row.side
        self.start_ts = row.ts
        self.end_ts = row.ts
        self.rows: List[SignalRow] = [row]
        self.run_ids = {row.run_id} if row.run_id else set()

    @staticmethod
    def _key_of(row: SignalRow) -> str:
        return f"{row.market_id}:{row.side.upper().strip()}"

    def can_merge(self, row: SignalRow, merge_gap_sec: float) -> bool:
        if self._key_of(row) != self.key:
            return False
        gap = (row.ts - self.end_ts).total_seconds()
        return gap >= 0.0 and gap <= float(merge_gap_sec)

    def add(self, row: SignalRow) -> None:
        self.rows.append(row)
        if row.run_id:
            self.run_ids.add(row.run_id)
        if row.ts > self.end_ts:
            self.end_ts = row.ts
        if row.ts < self.start_ts:
            self.start_ts = row.ts

    def build(self) -> Episode:
        edges = [r.edge_cents for r in self.rows]
        conf = [r.confidence for r in self.rows]
        prices = [r.selected_price for r in self.rows if r.selected_price > 0.0]
        stakes = [r.suggested_stake_usd for r in self.rows if r.suggested_stake_usd > 0.0]
        dtes = [r.days_to_end for r in self.rows if r.days_to_end == r.days_to_end]

        ev_vals: List[float] = []
        for r in self.rows:
            if r.selected_price <= 0.0:
                continue
            shares = r.suggested_stake_usd / r.selected_price
            ev_vals.append(shares * (r.edge_cents / 100.0))

        return Episode(
            key=self.key,
            market_id=self.market_id,
            event_slug=self.event_slug,
            question=self.question,
            event_class=self.event_class,
            side=self.side,
            start_ts=self.start_ts,
            end_ts=self.end_ts,
            samples=len(self.rows),
            run_count=len(self.run_ids),
            edge_cents_max=max(edges) if edges else 0.0,
            edge_cents_median=safe_median(edges, 0.0),
            confidence_mean=safe_mean(conf, 0.0),
            selected_price_median=safe_median(prices, 0.0),
            stake_usd_median=safe_median(stakes, 0.0),
            ev_usd_median=safe_median(ev_vals, 0.0),
            dte_median=safe_median(dtes, float("nan")),
        )


def iter_signals(lines: Iterable[str]) -> Iterable[SignalRow]:
    for line in lines:
        raw = str(line or "").strip()
        if not raw:
            continue
        try:
            o = json.loads(raw)
            ts = parse_ts(str(o.get("ts") or ""))
            yield SignalRow(
                ts=ts,
                run_id=str(o.get("run_id") or "").strip(),
                market_id=str(o.get("market_id") or "").strip(),
                event_slug=str(o.get("event_slug") or "").strip(),
                question=str(o.get("question") or "").strip(),
                event_class=str(o.get("event_class") or "").strip(),
                side=str(o.get("side") or "").strip(),
                selected_price=as_float(o.get("selected_price"), 0.0),
                edge_cents=as_float(o.get("edge_cents"), 0.0),
                confidence=as_float(o.get("confidence"), 0.0),
                suggested_stake_usd=as_float(o.get("suggested_stake_usd"), 0.0),
                days_to_end=as_float(o.get("days_to_end"), float("nan")),
            )
        except Exception:
            continue


def iter_metrics(lines: Iterable[str]) -> Iterable[MetricRow]:
    for line in lines:
        raw = str(line or "").strip()
        if not raw:
            continue
        try:
            o = json.loads(raw)
            ts = parse_ts(str(o.get("ts") or ""))
            yield MetricRow(
                ts=ts,
                run_id=str(o.get("run_id") or "").strip(),
                candidate_count=as_int(o.get("candidate_count"), 0),
                top_written=as_int(o.get("top_written"), 0),
                suppressed_count=as_int(o.get("suppressed_count"), 0),
                event_count=as_int(o.get("event_count"), 0),
                scanned=as_int(o.get("scanned"), 0),
            )
        except Exception:
            continue


def build_episodes(rows: List[SignalRow], merge_gap_sec: float) -> List[Episode]:
    if not rows:
        return []
    rows = sorted(rows, key=lambda x: x.ts)
    active: dict[str, EpisodeBuilder] = {}
    out: List[Episode] = []

    for row in rows:
        key = f"{row.market_id}:{row.side.upper().strip()}"
        builder = active.get(key)
        if builder is None:
            active[key] = EpisodeBuilder(row)
            continue

        if builder.can_merge(row, merge_gap_sec=merge_gap_sec):
            builder.add(row)
            continue

        out.append(builder.build())
        active[key] = EpisodeBuilder(row)

    for builder in active.values():
        out.append(builder.build())

    out.sort(key=lambda x: x.start_ts)
    return out


def project_monthly_return(
    ev_usd_per_trade: float,
    opportunities_per_day: float,
    capture_ratio: float,
    days_per_month: float,
    assumed_bankroll_usd: float,
) -> dict:
    if ev_usd_per_trade <= 0.0 or opportunities_per_day <= 0.0:
        return {
            "monthly_profit_usd": 0.0,
            "monthly_return": 0.0,
            "monthly_return_pct": 0.0,
        }
    c = max(0.0, min(1.0, float(capture_ratio)))
    trades_per_month = max(0.0, float(opportunities_per_day) * float(days_per_month))
    monthly_profit = float(ev_usd_per_trade) * float(trades_per_month) * c
    if assumed_bankroll_usd <= 0.0:
        monthly_return = 0.0
    else:
        monthly_return = monthly_profit / float(assumed_bankroll_usd)
    return {
        "monthly_profit_usd": float(monthly_profit),
        "monthly_return": float(monthly_return),
        "monthly_return_pct": float(monthly_return * 100.0),
    }


def threshold_stats(
    episodes: List[Episode],
    threshold_cents: float,
    capture_ratios: List[float],
    base_capture_ratio: float,
    max_ev_multiple_of_stake: float,
    span_days: float,
    max_assumed_trades_per_day: float,
    days_per_month: float,
    assumed_bankroll_usd: float,
) -> dict:
    hit_rows: List[tuple[Episode, float]] = []
    for e in episodes:
        if e.edge_cents_median < threshold_cents:
            continue
        ev_cap = capped_ev_usd(e.ev_usd_median, e.stake_usd_median, max_ev_multiple_of_stake)
        if ev_cap <= 0.0:
            continue
        hit_rows.append((e, ev_cap))
    hits = [x[0] for x in hit_rows]
    hit_ratio = (float(len(hits)) / float(len(episodes))) if episodes else 0.0
    unique_events = len({(e.event_slug or e.market_id) for e in hits})
    opp_day_raw = (float(len(hits)) / float(span_days)) if span_days > 0.0 else 0.0
    opp_day_cap = min(float(max_assumed_trades_per_day), opp_day_raw)

    ev_vals = [x[1] for x in hit_rows]
    ev_median = safe_median(ev_vals, 0.0)
    ev_mean = safe_mean(ev_vals, 0.0)
    edge_med = safe_median([e.edge_cents_median for e in hits], 0.0)
    conf_mean = safe_mean([e.confidence_mean for e in hits], 0.0)

    scenarios: List[dict] = []
    for c in capture_ratios:
        proj = project_monthly_return(
            ev_usd_per_trade=ev_median,
            opportunities_per_day=opp_day_cap,
            capture_ratio=c,
            days_per_month=days_per_month,
            assumed_bankroll_usd=assumed_bankroll_usd,
        )
        scenarios.append(
            {
                "capture_ratio": float(c),
                "monthly_profit_usd": float(proj["monthly_profit_usd"]),
                "monthly_return": float(proj["monthly_return"]),
                "monthly_return_pct": float(proj["monthly_return_pct"]),
            }
        )

    if scenarios:
        base = min(scenarios, key=lambda x: abs(float(x["capture_ratio"]) - float(base_capture_ratio)))
    else:
        base = {
            "capture_ratio": float(base_capture_ratio),
            "monthly_profit_usd": 0.0,
            "monthly_return": 0.0,
            "monthly_return_pct": 0.0,
        }

    return {
        "threshold_cents": float(threshold_cents),
        "episodes_hit": len(hits),
        "episodes_total": len(episodes),
        "hit_ratio": float(hit_ratio),
        "unique_events": int(unique_events),
        "opportunities_per_day_raw": float(opp_day_raw),
        "opportunities_per_day_capped": float(opp_day_cap),
        "ev_usd_median": float(ev_median),
        "ev_usd_mean": float(ev_mean),
        "edge_cents_median": float(edge_med),
        "confidence_mean": float(conf_mean),
        "scenarios": scenarios,
        "base_scenario": base,
    }


def capped_ev_usd(ev_usd: float, stake_usd: float, max_multiple: float) -> float:
    v = float(ev_usd)
    if not math.isfinite(v):
        return 0.0
    mult = max(0.0, float(max_multiple))
    if mult <= 0.0:
        return v
    stake = max(0.0, float(stake_usd))
    cap = stake * mult
    if cap <= 0.0:
        return 0.0
    if v > 0.0:
        return min(v, cap)
    if v < 0.0:
        return max(v, -cap)
    return 0.0


def choose_threshold(
    stats: List[dict],
    min_opportunities_per_day: float,
    min_unique_events: int,
    min_hit_ratio: float,
) -> tuple[Optional[dict], bool]:
    if not stats:
        return None, False
    qualified = [
        s
        for s in stats
        if float(s.get("opportunities_per_day_capped") or 0.0) >= float(min_opportunities_per_day)
        and int(s.get("unique_events") or 0) >= int(min_unique_events)
        and float(s.get("hit_ratio") or 0.0) >= float(min_hit_ratio)
    ]
    pool = qualified if qualified else stats
    selected = max(
        pool,
        key=lambda s: (
            float((s.get("base_scenario") or {}).get("monthly_return") or 0.0),
            float(s.get("opportunities_per_day_capped") or 0.0),
            float(s.get("threshold_cents") or 0.0),
        ),
    )
    return selected, bool(qualified)


def summarize_top_episodes(episodes: List[Episode], top_n: int = 8) -> List[dict]:
    items = sorted(
        episodes,
        key=lambda e: (e.edge_cents_median, e.ev_usd_median, e.samples),
        reverse=True,
    )
    out: List[dict] = []
    for e in items[: max(1, int(top_n))]:
        out.append(
            {
                "start_local": e.start_ts.strftime("%Y-%m-%d %H:%M:%S"),
                "end_local": e.end_ts.strftime("%Y-%m-%d %H:%M:%S"),
                "market_id": e.market_id,
                "event_slug": e.event_slug,
                "event_class": e.event_class,
                "side": e.side,
                "samples": e.samples,
                "run_count": e.run_count,
                "edge_cents_median": e.edge_cents_median,
                "edge_cents_max": e.edge_cents_max,
                "ev_usd_median": e.ev_usd_median,
                "confidence_mean": e.confidence_mean,
                "question": e.question,
            }
        )
    return out


def class_counts(episodes: List[Episode]) -> List[dict]:
    buckets: dict[str, int] = {}
    for e in episodes:
        k = e.event_class or "unclassified"
        buckets[k] = buckets.get(k, 0) + 1
    return [{"event_class": k, "episodes": v} for k, v in sorted(buckets.items(), key=lambda x: (-x[1], x[0]))]


def build_text_report(result: dict) -> str:
    window = result.get("window") or {}
    summary = result.get("summary") or {}
    decision = result.get("decision") or {}
    settings = result.get("settings") or {}
    selected = result.get("selected_threshold") or {}
    base = selected.get("base_scenario") if isinstance(selected, dict) else None
    metrics = summary.get("metrics_window") if isinstance(summary.get("metrics_window"), dict) else {}

    lines: List[str] = []
    lines.append("Event-Driven Profit Window")
    lines.append(
        f"Observed: {window.get('start_local')} -> {window.get('end_local')} | "
        f"span_hours={float(window.get('span_hours') or 0.0):.2f}"
    )
    lines.append(
        f"Signals={int(summary.get('signals') or 0)} | episodes={int(summary.get('episodes') or 0)} | "
        f"dedupe_ratio={float(summary.get('signal_to_episode_ratio') or 0.0):.2f}"
    )
    lines.append(
        f"Events={int(summary.get('unique_events') or 0)} | markets={int(summary.get('unique_markets') or 0)} | "
        f"positive_ev_ratio={float(summary.get('positive_ev_ratio') or 0.0) * 100.0:.1f}%"
    )
    lines.append(
        f"EV/trade median=${float(summary.get('ev_usd_median') or 0.0):.2f} "
        f"p90=${float(summary.get('ev_usd_p90') or 0.0):.2f} | "
        f"edge median={float(summary.get('edge_cents_median') or 0.0):.2f}c"
    )
    lines.append(
        f"Assumed bankroll=${float(settings.get('assumed_bankroll_usd') or 0.0):.2f} "
        f"(source={str(settings.get('assumed_bankroll_source') or '-')})"
    )

    if metrics:
        lines.append(
            f"Runs={int(metrics.get('runs') or 0)} | candidates={int(metrics.get('candidates_sum') or 0)} | "
            f"written={int(metrics.get('top_written_sum') or 0)} | suppressed={int(metrics.get('suppressed_sum') or 0)} "
            f"(rate={float(metrics.get('suppressed_rate') or 0.0) * 100.0:.1f}%)"
        )

    if selected:
        lines.append("Selected threshold:")
        lines.append(
            f"  >= {float(selected.get('threshold_cents') or 0.0):.2f}c | "
            f"opp/day(capped)={float(selected.get('opportunities_per_day_capped') or 0.0):.2f} | "
            f"unique_events={int(selected.get('unique_events') or 0)} | "
            f"hit_ratio={float(selected.get('hit_ratio') or 0.0) * 100.0:.1f}%"
        )
        if isinstance(base, dict):
            lines.append(
                f"  base capture={float(base.get('capture_ratio') or 0.0) * 100.0:.0f}% "
                f"=> projected monthly={float(base.get('monthly_return_pct') or 0.0):+.2f}%"
            )
        scenarios = selected.get("scenarios") if isinstance(selected, dict) else []
        if isinstance(scenarios, list) and scenarios:
            lines.append("  scenarios:")
            for x in scenarios:
                lines.append(
                    f"    capture {float(x.get('capture_ratio') or 0.0) * 100.0:.0f}% "
                    f"=> monthly {float(x.get('monthly_return_pct') or 0.0):+.2f}%"
                )

    lines.append(
        f"Decision: {decision.get('decision', 'NO_GO')} | "
        f"target_monthly={float((decision.get('target_monthly_return') or 0.0) * 100.0):.2f}%"
    )
    reasons = decision.get("reasons") if isinstance(decision.get("reasons"), list) else []
    for r in reasons:
        lines.append(f"  - {str(r)}")
    return "\n".join(lines)


def resolve_path(root: Path, raw: str, default_rel: str) -> Path:
    p = Path(str(raw or "").strip()) if str(raw or "").strip() else Path(default_rel)
    if p.is_absolute():
        return p
    return root / p


def extract_initial_bankroll_from_strategy_md(path: Path) -> Optional[float]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    in_policy = False
    for line in lines:
        if line.startswith("## "):
            title = line[3:].strip().lower()
            if in_policy and title != "bankroll policy":
                break
            in_policy = title == "bankroll policy"
            continue
        if not in_policy:
            continue
        s = line.strip()
        if not s.startswith("- "):
            continue
        bullet = s[2:].strip()
        if not bullet.lower().startswith("initial bankroll"):
            continue
        m = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", bullet)
        if not m:
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)", bullet)
        if not m:
            continue
        n = as_optional_float(m.group(1))
        if n is not None and n > 0:
            return float(n)
    return None


def extract_initial_bankroll_from_snapshot(path: Path) -> Optional[float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    bp = payload.get("bankroll_policy") if isinstance(payload.get("bankroll_policy"), dict) else {}
    n = as_optional_float(bp.get("initial_bankroll_usd"))
    if n is not None and n > 0:
        return float(n)
    return None


def resolve_assumed_bankroll_usd(root: Path, cli_value: Optional[float]) -> tuple[float, str]:
    n_cli = as_optional_float(cli_value)
    if n_cli is not None:
        return float(n_cli), "cli_arg"

    snapshot_path = root / "logs" / "strategy_register_latest.json"
    n_snapshot = extract_initial_bankroll_from_snapshot(snapshot_path)
    if n_snapshot is not None:
        return float(n_snapshot), "logs/strategy_register_latest.json:bankroll_policy.initial_bankroll_usd"

    strategy_path = root / "docs" / "llm" / "STRATEGY.md"
    n_policy = extract_initial_bankroll_from_strategy_md(strategy_path)
    if n_policy is not None:
        return float(n_policy), "docs/llm/STRATEGY.md:Bankroll Policy.initial_bankroll"

    return float(DEFAULT_ASSUMED_BANKROLL_USD), f"hardcoded_default:{DEFAULT_ASSUMED_BANKROLL_USD:.2f}"


def main() -> int:
    p = argparse.ArgumentParser(description="Estimate event-driven monthly return range from observe artifacts (observe-only).")
    p.add_argument("--signals-file", default="logs/event-driven-observe-signals.jsonl")
    p.add_argument("--metrics-file", default="logs/event-driven-observe-metrics.jsonl")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--since", default="", help='Local time "YYYY-MM-DD HH:MM:SS" (overrides --hours)')
    p.add_argument("--until", default="", help='Local time "YYYY-MM-DD HH:MM:SS" (default now)')
    p.add_argument("--episode-merge-gap-sec", type=float, default=7200.0)
    p.add_argument("--thresholds-cents", default="0.8,1,2,3,5")
    p.add_argument("--capture-ratios", default="0.25,0.35,0.50")
    p.add_argument("--base-capture-ratio", type=float, default=0.35)
    p.add_argument("--max-ev-multiple-of-stake", type=float, default=0.35)
    p.add_argument("--days-per-month", type=float, default=30.0)
    p.add_argument(
        "--assumed-bankroll-usd",
        type=float,
        default=None,
        help="When omitted, resolve from strategy bankroll policy (fallback: 100).",
    )
    p.add_argument("--max-assumed-trades-per-day", type=float, default=6.0)
    p.add_argument("--target-monthly-return-pct", type=float, default=12.0)
    p.add_argument("--min-window-hours", type=float, default=12.0)
    p.add_argument("--min-runs", type=int, default=30)
    p.add_argument("--min-episodes", type=int, default=8)
    p.add_argument("--min-unique-events", type=int, default=4)
    p.add_argument("--min-opportunities-per-day", type=float, default=1.0)
    p.add_argument("--min-hit-ratio-pct", type=float, default=15.0)
    p.add_argument("--min-positive-ev-ratio-pct", type=float, default=60.0)
    p.add_argument("--out-json", default="logs/event_driven_profit_window_latest.json")
    p.add_argument("--out-txt", default="logs/event_driven_profit_window_latest.txt")
    p.add_argument("--pretty", action="store_true")
    p.add_argument("--fail-on-no-go", action="store_true")
    args = p.parse_args()

    root = Path(__file__).resolve().parents[1]
    signals_file = resolve_path(root, args.signals_file, "logs/event-driven-observe-signals.jsonl")
    metrics_file = resolve_path(root, args.metrics_file, "logs/event-driven-observe-metrics.jsonl")
    out_json = resolve_path(root, args.out_json, "logs/event_driven_profit_window_latest.json")
    out_txt = resolve_path(root, args.out_txt, "logs/event_driven_profit_window_latest.txt")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_txt.parent.mkdir(parents=True, exist_ok=True)

    until = parse_ts(args.until) if str(args.until).strip() else now_local()
    since = parse_ts(args.since) if str(args.since).strip() else (until - dt.timedelta(hours=float(args.hours)))

    signals: List[SignalRow] = []
    if signals_file.exists():
        lines = signals_file.read_text(encoding="utf-8", errors="replace").splitlines()
        signals = [r for r in iter_signals(lines) if (since <= r.ts <= until)]

    metrics: List[MetricRow] = []
    if metrics_file.exists():
        lines = metrics_file.read_text(encoding="utf-8", errors="replace").splitlines()
        metrics = [r for r in iter_metrics(lines) if (since <= r.ts <= until)]

    if not signals and not metrics:
        print(f"no rows in window; signals={signals_file} metrics={metrics_file}")
        return 2

    episodes = build_episodes(signals, merge_gap_sec=max(0.0, float(args.episode_merge_gap_sec)))

    if signals:
        signal_start = min(r.ts for r in signals)
        signal_end = max(r.ts for r in signals)
    else:
        signal_start = since
        signal_end = until

    span_sec = max(0.0, (until - since).total_seconds())
    span_hours = span_sec / 3600.0
    span_days = span_sec / 86400.0

    thresholds_cents = parse_thresholds_cents(args.thresholds_cents)
    if not thresholds_cents:
        thresholds_cents = [0.8, 1.0, 2.0, 3.0, 5.0]
    capture_ratios = parse_capture_ratios(args.capture_ratios)
    if not capture_ratios:
        capture_ratios = [0.25, 0.35, 0.5]
    assumed_bankroll_usd, assumed_bankroll_source = resolve_assumed_bankroll_usd(root, args.assumed_bankroll_usd)

    tstats = [
        threshold_stats(
            episodes=episodes,
            threshold_cents=t,
            capture_ratios=capture_ratios,
            base_capture_ratio=float(args.base_capture_ratio),
            max_ev_multiple_of_stake=float(args.max_ev_multiple_of_stake),
            span_days=span_days,
            max_assumed_trades_per_day=float(args.max_assumed_trades_per_day),
            days_per_month=float(args.days_per_month),
            assumed_bankroll_usd=float(assumed_bankroll_usd),
        )
        for t in thresholds_cents
    ]

    selected, selected_qualified = choose_threshold(
        stats=tstats,
        min_opportunities_per_day=float(args.min_opportunities_per_day),
        min_unique_events=int(args.min_unique_events),
        min_hit_ratio=float(args.min_hit_ratio_pct) / 100.0,
    )

    ev_vals = [
        capped_ev_usd(e.ev_usd_median, e.stake_usd_median, float(args.max_ev_multiple_of_stake))
        for e in episodes
        if e.ev_usd_median == e.ev_usd_median
    ]
    edge_vals = [e.edge_cents_median for e in episodes]
    pos_ev_count = len([x for x in ev_vals if x > 0.0])
    pos_ev_ratio = (float(pos_ev_count) / float(len(episodes))) if episodes else 0.0
    unique_events = len({(e.event_slug or e.market_id) for e in episodes})
    unique_markets = len({e.market_id for e in episodes if e.market_id})

    candidates_sum = sum(m.candidate_count for m in metrics)
    top_written_sum = sum(m.top_written for m in metrics)
    suppressed_sum = sum(m.suppressed_count for m in metrics)
    suppress_denom = float(top_written_sum + suppressed_sum)
    suppressed_rate = (float(suppressed_sum) / suppress_denom) if suppress_denom > 0.0 else 0.0

    target_monthly = float(args.target_monthly_return_pct) / 100.0
    selected_base_monthly = 0.0
    reasons: List[str] = []
    hard_ok = True

    if span_hours < float(args.min_window_hours):
        hard_ok = False
        reasons.append(
            f"span_hours {span_hours:.2f} < min_window_hours {float(args.min_window_hours):.2f}"
        )
    else:
        reasons.append(
            f"span_hours {span_hours:.2f} >= min_window_hours {float(args.min_window_hours):.2f}"
        )

    if len(metrics) < int(args.min_runs):
        hard_ok = False
        reasons.append(f"runs {len(metrics)} < min_runs {int(args.min_runs)}")
    else:
        reasons.append(f"runs {len(metrics)} >= min_runs {int(args.min_runs)}")

    if len(episodes) < int(args.min_episodes):
        hard_ok = False
        reasons.append(f"episodes {len(episodes)} < min_episodes {int(args.min_episodes)}")
    else:
        reasons.append(f"episodes {len(episodes)} >= min_episodes {int(args.min_episodes)}")

    if unique_events < int(args.min_unique_events):
        hard_ok = False
        reasons.append(f"unique_events {unique_events} < min_unique_events {int(args.min_unique_events)}")
    else:
        reasons.append(f"unique_events {unique_events} >= min_unique_events {int(args.min_unique_events)}")

    min_pos_ev_ratio = float(args.min_positive_ev_ratio_pct) / 100.0
    if pos_ev_ratio < min_pos_ev_ratio:
        hard_ok = False
        reasons.append(
            f"positive_ev_ratio {pos_ev_ratio * 100.0:.1f}% < min_positive_ev_ratio_pct {float(args.min_positive_ev_ratio_pct):.1f}%"
        )
    else:
        reasons.append(
            f"positive_ev_ratio {pos_ev_ratio * 100.0:.1f}% >= min_positive_ev_ratio_pct {float(args.min_positive_ev_ratio_pct):.1f}%"
        )

    if not selected:
        hard_ok = False
        reasons.append("no threshold stats available")
    else:
        base = selected.get("base_scenario") if isinstance(selected.get("base_scenario"), dict) else {}
        selected_base_monthly = float(base.get("monthly_return") or 0.0)
        if not selected_qualified:
            hard_ok = False
            reasons.append("selected threshold failed opportunity/event/hit-ratio qualifiers")
        else:
            reasons.append("selected threshold passed opportunity/event/hit-ratio qualifiers")
        if selected_base_monthly < target_monthly:
            hard_ok = False
            reasons.append(
                f"projected_monthly {selected_base_monthly * 100.0:.2f}% < target {target_monthly * 100.0:.2f}%"
            )
        else:
            reasons.append(
                f"projected_monthly {selected_base_monthly * 100.0:.2f}% >= target {target_monthly * 100.0:.2f}%"
            )

    decision = "GO" if hard_ok else "NO_GO"

    result = {
        "generated_utc": now_utc_iso(),
        "meta": {
            "observe_only": True,
            "source": "scripts/report_event_driven_profit_window.py",
            "signals_file": str(signals_file),
            "metrics_file": str(metrics_file),
        },
        "window": {
            "since_local": since.strftime("%Y-%m-%d %H:%M:%S"),
            "until_local": until.strftime("%Y-%m-%d %H:%M:%S"),
            "start_local": signal_start.strftime("%Y-%m-%d %H:%M:%S") if signals else None,
            "end_local": signal_end.strftime("%Y-%m-%d %H:%M:%S") if signals else None,
            "span_hours": float(span_hours),
            "span_days": float(span_days),
        },
        "settings": {
            "episode_merge_gap_sec": float(args.episode_merge_gap_sec),
            "thresholds_cents": thresholds_cents,
            "capture_ratios": capture_ratios,
            "base_capture_ratio": float(args.base_capture_ratio),
            "max_ev_multiple_of_stake": float(args.max_ev_multiple_of_stake),
            "days_per_month": float(args.days_per_month),
            "assumed_bankroll_usd": float(assumed_bankroll_usd),
            "assumed_bankroll_source": assumed_bankroll_source,
            "max_assumed_trades_per_day": float(args.max_assumed_trades_per_day),
            "target_monthly_return_pct": float(args.target_monthly_return_pct),
            "min_window_hours": float(args.min_window_hours),
            "min_runs": int(args.min_runs),
            "min_episodes": int(args.min_episodes),
            "min_unique_events": int(args.min_unique_events),
            "min_opportunities_per_day": float(args.min_opportunities_per_day),
            "min_hit_ratio_pct": float(args.min_hit_ratio_pct),
            "min_positive_ev_ratio_pct": float(args.min_positive_ev_ratio_pct),
        },
        "summary": {
            "signals": len(signals),
            "episodes": len(episodes),
            "signal_to_episode_ratio": (float(len(signals)) / float(len(episodes))) if episodes else 0.0,
            "unique_events": unique_events,
            "unique_markets": unique_markets,
            "positive_ev_episodes": pos_ev_count,
            "positive_ev_ratio": float(pos_ev_ratio),
            "ev_usd_median": safe_median(ev_vals, 0.0),
            "ev_usd_p90": percentile(ev_vals, 0.90, 0.0),
            "edge_cents_median": safe_median(edge_vals, 0.0),
            "edge_cents_p90": percentile(edge_vals, 0.90, 0.0),
            "class_counts": class_counts(episodes),
            "metrics_window": {
                "runs": len(metrics),
                "scanned_sum": sum(m.scanned for m in metrics),
                "event_count_sum": sum(m.event_count for m in metrics),
                "candidates_sum": candidates_sum,
                "top_written_sum": top_written_sum,
                "suppressed_sum": suppressed_sum,
                "suppressed_rate": float(suppressed_rate),
            },
        },
        "threshold_stats": tstats,
        "selected_threshold": selected,
        "selected_threshold_qualified": bool(selected_qualified),
        "top_episodes": summarize_top_episodes(episodes),
        "decision": {
            "decision": decision,
            "target_monthly_return": float(target_monthly),
            "projected_monthly_return": float(selected_base_monthly),
            "reasons": reasons,
        },
        "artifacts": {
            "out_json": str(out_json),
            "out_txt": str(out_txt),
        },
    }

    report_txt = build_text_report(result)
    out_txt.write_text(report_txt + "\n", encoding="utf-8")
    if args.pretty:
        out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        out_json.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(
        f"[event-driven-profit] assumed_bankroll=${assumed_bankroll_usd:.2f} "
        f"source={assumed_bankroll_source}"
    )
    print(f"[event-driven-profit] decision={decision} projected_monthly={selected_base_monthly * 100.0:+.2f}%")
    print(f"[event-driven-profit] wrote {out_json}")
    print(f"[event-driven-profit] wrote {out_txt}")

    if args.fail_on_no_go and decision != "GO":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
