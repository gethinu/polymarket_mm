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
        ratio = None
        for key in (
            "recommended_monthly_return",
            "weighted_monthly_trim_edgepct_le_25",
            "weighted_monthly_capped20_trim_edgepct_le_25",
            "weighted_monthly_all_events",
            "weighted_monthly_capped20_all_events",
        ):
            ratio = _as_float(obj.get(key))
            if ratio is not None:
                break
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


def _build_strategy_records(
    strategy_ids: Sequence[str],
    logs: Path,
    strategy_register: dict,
    min_realized_days_for_corr: int,
) -> Tuple[List[dict], Dict[str, Dict[str, float]], Dict[str, List[str]]]:
    out: List[dict] = []
    correlation_series: Dict[str, Dict[str, float]] = {}
    missing: Dict[str, List[str]] = {}

    weather_realized, weather_realized_meta = _load_weather_realized_series(logs)
    weather_proxy, weather_proxy_meta = _load_weather_proxy_series(logs)

    no_realized, no_realized_meta = _load_no_longshot_realized_series(logs)
    no_proxy, no_proxy_meta = _load_no_longshot_proxy_series(logs)

    gamma_proxy, gamma_proxy_meta = _load_gamma_proxy_series(logs)
    hourly_proxy, hourly_proxy_meta = _load_hourly_proxy_series(logs)
    link_proxy, link_proxy_meta = _load_link_intake_proxy_series(logs)

    for sid in strategy_ids:
        monthly_proxy, monthly_source = _extract_monthly_proxy(sid, strategy_register, logs)
        rec = {
            "strategy_id": sid,
            "monthly_return_proxy_ratio": monthly_proxy,
            "monthly_return_proxy_source": monthly_source,
            "realized_daily": {"available": False, "days": 0},
            "observe_proxy_daily": {"available": False, "days": 0},
            "correlation_series_mode": "none",
            "correlation_series_days": 0,
        }
        miss: List[str] = []
        chosen: Dict[str, float] = {}

        if sid == "weather_clob_arb_buckets_observe":
            rec["realized_daily"] = weather_realized_meta
            rec["observe_proxy_daily"] = weather_proxy_meta
            use_realized = (
                bool(weather_realized_meta.get("available"))
                and int(weather_realized_meta.get("days") or 0) >= int(min_realized_days_for_corr)
                and weather_realized_meta.get("variance") not in (None, 0.0)
            )
            if use_realized:
                chosen = weather_realized
                rec["correlation_series_mode"] = "realized_daily"
            elif weather_proxy_meta.get("available"):
                chosen = weather_proxy
                rec["correlation_series_mode"] = "observe_proxy_daily"
                miss.append("realized daily return has zero variance or insufficient length; proxy used for correlation")
            else:
                miss.append("missing daily return series for correlation")
            if not weather_realized_meta.get("available"):
                miss.append("missing canonical realized daily series: logs/strategy_realized_pnl_daily.jsonl")

        elif sid == "no_longshot_daily_observe":
            rec["realized_daily"] = no_realized_meta
            rec["observe_proxy_daily"] = no_proxy_meta
            use_realized = (
                bool(no_realized_meta.get("available"))
                and int(no_realized_meta.get("days") or 0) >= int(min_realized_days_for_corr)
                and no_realized_meta.get("variance") not in (None, 0.0)
            )
            if use_realized:
                chosen = no_realized
                rec["correlation_series_mode"] = "realized_daily"
            elif no_proxy_meta.get("available"):
                chosen = no_proxy
                rec["correlation_series_mode"] = "observe_proxy_daily"
                miss.append("realized daily return series is short/low-variance; proxy used for correlation")
            else:
                miss.append("missing daily return series for correlation")
            if not no_realized_meta.get("available"):
                miss.append("missing canonical realized daily series: logs/no_longshot_realized_daily.jsonl")

        elif sid == "gamma_eventpair_exec_edge_filter_observe":
            rec["observe_proxy_daily"] = gamma_proxy_meta
            if gamma_proxy_meta.get("available"):
                chosen = gamma_proxy
                rec["correlation_series_mode"] = "observe_proxy_daily"
                miss.append("no strategy-level realized daily return/PnL series; observe proxy used")
            else:
                miss.append("missing strategy-level daily return/PnL series and observe proxy")

        elif sid == "hourly_updown_highprob_calibration_observe":
            rec["observe_proxy_daily"] = hourly_proxy_meta
            if hourly_proxy_meta.get("available"):
                chosen = hourly_proxy
                rec["correlation_series_mode"] = "observe_proxy_daily"
                miss.append("no strategy-level realized daily return/PnL series; observe proxy used")
            else:
                miss.append("missing strategy-level daily return/PnL series and observe proxy")
            if monthly_proxy is None:
                miss.append("missing bankroll-normalized monthly return proxy in logs")

        elif sid == "link_intake_walletseed_cohort_observe":
            rec["observe_proxy_daily"] = link_proxy_meta
            chosen = {}
            rec["correlation_series_mode"] = "none"
            miss.append("missing strategy-level daily return/PnL series")
            if link_proxy_meta.get("available"):
                miss.append("only cohort quality proxy exists; return correlation is not estimable")
            if monthly_proxy is None:
                miss.append("missing monthly return proxy in logs")
        else:
            miss.append("strategy handler not implemented")

        rec["correlation_series_days"] = len(chosen)
        correlation_series[sid] = chosen
        missing[sid] = miss
        out.append(rec)

    return out, correlation_series, missing


