#!/usr/bin/env python3
"""
Build one ranked weather watchlist from weather mimic scanner CSV outputs.

Observe-only helper:
  - reads no_longshot screen CSV
  - reads lateprob screen CSV
  - merges by market_id (fallback: normalized question)
  - emits consensus-ranked CSV/JSON under logs/
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional


SCORE_MODE_WEIGHTS: Dict[str, Dict[str, float]] = {
    "balanced": {
        "overlap": 0.20,
        "net_yield": 0.35,
        "max_profit": 0.25,
        "liquidity": 0.12,
        "volume": 0.08,
    },
    "liquidity": {
        "overlap": 0.18,
        "net_yield": 0.22,
        "max_profit": 0.15,
        "liquidity": 0.30,
        "volume": 0.15,
    },
    "edge": {
        "overlap": 0.20,
        "net_yield": 0.45,
        "max_profit": 0.25,
        "liquidity": 0.07,
        "volume": 0.03,
    },
}


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_str(value) -> str:
    return str(value or "").strip()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_path(raw: str, default_name: str) -> Path:
    logs_dir = repo_root() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    if not raw.strip():
        return logs_dir / default_name
    p = Path(raw)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs_dir / p.name
    return repo_root() / p


def normalize_question(text: str) -> str:
    q = as_str(text).lower()
    q = re.sub(r"\s+", " ", q)
    return q


def load_csv_rows(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return [dict(r) for r in reader]


def score_log(value: float, cap: float) -> float:
    if value <= 0 or cap <= 0:
        return 0.0
    return min(1.0, math.log10(1.0 + value) / math.log10(1.0 + cap))


@dataclass
class ConsensusRow:
    rank: int
    market_id: str
    question: str
    correlation_bucket: str
    end_iso: str
    hours_to_end: float
    yes_price: float
    no_price: float
    side_hint: str
    entry_price: float
    max_profit: float
    net_yield_on_no: float
    net_yield_per_day: float
    liquidity_num: float
    volume_24h: float
    turnover_ratio: float
    in_no_longshot: bool
    in_lateprob: bool
    score_total: float
    score_components: Dict[str, float]


def infer_correlation_bucket(question: str) -> str:
    q = normalize_question(question)
    if not q:
        return "unknown"

    metric = "weather"
    if "highest temperature" in q:
        metric = "temp_high"
    elif "snow" in q:
        metric = "snow"
    elif "lowest temperature" in q:
        metric = "temp_low"
    elif "rain" in q:
        metric = "rain"
    elif "precipitation" in q:
        metric = "precip"
    elif "wind" in q:
        metric = "wind"
    elif "humidity" in q:
        metric = "humidity"

    m = re.search(r"will the highest temperature in (.+?) be .+ on ([a-z0-9 ,'\-]+)\??$", q)
    if m:
        loc = re.sub(r"\s+", " ", m.group(1)).strip()
        date_part = re.sub(r"\s+", " ", m.group(2)).strip()
        return f"temp_high|{loc}|{date_part}"

    m = re.search(r"will the lowest temperature in (.+?) be .+ on ([a-z0-9 ,'\-]+)\??$", q)
    if m:
        loc = re.sub(r"\s+", " ", m.group(1)).strip()
        date_part = re.sub(r"\s+", " ", m.group(2)).strip()
        return f"temp_low|{loc}|{date_part}"

    m = re.search(r"\bin\s+(.+?)\s+on\s+([a-z0-9 ,'\-]+)\??$", q)
    if m:
        loc = re.sub(r"\s+", " ", m.group(1)).strip()
        date_part = re.sub(r"\s+", " ", m.group(2)).strip()
        return f"{metric}|{loc}|{date_part}"

    m = re.search(r"\bin\s+(.+?)\s+(this weekend|today|tomorrow|tonight)\??$", q)
    if m:
        loc = re.sub(r"\s+", " ", m.group(1)).strip()
        date_part = re.sub(r"\s+", " ", m.group(2)).strip()
        return f"{metric}|{loc}|{date_part}"

    # Fallback: normalize away concrete strike values to avoid over-segmentation.
    reduced = re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", q)
    reduced = re.sub(r"\s+", " ", reduced).strip()
    return f"{metric}|{reduced[:96]}"


def merge_rows(no_rows: List[dict], late_rows: List[dict]) -> List[dict]:
    by_market: Dict[str, dict] = {}
    by_question: Dict[str, dict] = {}

    def _key_market(row: dict) -> str:
        return as_str(row.get("market_id"))

    def _key_question(row: dict) -> str:
        return normalize_question(as_str(row.get("question")))

    for r in no_rows:
        mk = _key_market(r)
        qk = _key_question(r)
        obj = {
            "market_id": mk,
            "question": as_str(r.get("question")),
            "end_iso": as_str(r.get("end_iso")),
            "hours_to_end": as_float(r.get("hours_to_end")),
            "yes_price": as_float(r.get("yes_price"), math.nan),
            "no_price": as_float(r.get("no_price"), math.nan),
            "net_yield_on_no": as_float(r.get("net_yield_on_no"), math.nan),
            "net_yield_per_day": as_float(r.get("net_yield_per_day"), math.nan),
            "side_hint": "no",
            "entry_price": as_float(r.get("no_price"), math.nan),
            "liquidity_num": as_float(r.get("liquidity_num")),
            "volume_24h": as_float(r.get("volume_24h")),
            "in_no_longshot": True,
            "in_lateprob": False,
        }
        if mk:
            by_market[mk] = obj
        if qk:
            by_question[qk] = obj

    for r in late_rows:
        mk = _key_market(r)
        qk = _key_question(r)
        side = as_str(r.get("side")).lower() or "no"
        entry = as_float(r.get("entry_price"), math.nan)
        yes = as_float(r.get("yes_price"), math.nan)
        no_price = 1.0 - yes if math.isfinite(yes) else math.nan

        target = None
        if mk and mk in by_market:
            target = by_market[mk]
        elif qk and qk in by_question:
            target = by_question[qk]

        if target is None:
            target = {
                "market_id": mk,
                "question": as_str(r.get("question")),
                "end_iso": as_str(r.get("end_iso")),
                "hours_to_end": as_float(r.get("hours_to_end")),
                "yes_price": yes,
                "no_price": no_price,
                "net_yield_on_no": math.nan,
                "net_yield_per_day": math.nan,
                "side_hint": side,
                "entry_price": entry,
                "liquidity_num": as_float(r.get("liquidity_num")),
                "volume_24h": as_float(r.get("volume_24h")),
                "in_no_longshot": False,
                "in_lateprob": True,
            }
            if mk:
                by_market[mk] = target
            if qk:
                by_question[qk] = target
            continue

        target["in_lateprob"] = True
        if not target.get("side_hint"):
            target["side_hint"] = side
        if not math.isfinite(as_float(target.get("entry_price"), math.nan)):
            target["entry_price"] = entry
        target["hours_to_end"] = min(
            float(target.get("hours_to_end") or 0.0) if target.get("hours_to_end") else float("inf"),
            as_float(r.get("hours_to_end"), float("inf")),
        )
        if not math.isfinite(as_float(target.get("yes_price"), math.nan)) and math.isfinite(yes):
            target["yes_price"] = yes
        if not math.isfinite(as_float(target.get("no_price"), math.nan)) and math.isfinite(no_price):
            target["no_price"] = no_price
        target["liquidity_num"] = max(as_float(target.get("liquidity_num")), as_float(r.get("liquidity_num")))
        target["volume_24h"] = max(as_float(target.get("volume_24h")), as_float(r.get("volume_24h")))

    rows: List[dict] = list(by_market.values())
    # Include rows merged only by question (without market_id).
    for qk, row in by_question.items():
        if row in rows:
            continue
        rows.append(row)
    return rows


def build_consensus(
    merged: List[dict],
    min_liquidity: float,
    min_volume_24h: float,
    min_turnover_ratio: float,
    max_hours_to_end: float,
    top_n: int,
    weights: Dict[str, float],
    require_overlap: bool,
    max_per_correlation_bucket: int,
) -> List[ConsensusRow]:
    out: List[ConsensusRow] = []
    for r in merged:
        liq = as_float(r.get("liquidity_num"))
        vol = as_float(r.get("volume_24h"))
        if liq < min_liquidity:
            continue
        if vol < min_volume_24h:
            continue
        turnover_ratio = (vol / liq) if liq > 0 else 0.0
        if min_turnover_ratio > 0 and turnover_ratio < float(min_turnover_ratio):
            continue

        hours_to_end = as_float(r.get("hours_to_end"), math.nan)
        if max_hours_to_end > 0:
            if not math.isfinite(hours_to_end):
                continue
            if hours_to_end > float(max_hours_to_end):
                continue

        yes = as_float(r.get("yes_price"), math.nan)
        no_price = as_float(r.get("no_price"), math.nan)
        if not math.isfinite(no_price) and math.isfinite(yes):
            no_price = 1.0 - yes
        side = as_str(r.get("side_hint")).lower() or "no"
        entry = as_float(r.get("entry_price"), math.nan)
        if not math.isfinite(entry):
            entry = no_price if side == "no" else yes
        max_profit = 1.0 - entry if math.isfinite(entry) else math.nan

        nyd = as_float(r.get("net_yield_per_day"), math.nan)
        ny = as_float(r.get("net_yield_on_no"), math.nan)
        in_no = bool(r.get("in_no_longshot"))
        in_late = bool(r.get("in_lateprob"))
        if require_overlap and not (in_no and in_late):
            continue
        overlap_score = 1.0 if (in_no and in_late) else 0.5
        nyd_score = clamp_unit(nyd / 0.15) if math.isfinite(nyd) else 0.0
        edge_score = clamp_unit(max_profit / 0.15) if math.isfinite(max_profit) else 0.0
        liq_score = score_log(liq, cap=5000.0)
        vol_score = score_log(vol, cap=5000.0)

        total = 100.0 * (
            float(weights["overlap"]) * overlap_score
            + float(weights["net_yield"]) * nyd_score
            + float(weights["max_profit"]) * edge_score
            + float(weights["liquidity"]) * liq_score
            + float(weights["volume"]) * vol_score
        )

        out.append(
            ConsensusRow(
                rank=0,
                market_id=as_str(r.get("market_id")),
                question=as_str(r.get("question")),
                correlation_bucket=infer_correlation_bucket(as_str(r.get("question"))),
                end_iso=as_str(r.get("end_iso")),
                hours_to_end=hours_to_end,
                yes_price=yes,
                no_price=no_price,
                side_hint=side,
                entry_price=entry,
                max_profit=max_profit,
                net_yield_on_no=ny,
                net_yield_per_day=nyd,
                liquidity_num=liq,
                volume_24h=vol,
                turnover_ratio=turnover_ratio,
                in_no_longshot=in_no,
                in_lateprob=in_late,
                score_total=total,
                score_components={
                    "overlap_score": overlap_score,
                    "net_yield_per_day_score": nyd_score,
                    "max_profit_score": edge_score,
                    "liquidity_score": liq_score,
                    "volume_score": vol_score,
                    "weight_overlap": float(weights["overlap"]),
                    "weight_net_yield": float(weights["net_yield"]),
                    "weight_max_profit": float(weights["max_profit"]),
                    "weight_liquidity": float(weights["liquidity"]),
                    "weight_volume": float(weights["volume"]),
                    "weighted_overlap": float(weights["overlap"]) * overlap_score,
                    "weighted_net_yield": float(weights["net_yield"]) * nyd_score,
                    "weighted_max_profit": float(weights["max_profit"]) * edge_score,
                    "weighted_liquidity": float(weights["liquidity"]) * liq_score,
                    "weighted_volume": float(weights["volume"]) * vol_score,
                },
            )
        )

    out.sort(
        key=lambda x: (
            x.score_total,
            x.in_no_longshot and x.in_lateprob,
            x.liquidity_num,
            x.volume_24h,
        ),
        reverse=True,
    )

    if int(max_per_correlation_bucket) > 0:
        capped: List[ConsensusRow] = []
        bucket_counts: Dict[str, int] = {}
        cap = int(max_per_correlation_bucket)
        for row in out:
            key = row.correlation_bucket or "unknown"
            used = int(bucket_counts.get(key, 0))
            if used >= cap:
                continue
            bucket_counts[key] = used + 1
            capped.append(row)
        out = capped

    for i, row in enumerate(out, start=1):
        row.rank = i
    return out[: max(1, int(top_n))]


def clamp_unit(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def resolve_weights(
    score_mode: str,
    weight_overlap: Optional[float],
    weight_net_yield: Optional[float],
    weight_max_profit: Optional[float],
    weight_liquidity: Optional[float],
    weight_volume: Optional[float],
) -> Dict[str, float]:
    mode = as_str(score_mode).lower()
    if mode not in SCORE_MODE_WEIGHTS:
        mode = "balanced"
    w = dict(SCORE_MODE_WEIGHTS[mode])

    if weight_overlap is not None:
        w["overlap"] = max(0.0, float(weight_overlap))
    if weight_net_yield is not None:
        w["net_yield"] = max(0.0, float(weight_net_yield))
    if weight_max_profit is not None:
        w["max_profit"] = max(0.0, float(weight_max_profit))
    if weight_liquidity is not None:
        w["liquidity"] = max(0.0, float(weight_liquidity))
    if weight_volume is not None:
        w["volume"] = max(0.0, float(weight_volume))

    total = sum(float(v) for v in w.values())
    if total <= 1e-12:
        w = dict(SCORE_MODE_WEIGHTS["balanced"])
        total = sum(float(v) for v in w.values())
    return {k: float(v) / float(total) for k, v in w.items()}


def write_outputs(rows: List[ConsensusRow], out_csv: Path, out_json: Path, meta: dict, pretty: bool) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)

    with out_csv.open("w", encoding="utf-8", newline="") as f:
        fieldnames = list(ConsensusRow.__dataclass_fields__.keys())
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))

    payload = {
        "generated_utc": now_utc().isoformat(),
        "meta": meta,
        "count": len(rows),
        "top": [asdict(r) for r in rows],
        "artifacts": {"csv": str(out_csv)},
    }
    with out_json.open("w", encoding="utf-8") as f:
        if pretty:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        else:
            json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)


def main() -> int:
    p = argparse.ArgumentParser(description="Build consensus weather watchlist from scanner CSV outputs")
    p.add_argument(
        "--no-longshot-csv",
        default="logs/weather_focus_mimic_no_longshot_latest.csv",
        help="Input CSV from polymarket_no_longshot_observe.py screen",
    )
    p.add_argument(
        "--lateprob-csv",
        default="logs/weather_focus_mimic_lateprob_latest.csv",
        help="Input CSV from polymarket_lateprob_observe.py screen",
    )
    p.add_argument("--profile-name", default="weather_focus_mimic", help="Output filename prefix")
    p.add_argument("--top-n", type=int, default=50, help="Rows kept in final watchlist")
    p.add_argument("--min-liquidity", type=float, default=500.0, help="Minimum liquidity_num filter")
    p.add_argument("--min-volume-24h", type=float, default=100.0, help="Minimum volume_24h filter")
    p.add_argument(
        "--min-turnover-ratio",
        type=float,
        default=0.0,
        help="Minimum volume_24h / liquidity_num ratio (0 disables)",
    )
    p.add_argument(
        "--max-hours-to-end",
        type=float,
        default=0.0,
        help="Maximum hours_to_end filter (0 disables)",
    )
    p.add_argument("--score-mode", choices=("balanced", "liquidity", "edge"), default="balanced")
    p.add_argument("--weight-overlap", type=float, default=None)
    p.add_argument("--weight-net-yield", type=float, default=None)
    p.add_argument("--weight-max-profit", type=float, default=None)
    p.add_argument("--weight-liquidity", type=float, default=None)
    p.add_argument("--weight-volume", type=float, default=None)
    p.add_argument(
        "--require-overlap",
        action="store_true",
        help="Keep only rows present in both no_longshot and lateprob inputs",
    )
    p.add_argument(
        "--max-per-correlation-bucket",
        type=int,
        default=0,
        help="Optional cap of rows kept per inferred correlation bucket (0 disables)",
    )
    p.add_argument("--out-csv", default="", help="Output CSV path (simple filename goes under logs/)")
    p.add_argument("--out-json", default="", help="Output JSON path (simple filename goes under logs/)")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = p.parse_args()

    no_path = resolve_path(args.no_longshot_csv, f"{args.profile_name}_no_longshot_latest.csv")
    late_path = resolve_path(args.lateprob_csv, f"{args.profile_name}_lateprob_latest.csv")
    out_csv = resolve_path(args.out_csv, f"{args.profile_name}_consensus_watchlist_latest.csv")
    out_json = resolve_path(args.out_json, f"{args.profile_name}_consensus_watchlist_latest.json")

    no_rows = load_csv_rows(no_path)
    late_rows = load_csv_rows(late_path)

    merged = merge_rows(no_rows, late_rows)
    weights = resolve_weights(
        score_mode=args.score_mode,
        weight_overlap=args.weight_overlap,
        weight_net_yield=args.weight_net_yield,
        weight_max_profit=args.weight_max_profit,
        weight_liquidity=args.weight_liquidity,
        weight_volume=args.weight_volume,
    )
    ranked = build_consensus(
        merged=merged,
        min_liquidity=float(args.min_liquidity),
        min_volume_24h=float(args.min_volume_24h),
        min_turnover_ratio=float(args.min_turnover_ratio),
        max_hours_to_end=float(args.max_hours_to_end),
        top_n=int(args.top_n),
        weights=weights,
        require_overlap=bool(args.require_overlap),
        max_per_correlation_bucket=int(args.max_per_correlation_bucket),
    )

    meta = {
        "observe_only": True,
        "no_longshot_csv": str(no_path),
        "lateprob_csv": str(late_path),
        "input_counts": {
            "no_longshot_rows": len(no_rows),
            "lateprob_rows": len(late_rows),
            "merged_rows": len(merged),
        },
        "filters": {
            "min_liquidity": float(args.min_liquidity),
            "min_volume_24h": float(args.min_volume_24h),
            "min_turnover_ratio": float(args.min_turnover_ratio),
            "max_hours_to_end": float(args.max_hours_to_end),
            "top_n": int(args.top_n),
            "require_overlap": bool(args.require_overlap),
            "max_per_correlation_bucket": int(args.max_per_correlation_bucket),
        },
        "scoring": {
            "score_mode": str(args.score_mode),
            "weights": weights,
        },
    }
    write_outputs(ranked, out_csv=out_csv, out_json=out_json, meta=meta, pretty=bool(args.pretty))

    print(f"[consensus] input no_longshot={len(no_rows)} lateprob={len(late_rows)} merged={len(merged)}")
    print(f"[consensus] score_mode={args.score_mode} weights={weights}")
    print(f"[consensus] output rows={len(ranked)}")
    for r in ranked[: min(10, len(ranked))]:
        print(
            f"rank={r.rank:2d} score={r.score_total:6.2f} liq={r.liquidity_num:8.0f} "
            f"vol24h={r.volume_24h:8.0f} turn={r.turnover_ratio:5.2f} "
            f"h2e={r.hours_to_end:6.1f} side={r.side_hint:>3s} yes={r.yes_price:0.4f} | {r.question[:96]}"
        )
    print(f"[consensus] wrote {out_csv}")
    print(f"[consensus] wrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
