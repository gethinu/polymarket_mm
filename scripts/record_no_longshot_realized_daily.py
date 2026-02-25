#!/usr/bin/env python3
"""
Observe-only forward realized-return tracker for no-longshot daily screens.

This script does not place trades. It keeps a local "paper entry" ledger from
`screen` output, resolves entries when markets settle, and updates:

- logs/no_longshot_forward_positions.json
- logs/no_longshot_realized_daily.jsonl
- logs/no_longshot_realized_latest.json
- logs/no_longshot_monthly_return_latest.txt
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


GAMMA_API_BASE = "https://gamma-api.polymarket.com"
USER_AGENT = "Mozilla/5.0 (compatible; no-longshot-realized/1.0)"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def logs_dir() -> Path:
    p = repo_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_path(raw: str, default_name: str) -> Path:
    s = (raw or "").strip()
    if not s:
        return logs_dir() / default_name
    p = Path(s)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs_dir() / p.name
    return repo_root() / p


def as_float(v, default: Optional[float] = None) -> Optional[float]:
    try:
        n = float(v)
    except Exception:
        return default
    if n != n or n in (float("inf"), float("-inf")):
        return default
    return n


def parse_list_field(value) -> List:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            return []
    return []


def parse_iso_ts(value: str) -> Optional[int]:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def day_from_iso(value: str) -> Optional[str]:
    ts = parse_iso_ts(value)
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).date().isoformat()


def fetch_json(url: str, timeout_sec: float = 25.0, retries: int = 3) -> Optional[object]:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for i in range(max(1, retries)):
        try:
            with urlopen(req, timeout=timeout_sec) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError):
            if i >= retries - 1:
                return None
    return None


def fetch_market_by_id(market_id: str, timeout_sec: float) -> Optional[dict]:
    mid = str(market_id or "").strip()
    if not mid:
        return None
    url = f"{GAMMA_API_BASE}/markets?id={mid}"
    data = fetch_json(url, timeout_sec=timeout_sec, retries=3)
    if isinstance(data, list) and data:
        row = data[0]
        if isinstance(row, dict):
            return row
    return None


def extract_yes_no_prices(market: dict) -> Optional[Tuple[float, float]]:
    outcomes = [str(x).strip().lower() for x in parse_list_field(market.get("outcomes"))]
    prices_raw = parse_list_field(market.get("outcomePrices"))
    prices: List[float] = []
    for x in prices_raw:
        p = as_float(x, None)
        if p is None:
            return None
        prices.append(float(p))
    if len(outcomes) != 2 or len(prices) != 2:
        return None
    if "yes" not in outcomes or "no" not in outcomes:
        return None
    yes_i = outcomes.index("yes")
    return float(prices[yes_i]), float(prices[1 - yes_i])


def load_json(path: Path, default_obj):
    if not path.exists():
        return default_obj
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default_obj
    return obj if isinstance(obj, type(default_obj)) else default_obj


def write_json(path: Path, obj, pretty: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        if pretty:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        else:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def load_screen_rows(path: Path) -> List[dict]:
    if not path.exists():
        return []
    rows: List[dict] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if isinstance(row, dict):
                    rows.append(row)
    except Exception:
        return []
    return rows


def build_entry(row: dict, per_trade_cost: float) -> Optional[dict]:
    market_id = str(row.get("market_id") or "").strip()
    if not market_id:
        return None
    yes_price = as_float(row.get("yes_price"), None)
    no_price = as_float(row.get("no_price"), None)
    if yes_price is None or no_price is None:
        return None
    if no_price <= 0.0:
        return None

    t = now_utc()
    return {
        "position_id": f"m{market_id}",
        "market_id": market_id,
        "question": str(row.get("question") or ""),
        "entry_utc": t.isoformat(),
        "entry_day": t.date().isoformat(),
        "entry_yes_price": float(yes_price),
        "entry_no_price": float(no_price),
        "entry_per_trade_cost": float(per_trade_cost),
        "end_iso_at_entry": str(row.get("end_iso") or ""),
        "days_to_end_at_entry": as_float(row.get("days_to_end"), None),
        "status": "open",
        "resolved_utc": None,
        "resolved_day": None,
        "resolution": None,
        "resolved_yes_price": None,
        "resolved_no_price": None,
        "resolved_end_iso": None,
        "payout_per_share": None,
        "cost_basis_per_share": None,
        "pnl_per_share": None,
        "realized_return_pct": None,
    }


def ingest_entries(positions: List[dict], screen_rows: List[dict], top_n: int, per_trade_cost: float) -> int:
    seen = set()
    for p in positions:
        mid = str(p.get("market_id") or "").strip()
        if mid:
            seen.add(mid)

    added = 0
    for row in screen_rows[: max(0, int(top_n))]:
        mid = str(row.get("market_id") or "").strip()
        if not mid or mid in seen:
            continue
        entry = build_entry(row=row, per_trade_cost=per_trade_cost)
        if entry is None:
            continue
        positions.append(entry)
        seen.add(mid)
        added += 1
    return added


def try_resolve_position(
    pos: dict,
    timeout_sec: float,
    win_threshold: float,
    lose_threshold: float,
) -> bool:
    if str(pos.get("status") or "") != "open":
        return False
    mid = str(pos.get("market_id") or "").strip()
    if not mid:
        return False

    market = fetch_market_by_id(mid, timeout_sec=timeout_sec)
    if market is None:
        return False
    yn = extract_yes_no_prices(market)
    if yn is None:
        return False
    yes_price, no_price = yn

    no_wins = no_price >= win_threshold and yes_price <= lose_threshold
    yes_wins = yes_price >= win_threshold and no_price <= lose_threshold
    if not no_wins and not yes_wins:
        return False

    entry_no = as_float(pos.get("entry_no_price"), 0.0) or 0.0
    per_trade_cost = as_float(pos.get("entry_per_trade_cost"), 0.0) or 0.0
    cost_basis = entry_no + per_trade_cost
    payout = 1.0 if no_wins else 0.0
    pnl = payout - cost_basis
    ret = (pnl / cost_basis) if cost_basis > 1e-12 else None

    now = now_utc()
    resolved_day = day_from_iso(str(market.get("endDate") or "")) or now.date().isoformat()

    pos["status"] = "resolved"
    pos["resolved_utc"] = now.isoformat()
    pos["resolved_day"] = resolved_day
    pos["resolution"] = "NO_WIN" if no_wins else "NO_LOSE"
    pos["resolved_yes_price"] = float(yes_price)
    pos["resolved_no_price"] = float(no_price)
    pos["resolved_end_iso"] = str(market.get("endDate") or "")
    pos["payout_per_share"] = float(payout)
    pos["cost_basis_per_share"] = float(cost_basis)
    pos["pnl_per_share"] = float(pnl)
    pos["realized_return_pct"] = float(ret) if ret is not None else None
    return True


def aggregate_daily(positions: List[dict]) -> Dict[str, dict]:
    by_day: Dict[str, dict] = {}
    for p in positions:
        if str(p.get("status") or "") != "resolved":
            continue
        day = str(p.get("resolved_day") or "").strip()
        if not day:
            continue
        pnl = as_float(p.get("pnl_per_share"), None)
        cost = as_float(p.get("cost_basis_per_share"), None)
        if pnl is None or cost is None:
            continue
        rec = by_day.setdefault(
            day,
            {
                "day": day,
                "observe_only": True,
                "source": "record_no_longshot_realized_daily.py",
                "strategy": "no_longshot_forward",
                "realized_pnl_usd": 0.0,
                "realized_cost_basis_usd": 0.0,
                "resolved_trades": 0,
            },
        )
        rec["realized_pnl_usd"] = float(rec["realized_pnl_usd"]) + float(pnl)
        rec["realized_cost_basis_usd"] = float(rec["realized_cost_basis_usd"]) + float(cost)
        rec["resolved_trades"] = int(rec["resolved_trades"]) + 1

    for rec in by_day.values():
        pnl = float(rec["realized_pnl_usd"])
        cost = float(rec["realized_cost_basis_usd"])
        rec["realized_return_pct"] = (pnl / cost) if cost > 1e-12 else None
    return by_day


def write_jsonl_sorted(path: Path, rows_by_day: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for day in sorted(rows_by_day.keys()):
            f.write(json.dumps(rows_by_day[day], ensure_ascii=True) + "\n")


def rolling_30_bounds(today: dt.date) -> Tuple[dt.date, dt.date]:
    start = today - dt.timedelta(days=29)
    return start, today


def compute_metrics(rows_by_day: Dict[str, dict], positions: List[dict]) -> dict:
    days = sorted(rows_by_day.keys())
    total_pnl = 0.0
    total_cost = 0.0
    total_trades = 0
    for day in days:
        rec = rows_by_day[day]
        total_pnl += float(rec.get("realized_pnl_usd") or 0.0)
        total_cost += float(rec.get("realized_cost_basis_usd") or 0.0)
        total_trades += int(rec.get("resolved_trades") or 0)
    total_ret = (total_pnl / total_cost) if total_cost > 1e-12 else None

    today = now_utc().date()
    w_start, w_end = rolling_30_bounds(today)
    roll_pnl = 0.0
    roll_cost = 0.0
    roll_trades = 0
    roll_days = 0
    for day in days:
        try:
            d = dt.date.fromisoformat(day)
        except Exception:
            continue
        if d < w_start or d > w_end:
            continue
        rec = rows_by_day[day]
        roll_days += 1
        roll_pnl += float(rec.get("realized_pnl_usd") or 0.0)
        roll_cost += float(rec.get("realized_cost_basis_usd") or 0.0)
        roll_trades += int(rec.get("resolved_trades") or 0)
    roll_ret = (roll_pnl / roll_cost) if roll_cost > 1e-12 else None

    open_positions = sum(1 for p in positions if str(p.get("status") or "") == "open")
    resolved_positions = sum(1 for p in positions if str(p.get("status") or "") == "resolved")

    return {
        "observed_days": len(days),
        "first_day": (days[0] if days else None),
        "last_day": (days[-1] if days else None),
        "open_positions": int(open_positions),
        "resolved_positions": int(resolved_positions),
        "total_realized_pnl_usd": float(total_pnl),
        "total_realized_cost_basis_usd": float(total_cost),
        "total_realized_return_pct": float(total_ret) if total_ret is not None else None,
        "total_resolved_trades": int(total_trades),
        "rolling_30d": {
            "start_day": w_start.isoformat(),
            "end_day": w_end.isoformat(),
            "observed_days_with_resolutions": int(roll_days),
            "resolved_trades": int(roll_trades),
            "realized_pnl_usd": float(roll_pnl),
            "realized_cost_basis_usd": float(roll_cost),
            "return_pct": float(roll_ret) if roll_ret is not None else None,
        },
    }


def write_monthly_txt(path: Path, metrics: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    roll = metrics.get("rolling_30d") if isinstance(metrics.get("rolling_30d"), dict) else {}
    ret = as_float(roll.get("return_pct"), None)
    if ret is None:
        line = "monthly_return_pct_rolling_30d=n/a"
    else:
        line = f"monthly_return_pct_rolling_30d={ret:+.4%}"
    path.write_text(line + "\n", encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description="Record no-longshot forward realized monthly return (observe-only).")
    p.add_argument("--screen-csv", default="", help="Screen CSV path (default logs/no_longshot_daily_screen.csv)")
    p.add_argument("--positions-json", default="", help="Forward ledger JSON path (default logs/no_longshot_forward_positions.json)")
    p.add_argument("--out-daily-jsonl", default="", help="Daily realized JSONL path (default logs/no_longshot_realized_daily.jsonl)")
    p.add_argument("--out-latest-json", default="", help="Latest summary JSON path (default logs/no_longshot_realized_latest.json)")
    p.add_argument("--out-monthly-txt", default="", help="Monthly return txt path (default logs/no_longshot_monthly_return_latest.txt)")
    p.add_argument("--entry-top-n", type=int, default=10, help="How many top screen rows to ingest per run")
    p.add_argument("--per-trade-cost", type=float, default=0.002, help="Per-trade cost used in paper realized calc")
    p.add_argument("--win-threshold", type=float, default=0.99, help="Resolution threshold for settled winner price")
    p.add_argument("--lose-threshold", type=float, default=0.01, help="Resolution threshold for settled loser price")
    p.add_argument("--api-timeout-sec", type=float, default=20.0, help="Gamma market fetch timeout seconds")
    p.add_argument("--pretty", action="store_true")
    args = p.parse_args()

    screen_csv = resolve_path(str(args.screen_csv), "no_longshot_daily_screen.csv")
    positions_json = resolve_path(str(args.positions_json), "no_longshot_forward_positions.json")
    out_daily_jsonl = resolve_path(str(args.out_daily_jsonl), "no_longshot_realized_daily.jsonl")
    out_latest_json = resolve_path(str(args.out_latest_json), "no_longshot_realized_latest.json")
    out_monthly_txt = resolve_path(str(args.out_monthly_txt), "no_longshot_monthly_return_latest.txt")

    raw_ledger = load_json(positions_json, {"positions": []})
    positions = raw_ledger.get("positions") if isinstance(raw_ledger, dict) else []
    if not isinstance(positions, list):
        positions = []

    screen_rows = load_screen_rows(screen_csv)
    added = ingest_entries(
        positions=positions,
        screen_rows=screen_rows,
        top_n=max(0, int(args.entry_top_n)),
        per_trade_cost=max(0.0, float(args.per_trade_cost)),
    )

    resolved_now = 0
    for pos in positions:
        if try_resolve_position(
            pos=pos,
            timeout_sec=max(1.0, float(args.api_timeout_sec)),
            win_threshold=max(0.5, min(1.0, float(args.win_threshold))),
            lose_threshold=max(0.0, min(0.5, float(args.lose_threshold))),
        ):
            resolved_now += 1

    positions.sort(key=lambda x: str(x.get("entry_utc") or ""))
    ledger_out = {
        "generated_utc": now_utc().isoformat(),
        "meta": {"observe_only": True, "source": "scripts/record_no_longshot_realized_daily.py"},
        "positions": positions,
    }
    write_json(positions_json, ledger_out, pretty=True)

    rows_by_day = aggregate_daily(positions)
    write_jsonl_sorted(out_daily_jsonl, rows_by_day)

    metrics = compute_metrics(rows_by_day=rows_by_day, positions=positions)
    summary = {
        "generated_utc": now_utc().isoformat(),
        "meta": {"observe_only": True, "source": "scripts/record_no_longshot_realized_daily.py"},
        "counts": {
            "screen_rows": len(screen_rows),
            "positions_added": int(added),
            "positions_resolved_now": int(resolved_now),
            "positions_total": len(positions),
        },
        "metrics": metrics,
        "artifacts": {
            "screen_csv": str(screen_csv),
            "positions_json": str(positions_json),
            "daily_jsonl": str(out_daily_jsonl),
            "latest_json": str(out_latest_json),
            "monthly_txt": str(out_monthly_txt),
        },
    }
    write_json(out_latest_json, summary, pretty=bool(args.pretty))
    write_monthly_txt(out_monthly_txt, metrics)

    roll = metrics.get("rolling_30d") if isinstance(metrics.get("rolling_30d"), dict) else {}
    roll_ret = as_float(roll.get("return_pct"), None)
    roll_txt = "n/a" if roll_ret is None else f"{roll_ret:+.4%}"
    print(
        f"[no-longshot-realized] added={added} resolved_now={resolved_now} "
        f"open={metrics.get('open_positions')} resolved_total={metrics.get('resolved_positions')}"
    )
    print(f"[no-longshot-realized] monthly_return_30d={roll_txt}")
    print(f"[no-longshot-realized] out_daily_jsonl={out_daily_jsonl}")
    print(f"[no-longshot-realized] out_latest_json={out_latest_json}")
    print(f"[no-longshot-realized] out_monthly_txt={out_monthly_txt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