def _pairwise(strategy_ids: Sequence[str], corr_series: Dict[str, Dict[str, float]], min_overlap_days: int) -> List[dict]:
    rows: List[dict] = []
    for i, a in enumerate(strategy_ids):
        for b in strategy_ids[i + 1 :]:
            corr, overlap, reason = _pearson_corr(corr_series.get(a, {}), corr_series.get(b, {}), min_overlap_days=min_overlap_days)
            rows.append(
                {
                    "a": a,
                    "b": b,
                    "overlap_days": len(overlap),
                    "overlap_days_list": overlap,
                    "corr": corr,
                    "reason": reason,
                }
            )
    return rows


def _recommend_min_set(strategy_records: Sequence[dict], pairwise: Sequence[dict], threshold_abs: float) -> dict:
    monthly_map: Dict[str, Optional[float]] = {}
    for row in strategy_records:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("strategy_id") or "")
        monthly_map[sid] = _as_float(row.get("monthly_return_proxy_ratio"))

    low_corr_pairs: List[dict] = []
    for p in pairwise:
        if not isinstance(p, dict):
            continue
        c = _as_float(p.get("corr"))
        if c is None:
            continue
        if abs(c) <= float(threshold_abs):
            low_corr_pairs.append(p)

    best_pair: Optional[dict] = None
    for p in low_corr_pairs:
        a = str(p.get("a") or "")
        b = str(p.get("b") or "")
        ra = monthly_map.get(a)
        rb = monthly_map.get(b)
        if ra is None or rb is None:
            continue
        avg = (ra + rb) / 2.0
        cand = {
            "pair": [a, b],
            "corr": _as_float(p.get("corr")),
            "overlap_days": int(p.get("overlap_days") or 0),
            "avg_monthly_return_proxy_ratio": avg,
        }
        if best_pair is None:
            best_pair = cand
            continue
        lhs = (
            cand["avg_monthly_return_proxy_ratio"],
            -abs(cand["corr"] or 0.0),
            cand["overlap_days"],
        )
        rhs = (
            best_pair["avg_monthly_return_proxy_ratio"],
            -abs(best_pair["corr"] or 0.0),
            best_pair["overlap_days"],
        )
        if lhs > rhs:
            best_pair = cand

    recommended = best_pair["pair"] if isinstance(best_pair, dict) else []
    return {
        "low_corr_threshold_abs": float(threshold_abs),
        "low_corr_pairs": low_corr_pairs,
        "best_pair_with_monthly_proxy": best_pair,
        "recommended_min_set": recommended,
    }


def _risk_proxy_for_pair(pair: Sequence[str], corr_series: Dict[str, Dict[str, float]]) -> Optional[dict]:
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return None
    a = str(pair[0])
    b = str(pair[1])
    sa = corr_series.get(a, {})
    sb = corr_series.get(b, {})
    overlap = sorted(set(sa.keys()) & set(sb.keys()))
    if len(overlap) < 2:
        return None
    xa = [sa[d] for d in overlap]
    xb = [sb[d] for d in overlap]
    xp = [0.5 * xa[i] + 0.5 * xb[i] for i in range(len(overlap))]
    std_a = _sample_std(xa)
    std_b = _sample_std(xb)
    std_p = _sample_std(xp)
    mean_std = None
    risk_reduction = None
    if std_a is not None and std_b is not None:
        mean_std = (std_a + std_b) / 2.0
        if mean_std > 0 and std_p is not None:
            risk_reduction = (mean_std - std_p) / mean_std
    return {
        "pair": [a, b],
        "overlap_days": len(overlap),
        "overlap_days_list": overlap,
        "std_a": std_a,
        "std_b": std_b,
        "std_equal_weight_portfolio": std_p,
        "risk_reduction_vs_avg_std": risk_reduction,
    }


