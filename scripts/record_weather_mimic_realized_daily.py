#!/usr/bin/env python3
"""
Observe-only forward realized-return tracker for weather mimic consensus watchlists.

This script does not place trades. It keeps a local paper-entry ledger from the
latest consensus watchlist, resolves entries when markets settle, and updates:

- logs/<profile_name>_forward_positions.json
- logs/<profile_name>_realized_daily.jsonl
- logs/<profile_name>_realized_latest.json
- logs/<profile_name>_monthly_return_latest.txt
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_PROFILE_NAME = "weather_7acct_auto"
DEFAULT_ASSUMED_BANKROLL_USD = 60.0
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
USER_AGENT = "Mozilla/5.0 (compatible; weather-mimic-realized/1.0)"


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
    if not math.isfinite(n):
        return default
    return n


def as_int(v, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return default


def as_str(v) -> str:
    return str(v or "").strip()


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
    s = as_str(value)
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
    mid = as_str(market_id)
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
    outcomes = [as_str(x).lower() for x in parse_list_field(market.get("outcomes"))]
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


def load_positions_ledger(path: Path) -> Tuple[List[dict], Optional[str]]:
    if not path.exists():
        return [], None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return [], f"positions ledger parse failed: {type(e).__name__}: {e}"
    if isinstance(raw, list):
        xs = raw
    elif isinstance(raw, dict):
        xs = raw.get("positions")
    else:
        return [], "positions ledger type invalid (expected object or list)"
    if not isinstance(xs, list):
        return [], "positions ledger missing list field: positions"
    out: List[dict] = []
    for i, row in enumerate(xs):
        if isinstance(row, dict):
            out.append(row)
        else:
            return [], f"positions ledger row[{i}] is not object"
    return out, None


def write_json(path: Path, obj, pretty: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        if pretty:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        else:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    tmp.replace(path)


def write_jsonl_sorted(path: Path, rows_by_day: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for day in sorted(rows_by_day.keys()):
            f.write(json.dumps(rows_by_day[day], ensure_ascii=True) + "\n")


def write_monthly_txt(path: Path, realized_monthly: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ratio = as_float(realized_monthly.get("rolling_30d_return_ratio"), None)
    if ratio is None:
        line = "monthly_return_pct_rolling_30d=n/a"
    else:
        line = f"monthly_return_pct_rolling_30d={ratio:+.4%}"
    path.write_text(line + "\n", encoding="utf-8")


def load_consensus_rows(path: Path) -> List[dict]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(raw, dict):
        rows = raw.get("top")
        if isinstance(rows, list):
            return [dict(x) for x in rows if isinstance(x, dict)]
        rows = raw.get("rows")
        if isinstance(rows, list):
            return [dict(x) for x in rows if isinstance(x, dict)]
    return []


def build_entry(row: dict, per_trade_cost: float, shares_per_entry: float) -> Optional[dict]:
    market_id = as_str(row.get("market_id"))
    if not market_id:
        return None
    side = as_str(row.get("side_hint")).lower() or "no"
    if side not in {"yes", "no"}:
        return None

    entry_price = as_float(row.get("entry_price"), None)
    yes_price = as_float(row.get("yes_price"), None)
    no_price = as_float(row.get("no_price"), None)
    if entry_price is None:
        entry_price = no_price if side == "no" else yes_price
    if entry_price is None or entry_price <= 0.0 or entry_price >= 1.0:
        return None

    shares = max(0.0, float(shares_per_entry))
    if shares <= 0.0:
        return None

    t = now_utc()
    cost_basis_per_share = float(entry_price) + float(per_trade_cost)
    return {
        "position_id": f"m{market_id}",
        "market_id": market_id,
        "question": as_str(row.get("question")),
        "side": side,
        "entry_utc": t.isoformat(),
        "entry_day": t.date().isoformat(),
        "entry_price": float(entry_price),
        "entry_yes_price": float(yes_price) if yes_price is not None else None,
        "entry_no_price": float(no_price) if no_price is not None else None,
        "entry_per_trade_cost": float(per_trade_cost),
        "shares": float(shares),
        "entry_cost_basis_per_share": float(cost_basis_per_share),
        "entry_cost_basis_usd": float(cost_basis_per_share * shares),
        "rank_at_entry": as_int(row.get("rank"), None),
        "score_total_at_entry": as_float(row.get("score_total"), None),
        "correlation_bucket": as_str(row.get("correlation_bucket")),
        "in_no_longshot": bool(row.get("in_no_longshot")),
        "in_lateprob": bool(row.get("in_lateprob")),
        "entry_max_profit": as_float(row.get("max_profit"), None),
        "entry_net_yield_per_day": as_float(row.get("net_yield_per_day"), None),
        "hours_to_end_at_entry": as_float(row.get("hours_to_end"), None),
        "end_iso_at_entry": as_str(row.get("end_iso")),
        "status": "open",
        "resolved_utc": None,
        "resolved_day": None,
        "resolution": None,
        "resolved_yes_price": None,
        "resolved_no_price": None,
        "resolved_end_iso": None,
        "winning_outcome": None,
        "payout_per_share": None,
        "payout_usd": None,
        "cost_basis_per_share": None,
        "cost_basis_usd": None,
        "pnl_per_share": None,
        "pnl_usd": None,
        "realized_return_pct": None,
    }


def ingest_entries(
    positions: List[dict],
    consensus_rows: List[dict],
    top_n: int,
    per_trade_cost: float,
    shares_per_entry: float,
) -> int:
    seen = set()
    for p in positions:
        mid = as_str(p.get("market_id"))
        if mid:
            seen.add(mid)

    added = 0
    for row in consensus_rows[: max(0, int(top_n))]:
        mid = as_str(row.get("market_id"))
        if not mid or mid in seen:
            continue
        entry = build_entry(row=row, per_trade_cost=per_trade_cost, shares_per_entry=shares_per_entry)
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
    if as_str(pos.get("status")) != "open":
        return False

    market = fetch_market_by_id(as_str(pos.get("market_id")), timeout_sec=timeout_sec)
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

    side = as_str(pos.get("side")).lower() or "no"
    shares = as_float(pos.get("shares"), 1.0) or 1.0
    entry_price = as_float(pos.get("entry_price"), None)
    per_trade_cost = as_float(pos.get("entry_per_trade_cost"), 0.0) or 0.0
    if entry_price is None:
        return False

    payout_per_share = 1.0 if ((side == "no" and no_wins) or (side == "yes" and yes_wins)) else 0.0
    cost_basis_per_share = float(entry_price) + float(per_trade_cost)
    payout_usd = float(payout_per_share * shares)
    cost_basis_usd = float(cost_basis_per_share * shares)
    pnl_per_share = float(payout_per_share - cost_basis_per_share)
    pnl_usd = float(pnl_per_share * shares)
    ret = (pnl_usd / cost_basis_usd) if cost_basis_usd > 1e-12 else None

    now = now_utc()
    resolved_day = day_from_iso(as_str(market.get("endDate"))) or now.date().isoformat()
    winning_outcome = "NO" if no_wins else "YES"

    pos["status"] = "resolved"
    pos["resolved_utc"] = now.isoformat()
    pos["resolved_day"] = resolved_day
    pos["resolution"] = "WIN" if payout_per_share > 0.0 else "LOSE"
    pos["resolved_yes_price"] = float(yes_price)
    pos["resolved_no_price"] = float(no_price)
    pos["resolved_end_iso"] = as_str(market.get("endDate"))
    pos["winning_outcome"] = winning_outcome
    pos["payout_per_share"] = float(payout_per_share)
    pos["payout_usd"] = float(payout_usd)
    pos["cost_basis_per_share"] = float(cost_basis_per_share)
    pos["cost_basis_usd"] = float(cost_basis_usd)
    pos["pnl_per_share"] = float(pnl_per_share)
    pos["pnl_usd"] = float(pnl_usd)
    pos["realized_return_pct"] = float(ret) if ret is not None else None
    return True


def aggregate_daily(
    positions: List[dict],
    profile_name: str,
    strategy_id: str,
    assumed_bankroll_usd: float,
) -> Dict[str, dict]:
    by_day: Dict[str, dict] = {}
    for p in positions:
        if as_str(p.get("status")) != "resolved":
            continue
        day = as_str(p.get("resolved_day"))
        pnl = as_float(p.get("pnl_usd"), None)
        cost = as_float(p.get("cost_basis_usd"), None)
        if not day or pnl is None or cost is None:
            continue
        rec = by_day.setdefault(
            day,
            {
                "day": day,
                "strategy_id": strategy_id,
                "profile_name": profile_name,
                "observe_only": True,
                "source": "record_weather_mimic_realized_daily.py",
                "realized_pnl_usd": 0.0,
                "realized_cost_basis_usd": 0.0,
                "resolved_trades": 0,
                "bankroll_usd": float(assumed_bankroll_usd),
            },
        )
        rec["realized_pnl_usd"] = float(rec["realized_pnl_usd"]) + float(pnl)
        rec["realized_cost_basis_usd"] = float(rec["realized_cost_basis_usd"]) + float(cost)
        rec["resolved_trades"] = int(rec["resolved_trades"]) + 1

    for rec in by_day.values():
        pnl = float(rec.get("realized_pnl_usd") or 0.0)
        cost = float(rec.get("realized_cost_basis_usd") or 0.0)
        bankroll = as_float(rec.get("bankroll_usd"), None)
        rec["realized_return_pct"] = (pnl / cost) if cost > 1e-12 else None
        rec["bankroll_return_pct"] = (pnl / bankroll) if bankroll is not None and bankroll > 1e-12 else None
    return by_day


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
    roll_bankroll = None
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
        bankroll = as_float(rec.get("bankroll_usd"), None)
        if bankroll is not None and bankroll > 0:
            roll_bankroll = bankroll
    roll_ret = (roll_pnl / roll_cost) if roll_cost > 1e-12 else None
    roll_bankroll_ret = (roll_pnl / roll_bankroll) if roll_bankroll is not None and roll_bankroll > 1e-12 else None

    open_positions = sum(1 for p in positions if as_str(p.get("status")) == "open")
    resolved_positions = sum(1 for p in positions if as_str(p.get("status")) == "resolved")

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
            "bankroll_usd": float(roll_bankroll) if roll_bankroll is not None else None,
            "bankroll_return_pct": float(roll_bankroll_ret) if roll_bankroll_ret is not None else None,
        },
    }


def _fmt_ratio_pct(ratio: Optional[float], digits: int = 2) -> str:
    if ratio is None:
        return "n/a"
    return f"{ratio:+.{max(0, int(digits))}%}"


def _gate_stage_label_ja(stage: str, min_days: int) -> str:
    s = as_str(stage).upper()
    d = max(1, int(min_days))
    if s == "TENTATIVE":
        return f"{d}日暫定"
    if s == "INTERIM":
        return f"{d}日中間"
    if s == "FINAL":
        return f"{d}日確定"
    return "-"


def _gate_decision_label_ja(decision: str, tentative_days: int, interim_days: int, final_days: int) -> str:
    s = as_str(decision).upper()
    if s == "PENDING_TENTATIVE":
        return f"{tentative_days}日暫定判定待ち"
    if s == "READY_TENTATIVE":
        return f"{tentative_days}日暫定判定"
    if s == "READY_INTERIM":
        return f"{interim_days}日中間判定"
    if s == "READY_FINAL":
        return f"{final_days}日確定判定"
    return "-"


def build_realized_30d_gate(rows_by_day: Dict[str, dict], strategy_id: str, min_days: int) -> dict:
    observed_days = len(rows_by_day)
    final_days = max(1, int(min_days))
    tentative_days = min(7, final_days)
    interim_days = min(14, final_days)
    if interim_days < tentative_days:
        interim_days = tentative_days

    decision = "READY_FOR_JUDGMENT" if observed_days >= final_days else "PENDING_30D"
    reason = (
        f"observed_realized_days={observed_days} >= min_days={final_days}"
        if decision == "READY_FOR_JUDGMENT"
        else f"observed_realized_days={observed_days} < min_days={final_days}"
    )

    decision_3stage = "PENDING_TENTATIVE"
    stage_label = f"PRE_TENTATIVE_{tentative_days}D"
    stage_label_ja = f"{tentative_days}日暫定到達前"
    reason_3stage = f"observed_realized_days={observed_days} < tentative_days={tentative_days}"
    next_stage: Optional[dict] = {
        "stage": "TENTATIVE",
        "label": f"{tentative_days}d tentative",
        "label_ja": _gate_stage_label_ja("TENTATIVE", tentative_days),
        "min_days": int(tentative_days),
        "remaining_days": int(max(0, tentative_days - observed_days)),
    }

    if observed_days >= tentative_days:
        decision_3stage = "READY_TENTATIVE"
        stage_label = f"TENTATIVE_{tentative_days}D"
        stage_label_ja = _gate_stage_label_ja("TENTATIVE", tentative_days)
        reason_3stage = (
            f"observed_realized_days={observed_days} >= tentative_days={tentative_days}"
            f" and < interim_days={interim_days}"
        )
        next_stage = {
            "stage": "INTERIM",
            "label": f"{interim_days}d interim",
            "label_ja": _gate_stage_label_ja("INTERIM", interim_days),
            "min_days": int(interim_days),
            "remaining_days": int(max(0, interim_days - observed_days)),
        }

    if observed_days >= interim_days:
        decision_3stage = "READY_INTERIM"
        stage_label = f"INTERIM_{interim_days}D"
        stage_label_ja = _gate_stage_label_ja("INTERIM", interim_days)
        reason_3stage = (
            f"observed_realized_days={observed_days} >= interim_days={interim_days}"
            f" and < final_days={final_days}"
        )
        next_stage = {
            "stage": "FINAL",
            "label": f"{final_days}d final",
            "label_ja": _gate_stage_label_ja("FINAL", final_days),
            "min_days": int(final_days),
            "remaining_days": int(max(0, final_days - observed_days)),
        }

    if observed_days >= final_days:
        decision_3stage = "READY_FINAL"
        stage_label = f"FINAL_{final_days}D"
        stage_label_ja = _gate_stage_label_ja("FINAL", final_days)
        reason_3stage = f"observed_realized_days={observed_days} >= final_days={final_days}"
        next_stage = None

    stages = [
        {
            "stage": "TENTATIVE",
            "label": f"{tentative_days}d tentative",
            "label_ja": _gate_stage_label_ja("TENTATIVE", tentative_days),
            "min_days": int(tentative_days),
            "reached": bool(observed_days >= tentative_days),
        },
        {
            "stage": "INTERIM",
            "label": f"{interim_days}d interim",
            "label_ja": _gate_stage_label_ja("INTERIM", interim_days),
            "min_days": int(interim_days),
            "reached": bool(observed_days >= interim_days),
        },
        {
            "stage": "FINAL",
            "label": f"{final_days}d final",
            "label_ja": _gate_stage_label_ja("FINAL", final_days),
            "min_days": int(final_days),
            "reached": bool(observed_days >= final_days),
        },
    ]

    total_realized = 0.0
    for row in rows_by_day.values():
        total_realized += float(row.get("realized_pnl_usd") or 0.0)

    return {
        "decision": decision,
        "decision_3stage": decision_3stage,
        "decision_3stage_label_ja": _gate_decision_label_ja(
            decision_3stage, tentative_days, interim_days, final_days
        ),
        "stage_label": stage_label,
        "stage_label_ja": stage_label_ja,
        "reason": reason,
        "reason_3stage": reason_3stage,
        "next_stage": next_stage,
        "stages": stages,
        "stage_thresholds_days": {
            "tentative": int(tentative_days),
            "interim": int(interim_days),
            "final": int(final_days),
        },
        "strategy_id": strategy_id,
        "min_realized_days": int(final_days),
        "observed_realized_days": int(observed_days),
        "observed_total_realized_pnl_usd": float(total_realized),
        "series_mode": "daily_realized",
        "source_files": [],
        "source_modes": {},
    }


def summarize_realized_monthly_return(
    rows_by_day: Dict[str, dict],
    strategy_id: str,
    min_days: int,
) -> dict:
    day_keys = sorted(rows_by_day.keys())
    observed_days = len(day_keys)
    total_realized = 0.0
    trailing_sum = 0.0
    trailing_days = min(30, observed_days)
    latest_day = day_keys[-1] if day_keys else ""
    latest_daily = None
    bankroll = None

    for day in day_keys:
        rec = rows_by_day[day]
        pnl = float(rec.get("realized_pnl_usd") or 0.0)
        total_realized += pnl
        bankroll = as_float(rec.get("bankroll_usd"), bankroll)
    if latest_day:
        latest_daily = float(rows_by_day[latest_day].get("realized_pnl_usd") or 0.0)
    for day in day_keys[-trailing_days:]:
        trailing_sum += float(rows_by_day[day].get("realized_pnl_usd") or 0.0)

    mean_daily = (total_realized / observed_days) if observed_days > 0 else None
    trailing_window_return = (
        trailing_sum / bankroll if bankroll is not None and bankroll > 0 and trailing_days > 0 else None
    )
    rolling_30d_return = (
        trailing_sum / bankroll if bankroll is not None and bankroll > 0 and observed_days >= 30 else None
    )

    max_drawdown_30d = None
    if bankroll is not None and bankroll > 0 and trailing_days > 0:
        eq = 1.0
        peak = 1.0
        worst_dd = 0.0
        for day in day_keys[-trailing_days:]:
            daily_ret = float(rows_by_day[day].get("realized_pnl_usd") or 0.0) / float(bankroll)
            if daily_ret <= -1.0:
                eq = 0.0
            else:
                eq = eq * (1.0 + daily_ret)
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (eq / peak) - 1.0
                if dd < worst_dd:
                    worst_dd = dd
        max_drawdown_30d = float(worst_dd)

    projected_monthly = None
    if bankroll is not None and bankroll > 0 and mean_daily is not None:
        daily_ret = mean_daily / bankroll
        if daily_ret > -1.0:
            projected_monthly = (1.0 + daily_ret) ** 30.0 - 1.0

    decision = "READY_FOR_JUDGMENT" if observed_days >= int(min_days) else "INSUFFICIENT_DATA"
    if observed_days < int(min_days):
        reason = f"observed_realized_days={observed_days} < min_days={int(min_days)}"
    elif bankroll is None or bankroll <= 0:
        reason = "bankroll is unavailable; return ratio cannot be computed"
    else:
        reason = f"observed_realized_days={observed_days} >= min_days={int(min_days)}"

    return {
        "decision": decision,
        "reason": reason,
        "strategy_id": strategy_id,
        "min_realized_days": int(min_days),
        "observed_realized_days": int(observed_days),
        "series_mode": "daily_realized",
        "source_files": [],
        "source_modes": {},
        "bankroll_usd": float(bankroll) if bankroll is not None else None,
        "bankroll_source": "assumed_bankroll_usd" if bankroll is not None else "",
        "total_realized_pnl_usd": float(total_realized),
        "latest_day": latest_day,
        "daily_realized_pnl_usd_latest": float(latest_daily) if latest_daily is not None else None,
        "mean_daily_realized_pnl_usd": float(mean_daily) if mean_daily is not None else None,
        "trailing_window_days": int(trailing_days),
        "trailing_window_realized_pnl_usd": float(trailing_sum),
        "projected_monthly_return_ratio": float(projected_monthly) if projected_monthly is not None else None,
        "projected_monthly_return_text": _fmt_ratio_pct(projected_monthly, digits=2),
        "trailing_window_return_ratio": float(trailing_window_return) if trailing_window_return is not None else None,
        "trailing_window_return_text": _fmt_ratio_pct(trailing_window_return, digits=2),
        "rolling_30d_return_ratio": float(rolling_30d_return) if rolling_30d_return is not None else None,
        "rolling_30d_return_text": _fmt_ratio_pct(rolling_30d_return, digits=2),
        "max_drawdown_30d_ratio": float(max_drawdown_30d) if max_drawdown_30d is not None else None,
        "max_drawdown_30d_text": _fmt_ratio_pct(max_drawdown_30d, digits=2),
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="Record weather mimic forward realized monthly return (observe-only)."
    )
    p.add_argument("--profile-name", default=DEFAULT_PROFILE_NAME, help="Weather mimic profile name")
    p.add_argument(
        "--strategy-id",
        default="",
        help="Strategy id used for downstream materialization (default: profile name)",
    )
    p.add_argument(
        "--consensus-json",
        default="",
        help="Consensus watchlist JSON path (default logs/<profile>_consensus_watchlist_latest.json)",
    )
    p.add_argument(
        "--positions-json",
        default="",
        help="Forward ledger JSON path (default logs/<profile>_forward_positions.json)",
    )
    p.add_argument(
        "--out-daily-jsonl",
        default="",
        help="Daily realized JSONL path (default logs/<profile>_realized_daily.jsonl)",
    )
    p.add_argument(
        "--out-latest-json",
        default="",
        help="Latest summary JSON path (default logs/<profile>_realized_latest.json)",
    )
    p.add_argument(
        "--out-monthly-txt",
        default="",
        help="Monthly return txt path (default logs/<profile>_monthly_return_latest.txt)",
    )
    p.add_argument("--entry-top-n", type=int, default=30, help="How many top watchlist rows to ingest per run")
    p.add_argument("--shares-per-entry", type=float, default=1.0, help="Paper shares per entry")
    p.add_argument("--per-trade-cost", type=float, default=0.002, help="Per-trade cost used in paper realized calc")
    p.add_argument(
        "--assumed-bankroll-usd",
        type=float,
        default=DEFAULT_ASSUMED_BANKROLL_USD,
        help="Assumed bankroll for return and drawdown summaries",
    )
    p.add_argument("--min-realized-days", type=int, default=30, help="Final realized gate threshold days")
    p.add_argument("--win-threshold", type=float, default=0.99, help="Resolution threshold for settled winner price")
    p.add_argument("--lose-threshold", type=float, default=0.01, help="Resolution threshold for settled loser price")
    p.add_argument("--api-timeout-sec", type=float, default=20.0, help="Gamma market fetch timeout seconds")
    p.add_argument("--pretty", action="store_true")
    args = p.parse_args()

    profile_name = as_str(args.profile_name) or DEFAULT_PROFILE_NAME
    strategy_id = as_str(args.strategy_id) or profile_name

    consensus_json = resolve_path(str(args.consensus_json), f"{profile_name}_consensus_watchlist_latest.json")
    positions_json = resolve_path(str(args.positions_json), f"{profile_name}_forward_positions.json")
    out_daily_jsonl = resolve_path(str(args.out_daily_jsonl), f"{profile_name}_realized_daily.jsonl")
    out_latest_json = resolve_path(str(args.out_latest_json), f"{profile_name}_realized_latest.json")
    out_monthly_txt = resolve_path(str(args.out_monthly_txt), f"{profile_name}_monthly_return_latest.txt")

    if int(args.entry_top_n) > 0 and not consensus_json.exists():
        print(f"[weather-mimic-realized] error: consensus json not found: {consensus_json}")
        return 2

    positions, ledger_err = load_positions_ledger(positions_json)
    if ledger_err:
        print(f"[weather-mimic-realized] error: {ledger_err}")
        return 2

    consensus_rows = load_consensus_rows(consensus_json)
    added = ingest_entries(
        positions=positions,
        consensus_rows=consensus_rows,
        top_n=max(0, int(args.entry_top_n)),
        per_trade_cost=max(0.0, float(args.per_trade_cost)),
        shares_per_entry=max(0.0, float(args.shares_per_entry)),
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

    positions.sort(key=lambda x: as_str(x.get("entry_utc")))
    ledger_out = {
        "generated_utc": now_utc().isoformat(),
        "meta": {
            "observe_only": True,
            "source": "scripts/record_weather_mimic_realized_daily.py",
            "profile_name": profile_name,
            "strategy_id": strategy_id,
        },
        "positions": positions,
    }
    write_json(positions_json, ledger_out, pretty=True)

    rows_by_day = aggregate_daily(
        positions=positions,
        profile_name=profile_name,
        strategy_id=strategy_id,
        assumed_bankroll_usd=max(0.0, float(args.assumed_bankroll_usd)),
    )
    write_jsonl_sorted(out_daily_jsonl, rows_by_day)

    metrics = compute_metrics(rows_by_day=rows_by_day, positions=positions)
    realized_gate = build_realized_30d_gate(
        rows_by_day=rows_by_day,
        strategy_id=strategy_id,
        min_days=max(1, int(args.min_realized_days)),
    )
    realized_monthly = summarize_realized_monthly_return(
        rows_by_day=rows_by_day,
        strategy_id=strategy_id,
        min_days=max(1, int(args.min_realized_days)),
    )
    summary = {
        "generated_utc": now_utc().isoformat(),
        "meta": {
            "observe_only": True,
            "source": "scripts/record_weather_mimic_realized_daily.py",
            "profile_name": profile_name,
            "strategy_id": strategy_id,
        },
        "counts": {
            "consensus_rows": len(consensus_rows),
            "positions_added": int(added),
            "positions_resolved_now": int(resolved_now),
            "positions_total": len(positions),
        },
        "metrics": metrics,
        "realized_30d_gate": realized_gate,
        "realized_monthly_return": realized_monthly,
        "artifacts": {
            "consensus_json": str(consensus_json),
            "positions_json": str(positions_json),
            "daily_jsonl": str(out_daily_jsonl),
            "latest_json": str(out_latest_json),
            "monthly_txt": str(out_monthly_txt),
        },
    }
    write_json(out_latest_json, summary, pretty=bool(args.pretty))
    write_monthly_txt(out_monthly_txt, realized_monthly)

    roll = metrics.get("rolling_30d") if isinstance(metrics.get("rolling_30d"), dict) else {}
    roll_ret = as_float(roll.get("bankroll_return_pct"), None)
    roll_txt = "n/a" if roll_ret is None else f"{roll_ret:+.4%}"
    print(
        f"[weather-mimic-realized] profile={profile_name} added={added} resolved_now={resolved_now} "
        f"open={metrics.get('open_positions')} resolved_total={metrics.get('resolved_positions')}"
    )
    print(
        f"[weather-mimic-realized] gate={realized_gate.get('decision_3stage')} "
        f"observed_days={realized_gate.get('observed_realized_days')}"
    )
    print(f"[weather-mimic-realized] monthly_return_30d={roll_txt}")
    print(f"[weather-mimic-realized] out_daily_jsonl={out_daily_jsonl}")
    print(f"[weather-mimic-realized] out_latest_json={out_latest_json}")
    print(f"[weather-mimic-realized] out_monthly_txt={out_monthly_txt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
