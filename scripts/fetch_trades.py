#!/usr/bin/env python3
"""
Fetch Polymarket user trades from public Data API (read-only).

Examples:
  python scripts/fetch_trades.py 0xabc...
  python scripts/fetch_trades.py 0xabc... --market 0x<conditionId>
  python scripts/fetch_trades.py 0xabc... --market 0x<conditionId> --out my_trades.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import Request, urlopen


DATA_API_BASE = "https://data-api.polymarket.com"
USER_AGENT = "Mozilla/5.0 (compatible; polymarket-fetch-trades/1.0)"
WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
HANDLE_WALLET_PREFIX_RE = re.compile(r"^(0x[a-fA-F0-9]{40})(?:[-_].*)?$")


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_tag() -> str:
    return now_utc().strftime("%Y%m%d_%H%M%S")


def is_wallet_address(value: str) -> bool:
    return bool(WALLET_RE.match((value or "").strip()))


def extract_profile_handle(value: str) -> Optional[str]:
    raw = (value or "").strip()
    if not raw:
        return None
    if is_wallet_address(raw):
        return None

    candidate = raw
    if raw.startswith("@"):
        candidate = raw[1:]
    elif raw.startswith("http://") or raw.startswith("https://"):
        try:
            parsed = urlparse(raw)
        except Exception:
            return None
        host = (parsed.netloc or "").lower()
        if "polymarket.com" not in host:
            return None
        path = unquote(parsed.path or "").strip("/")
        if not path:
            return None
        parts = [p for p in path.split("/") if p]
        found: Optional[str] = None
        for p in parts:
            if p.startswith("@") and len(p) > 1:
                found = p[1:]
                break
        if found is None and "profile" in parts:
            i = parts.index("profile")
            if i + 1 < len(parts):
                nxt = parts[i + 1]
                found = nxt[1:] if nxt.startswith("@") else nxt
        if found is None:
            return None
        candidate = found

    candidate = candidate.strip()
    if candidate.startswith("@"):
        candidate = candidate[1:]
    if not HANDLE_RE.match(candidate):
        return None
    return candidate


def extract_wallet_from_handle_prefix(handle: str) -> Optional[str]:
    """
    Accept profile handle forms like:
      - 0x<40hex>
      - 0x<40hex>-<suffix>
      - 0x<40hex>_<suffix>
    and return the wallet prefix.
    """
    h = (handle or "").strip().lstrip("@")
    m = HANDLE_WALLET_PREFIX_RE.match(h)
    if not m:
        return None
    return str(m.group(1)).lower()


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
            time.sleep(0.20 * (i + 1))
    return None


def fetch_text(url: str, timeout_sec: float = 25.0, retries: int = 4) -> Optional[str]:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    for i in range(retries):
        try:
            with urlopen(req, timeout=timeout_sec) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError, ValueError):
            if i >= retries - 1:
                return None
            time.sleep(0.20 * (i + 1))
    return None


def resolve_wallet_from_profile_handle(handle: str) -> Optional[str]:
    h = (handle or "").strip().lstrip("@")
    if not HANDLE_RE.match(h):
        return None
    html = fetch_text(f"https://polymarket.com/@{h}", timeout_sec=30.0, retries=4)
    if not html:
        return None

    keys = ("proxyAddress", "primaryAddress", "baseAddress")
    for key in keys:
        m = re.search(rf'"{key}"\s*:\s*"(0x[a-fA-F0-9]{{40}})"', html)
        if m:
            return m.group(1).lower()
        m2 = re.search(rf"{key}\\\":\\\"(0x[a-fA-F0-9]{{40}})", html)
        if m2:
            return m2.group(1).lower()
    return None


def resolve_user_identifier(raw_user: str) -> Tuple[Optional[str], Dict[str, str]]:
    user = (raw_user or "").strip()
    meta: Dict[str, str] = {"input_user": user}
    if is_wallet_address(user):
        meta["resolved_via"] = "wallet"
        return user.lower(), meta

    handle = extract_profile_handle(user)
    if not handle:
        return None, meta

    wallet_from_prefix = extract_wallet_from_handle_prefix(handle)
    if wallet_from_prefix:
        meta["resolved_via"] = "handle_wallet_prefix"
        meta["profile_handle"] = f"@{handle}"
        meta["profile_url"] = f"https://polymarket.com/@{handle}"
        return wallet_from_prefix, meta

    wallet = resolve_wallet_from_profile_handle(handle)
    if not wallet:
        return None, meta

    meta["resolved_via"] = "profile"
    meta["profile_handle"] = f"@{handle}"
    meta["profile_url"] = f"https://polymarket.com/@{handle}"
    return wallet, meta


def fetch_user_trades(
    user: str,
    market: str = "",
    limit: int = 500,
    max_trades: int = 4000,
    sleep_sec: float = 0.0,
) -> List[dict]:
    """
    Data API filter keys:
      - user: wallet address (required for this helper)
      - market: conditionId (optional)
      - limit / offset: pagination
    """
    rows: List[dict] = []
    offset = 0
    page_size = max(1, min(int(limit), 500))
    hard_max = max(1, int(max_trades))

    while len(rows) < hard_max:
        batch = min(page_size, hard_max - len(rows))
        params: Dict[str, str] = {
            "user": user,
            "limit": str(batch),
            "offset": str(offset),
        }
        if market:
            params["market"] = market
        url = f"{DATA_API_BASE}/trades?{urlencode(params)}"

        data = fetch_json(url)
        if not isinstance(data, list) or not data:
            break

        rows.extend(data)
        if len(data) < batch:
            break

        offset += batch
        if sleep_sec > 0:
            time.sleep(sleep_sec)

    rows.sort(key=lambda x: int(x.get("timestamp") or 0))
    return rows[:hard_max]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_output_path(raw_out: str, user: str, market: str) -> Path:
    logs_dir = repo_root() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    if raw_out:
        p = Path(raw_out)
        if p.is_absolute():
            return p
        # Keep simple filenames under logs/.
        if len(p.parts) == 1:
            return logs_dir / p.name
        return repo_root() / p

    market_tag = "all"
    if market:
        market_tag = market[:10]
    return logs_dir / f"trades_{user[:10]}_{market_tag}_{utc_tag()}.json"


def main() -> int:
    p = argparse.ArgumentParser(description="Fetch Polymarket user trades (observe-only)")
    p.add_argument("user", help="Wallet address (0x...), @profile handle, or Polymarket profile URL")
    p.add_argument("--market", default="", help="ConditionId filter (0x...)")
    p.add_argument("--limit", type=int, default=500, help="Page size for Data API pagination")
    p.add_argument("--max-trades", type=int, default=4000, help="Maximum trades to fetch")
    p.add_argument("--sleep-sec", type=float, default=0.0, help="Optional sleep between pages")
    p.add_argument("--out", default="", help="Output JSON path (simple filename goes under logs/)")
    p.add_argument("--pretty", action="store_true", help="Write pretty-printed JSON")
    args = p.parse_args()

    raw_user = args.user.strip()
    market = args.market.strip()
    user, resolve_meta = resolve_user_identifier(raw_user)
    if not user:
        print(
            "Invalid user identifier. Provide wallet (0x...), @profile handle, "
            "or profile URL (https://polymarket.com/@handle)."
        )
        return 2

    rows = fetch_user_trades(
        user=user,
        market=market,
        limit=args.limit,
        max_trades=args.max_trades,
        sleep_sec=args.sleep_sec,
    )

    out_path = resolve_output_path(args.out, user=user.lower(), market=market.lower())
    payload = {
        "meta": {
            "fetched_at_utc": now_utc().isoformat(),
            "source": "data-api.polymarket.com/trades",
            "user": user,
            "input_user": resolve_meta.get("input_user", raw_user),
            "resolved_via": resolve_meta.get("resolved_via", ""),
            "profile_handle": resolve_meta.get("profile_handle", ""),
            "profile_url": resolve_meta.get("profile_url", ""),
            "market": market,
            "trade_count": len(rows),
            "page_size": int(args.limit),
            "max_trades": int(args.max_trades),
        },
        "trades": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        if args.pretty:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        else:
            json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)

    resolved_via = resolve_meta.get("resolved_via", "")
    if resolved_via in {"profile", "handle_wallet_prefix"}:
        print(f"Resolved profile {resolve_meta.get('profile_handle', '')} -> {user}")
    print(f"Fetched trades: {len(rows)}")
    print(f"Saved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