def _monthly_portfolio_estimate(pair: Sequence[str], strategy_records: Sequence[dict]) -> Optional[dict]:
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return None
    by_id: Dict[str, dict] = {str(r.get("strategy_id") or ""): r for r in strategy_records if isinstance(r, dict)}
    a = by_id.get(str(pair[0]))
    b = by_id.get(str(pair[1]))
    if not isinstance(a, dict) or not isinstance(b, dict):
        return None
    ra = _as_float(a.get("monthly_return_proxy_ratio"))
    rb = _as_float(b.get("monthly_return_proxy_ratio"))
    if ra is None or rb is None:
        return None
    portfolio = 0.5 * ra + 0.5 * rb
    no_longshot = by_id.get("no_longshot_daily_observe")
    nl = _as_float(no_longshot.get("monthly_return_proxy_ratio")) if isinstance(no_longshot, dict) else None
    improve_vs_nl = (portfolio - nl) if nl is not None else None
    better_of_pair = max(ra, rb)
    delta_vs_best_single = portfolio - better_of_pair
    return {
        "pair": [str(pair[0]), str(pair[1])],
        "monthly_return_proxy_a": ra,
        "monthly_return_proxy_b": rb,
        "monthly_return_proxy_equal_weight": portfolio,
        "improvement_vs_no_longshot_monthly_proxy": improve_vs_nl,
        "delta_vs_best_single_in_pair": delta_vs_best_single,
    }


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    return f"{v * 100.0:+.4f}%"


