#!/usr/bin/env python3
"""
Record one daily realized-PnL snapshot from Simmer SDK (observe-only).

Outputs:
  - logs/clob_arb_realized_daily.jsonl   (upsert by day)
  - logs/clob_arb_realized_latest.json   (latest snapshot)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SIMMER_API_BASE = "https://api.simmer.markets"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def logs_dir() -> Path:
    p = repo_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_path(raw: str, default_name: str) -> Path:
    if not raw.strip():
        return logs_dir() / default_name
    p = Path(raw)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs_dir() / p.name
    return repo_root() / p


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


def _as_float(v) -> Optional[float]:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n != n or n in (float("inf"), float("-inf")):
        return None
    return n


def api_request(api_key: str, endpoint: str, timeout_sec: float) -> dict:
    url = f"{SIMMER_API_BASE}{endpoint}"
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=timeout_sec) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"_error": f"HTTP {e.code}: {body}", "_status": e.code}
    except URLError as e:
        return {"_error": f"Connection error: {e.reason}"}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def _day_utc(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return now_utc().date().isoformat()
    try:
        return dt.date.fromisoformat(s).isoformat()
    except Exception:
        raise SystemExit(f"invalid --day (expected YYYY-MM-DD): {s}")


def load_jsonl_by_day(path: Path) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            row = json.loads(s)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        day = str(row.get("day") or "").strip()
        if not day:
            continue
        out[day] = row
    return out


def write_jsonl_sorted(path: Path, rows_by_day: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    days = sorted(rows_by_day.keys())
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for day in days:
            f.write(json.dumps(rows_by_day[day], ensure_ascii=True) + "\n")


def build_daily_row(day: str, timeout_sec: float) -> dict:
    api_key = os.environ.get("SIMMER_API_KEY") or _user_env_from_registry("SIMMER_API_KEY")
    api_key = (api_key or "").strip()
    captured = now_utc().isoformat()

    base_row = {
        "day": day,
        "captured_utc": captured,
        "observe_only": True,
        "source": "record_simmer_realized_daily.py",
    }

    if not api_key:
        row = dict(base_row)
        row.update(
            {
                "status": "skipped_no_api_key",
                "reason": "SIMMER_API_KEY is not set",
                "realized_pnl_usd": None,
            }
        )
        return row

    positions = api_request(api_key, "/api/sdk/positions", timeout_sec=timeout_sec)
    portfolio = api_request(api_key, "/api/sdk/portfolio", timeout_sec=timeout_sec)
    settings = api_request(api_key, "/api/sdk/settings", timeout_sec=timeout_sec)

    pnl_summary = positions.get("pnl_summary") if isinstance(positions, dict) else None
    pm = pnl_summary.get("polymarket") if isinstance(pnl_summary, dict) else None
    comb = pnl_summary.get("combined") if isinstance(pnl_summary, dict) else None

    realized_pm = _as_float(pm.get("realized")) if isinstance(pm, dict) else None
    unrealized_pm = _as_float(pm.get("unrealized")) if isinstance(pm, dict) else None
    total_pm = _as_float(pm.get("total")) if isinstance(pm, dict) else None

    realized_comb = _as_float(comb.get("realized")) if isinstance(comb, dict) else None
    unrealized_comb = _as_float(comb.get("unrealized")) if isinstance(comb, dict) else None
    total_comb = _as_float(comb.get("total")) if isinstance(comb, dict) else None

    exposure = _as_float(portfolio.get("total_exposure")) if isinstance(portfolio, dict) else None
    balance = _as_float(portfolio.get("balance_usdc")) if isinstance(portfolio, dict) else None
    if balance is None and isinstance(settings, dict):
        balance = _as_float(settings.get("polymarket_usdc_balance"))

    if realized_pm is None and total_pm is None and isinstance(portfolio, dict):
        # Conservative fallback: only map total->realized when exposure is effectively flat.
        total_port = _as_float(portfolio.get("pnl_total"))
        if total_port is not None:
            total_pm = total_port
            if exposure is not None and abs(exposure) <= 1e-6:
                realized_pm = total_port
                unrealized_pm = 0.0

    row = dict(base_row)
    row.update(
        {
            "status": "ok",
            "realized_pnl_usd": realized_pm,
            "unrealized_pnl_usd": unrealized_pm,
            "total_pnl_usd": total_pm,
            "combined_realized_pnl_usd": realized_comb,
            "combined_unrealized_pnl_usd": unrealized_comb,
            "combined_total_pnl_usd": total_comb,
            "balance_usdc": balance,
            "exposure_usd": exposure,
            "positions_count": (
                positions.get("position_counts", {}).get("active")
                if isinstance(positions, dict) and isinstance(positions.get("position_counts"), dict)
                else None
            ),
            "endpoint_health": {
                "positions_ok": isinstance(positions, dict) and "_error" not in positions,
                "portfolio_ok": isinstance(portfolio, dict) and "_error" not in portfolio,
                "settings_ok": isinstance(settings, dict) and "_error" not in settings,
            },
        }
    )
    return row


def main() -> int:
    p = argparse.ArgumentParser(description="Record daily realized PnL snapshot from Simmer SDK (observe-only).")
    p.add_argument("--day", default="", help="UTC day key YYYY-MM-DD (default: today UTC)")
    p.add_argument("--out-jsonl", default="", help="Daily JSONL output path (default logs/clob_arb_realized_daily.jsonl)")
    p.add_argument("--out-latest-json", default="", help="Latest snapshot JSON path (default logs/clob_arb_realized_latest.json)")
    p.add_argument("--api-timeout-sec", type=float, default=20.0, help="Simmer API timeout seconds")
    p.add_argument("--pretty", action="store_true")
    args = p.parse_args()

    day = _day_utc(str(args.day))
    out_jsonl = resolve_path(str(args.out_jsonl), "clob_arb_realized_daily.jsonl")
    out_latest = resolve_path(str(args.out_latest_json), "clob_arb_realized_latest.json")

    row = build_daily_row(day=day, timeout_sec=max(1.0, float(args.api_timeout_sec)))

    rows_by_day = load_jsonl_by_day(out_jsonl)
    if row.get("status") == "ok":
        rows_by_day[day] = row
        write_jsonl_sorted(out_jsonl, rows_by_day)

    summary = {
        "generated_utc": now_utc().isoformat(),
        "meta": {"observe_only": True, "source": "scripts/record_simmer_realized_daily.py"},
        "artifacts": {
            "out_jsonl": str(out_jsonl),
            "out_latest_json": str(out_latest),
        },
        "latest_row": row,
        "series": {
            "observed_days": len(rows_by_day),
            "first_day": (sorted(rows_by_day.keys())[0] if rows_by_day else None),
            "last_day": (sorted(rows_by_day.keys())[-1] if rows_by_day else None),
        },
    }

    out_latest.parent.mkdir(parents=True, exist_ok=True)
    with out_latest.open("w", encoding="utf-8") as f:
        if args.pretty:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        else:
            json.dump(summary, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")

    print(f"[realized-daily] status={row.get('status')} day={day}")
    print(f"[realized-daily] out_jsonl={out_jsonl}")
    print(f"[realized-daily] out_latest_json={out_latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

