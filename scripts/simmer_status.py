#!/usr/bin/env python3
"""
Simmer account status helper (SDK).

Usage:
  python scripts/simmer_status.py
  python scripts/simmer_status.py --positions
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SIMMER_API_BASE = "https://api.simmer.markets"


def _user_env_from_registry(name: str) -> str:
    if not sys.platform.startswith("win"):
        return ""
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            v, _t = winreg.QueryValueEx(k, name)
            return str(v or "").strip()
    except Exception:
        return ""


def _safe_print(s: str) -> None:
    try:
        print(s)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(s.encode(enc, errors="replace").decode(enc, errors="replace"))


def api_request(api_key: str, endpoint: str, fail_on_error: bool = True) -> dict:
    url = f"{SIMMER_API_BASE}{endpoint}"
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode() if e.fp else ""
        if fail_on_error:
            _safe_print(f"API Error {e.code}: {body}")
            sys.exit(1)
        return {"_error": f"HTTP {e.code}: {body}", "_status": e.code}
    except URLError as e:
        if fail_on_error:
            _safe_print(f"Connection error: {e.reason}")
            sys.exit(1)
        return {"_error": f"Connection error: {e.reason}"}


def format_usd(amount: float) -> str:
    return f"${amount:,.2f}"


def _effective_position_count(positions: list[dict], eps: float = 0.005) -> int:
    count = 0
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        shares_yes = float(pos.get("shares_yes", 0) or 0)
        shares_no = float(pos.get("shares_no", 0) or 0)
        if abs(shares_yes) > eps or abs(shares_no) > eps:
            count += 1
    return count


def main() -> int:
    ap = argparse.ArgumentParser(description="Simmer account status")
    ap.add_argument("--positions", action="store_true", help="Show detailed positions")
    args = ap.parse_args()

    api_key = os.environ.get("SIMMER_API_KEY") or _user_env_from_registry("SIMMER_API_KEY")
    if not api_key:
        _safe_print("SIMMER_API_KEY is not set. Get it from simmer.markets/dashboard -> SDK.")
        return 2

    _safe_print("Fetching account status...\n")
    portfolio = api_request(api_key, "/api/sdk/portfolio", fail_on_error=False)
    settings = api_request(api_key, "/api/sdk/settings", fail_on_error=False)
    using_fallback = isinstance(portfolio, dict) and "_error" in portfolio

    positions_result = api_request(api_key, "/api/sdk/positions", fail_on_error=False)
    positions = positions_result.get("positions", []) if isinstance(positions_result, dict) else []
    effective_positions_count = _effective_position_count(positions)

    if using_fallback:
        agent = api_request(api_key, "/api/sdk/agents/me", fail_on_error=False)
        balance = float(agent.get("balance", 0) if isinstance(agent, dict) else 0)
        exposure = 0.0
        positions_count = effective_positions_count
        pnl_total = agent.get("total_pnl") if isinstance(agent, dict) else None
        account_label = "SIM Balance"
    else:
        balance = float(portfolio.get("balance_usdc", 0) if isinstance(portfolio, dict) else 0)
        exposure = float(portfolio.get("total_exposure", 0) if isinstance(portfolio, dict) else 0)
        positions_count = int(portfolio.get("positions_count", 0) if isinstance(portfolio, dict) else 0)
        pnl_total = portfolio.get("pnl_total") if isinstance(portfolio, dict) else None
        account_label = "Available Balance"

        # Some accounts intermittently return 0 from /portfolio but /settings has balance.
        if (
            isinstance(settings, dict)
            and "_error" not in settings
            and balance <= 0
            and float(settings.get("polymarket_usdc_balance") or 0) > 0
        ):
            balance = float(settings.get("polymarket_usdc_balance", balance))

        # /portfolio can intermittently report zero positions while /positions still has non-zero holdings.
        if positions_count <= 0 and effective_positions_count > 0:
            positions_count = effective_positions_count

    _safe_print("=" * 50)
    _safe_print("ACCOUNT SUMMARY")
    _safe_print("=" * 50)
    if using_fallback:
        _safe_print(f"Portfolio endpoint unavailable ({portfolio.get('_error')}) -> using agent fallback.")
    _safe_print(f"{account_label}: {format_usd(balance)}")
    _safe_print(f"Total Exposure: {format_usd(exposure)}")
    _safe_print(f"Open Positions: {positions_count}")
    if pnl_total is not None:
        _safe_print(f"Total PnL: {format_usd(float(pnl_total))}")
    _safe_print("=" * 50)

    if args.positions:
        _safe_print("\nOPEN POSITIONS")
        _safe_print("=" * 50)
        if not positions:
            _safe_print("No open positions")
        else:
            for pos in positions:
                q = pos.get("question", pos.get("market_id", "Unknown"))
                q = (q[:77] + "...") if isinstance(q, str) and len(q) > 80 else q
                shares_yes = float(pos.get("shares_yes", 0) or 0)
                shares_no = float(pos.get("shares_no", 0) or 0)
                current_price = float(pos.get("current_price", 0) or 0)
                cost_basis = float(pos.get("cost_basis", 0) or 0)
                pnl = float(pos.get("pnl", 0) or 0)
                if shares_yes > 0.005:
                    side = "YES"
                    shares = shares_yes
                elif shares_no > 0.005:
                    side = "NO"
                    shares = shares_no
                else:
                    continue
                _safe_print(f"\n{q}")
                _safe_print(f"  {side}: {shares:.2f} shares, cost ${cost_basis:.2f}")
                _safe_print(f"  Current: {current_price:.1%} | PnL: {format_usd(pnl)}")
        _safe_print("\n" + "=" * 50)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
