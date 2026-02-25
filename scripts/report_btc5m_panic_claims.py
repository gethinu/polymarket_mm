#!/usr/bin/env python3
"""
Validate BTC short-window panic-pricing frequency claims (observe-only).

This script scans closed BTC up/down windows and measures how often the
eventual winner traded at or below configured price thresholds.

Data sources:
- Gamma API event slug endpoint (market metadata / resolved outcomes)
- Polymarket Data API trades endpoint (market-wide trade history)

No orders are placed.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GAMMA_API_BASE = "https://gamma-api.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"
USER_AGENT = "btc-panic-claims-report/1.0"
REPO_ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = REPO_ROOT / "logs"


def as_float(value, default: float = math.nan) -> float:
    try:
        return float(value)
    except Exception:
        return default


def as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def parse_json_string_field(value) -> List[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            obj = json.loads(value)
            if isinstance(obj, list):
                return [str(v) for v in obj]
        except Exception:
            return []
    return []


def parse_json_float_field(value) -> List[float]:
    out: List[float] = []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return out
    if not isinstance(value, list):
        return out
    for x in value:
        f = as_float(x, math.nan)
        if math.isfinite(f):
            out.append(float(f))
    return out


def parse_local_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def fmt_utc(ts: int) -> str:
    return dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _http_get_json(url: str, timeout_sec: float = 20.0, retries: int = 3) -> Optional[object]:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        method="GET",
    )
    for i in range(max(1, retries)):
        try:
            with urlopen(req, timeout=timeout_sec) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except (HTTPError, URLError, TimeoutError, ValueError):
            if i >= retries - 1:
                return None
            time.sleep(0.25 * (i + 1))
    return None


def fetch_gamma_event_by_slug(slug: str) -> Optional[dict]:
    obj = _http_get_json(f"{GAMMA_API_BASE}/events/slug/{slug}", timeout_sec=20.0, retries=3)
    return obj if isinstance(obj, dict) else None


def fetch_market_trades(
    condition_id: str,
    page_size: int,
    max_trades: int,
    sleep_sec: float,
) -> List[dict]:
    rows: List[dict] = []
    offset = 0
    page_size = max(1, min(int(page_size), 500))
    max_trades = max(1, int(max_trades))
    while len(rows) < max_trades:
        batch = min(page_size, max_trades - len(rows))
        q = urlencode(
            {
                "market": condition_id,
                "limit": str(batch),
                "offset": str(offset),
            }
        )
        url = f"{DATA_API_BASE}/trades?{q}"
        obj = _http_get_json(url, timeout_sec=20.0, retries=3)
        if not isinstance(obj, list) or not obj:
            break
        rows.extend(obj)
        if len(obj) < batch:
            break
        offset += batch
        if sleep_sec > 0:
            time.sleep(max(0.0, float(sleep_sec)))
    return rows


def match_outcome_label(target: str, labels: Iterable[str]) -> Optional[str]:
    t = (target or "").strip().lower()
    if not t:
        return None
    for lbl in labels:
        if (lbl or "").strip().lower() == t:
            return lbl
    return None


def percentile(xs: List[float], p: float) -> float:
    if not xs:
        return math.nan
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    k = max(0.0, min(1.0, float(p))) * float(len(ys) - 1)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return ys[lo]
    w = k - float(lo)
    return ys[lo] + (ys[hi] - ys[lo]) * w


def parse_thresholds(raw: str) -> List[float]:
    out: List[float] = []
    for part in (raw or "").split(","):
        s = part.strip()
        if not s:
            continue
        v = as_float(s, math.nan)
        if not math.isfinite(v):
            continue
        if v > 1.0:
            v = v / 100.0
        if v <= 0 or v >= 1:
            continue
        out.append(float(v))
    return sorted(set(out))


def _resolve_output_path(raw: str, default_name: str) -> Path:
    if not raw:
        return LOGS_DIR / default_name
    p = Path(raw)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return LOGS_DIR / p.name
    return REPO_ROOT / p


def default_output_paths(window_minutes: int) -> Tuple[str, str]:
    wm = int(window_minutes)
    if wm == 15:
        return "btc15m-panic-claims-latest.json", "btc15m-panic-claims-markets-latest.csv"
    return "btc5m-panic-claims-latest.json", "btc5m-panic-claims-markets-latest.csv"


@dataclass(frozen=True)
class MarketResult:
    slug: str
    start_ts: int
    end_date: str
    condition_id: str
    market_id: str
    title: str
    winner_outcome: str
    winner_price: float
    loser_outcome: str
    winner_min_trade: Optional[float]
    loser_min_trade: Optional[float]
    winner_trade_count: int
    loser_trade_count: int
    total_valid_trades: int
    fetched_trade_rows: int


def choose_best_closed_market(event_obj: dict) -> Optional[dict]:
    rows = event_obj.get("markets")
    if not isinstance(rows, list):
        return None
    candidates: List[Tuple[float, dict]] = []
    for m in rows:
        if not isinstance(m, dict):
            continue
        if not bool(m.get("closed", False)):
            continue
        condition_id = str(m.get("conditionId") or "").strip()
        if not condition_id.startswith("0x"):
            continue
        token_ids = parse_json_string_field(m.get("clobTokenIds"))
        if len(token_ids) < 2:
            continue
        liq = as_float(m.get("liquidityNum", m.get("liquidity", 0.0)), 0.0)
        candidates.append((liq, m))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def resolve_winner_and_loser(market: dict) -> Optional[Tuple[int, int, str, str, float]]:
    outcomes = parse_json_string_field(market.get("outcomes"))
    prices = parse_json_float_field(market.get("outcomePrices"))
    if len(outcomes) < 2 or len(prices) < 2:
        return None
    n = min(len(outcomes), len(prices))
    pairs = [(i, outcomes[i], prices[i]) for i in range(n)]
    pairs = [x for x in pairs if math.isfinite(x[2])]
    if len(pairs) < 2:
        return None
    pairs.sort(key=lambda x: x[2], reverse=True)
    win_i, win_outcome, win_price = pairs[0]
    lose_i, lose_outcome, _lose_price = pairs[-1]
    return win_i, lose_i, str(win_outcome), str(lose_outcome), float(win_price)


def build_window_starts(since_ts: int, until_ts: int, window_minutes: int, max_markets: int) -> List[int]:
    sec = int(window_minutes) * 60
    if sec <= 0:
        return []
    start = (int(since_ts) // sec) * sec
    end = (int(until_ts) // sec) * sec
    if end < start:
        start, end = end, start
    vals = list(range(start, end + 1, sec))
    vals.sort(reverse=True)  # newest first
    if max_markets > 0:
        return vals[: int(max_markets)]
    return vals


def summarize(results: List[MarketResult], thresholds: List[float]) -> dict:
    analyzed = len(results)
    winner_prices = [r.winner_min_trade for r in results if r.winner_min_trade is not None]
    winner_prices = [float(x) for x in winner_prices if x is not None]
    markets_with_winner_trade = len(winner_prices)
    markets_no_winner_trade = analyzed - markets_with_winner_trade
    markets_no_trades = sum(1 for r in results if r.total_valid_trades <= 0)

    threshold_rows = []
    for thr in thresholds:
        hits = sum(1 for r in results if r.winner_min_trade is not None and float(r.winner_min_trade) <= float(thr))
        pct_over_winner = (hits / markets_with_winner_trade) if markets_with_winner_trade > 0 else 0.0
        pct_over_all = (hits / analyzed) if analyzed > 0 else 0.0
        threshold_rows.append(
            {
                "threshold": float(thr),
                "hits": int(hits),
                "pct_over_winner_trade_markets": float(pct_over_winner),
                "pct_over_all_markets": float(pct_over_all),
            }
        )

    stats = {
        "mean": float(sum(winner_prices) / len(winner_prices)) if winner_prices else None,
        "p10": float(percentile(winner_prices, 0.10)) if winner_prices else None,
        "p25": float(percentile(winner_prices, 0.25)) if winner_prices else None,
        "p50": float(percentile(winner_prices, 0.50)) if winner_prices else None,
        "p75": float(percentile(winner_prices, 0.75)) if winner_prices else None,
        "p90": float(percentile(winner_prices, 0.90)) if winner_prices else None,
        "min": float(min(winner_prices)) if winner_prices else None,
        "max": float(max(winner_prices)) if winner_prices else None,
    }

    return {
        "markets_analyzed": analyzed,
        "markets_with_winner_trade": markets_with_winner_trade,
        "markets_no_winner_trade": markets_no_winner_trade,
        "markets_no_valid_trades": markets_no_trades,
        "winner_min_trade_price_stats": stats,
        "threshold_hits": threshold_rows,
    }


def build_text_report(meta: dict, summary: dict) -> str:
    lines: List[str] = []
    lines.append(
        f"Window: {meta['since_local']} -> {meta['until_local']} (local) | "
        f"window={meta['window_minutes']}m"
    )
    lines.append(
        f"Windows scanned: {meta['windows_scanned']} | events_found: {meta['events_found']} | "
        f"closed_markets_analyzed: {summary['markets_analyzed']}"
    )
    lines.append(
        f"Winner-trade coverage: {summary['markets_with_winner_trade']} / {summary['markets_analyzed']} "
        f"(no_winner_trade={summary['markets_no_winner_trade']}, no_valid_trades={summary['markets_no_valid_trades']})"
    )

    s = summary.get("winner_min_trade_price_stats") or {}
    if s.get("p50") is not None:
        lines.append(
            "Winner min traded-price stats: "
            f"mean={s['mean']*100:.2f}c p10={s['p10']*100:.2f}c p50={s['p50']*100:.2f}c "
            f"p90={s['p90']*100:.2f}c min={s['min']*100:.2f}c max={s['max']*100:.2f}c"
        )

    lines.append("Threshold hits (winner-side min traded price):")
    for row in summary.get("threshold_hits") or []:
        lines.append(
            f"  <= {row['threshold']*100:.2f}c : {row['hits']} "
            f"({row['pct_over_winner_trade_markets']*100:.2f}% of winner-trade markets, "
            f"{row['pct_over_all_markets']*100:.2f}% of all markets)"
        )
    return "\n".join(lines)


def write_csv(path: Path, rows: List[MarketResult], thresholds: List[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "slug",
        "start_ts",
        "start_utc",
        "end_date",
        "condition_id",
        "market_id",
        "title",
        "winner_outcome",
        "winner_price",
        "loser_outcome",
        "winner_min_trade",
        "loser_min_trade",
        "winner_trade_count",
        "loser_trade_count",
        "total_valid_trades",
        "fetched_trade_rows",
    ]
    thr_cols = [f"hit_le_{int(round(t * 10000)):04d}bp" for t in thresholds]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames + thr_cols)
        w.writeheader()
        for r in rows:
            d = asdict(r)
            d["start_utc"] = fmt_utc(r.start_ts)
            for t, c in zip(thresholds, thr_cols):
                if r.winner_min_trade is None:
                    d[c] = ""
                else:
                    d[c] = "1" if float(r.winner_min_trade) <= float(t) else "0"
            w.writerow(d)


def parse_args():
    p = argparse.ArgumentParser(description="Validate BTC short-window panic-pricing frequency claims (observe-only)")
    p.add_argument("--window-minutes", type=int, choices=[5, 15], default=5, help="Window size in minutes")
    p.add_argument("--hours", type=float, default=24.0, help="Lookback hours when --since is not provided")
    p.add_argument("--since", default="", help='Start local time "YYYY-MM-DD HH:MM:SS"')
    p.add_argument("--until", default="", help='End local time "YYYY-MM-DD HH:MM:SS" (default: now)')
    p.add_argument("--max-markets", type=int, default=0, help="Maximum number of window slugs to scan (0=all)")
    p.add_argument(
        "--thresholds",
        default="0.05,0.10,0.15",
        help="Comma-separated price thresholds (probability units; >1 treated as cents)",
    )
    p.add_argument("--page-size", type=int, default=500, help="Data API trades page size (max 500)")
    p.add_argument("--max-trades-per-market", type=int, default=5000, help="Cap fetched trades per market")
    p.add_argument("--sleep-sec", type=float, default=0.0, help="Optional sleep between market trade pages")
    p.add_argument("--out-json", default="", help="Output summary JSON path (simple filename -> logs/)")
    p.add_argument("--out-csv", default="", help="Output per-market CSV path (simple filename -> logs/)")
    p.add_argument("--pretty", action="store_true", help="Pretty-print output JSON")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    thresholds = parse_thresholds(args.thresholds)
    if not thresholds:
        print("No valid thresholds parsed from --thresholds")
        return 2

    now_local = dt.datetime.now()
    until_local = parse_local_ts(args.until) if args.until else now_local
    since_local = parse_local_ts(args.since) if args.since else (until_local - dt.timedelta(hours=float(args.hours)))
    if since_local > until_local:
        since_local, until_local = until_local, since_local

    since_ts = int(since_local.timestamp())
    until_ts = int(until_local.timestamp())

    default_json, default_csv = default_output_paths(int(args.window_minutes))
    out_json = _resolve_output_path(args.out_json, default_json)
    out_csv = _resolve_output_path(args.out_csv, default_csv)

    starts = build_window_starts(
        since_ts=since_ts,
        until_ts=until_ts,
        window_minutes=int(args.window_minutes),
        max_markets=int(args.max_markets),
    )

    results: List[MarketResult] = []
    events_found = 0

    for start_ts in starts:
        slug = f"btc-updown-{int(args.window_minutes)}m-{int(start_ts)}"
        event_obj = fetch_gamma_event_by_slug(slug)
        if not isinstance(event_obj, dict):
            continue
        events_found += 1
        market = choose_best_closed_market(event_obj)
        if not isinstance(market, dict):
            continue

        resolved = resolve_winner_and_loser(market)
        if resolved is None:
            continue
        win_i, lose_i, win_outcome, lose_outcome, win_price = resolved

        condition_id = str(market.get("conditionId") or "").strip()
        if not condition_id.startswith("0x"):
            continue

        trade_rows = fetch_market_trades(
            condition_id=condition_id,
            page_size=int(args.page_size),
            max_trades=int(args.max_trades_per_market),
            sleep_sec=float(args.sleep_sec),
        )

        winner_min = math.nan
        loser_min = math.nan
        winner_count = 0
        loser_count = 0
        total_valid = 0

        for row in trade_rows:
            if not isinstance(row, dict):
                continue
            price = as_float(row.get("price"), math.nan)
            if not math.isfinite(price) or price <= 0:
                continue
            total_valid += 1
            row_outcome = str(row.get("outcome") or "").strip()
            row_idx = as_int(row.get("outcomeIndex"), -1)

            is_winner = (row_idx == win_i) or ((row_outcome or "").lower() == win_outcome.lower())
            is_loser = (row_idx == lose_i) or ((row_outcome or "").lower() == lose_outcome.lower())

            if is_winner:
                winner_count += 1
                if not math.isfinite(winner_min) or price < winner_min:
                    winner_min = float(price)
            if is_loser:
                loser_count += 1
                if not math.isfinite(loser_min) or price < loser_min:
                    loser_min = float(price)

        results.append(
            MarketResult(
                slug=slug,
                start_ts=int(start_ts),
                end_date=str(market.get("endDate") or ""),
                condition_id=condition_id,
                market_id=str(market.get("id") or ""),
                title=str(market.get("question") or event_obj.get("title") or slug),
                winner_outcome=win_outcome,
                winner_price=float(win_price),
                loser_outcome=lose_outcome,
                winner_min_trade=float(winner_min) if math.isfinite(winner_min) else None,
                loser_min_trade=float(loser_min) if math.isfinite(loser_min) else None,
                winner_trade_count=int(winner_count),
                loser_trade_count=int(loser_count),
                total_valid_trades=int(total_valid),
                fetched_trade_rows=int(len(trade_rows)),
            )
        )

    results.sort(key=lambda x: x.start_ts)
    summary = summarize(results, thresholds=thresholds)
    meta = {
        "generated_at_local": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_minutes": int(args.window_minutes),
        "since_local": since_local.strftime("%Y-%m-%d %H:%M:%S"),
        "until_local": until_local.strftime("%Y-%m-%d %H:%M:%S"),
        "since_ts": int(since_ts),
        "until_ts": int(until_ts),
        "windows_scanned": int(len(starts)),
        "events_found": int(events_found),
        "thresholds": [float(x) for x in thresholds],
        "page_size": int(args.page_size),
        "max_trades_per_market": int(args.max_trades_per_market),
    }
    payload = {
        "meta": meta,
        "summary": summary,
        "markets": [asdict(r) for r in results],
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8", newline="\n") as f:
        if args.pretty:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        else:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")

    write_csv(out_csv, results, thresholds=thresholds)

    report = build_text_report(meta, summary)
    print(report)
    print()
    print(f"Saved JSON: {out_json}")
    print(f"Saved CSV:  {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
