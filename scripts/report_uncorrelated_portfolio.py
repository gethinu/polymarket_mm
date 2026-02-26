#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_STRATEGY_IDS: Sequence[str] = (
    "weather_clob_arb_buckets_observe",
    "no_longshot_daily_observe",
    "link_intake_walletseed_cohort_observe",
    "gamma_eventpair_exec_edge_filter_observe",
    "hourly_updown_highprob_calibration_observe",
)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def today_yyyymmdd() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def logs_dir() -> Path:
    p = repo_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def docs_dir() -> Path:
    p = repo_root() / "docs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_path(raw: str, default_name: str, base: str = "logs") -> Path:
    s = str(raw or "").strip()
    if not s:
        if base == "docs":
            return docs_dir() / default_name
        return logs_dir() / default_name
    p = Path(s)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        if base == "docs":
            return docs_dir() / p.name
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
    if not path.exists():
        return
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


def _extract_day(value: str) -> str:
    s = str(value or "").strip()
    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        return s[:10]
    return ""


def _sample_variance(values: Sequence[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    m = sum(values) / float(len(values))
    return sum((x - m) * (x - m) for x in values) / float(len(values) - 1)


def _sample_std(values: Sequence[float]) -> Optional[float]:
    v = _sample_variance(values)
    if v is None or v < 0:
        return None
    return math.sqrt(v)


def _pearson_corr(a: Dict[str, float], b: Dict[str, float], min_overlap_days: int) -> Tuple[Optional[float], List[str], Optional[str]]:
    overlap = sorted(set(a.keys()) & set(b.keys()))
    if len(overlap) < int(min_overlap_days):
        return None, overlap, f"overlap_days<{int(min_overlap_days)}"
    xa = [a[d] for d in overlap]
    xb = [b[d] for d in overlap]
    ma = sum(xa) / float(len(xa))
    mb = sum(xb) / float(len(xb))
    va = sum((x - ma) * (x - ma) for x in xa)
    vb = sum((x - mb) * (x - mb) for x in xb)
    if va <= 0 or vb <= 0:
        return None, overlap, "zero_variance"
    cov = sum((xa[i] - ma) * (xb[i] - mb) for i in range(len(overlap)))
    return float(cov / math.sqrt(va * vb)), overlap, None


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(float(v) for v in values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _load_strategy_register(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _active_strategy_ids(strategy_register: dict) -> List[str]:
    out: List[str] = []
    block = strategy_register.get("strategy_register")
    entries = block.get("entries") if isinstance(block, dict) else None
    if not isinstance(entries, list):
        return []
    for row in entries:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").strip().upper() != "ADOPTED":
            continue
        sid = str(row.get("strategy_id") or "").strip()
        if sid:
            out.append(sid)
    return out


def _latest_eventpair_monthly_estimate_file(logs: Path) -> Optional[Path]:
    files = sorted(logs.glob("clob-arb-eventpair-monthly-estimate*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _extract_monthly_proxy(strategy_id: str, strategy_register: dict, logs: Path) -> Tuple[Optional[float], str]:
    if strategy_id == "no_longshot_daily_observe":
        block = strategy_register.get("no_longshot_status")
        ratio = _as_float(block.get("monthly_return_now_ratio")) if isinstance(block, dict) else None
        return ratio, "logs/strategy_register_latest.json:no_longshot_status.monthly_return_now_ratio"
    if strategy_id == "weather_clob_arb_buckets_observe":
        block = strategy_register.get("realized_monthly_return")
        ratio = _as_float(block.get("projected_monthly_return_ratio")) if isinstance(block, dict) else None
        return ratio, "logs/strategy_register_latest.json:realized_monthly_return.projected_monthly_return_ratio"
    if strategy_id == "gamma_eventpair_exec_edge_filter_observe":
        p = _latest_eventpair_monthly_estimate_file(logs)
        if p is None:
            return None, "missing:logs/clob-arb-eventpair-monthly-estimate*.json"
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None, f"parse_error:{p}"
        if not isinstance(obj, dict):
            return None, f"parse_error:{p}"
        ratio = _as_float(obj.get("recommended_monthly_return"))
        return ratio, str(p)
    return None, "not_available_in_logs"
