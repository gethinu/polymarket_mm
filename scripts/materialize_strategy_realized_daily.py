#!/usr/bin/env python3
"""
Materialize strategy-scoped realized daily PnL series (observe-only).

Primary use:
- consume account-level realized snapshots (e.g. logs/clob_arb_realized_daily.jsonl)
- convert cumulative realized snapshots into day-over-day deltas
- write/update strategy-scoped daily ledger:
  - logs/strategy_realized_pnl_daily.jsonl
  - logs/strategy_realized_latest.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


DEFAULT_STRATEGY_ID = "weather_clob_arb_buckets_observe"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def logs_dir() -> Path:
    p = repo_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_path(raw: str, default_name: str) -> Path:
    s = str(raw or "").strip()
    if not s:
        return logs_dir() / default_name
    p = Path(s)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs_dir() / p.name
    return repo_root() / p


def _as_float(v) -> Optional[float]:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n):
        return None
    return n


def _iter_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def _extract_day_key(row: dict) -> str:
    day = str(row.get("day") or row.get("date") or "").strip()
    if len(day) >= 10 and day[4:5] == "-" and day[7:8] == "-":
        return day[:10]
    ts = str(row.get("ts") or row.get("generated_utc") or row.get("captured_utc") or "").strip()
    if len(ts) >= 10 and ts[4:5] == "-" and ts[7:8] == "-":
        return ts[:10]
    return ""


def _extract_realized_value(row: dict) -> Optional[float]:
    for key in ("realized_pnl_usd", "pnl_realized_usd", "realized_pnl", "pnl_realized", "realized"):
        if key in row:
            n = _as_float(row.get(key))
            if n is not None:
                return n
    return None


def _extract_balance_value(row: dict) -> Optional[float]:
    for key in ("balance_usdc", "balance_usd", "bankroll_usd"):
        if key in row:
            n = _as_float(row.get(key))
            if n is not None and n > 0:
                return n
    return None


def _infer_series_mode(raw_mode: str, source_path: Path, rows: List[dict]) -> str:
    mode = str(raw_mode or "auto").strip().lower()
    if mode in {"cumulative_snapshot", "daily_realized"}:
        return mode

    name = source_path.name.lower()
    if "clob_arb_realized_daily" in name:
        return "cumulative_snapshot"

    for r in rows:
        src = str(r.get("source") or "").strip().lower()
        if src.endswith("record_simmer_realized_daily.py"):
            return "cumulative_snapshot"
    return "daily_realized"


def _load_source_rows_by_day(path: Path) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not path.exists():
        return out
    for row in _iter_jsonl(path):
        day = _extract_day_key(row)
        if not day:
            continue
        val = _extract_realized_value(row)
        if val is None:
            continue
        out[day] = row
    return out


def _build_strategy_rows(
    source_rows_by_day: Dict[str, dict],
    strategy_id: str,
    allocation_ratio: float,
    source_path: Path,
    series_mode: str,
    first_day_delta_zero: bool,
) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not source_rows_by_day:
        return out

    days = sorted(source_rows_by_day.keys())
    prev_val: Optional[float] = None
    captured_utc = now_utc().isoformat()

    for day in days:
        row = source_rows_by_day[day]
        cur = _extract_realized_value(row)
        if cur is None:
            continue

        raw_daily = 0.0
        if series_mode == "cumulative_snapshot":
            if prev_val is None:
                raw_daily = 0.0 if first_day_delta_zero else float(cur)
            else:
                raw_daily = float(cur) - float(prev_val)
            prev_val = float(cur)
        else:
            raw_daily = float(cur)

        alloc_daily = float(raw_daily) * float(allocation_ratio)
        bal_raw = _extract_balance_value(row)
        bal_alloc = (float(bal_raw) * float(allocation_ratio)) if bal_raw is not None else None

        out[day] = {
            "day": day,
            "strategy_id": strategy_id,
            "observe_only": True,
            "source": "materialize_strategy_realized_daily.py",
            "captured_utc": captured_utc,
            "source_path": str(source_path),
            "source_series_mode": series_mode,
            "allocation_ratio": float(allocation_ratio),
            "realized_pnl_usd": float(alloc_daily),
            "raw_realized_pnl_usd": float(raw_daily),
            "bankroll_usd": float(bal_alloc) if bal_alloc is not None else None,
            "raw_bankroll_usd": float(bal_raw) if bal_raw is not None else None,
        }

    return out


def _load_existing_out(path: Path) -> Dict[Tuple[str, str], dict]:
    out: Dict[Tuple[str, str], dict] = {}
    if not path.exists():
        return out
    for row in _iter_jsonl(path):
        sid = str(row.get("strategy_id") or "").strip()
        day = _extract_day_key(row)
        if not sid or not day:
            continue
        out[(sid, day)] = row
    return out


def _write_out_jsonl(path: Path, rows_map: Dict[Tuple[str, str], dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = sorted(rows_map.keys(), key=lambda x: (x[1], x[0]))
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for key in keys:
            f.write(json.dumps(rows_map[key], ensure_ascii=True) + "\n")


def _strategy_rows(rows_map: Dict[Tuple[str, str], dict], strategy_id: str) -> List[dict]:
    out: List[dict] = []
    for (sid, _day), row in rows_map.items():
        if sid == strategy_id:
            out.append(row)
    out.sort(key=lambda r: str(r.get("day") or ""))
    return out


def _fmt_ratio_pct(ratio: Optional[float], digits: int = 2) -> str:
    if ratio is None:
        return "n/a"
    return f"{ratio:+.{max(0, int(digits))}%}"


def _build_latest_summary(
    strategy_id: str,
    source_path: Path,
    source_series_mode: str,
    allocation_ratio: float,
    rows_for_strategy: List[dict],
) -> dict:
    day_rows = [r for r in rows_for_strategy if _as_float(r.get("realized_pnl_usd")) is not None]
    observed_days = len(day_rows)
    total_realized = float(sum(float(r.get("realized_pnl_usd") or 0.0) for r in day_rows)) if day_rows else 0.0
    mean_daily = (total_realized / observed_days) if observed_days > 0 else None

    trailing_days = min(30, observed_days)
    trailing_rows = day_rows[-trailing_days:] if trailing_days > 0 else []
    trailing_sum = float(sum(float(r.get("realized_pnl_usd") or 0.0) for r in trailing_rows)) if trailing_rows else 0.0

    latest_bankroll = None
    latest_bankroll_day = ""
    for r in reversed(day_rows):
        bal = _as_float(r.get("bankroll_usd"))
        if bal is not None and bal > 0:
            latest_bankroll = bal
            latest_bankroll_day = str(r.get("day") or "")
            break

    projected_monthly = None
    if latest_bankroll is not None and latest_bankroll > 0 and mean_daily is not None:
        daily_ret = float(mean_daily) / float(latest_bankroll)
        if daily_ret > -1.0:
            projected_monthly = (1.0 + daily_ret) ** 30.0 - 1.0

    rolling_30d = None
    if latest_bankroll is not None and latest_bankroll > 0 and observed_days >= 30:
        rolling_30d = float(trailing_sum) / float(latest_bankroll)

    return {
        "generated_utc": now_utc().isoformat(),
        "meta": {"observe_only": True, "source": "scripts/materialize_strategy_realized_daily.py"},
        "inputs": {
            "strategy_id": strategy_id,
            "source_jsonl": str(source_path),
            "source_series_mode": source_series_mode,
            "allocation_ratio": float(allocation_ratio),
        },
        "latest_row": (day_rows[-1] if day_rows else None),
        "metrics": {
            "observed_days": observed_days,
            "total_realized_pnl_usd": float(total_realized),
            "mean_daily_realized_pnl_usd": (float(mean_daily) if mean_daily is not None else None),
            "trailing_window_days": trailing_days,
            "trailing_window_realized_pnl_usd": float(trailing_sum),
            "latest_bankroll_usd": latest_bankroll,
            "latest_bankroll_day": latest_bankroll_day,
            "projected_monthly_return_ratio": (float(projected_monthly) if projected_monthly is not None else None),
            "projected_monthly_return_text": _fmt_ratio_pct(projected_monthly, digits=2),
            "rolling_30d_return_ratio": (float(rolling_30d) if rolling_30d is not None else None),
            "rolling_30d_return_text": _fmt_ratio_pct(rolling_30d, digits=2),
        },
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Materialize strategy-scoped realized daily PnL series (observe-only).")
    p.add_argument("--source-jsonl", default="", help="Source realized jsonl path (default logs/clob_arb_realized_daily.jsonl)")
    p.add_argument("--strategy-id", default=DEFAULT_STRATEGY_ID, help="Target strategy id")
    p.add_argument(
        "--allocation-ratio",
        type=float,
        default=1.0,
        help="Allocation ratio [0..1] applied to source daily realized values",
    )
    p.add_argument(
        "--source-series-mode",
        choices=("auto", "cumulative_snapshot", "daily_realized"),
        default="auto",
        help="Source series mode. auto infers from file/source markers.",
    )
    p.add_argument(
        "--first-day-delta-zero",
        action="store_true",
        help="When source is cumulative snapshot, first day delta is forced to 0 (default: true).",
    )
    p.add_argument(
        "--no-first-day-delta-zero",
        action="store_true",
        help="When source is cumulative snapshot, keep first day as-is (do not zero).",
    )
    p.add_argument(
        "--out-jsonl",
        default="",
        help="Strategy realized daily jsonl (default logs/strategy_realized_pnl_daily.jsonl)",
    )
    p.add_argument(
        "--out-latest-json",
        default="",
        help="Latest summary json (default logs/strategy_realized_latest.json)",
    )
    p.add_argument("--pretty", action="store_true", help="Pretty-print latest JSON")
    args = p.parse_args()

    strategy_id = str(args.strategy_id or "").strip()
    if not strategy_id:
        raise SystemExit("invalid --strategy-id")

    allocation_ratio = float(args.allocation_ratio)
    if not math.isfinite(allocation_ratio) or allocation_ratio < 0.0 or allocation_ratio > 1.0:
        raise SystemExit("invalid --allocation-ratio (expected 0.0..1.0)")

    source_path = resolve_path(str(args.source_jsonl), "clob_arb_realized_daily.jsonl")
    out_jsonl = resolve_path(str(args.out_jsonl), "strategy_realized_pnl_daily.jsonl")
    out_latest = resolve_path(str(args.out_latest_json), "strategy_realized_latest.json")

    if not source_path.exists():
        raise SystemExit(f"source jsonl not found: {source_path}")

    first_day_delta_zero = True
    if bool(args.no_first_day_delta_zero):
        first_day_delta_zero = False
    if bool(args.first_day_delta_zero):
        first_day_delta_zero = True

    source_rows_by_day = _load_source_rows_by_day(source_path)
    source_rows_sorted = [source_rows_by_day[d] for d in sorted(source_rows_by_day.keys())]
    source_mode = _infer_series_mode(str(args.source_series_mode), source_path, source_rows_sorted)

    new_rows = _build_strategy_rows(
        source_rows_by_day=source_rows_by_day,
        strategy_id=strategy_id,
        allocation_ratio=allocation_ratio,
        source_path=source_path,
        series_mode=source_mode,
        first_day_delta_zero=first_day_delta_zero,
    )

    rows_map = _load_existing_out(out_jsonl)
    for day, row in new_rows.items():
        rows_map[(strategy_id, day)] = row
    _write_out_jsonl(out_jsonl, rows_map)

    rows_for_strategy = _strategy_rows(rows_map, strategy_id)
    latest_summary = _build_latest_summary(
        strategy_id=strategy_id,
        source_path=source_path,
        source_series_mode=source_mode,
        allocation_ratio=allocation_ratio,
        rows_for_strategy=rows_for_strategy,
    )

    out_latest.parent.mkdir(parents=True, exist_ok=True)
    with out_latest.open("w", encoding="utf-8") as f:
        if bool(args.pretty):
            json.dump(latest_summary, f, ensure_ascii=False, indent=2)
        else:
            json.dump(latest_summary, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")

    metrics = latest_summary.get("metrics") if isinstance(latest_summary.get("metrics"), dict) else {}
    print(f"[strategy-realized] strategy_id={strategy_id} mode={source_mode} observed_days={metrics.get('observed_days', 0)}")
    print(f"[strategy-realized] monthly_now={metrics.get('projected_monthly_return_text', 'n/a')} roll30={metrics.get('rolling_30d_return_text', 'n/a')}")
    print(f"[strategy-realized] out_jsonl={out_jsonl}")
    print(f"[strategy-realized] out_latest_json={out_latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

