#!/usr/bin/env python3
"""
Resolve a market by name, fetch wallet trades for that market, and run autopsy analysis.

Compatibility command:
  python scripts/analyze_user.py "Market Name" 0xWallet [output.json]

Observe-only. No order placement.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from analyze_trades import analyze_trades, print_report
from fetch_trades import fetch_user_trades, is_wallet_address


GAMMA_API_BASE = "https://gamma-api.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"
USER_AGENT = "Mozilla/5.0 (compatible; polymarket-analyze-user/1.0)"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_tag() -> str:
    return now_utc().strftime("%Y%m%d_%H%M%S")


def fetch_json(url: str, timeout_sec: float = 25.0, retries: int = 4) -> Optional[object]:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    for i in range(retries):
        try:
            with urlopen(req, timeout=timeout_sec) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError):
            if i >= retries - 1:
                return None
    return None


def normalize_text(value: str) -> str:
    s = (value or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def score_match(query: str, candidate: str) -> float:
    q = normalize_text(query)
    c = normalize_text(candidate)
    if not q or not c:
        return 0.0
    score = difflib.SequenceMatcher(None, q, c).ratio()
    if q == c:
        score += 1.0
    elif q in c:
        score += 0.35
    return score


def search_events(query: str, top_n: int = 10) -> List[dict]:
    q = urlencode({"q": query})
    url = f"{GAMMA_API_BASE}/public-search?{q}"
    data = fetch_json(url)
    if not isinstance(data, dict):
        return []
    events = data.get("events")
    if not isinstance(events, list):
        return []
    ranked = sorted(events, key=lambda e: score_match(query, str(e.get("title") or "")), reverse=True)
    return ranked[: max(1, top_n)]


def fetch_event_by_slug(slug: str) -> Optional[dict]:
    if not slug:
        return None
    url = f"{GAMMA_API_BASE}/events/slug/{slug}"
    data = fetch_json(url)
    if isinstance(data, dict):
        return data
    return None


def choose_market_from_event(event_obj: dict, query: str) -> Optional[dict]:
    markets = event_obj.get("markets")
    if not isinstance(markets, list) or not markets:
        return None

    def _mk_score(m: dict) -> Tuple[float, float]:
        q = str(m.get("question") or "")
        s = score_match(query, q)
        liq = 0.0
        try:
            liq = float(m.get("liquidity") or 0.0)
        except (TypeError, ValueError):
            liq = 0.0
        return (s, liq)

    ranked = sorted(markets, key=_mk_score, reverse=True)
    return ranked[0] if ranked else None


def resolve_market(query: str) -> Optional[dict]:
    hits = search_events(query, top_n=8)
    for ev in hits:
        slug = str(ev.get("slug") or "")
        full = fetch_event_by_slug(slug)
        if not full:
            continue
        market = choose_market_from_event(full, query)
        if not market:
            continue
        condition_id = str(market.get("conditionId") or "").strip()
        if not condition_id.startswith("0x"):
            continue
        return {
            "event_title": str(full.get("title") or ev.get("title") or ""),
            "event_slug": slug,
            "market_question": str(market.get("question") or ""),
            "market_slug": str(market.get("slug") or ""),
            "condition_id": condition_id,
            "market_id": str(market.get("id") or ""),
        }
    return None


def fetch_market_holders(condition_id: str, limit: int = 10) -> List[dict]:
    q = urlencode({"market": condition_id, "limit": str(max(1, int(limit)))})
    url = f"{DATA_API_BASE}/holders?{q}"
    data = fetch_json(url)
    if not isinstance(data, list):
        return []

    # Response shape: [{token, holders:[...]}]. Aggregate across tokens by wallet.
    agg: Dict[str, dict] = {}
    for token_group in data:
        holders = token_group.get("holders")
        if not isinstance(holders, list):
            continue
        for h in holders:
            wallet = str(h.get("proxyWallet") or "").strip().lower()
            if not wallet:
                continue
            amount = 0.0
            try:
                amount = float(h.get("amount") or 0.0)
            except (TypeError, ValueError):
                amount = 0.0
            row = agg.setdefault(
                wallet,
                {
                    "wallet": wallet,
                    "amount_total": 0.0,
                    "name": str(h.get("name") or ""),
                    "pseudonym": str(h.get("pseudonym") or ""),
                },
            )
            row["amount_total"] += amount
            if not row.get("name"):
                row["name"] = str(h.get("name") or "")
            if not row.get("pseudonym"):
                row["pseudonym"] = str(h.get("pseudonym") or "")

    out = sorted(agg.values(), key=lambda x: float(x.get("amount_total") or 0.0), reverse=True)
    return out


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_output_path(raw_out: str, market_slug: str, wallet_or_tag: str) -> Path:
    logs_dir = repo_root() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    if raw_out:
        p = Path(raw_out)
        if p.is_absolute():
            return p
        if len(p.parts) == 1:
            return logs_dir / p.name
        return repo_root() / p

    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", market_slug or "market")
    tag = re.sub(r"[^a-zA-Z0-9_-]+", "_", wallet_or_tag or "wallet")
    return logs_dir / f"autopsy_{slug}_{tag}_{utc_tag()}.json"


def print_holder_scan_summary(rows: List[dict]) -> None:
    print("-" * 78)
    print("TOP HOLDER AUTOPSY SUMMARY")
    print("-" * 78)
    if not rows:
        print("No rows.")
        return
    print(f"{'rank':>4}  {'wallet':<42} {'trades':>7} {'prof%':>7} {'comb_avg':>9} {'hedge':>18}")
    for row in rows:
        rank = int(row.get("rank", 0))
        wallet = str(row.get("wallet") or "")
        trade_count = int(row.get("trade_count") or 0)
        profitable_pct = row.get("time_profitable_pct")
        comb = row.get("combined_avg")
        hedge_status = str(row.get("hedge_status") or "-")
        prof_s = f"{float(profitable_pct):.1f}" if profitable_pct is not None else "-"
        comb_s = f"{float(comb):.4f}" if comb is not None else "-"
        print(f"{rank:>4}  {wallet:<42} {trade_count:>7} {prof_s:>7} {comb_s:>9} {hedge_status:>18}")


def main() -> int:
    p = argparse.ArgumentParser(description="Analyze one wallet (or top holders) for a market query")
    p.add_argument("market_name", help="Market/event name to search")
    p.add_argument("wallet", nargs="?", default="", help="Wallet address (0x...)")
    p.add_argument("out_json", nargs="?", default="", help="Optional output JSON filename/path")
    p.add_argument("--top-holders", type=int, default=0, help="If >0, analyze top N holders instead of wallet arg")
    p.add_argument("--holders-limit", type=int, default=20, help="Number of holders to fetch before ranking")
    p.add_argument("--page-size", type=int, default=500, help="Pagination page size for trades fetch")
    p.add_argument("--max-trades", type=int, default=2000, help="Max trades per wallet")
    p.add_argument("--pretty", action="store_true", help="Pretty-print output JSON")
    args = p.parse_args()

    target_market = resolve_market(args.market_name)
    if not target_market:
        print(f"Market not found for query: {args.market_name}")
        return 2

    print(f"Resolved event:  {target_market['event_title']}")
    print(f"Resolved market: {target_market['market_question']}")
    print(f"Condition ID:    {target_market['condition_id']}")
    print()

    if args.top_holders > 0:
        holders = fetch_market_holders(target_market["condition_id"], limit=max(args.holders_limit, args.top_holders))
        if not holders:
            print("No holders found for this market.")
            return 1

        selected = holders[: args.top_holders]
        summary_rows: List[dict] = []
        detailed: List[dict] = []
        for i, holder in enumerate(selected, start=1):
            wallet = str(holder.get("wallet") or "")
            trades = fetch_user_trades(
                user=wallet,
                market=target_market["condition_id"],
                limit=args.page_size,
                max_trades=args.max_trades,
            )
            analysis = analyze_trades(
                trades,
                market_title=target_market["market_question"],
                wallet=wallet,
            )
            inv = analysis.get("inventory") if isinstance(analysis, dict) else {}
            tl = analysis.get("timeline") if isinstance(analysis, dict) else {}
            summary_rows.append(
                {
                    "rank": i,
                    "wallet": wallet,
                    "trade_count": int(analysis.get("trade_count") or 0),
                    "time_profitable_pct": tl.get("time_profitable_pct") if isinstance(tl, dict) else None,
                    "combined_avg": inv.get("combined_avg") if isinstance(inv, dict) else None,
                    "hedge_status": inv.get("hedge_status") if isinstance(inv, dict) else "UNKNOWN",
                }
            )
            detailed.append(
                {
                    "rank": i,
                    "holder": holder,
                    "analysis": analysis,
                }
            )

        print_holder_scan_summary(summary_rows)

        out_path = resolve_output_path(
            args.out_json,
            market_slug=target_market["market_slug"] or target_market["event_slug"],
            wallet_or_tag=f"top{args.top_holders}",
        )
        payload = {
            "meta": {
                "generated_at_utc": now_utc().isoformat(),
                "market": target_market,
                "top_holders": int(args.top_holders),
                "holders_limit": int(args.holders_limit),
                "max_trades_per_wallet": int(args.max_trades),
            },
            "summary": summary_rows,
            "details": detailed,
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            if args.pretty:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            else:
                json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
        print()
        print(f"Saved: {out_path}")
        return 0

    wallet = args.wallet.strip()
    if not wallet:
        print("Wallet is required unless --top-holders is used.")
        return 2
    if not is_wallet_address(wallet):
        print(f"Invalid wallet address: {wallet}")
        return 2

    rows = fetch_user_trades(
        user=wallet,
        market=target_market["condition_id"],
        limit=args.page_size,
        max_trades=args.max_trades,
    )
    if not rows:
        print("No trades found for this wallet in the resolved market.")
        return 1

    analysis = analyze_trades(
        rows,
        market_title=target_market["market_question"],
        wallet=wallet,
    )
    print_report(analysis)

    out_path = resolve_output_path(
        args.out_json,
        market_slug=target_market["market_slug"] or target_market["event_slug"],
        wallet_or_tag=wallet[:10],
    )
    payload = {
        "meta": {
            "generated_at_utc": now_utc().isoformat(),
            "market": target_market,
            "wallet": wallet,
            "trade_count": len(rows),
        },
        "trades": rows,
        "analysis": analysis,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        if args.pretty:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        else:
            json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)

    print()
    print(f"Saved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