def _build_memo(
    *,
    strategy_records: Sequence[dict],
    pairwise: Sequence[dict],
    recommendation: dict,
    portfolio_monthly: Optional[dict],
    risk_proxy: Optional[dict],
    missing_metrics: Dict[str, List[str]],
    date_yyyymmdd: str,
    scope_text: str,
) -> str:
    if len(date_yyyymmdd) == 8:
        date_iso = f"{date_yyyymmdd[0:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}"
    else:
        date_iso = date_yyyymmdd
    lines: List[str] = []
    lines.append(f"memo_uncorrelated_portfolio_{date_yyyymmdd}")
    lines.append("")
    lines.append(f"Date: {date_iso}")
    lines.append(f"Scope: {scope_text}")
    lines.append("References: docs/llm/SPEC.md, docs/llm/STRATEGY.md, docs/llm/STATE.md")
    lines.append("Method: realized daily series preferred; observe proxy fallback when realized series are insufficient")
    lines.append("")
    lines.append("Analyzed strategies:")
    for rec in strategy_records:
        sid = str(rec.get("strategy_id") or "")
        lines.append(f"- {sid}")
    lines.append("")
    lines.append("1) Strategy metrics and daily series")
    lines.append("")
    for rec in strategy_records:
        sid = str(rec.get("strategy_id") or "")
        lines.append(f"{sid}")
        realized = rec.get("realized_daily") if isinstance(rec.get("realized_daily"), dict) else {}
        proxy = rec.get("observe_proxy_daily") if isinstance(rec.get("observe_proxy_daily"), dict) else {}
        lines.append(f"- realized_daily.available: {realized.get('available', False)}")
        if realized:
            lines.append(f"- realized_daily.metric: {realized.get('metric', 'n/a')}")
            lines.append(f"- realized_daily.days: {realized.get('days', 0)}")
            src = realized.get("source_file")
            if src:
                lines.append(f"- realized_daily.source: {src}")
        lines.append(f"- observe_proxy_daily.available: {proxy.get('available', False)}")
        if proxy:
            lines.append(f"- observe_proxy_daily.metric: {proxy.get('metric', 'n/a')}")
            lines.append(f"- observe_proxy_daily.days: {proxy.get('days', 0)}")
            src = proxy.get("source_file")
            if src:
                lines.append(f"- observe_proxy_daily.source: {src}")
            srcs = proxy.get("source_files")
            if isinstance(srcs, list) and srcs:
                lines.append(f"- observe_proxy_daily.source_files: {len(srcs)} files")
        lines.append(f"- correlation_series_mode: {rec.get('correlation_series_mode', 'none')}")
        lines.append(f"- correlation_series_days: {rec.get('correlation_series_days', 0)}")
        lines.append(f"- monthly_return_proxy: {_fmt_pct(_as_float(rec.get('monthly_return_proxy_ratio')))}")
        lines.append(f"- monthly_return_proxy_source: {rec.get('monthly_return_proxy_source', 'n/a')}")
        lines.append("")

    lines.append("2) Pairwise correlations")
    lines.append("")
    for p in pairwise:
        a = p.get("a")
        b = p.get("b")
        corr = _as_float(p.get("corr"))
        ov = int(p.get("overlap_days") or 0)
        reason = str(p.get("reason") or "")
        if corr is None:
            lines.append(f"- {a} vs {b}: insufficient (overlap_days={ov}, reason={reason})")
        else:
            lines.append(f"- {a} vs {b}: corr={corr:+.4f}, overlap_days={ov}")
    lines.append("")

    lines.append("3) Low-correlation recommendation")
    lines.append("")
    lines.append(f"- threshold |corr| <= {float(recommendation.get('low_corr_threshold_abs', 0.3)):.2f}")
    rec_set = recommendation.get("recommended_min_set")
    if isinstance(rec_set, list) and rec_set:
        lines.append(f"- recommended_min_set: {', '.join(str(x) for x in rec_set)}")
    else:
        lines.append("- recommended_min_set: n/a")
    best_pair = recommendation.get("best_pair_with_monthly_proxy")
    if isinstance(best_pair, dict):
        corr_txt = _as_float(best_pair.get("corr"))
        corr_fmt = f"{corr_txt:+.4f}" if corr_txt is not None else "n/a"
        lines.append(
            "- best_pair_with_monthly_proxy: "
            f"{best_pair.get('pair')} corr={corr_fmt} "
            f"avg_monthly={_fmt_pct(_as_float(best_pair.get('avg_monthly_return_proxy_ratio')))}"
        )
    else:
        lines.append("- best_pair_with_monthly_proxy: n/a")
    lines.append("")

    lines.append("4) Expected return and risk estimate (proxy)")
    lines.append("")
    if isinstance(portfolio_monthly, dict):
        lines.append(f"- pair: {portfolio_monthly.get('pair')}")
        lines.append(f"- monthly_return_proxy_equal_weight: {_fmt_pct(_as_float(portfolio_monthly.get('monthly_return_proxy_equal_weight')))}")
        lines.append(
            f"- improvement_vs_no_longshot_monthly_proxy: "
            f"{_fmt_pct(_as_float(portfolio_monthly.get('improvement_vs_no_longshot_monthly_proxy')))}"
        )
        lines.append(f"- delta_vs_best_single_in_pair: {_fmt_pct(_as_float(portfolio_monthly.get('delta_vs_best_single_in_pair')))}")
    else:
        lines.append("- monthly proxy estimate: insufficient")
    if isinstance(risk_proxy, dict):
        lines.append(f"- risk overlap_days: {risk_proxy.get('overlap_days', 0)}")
        lines.append(f"- std_a: {_as_float(risk_proxy.get('std_a'))}")
        lines.append(f"- std_b: {_as_float(risk_proxy.get('std_b'))}")
        lines.append(f"- std_equal_weight_portfolio: {_as_float(risk_proxy.get('std_equal_weight_portfolio'))}")
        lines.append(
            "- risk_reduction_vs_avg_std: "
            f"{_fmt_pct(_as_float(risk_proxy.get('risk_reduction_vs_avg_std')))}"
        )
    else:
        lines.append("- risk proxy estimate: insufficient")
    lines.append("")

    lines.append("5) Missing metrics")
    lines.append("")
    for sid in sorted(missing_metrics.keys()):
        lines.append(f"- {sid}:")
        notes = missing_metrics.get(sid) or []
        if not notes:
            lines.append("  - none")
            continue
        for note in notes:
            lines.append(f"  - {note}")
    lines.append("")
    lines.append("Notes:")
    lines.append("- Low overlap day counts imply low-confidence correlation estimates.")
    lines.append("- For robust covariance, align all strategies to the same realized daily return definition with >=30 overlapping days.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Report strategy correlation and provisional uncorrelated set from logs (observe-only)")
    parser.add_argument(
        "--strategy-ids",
        default="",
        help="Comma-separated strategy ids (default: ADOPTED from logs/strategy_register_latest.json, fallback fixed 5)",
    )
    parser.add_argument("--corr-threshold-abs", type=float, default=0.30, help="Absolute correlation threshold for low-correlation pair/set")
    parser.add_argument("--min-overlap-days", type=int, default=2, help="Minimum overlap days for correlation estimation")
    parser.add_argument(
        "--min-realized-days-for-correlation",
        type=int,
        default=7,
        help="Use realized series for correlation only when realized day count >= this threshold",
    )
    parser.add_argument("--date-yyyymmdd", default="", help="Memo date tag (default: today local)")
    parser.add_argument(
        "--out-json",
        default="",
        help="Analysis JSON output (default: logs/uncorrelated_portfolio_proxy_analysis_<date>.json)",
    )
    parser.add_argument(
        "--memo-out",
        default="",
        help="Memo text output (default: docs/memo_uncorrelated_portfolio_<date>.txt)",
    )
    parser.add_argument("--no-memo", action="store_true", help="Do not write docs memo")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = parser.parse_args()

    date_tag = str(args.date_yyyymmdd or "").strip() or today_yyyymmdd()
    strategy_register_path = logs_dir() / "strategy_register_latest.json"
    strategy_register = _load_strategy_register(strategy_register_path)

    raw_ids = str(args.strategy_ids or "").strip()
    if raw_ids:
        strategy_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]
        scope_text = "explicit strategy_ids (observe-only diagnostic cohort)"
    else:
        active = _active_strategy_ids(strategy_register)
        strategy_ids = active if active else list(DEFAULT_STRATEGY_IDS)
        scope_text = "ADOPTED observe-only strategies (from strategy register)"

    strategy_records, corr_series, missing_metrics = _build_strategy_records(
        strategy_ids,
        logs_dir(),
        strategy_register,
        min_realized_days_for_corr=max(2, int(args.min_realized_days_for_correlation)),
    )
    pairwise = _pairwise(strategy_ids, corr_series, min_overlap_days=max(2, int(args.min_overlap_days)))
    recommendation = _recommend_min_set(strategy_records, pairwise, threshold_abs=float(args.corr_threshold_abs))

    rec_set = recommendation.get("recommended_min_set") if isinstance(recommendation, dict) else []
    pair_for_risk = rec_set if isinstance(rec_set, list) and len(rec_set) == 2 else []
    risk_proxy = _risk_proxy_for_pair(pair_for_risk, corr_series) if pair_for_risk else None
    monthly_est = _monthly_portfolio_estimate(pair_for_risk, strategy_records) if pair_for_risk else None

    analysis = {
        "generated_utc": now_utc().replace(microsecond=0).isoformat(),
        "meta": {
            "observe_only": True,
            "source": "scripts/report_uncorrelated_portfolio.py",
            "strategy_register_source": str(strategy_register_path),
            "corr_threshold_abs": float(args.corr_threshold_abs),
            "min_overlap_days": max(2, int(args.min_overlap_days)),
            "min_realized_days_for_correlation": max(2, int(args.min_realized_days_for_correlation)),
            "date_tag": date_tag,
        },
        "strategy_ids": strategy_ids,
        "strategy_records": strategy_records,
        "pairwise_corr": pairwise,
        "recommendation": recommendation,
        "portfolio_monthly_proxy": monthly_est,
        "portfolio_risk_proxy": risk_proxy,
        "missing_metrics": missing_metrics,
    }

    out_json_default = f"uncorrelated_portfolio_proxy_analysis_{date_tag}.json"
    out_json_path = resolve_path(args.out_json, out_json_default, base="logs")
    out_json_path.parent.mkdir(parents=True, exist_ok=True)
    out_json_path.write_text(json.dumps(analysis, ensure_ascii=True, indent=2 if args.pretty else None), encoding="utf-8")

    memo_path = resolve_path(args.memo_out, f"memo_uncorrelated_portfolio_{date_tag}.txt", base="docs")
    if not bool(args.no_memo):
        memo_txt = _build_memo(
            strategy_records=strategy_records,
            pairwise=pairwise,
            recommendation=recommendation,
            portfolio_monthly=monthly_est,
            risk_proxy=risk_proxy,
            missing_metrics=missing_metrics,
            date_yyyymmdd=date_tag,
            scope_text=scope_text,
        )
        memo_path.parent.mkdir(parents=True, exist_ok=True)
        memo_path.write_text(memo_txt, encoding="utf-8")
        latest_memo_path = docs_dir() / "memo_uncorrelated_portfolio_latest.txt"
        latest_memo_path.write_text(memo_txt, encoding="utf-8")

    print(f"[uncorrelated-portfolio] wrote json: {out_json_path}")
    if not bool(args.no_memo):
        print(f"[uncorrelated-portfolio] wrote memo: {memo_path}")
        print(f"[uncorrelated-portfolio] wrote latest memo: {latest_memo_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
