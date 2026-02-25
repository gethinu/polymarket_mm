#!/usr/bin/env python3
"""
Calibrate high-probability near-expiry pricing for hourly crypto up/down markets.

Observe-only:
- Scans closed hourly event slugs (e.g., bitcoin-up-or-down-february-24-6am-et)
- Finds last traded price before a fixed time-to-end cutoff
- Compares empirical win rate vs quoted entry prices in a configured high-probability band
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo

    ET_ZONE = ZoneInfo("America/New_York")
except Exception:
    ET_ZONE = dt.timezone(dt.timedelta(hours=-5), name="ET")


GAMMA_API_BASE = "https://gamma-api.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"
USER_AGENT = "hourly-updown-highprob-calibration/1.0"
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


def parse_iso_ts(s: str) -> Optional[int]:
    raw = str(s or "").strip()
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    try:
        return int(dt.datetime.fromisoformat(raw).timestamp())
    except Exception:
        return None


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


def _resolve_output_path(raw: str, default_name: str) -> Path:
    if not raw:
        return LOGS_DIR / default_name
    p = Path(raw)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return LOGS_DIR / p.name
    return REPO_ROOT / p


def normalize_assets(raw: str) -> List[str]:
    out: List[str] = []
    seen = set()
    for part in str(raw or "").split(","):
        s = part.strip().lower()
        if not s:
            continue
        if not re.fullmatch(r"[a-z0-9-]{2,24}", s):
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def hour_label_et(ts: int) -> str:
    d = dt.datetime.fromtimestamp(int(ts), tz=ET_ZONE)
    h24 = int(d.hour)
    h12 = h24 % 12
    if h12 == 0:
        h12 = 12
    ampm = "am" if h24 < 12 else "pm"
    return f"{h12}{ampm}"


def build_hourly_slug_jobs(
    since_ts: int,
    until_ts: int,
    assets: Sequence[str],
    max_markets: int,
) -> List[Tuple[str, str, int]]:
    start = (int(since_ts) // 3600) * 3600
    end = (int(until_ts) // 3600) * 3600
    if end < start:
        start, end = end, start

    slot_ts = list(range(start, end + 1, 3600))
    slot_ts.sort(reverse=True)  # newest first

    jobs: List[Tuple[str, str, int]] = []
    for ts in slot_ts:
        d_et = dt.datetime.fromtimestamp(int(ts), tz=ET_ZONE)
        month = d_et.strftime("%B").lower()
        day = int(d_et.day)
        hh = hour_label_et(ts)
        for asset in assets:
            slug = f"{asset}-up-or-down-{month}-{day}-{hh}-et"
            jobs.append((asset, slug, int(ts)))

    if int(max_markets) > 0:
        return jobs[: int(max_markets)]
    return jobs


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


def map_trade_outcome_index(row: dict, outcomes: Sequence[str]) -> Optional[int]:
    idx = as_int(row.get("outcomeIndex"), -1)
    if 0 <= idx < len(outcomes):
        return int(idx)
    row_outcome = str(row.get("outcome") or "").strip().lower()
    if not row_outcome:
        return None
    for i, label in enumerate(outcomes):
        if str(label).strip().lower() == row_outcome:
            return int(i)
    return None


def price_bin_5c(price: float) -> str:
    p = float(price)
    lo = math.floor(p * 20.0) / 20.0
    hi = lo + 0.05
    return f"{lo:.2f}-{hi:.2f}"


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


@dataclass(frozen=True)
class SampleRow:
    asset: str
    slug: str
    slot_end_ts: int
    end_date: str
    condition_id: str
    market_id: str
    title: str
    tte_minutes: int
    cutoff_ts: int
    entry_max_age_minutes: int
    outcome_index: int
    outcome_label: str
    entry_price: float
    entry_trade_ts: int
    entry_trade_age_sec: int
    winner_index: int
    winner_outcome: str
    is_winner: bool
    qualified_price_band: bool
    price_bin_5c: str
    total_valid_trades: int
    fetched_trade_rows: int


def collect_market_samples(
    asset: str,
    slug: str,
    slot_end_ts: int,
    market: dict,
    page_size: int,
    max_trades_per_market: int,
    sleep_sec: float,
    tte_minutes: int,
    entry_max_age_minutes: int,
    price_min: float,
    price_max: float,
) -> List[SampleRow]:
    resolved = resolve_winner_and_loser(market)
    if resolved is None:
        return []
    win_i, _lose_i, win_outcome, _lose_outcome, _win_price = resolved

    condition_id = str(market.get("conditionId") or "").strip()
    if not condition_id.startswith("0x"):
        return []
    outcomes = parse_json_string_field(market.get("outcomes"))
    if len(outcomes) < 2:
        return []

    end_ts = parse_iso_ts(str(market.get("endDate") or "")) or int(slot_end_ts)
    cutoff_ts = int(end_ts) - int(tte_minutes) * 60
    oldest_ts = cutoff_ts - int(entry_max_age_minutes) * 60

    trade_rows = fetch_market_trades(
        condition_id=condition_id,
        page_size=int(page_size),
        max_trades=int(max_trades_per_market),
        sleep_sec=float(sleep_sec),
    )

    total_valid = 0
    # idx -> (ts, price)
    latest_before_cutoff: Dict[int, Tuple[int, float]] = {}
    for row in trade_rows:
        if not isinstance(row, dict):
            continue
        ts = as_int(row.get("timestamp"), 0)
        px = as_float(row.get("price"), math.nan)
        if ts <= 0 or not math.isfinite(px) or px <= 0:
            continue
        total_valid += 1
        if ts < oldest_ts or ts > cutoff_ts:
            continue
        idx = map_trade_outcome_index(row, outcomes=outcomes)
        if idx is None:
            continue
        cur = latest_before_cutoff.get(int(idx))
        if cur is None or int(ts) > int(cur[0]):
            latest_before_cutoff[int(idx)] = (int(ts), float(px))

    out: List[SampleRow] = []
    for idx, candidate in latest_before_cutoff.items():
        ts, px = candidate
        if not (0 <= idx < len(outcomes)):
            continue
        label = str(outcomes[idx])
        age_sec = max(0, int(cutoff_ts - ts))
        qualified = (float(price_min) <= float(px) <= float(price_max))
        out.append(
            SampleRow(
                asset=str(asset),
                slug=str(slug),
                slot_end_ts=int(end_ts),
                end_date=str(market.get("endDate") or ""),
                condition_id=condition_id,
                market_id=str(market.get("id") or ""),
                title=str(market.get("question") or slug),
                tte_minutes=int(tte_minutes),
                cutoff_ts=int(cutoff_ts),
                entry_max_age_minutes=int(entry_max_age_minutes),
                outcome_index=int(idx),
                outcome_label=label,
                entry_price=float(px),
                entry_trade_ts=int(ts),
                entry_trade_age_sec=int(age_sec),
                winner_index=int(win_i),
                winner_outcome=str(win_outcome),
                is_winner=bool(int(idx) == int(win_i)),
                qualified_price_band=bool(qualified),
                price_bin_5c=price_bin_5c(float(px)),
                total_valid_trades=int(total_valid),
                fetched_trade_rows=int(len(trade_rows)),
            )
        )
    return out


def summarize(
    rows: Sequence[SampleRow],
    events_scanned: int,
    events_found: int,
    markets_analyzed: int,
    markets_with_samples: int,
    price_min: float,
    price_max: float,
) -> dict:
    samples = list(rows)
    qualified = [r for r in samples if r.qualified_price_band]
    wins = sum(1 for r in qualified if r.is_winner)
    prices = [float(r.entry_price) for r in qualified]

    win_rate = (wins / len(qualified)) if qualified else None
    avg_price = (sum(prices) / len(prices)) if prices else None
    edge = (win_rate - avg_price) if (win_rate is not None and avg_price is not None) else None

    by_asset: Dict[str, dict] = {}
    for asset in sorted({r.asset for r in qualified}):
        rs = [r for r in qualified if r.asset == asset]
        awins = sum(1 for r in rs if r.is_winner)
        aps = [float(r.entry_price) for r in rs]
        wr = (awins / len(rs)) if rs else None
        ap = (sum(aps) / len(aps)) if aps else None
        by_asset[asset] = {
            "samples": int(len(rs)),
            "wins": int(awins),
            "empirical_win_rate": float(wr) if wr is not None else None,
            "avg_entry_price": float(ap) if ap is not None else None,
            "edge_empirical_minus_price": (float(wr - ap) if (wr is not None and ap is not None) else None),
        }

    by_bin: List[dict] = []
    bins = sorted({r.price_bin_5c for r in qualified})
    for b in bins:
        rs = [r for r in qualified if r.price_bin_5c == b]
        bwins = sum(1 for r in rs if r.is_winner)
        bps = [float(r.entry_price) for r in rs]
        wr = (bwins / len(rs)) if rs else None
        ap = (sum(bps) / len(bps)) if bps else None
        by_bin.append(
            {
                "price_bin_5c": b,
                "samples": int(len(rs)),
                "wins": int(bwins),
                "empirical_win_rate": float(wr) if wr is not None else None,
                "avg_entry_price": float(ap) if ap is not None else None,
                "edge_empirical_minus_price": (float(wr - ap) if (wr is not None and ap is not None) else None),
            }
        )

    ages = [float(r.entry_trade_age_sec) for r in qualified]
    age_stats = {
        "p10_sec": float(percentile(ages, 0.10)) if ages else None,
        "p50_sec": float(percentile(ages, 0.50)) if ages else None,
        "p90_sec": float(percentile(ages, 0.90)) if ages else None,
    }

    return {
        "events_scanned": int(events_scanned),
        "events_found": int(events_found),
        "closed_markets_analyzed": int(markets_analyzed),
        "markets_with_entry_samples": int(markets_with_samples),
        "entry_samples_total": int(len(samples)),
        "qualified_samples": int(len(qualified)),
        "qualified_band": {"price_min": float(price_min), "price_max": float(price_max)},
        "wins": int(wins),
        "empirical_win_rate": float(win_rate) if win_rate is not None else None,
        "avg_entry_price": float(avg_price) if avg_price is not None else None,
        "edge_empirical_minus_price": float(edge) if edge is not None else None,
        "entry_trade_age_stats": age_stats,
        "by_asset": by_asset,
        "by_price_bin_5c": by_bin,
    }


def build_text_report(meta: dict, summary: dict) -> str:
    lines: List[str] = []
    lines.append(
        f"Window: {meta['since_local']} -> {meta['until_local']} (local), "
        f"assets={','.join(meta['assets'])}, tte={meta['tte_minutes']}m"
    )
    lines.append(
        f"Events scanned={summary['events_scanned']} found={summary['events_found']} "
        f"markets={summary['closed_markets_analyzed']} markets_with_samples={summary['markets_with_entry_samples']}"
    )
    lines.append(
        f"Samples total={summary['entry_samples_total']} qualified={summary['qualified_samples']} "
        f"band=[{summary['qualified_band']['price_min']:.2f},{summary['qualified_band']['price_max']:.2f}]"
    )
    wr = summary.get("empirical_win_rate")
    ap = summary.get("avg_entry_price")
    ed = summary.get("edge_empirical_minus_price")
    if wr is not None and ap is not None and ed is not None:
        lines.append(
            f"Qualified calibration: empirical_win_rate={wr*100:.2f}% "
            f"avg_entry_price={ap*100:.2f}c edge={ed*100:.2f}c"
        )

    by_asset = summary.get("by_asset") if isinstance(summary.get("by_asset"), dict) else {}
    if by_asset:
        lines.append("By asset:")
        for asset in sorted(by_asset.keys()):
            row = by_asset[asset]
            wr = row.get("empirical_win_rate")
            ap = row.get("avg_entry_price")
            ed = row.get("edge_empirical_minus_price")
            lines.append(
                f"  {asset}: samples={row.get('samples',0)} "
                f"win_rate={(wr*100):.2f}% avg_price={(ap*100):.2f}c edge={(ed*100):.2f}c"
                if wr is not None and ap is not None and ed is not None
                else f"  {asset}: samples={row.get('samples',0)}"
            )
    return "\n".join(lines)


def write_csv(path: Path, rows: Sequence[SampleRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "asset",
        "slug",
        "slot_end_ts",
        "slot_end_utc",
        "end_date",
        "condition_id",
        "market_id",
        "title",
        "tte_minutes",
        "cutoff_ts",
        "cutoff_utc",
        "entry_max_age_minutes",
        "outcome_index",
        "outcome_label",
        "entry_price",
        "entry_trade_ts",
        "entry_trade_utc",
        "entry_trade_age_sec",
        "winner_index",
        "winner_outcome",
        "is_winner",
        "qualified_price_band",
        "price_bin_5c",
        "total_valid_trades",
        "fetched_trade_rows",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            d = asdict(r)
            d["slot_end_utc"] = fmt_utc(r.slot_end_ts)
            d["cutoff_utc"] = fmt_utc(r.cutoff_ts)
            d["entry_trade_utc"] = fmt_utc(r.entry_trade_ts)
            w.writerow(d)


def parse_args():
    p = argparse.ArgumentParser(
        description="Calibrate high-probability near-expiry pricing for hourly crypto up/down markets (observe-only)"
    )
    p.add_argument("--assets", default="bitcoin,ethereum", help="Comma-separated asset prefixes used in event slugs")
    p.add_argument("--hours", type=float, default=72.0, help="Lookback hours when --since is not provided")
    p.add_argument("--since", default="", help='Start local time "YYYY-MM-DD HH:MM:SS"')
    p.add_argument("--until", default="", help='End local time "YYYY-MM-DD HH:MM:SS" (default: now)')
    p.add_argument("--max-markets", type=int, default=0, help="Maximum event slug attempts (0=all)")
    p.add_argument("--tte-minutes", type=int, default=20, help="Entry cutoff minutes to event end")
    p.add_argument("--entry-max-age-minutes", type=int, default=45, help="Max trade age before cutoff to accept")
    p.add_argument("--price-min", type=float, default=0.80, help="Minimum entry price to qualify")
    p.add_argument("--price-max", type=float, default=0.95, help="Maximum entry price to qualify")
    p.add_argument("--page-size", type=int, default=500, help="Data API trades page size (max 500)")
    p.add_argument("--max-trades-per-market", type=int, default=3000, help="Cap fetched trades per market")
    p.add_argument("--sleep-sec", type=float, default=0.0, help="Optional sleep between market trade pages")
    p.add_argument("--out-json", default="", help="Output summary JSON path (simple filename -> logs/)")
    p.add_argument("--out-csv", default="", help="Output sample CSV path (simple filename -> logs/)")
    p.add_argument("--pretty", action="store_true", help="Pretty-print output JSON")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    assets = normalize_assets(args.assets)
    if not assets:
        print("No valid assets parsed from --assets")
        return 2
    if float(args.price_min) <= 0 or float(args.price_max) >= 1 or float(args.price_min) >= float(args.price_max):
        print("Invalid price band. Require 0 < --price-min < --price-max < 1")
        return 2
    if int(args.tte_minutes) <= 0:
        print("--tte-minutes must be > 0")
        return 2
    if int(args.entry_max_age_minutes) <= 0:
        print("--entry-max-age-minutes must be > 0")
        return 2

    now_local = dt.datetime.now()
    until_local = parse_local_ts(args.until) if args.until else now_local
    since_local = parse_local_ts(args.since) if args.since else (until_local - dt.timedelta(hours=float(args.hours)))
    if since_local > until_local:
        since_local, until_local = until_local, since_local
    since_ts = int(since_local.timestamp())
    until_ts = int(until_local.timestamp())

    out_json = _resolve_output_path(args.out_json, "hourly_updown_highprob_calibration_latest.json")
    out_csv = _resolve_output_path(args.out_csv, "hourly_updown_highprob_calibration_samples_latest.csv")

    jobs = build_hourly_slug_jobs(
        since_ts=since_ts,
        until_ts=until_ts,
        assets=assets,
        max_markets=int(args.max_markets),
    )
    rows: List[SampleRow] = []
    events_found = 0
    markets_analyzed = 0
    markets_with_samples = 0

    for asset, slug, slot_end_ts in jobs:
        event_obj = fetch_gamma_event_by_slug(slug)
        if not isinstance(event_obj, dict):
            continue
        events_found += 1
        market = choose_best_closed_market(event_obj)
        if not isinstance(market, dict):
            continue
        markets_analyzed += 1

        market_rows = collect_market_samples(
            asset=asset,
            slug=slug,
            slot_end_ts=int(slot_end_ts),
            market=market,
            page_size=int(args.page_size),
            max_trades_per_market=int(args.max_trades_per_market),
            sleep_sec=float(args.sleep_sec),
            tte_minutes=int(args.tte_minutes),
            entry_max_age_minutes=int(args.entry_max_age_minutes),
            price_min=float(args.price_min),
            price_max=float(args.price_max),
        )
        if market_rows:
            markets_with_samples += 1
            rows.extend(market_rows)

    rows.sort(key=lambda x: (x.slot_end_ts, x.slug, x.outcome_index))
    summary = summarize(
        rows=rows,
        events_scanned=len(jobs),
        events_found=events_found,
        markets_analyzed=markets_analyzed,
        markets_with_samples=markets_with_samples,
        price_min=float(args.price_min),
        price_max=float(args.price_max),
    )

    meta = {
        "generated_at_local": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assets": assets,
        "since_local": since_local.strftime("%Y-%m-%d %H:%M:%S"),
        "until_local": until_local.strftime("%Y-%m-%d %H:%M:%S"),
        "since_ts": int(since_ts),
        "until_ts": int(until_ts),
        "tte_minutes": int(args.tte_minutes),
        "entry_max_age_minutes": int(args.entry_max_age_minutes),
        "price_min": float(args.price_min),
        "price_max": float(args.price_max),
        "events_scanned": int(len(jobs)),
        "events_found": int(events_found),
        "closed_markets_analyzed": int(markets_analyzed),
        "markets_with_entry_samples": int(markets_with_samples),
        "page_size": int(args.page_size),
        "max_trades_per_market": int(args.max_trades_per_market),
    }
    payload = {
        "meta": meta,
        "summary": summary,
        "samples": [asdict(r) for r in rows],
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8", newline="\n") as f:
        if args.pretty:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        else:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")

    write_csv(out_csv, rows)

    print(build_text_report(meta=meta, summary=summary))
    print()
    print(f"Saved JSON: {out_json}")
    print(f"Saved CSV:  {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

