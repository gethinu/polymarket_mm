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


def _load_weather_realized_series(logs: Path) -> Tuple[Dict[str, float], dict]:
    path = logs / "strategy_realized_pnl_daily.jsonl"
    out: Dict[str, float] = {}
    for row in _iter_jsonl(path):
        if str(row.get("strategy_id") or "").strip() != "weather_clob_arb_buckets_observe":
            continue
        day = _extract_day(row.get("day") or "")
        pnl = _as_float(row.get("realized_pnl_usd"))
        bankroll = _as_float(row.get("bankroll_usd"))
        if not day or pnl is None or bankroll is None or bankroll <= 0:
            continue
        out[day] = pnl / bankroll
    vals = [out[d] for d in sorted(out.keys())]
    meta = {
        "available": bool(out),
        "source_file": str(path),
        "metric": "realized_pnl_usd / bankroll_usd",
        "days": len(out),
        "variance": _sample_variance(vals),
    }
    return out, meta


def _load_weather_proxy_series(logs: Path) -> Tuple[Dict[str, float], dict]:
    path = logs / "clob-arb-weather-observe-24h.log"
    out_rows: Dict[str, List[float]] = {}
    pat = re.compile(
        r"^\[(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}\]\s+summary\(30s\):.*?EDGE\s+\$[-+0-9.]+\s+\(([-+0-9.]+)%\)"
    )
    if path.exists():
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = pat.search(line)
                if not m:
                    continue
                day = m.group(1)
                edge_pct = _as_float(m.group(2))
                if edge_pct is None:
                    continue
                out_rows.setdefault(day, []).append(edge_pct / 100.0)
    out: Dict[str, float] = {}
    for day, vals in out_rows.items():
        med = _median(vals)
        if med is not None:
            out[day] = med
    vals = [out[d] for d in sorted(out.keys())]
    meta = {
        "available": bool(out),
        "source_file": str(path),
        "metric": "daily_median_edge_pct_from_summary_lines",
        "days": len(out),
        "variance": _sample_variance(vals),
    }
    return out, meta


def _load_no_longshot_realized_series(logs: Path) -> Tuple[Dict[str, float], dict]:
    path = logs / "no_longshot_realized_daily.jsonl"
    out: Dict[str, float] = {}
    for row in _iter_jsonl(path):
        day = _extract_day(row.get("day") or "")
        ret = _as_float(row.get("realized_return_pct"))
        if not day or ret is None:
            continue
        out[day] = ret
    vals = [out[d] for d in sorted(out.keys())]
    meta = {
        "available": bool(out),
        "source_file": str(path),
        "metric": "realized_return_pct",
        "days": len(out),
        "variance": _sample_variance(vals),
    }
    return out, meta


def _load_no_longshot_proxy_series(logs: Path) -> Tuple[Dict[str, float], dict]:
    path = logs / "no_longshot_daily_samples.csv"
    sums: Dict[str, Tuple[float, float]] = {}
    if path.exists():
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                day = _extract_day(row.get("end_iso") or "")
                cost = _as_float(row.get("entry_yes_price"))
                pnl = _as_float(row.get("pnl_per_no_share"))
                if not day or cost is None or pnl is None or cost <= 0:
                    continue
                spnl, scost = sums.get(day, (0.0, 0.0))
                sums[day] = (spnl + pnl, scost + cost)
    out: Dict[str, float] = {}
    for day, (spnl, scost) in sums.items():
        if scost > 0:
            out[day] = spnl / scost
    vals = [out[d] for d in sorted(out.keys())]
    meta = {
        "available": bool(out),
        "source_file": str(path),
        "metric": "daily_cost_weighted_pnl_per_no_share_over_entry_yes_price",
        "days": len(out),
        "variance": _sample_variance(vals),
    }
    return out, meta


def _load_gamma_proxy_series(logs: Path) -> Tuple[Dict[str, float], dict]:
    files = sorted(logs.glob("clob-arb-monitor-metrics-eventpair*.jsonl"))
    bucket: Dict[str, List[float]] = {}
    for p in files:
        for row in _iter_jsonl(p):
            if str(row.get("strategy") or "").strip() != "event-yes":
                continue
            if not bool(row.get("passes_exec_threshold")):
                continue
            day = _extract_day(row.get("ts") or "")
            edge = _as_float(row.get("edge_pct_exec_est"))
            if not day or edge is None:
                continue
            bucket.setdefault(day, []).append(edge)
    out: Dict[str, float] = {}
    for day, vals in bucket.items():
        if vals:
            out[day] = sum(vals) / float(len(vals))
    vals = [out[d] for d in sorted(out.keys())]
    meta = {
        "available": bool(out),
        "source_files": [str(p) for p in files],
        "metric": "daily_mean_edge_pct_exec_est_for_event_yes_passes_exec_threshold",
        "days": len(out),
        "variance": _sample_variance(vals),
    }
    return out, meta


def _load_hourly_proxy_series(logs: Path) -> Tuple[Dict[str, float], dict]:
    path = logs / "hourly_updown_highprob_calibration_168h_tte45_70_95_btc_eth_sol_xrp.json"
    sums: Dict[str, Tuple[float, float]] = {}
    if path.exists():
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            obj = {}
        samples = obj.get("samples") if isinstance(obj, dict) else None
        if isinstance(samples, list):
            for row in samples:
                if not isinstance(row, dict):
                    continue
                if not bool(row.get("qualified_price_band")):
                    continue
                day = _extract_day(row.get("end_date") or "")
                entry_price = _as_float(row.get("entry_price"))
                if not day or entry_price is None or entry_price <= 0:
                    continue
                pnl = (1.0 - entry_price) if bool(row.get("is_winner")) else (-entry_price)
                spnl, scost = sums.get(day, (0.0, 0.0))
                sums[day] = (spnl + pnl, scost + entry_price)
    out: Dict[str, float] = {}
    for day, (spnl, scost) in sums.items():
        if scost > 0:
            out[day] = spnl / scost
    vals = [out[d] for d in sorted(out.keys())]
    meta = {
        "available": bool(out),
        "source_file": str(path),
        "metric": "daily_cost_weighted_realized_return_from_qualified_samples",
        "days": len(out),
        "variance": _sample_variance(vals),
    }
    return out, meta


def _load_link_intake_proxy_series(logs: Path) -> Tuple[Dict[str, float], dict]:
    files = sorted(logs.glob("linkseed_7links_link_intake_summary_*.json"))
    rows: Dict[str, List[float]] = {}
    for p in files:
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta = obj.get("meta") if isinstance(obj, dict) else None
        stats = obj.get("stats") if isinstance(obj, dict) else None
        day = _extract_day(meta.get("generated_at_utc")) if isinstance(meta, dict) else ""
        ok = stats.get("cohort_ok") if isinstance(stats, dict) else None
        if not day or not isinstance(ok, bool):
            continue
        rows.setdefault(day, []).append(1.0 if ok else 0.0)
    out: Dict[str, float] = {}
    for day, vals in rows.items():
        if vals:
            out[day] = sum(vals) / float(len(vals))
    vals = [out[d] for d in sorted(out.keys())]
    meta = {
        "available": bool(out),
        "source_files": [str(p) for p in files],
        "metric": "daily_mean_cohort_ok_score_1_or_0",
        "days": len(out),
        "variance": _sample_variance(vals),
    }
    return out, meta
