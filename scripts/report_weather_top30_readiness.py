#!/usr/bin/env python3
"""
Aggregate weather Top30 readiness decision snapshots.

Observe-only helper:
  - reads readiness JSON files under logs/
  - keeps latest decision per profile/mode
  - writes compact JSON/TXT report under logs/
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def logs_dir() -> Path:
    p = repo_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_out_path(raw: str, default_name: str) -> Path:
    if not raw.strip():
        return logs_dir() / default_name
    p = Path(raw)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs_dir() / p.name
    return repo_root() / p


def parse_iso(ts: str) -> Optional[dt.datetime]:
    s = (ts or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def infer_mode(payload: dict, path: Path) -> str:
    thresholds = payload.get("thresholds") if isinstance(payload.get("thresholds"), dict) else {}
    req_exec = thresholds.get("require_execution_plan")
    if isinstance(req_exec, bool):
        return "strict" if req_exec else "quality"
    name = path.name.lower()
    if "strict" in name:
        return "strict"
    if "quality" in name:
        return "quality"
    return "unknown"


def hard_failed_gates(payload: dict) -> List[str]:
    gates = payload.get("gates") if isinstance(payload.get("gates"), list) else []
    out: List[str] = []
    for g in gates:
        if not isinstance(g, dict):
            continue
        if bool(g.get("hard")) and not bool(g.get("passed")):
            out.append(str(g.get("name") or "unknown"))
    return out


def load_record(path: Path) -> Optional[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    # Skip non-readiness JSON files that happen to match the glob.
    if not isinstance(payload.get("gates"), list):
        return None
    if "decision" not in payload:
        return None

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    profile = str(meta.get("profile_name") or "").strip()
    if not profile:
        stem = path.stem
        key = "_top30_readiness_"
        profile = stem.split(key, 1)[0] if key in stem else stem
    mode = infer_mode(payload, path)
    decision = str(payload.get("decision") or "UNKNOWN").strip().upper()
    gen = parse_iso(str(payload.get("generated_utc") or ""))
    if gen is None:
        gen = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)

    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    return {
        "path": str(path),
        "profile_name": profile,
        "mode": mode,
        "decision": decision,
        "generated_utc": gen,
        "generated_utc_iso": gen.isoformat(),
        "failed_hard_gates": hard_failed_gates(payload),
        "row_count": metrics.get("row_count"),
        "both_ratio": metrics.get("both_ratio"),
        "median_net_yield_per_day": metrics.get("median_net_yield_per_day"),
        "top10_avg_max_profit": metrics.get("top10_avg_max_profit"),
    }


def resolve_inputs(pattern: str) -> List[Path]:
    root = repo_root()
    pattern = pattern.strip() or "logs/*_top30_readiness_*latest.json"
    p = Path(pattern)

    if p.is_absolute():
        if any(ch in pattern for ch in ("*", "?", "[")):
            import glob

            return sorted(Path(x) for x in glob.glob(pattern))
        return [p] if p.exists() else []

    if any(ch in pattern for ch in ("*", "?", "[")):
        import glob

        return sorted(Path(x) for x in glob.glob(str(root / pattern)))

    q = root / p
    return [q] if q.exists() else []


def latest_per_profile_mode(rows: List[dict]) -> List[dict]:
    best: Dict[Tuple[str, str], dict] = {}
    for r in rows:
        key = (str(r["profile_name"]), str(r["mode"]))
        prev = best.get(key)
        if prev is None or r["generated_utc"] > prev["generated_utc"]:
            best[key] = r
    out = list(best.values())
    out.sort(key=lambda x: (x["profile_name"], x["mode"]))
    return out


def summarize_mode(rows: List[dict], mode: str) -> dict:
    xs = [r for r in rows if r["mode"] == mode]
    go = [r for r in xs if r["decision"] == "GO"]
    no_go = [r for r in xs if r["decision"] == "NO_GO"]
    failed_counter: Counter[str] = Counter()
    for r in xs:
        failed_counter.update(r["failed_hard_gates"])
    return {
        "mode": mode,
        "count": len(xs),
        "go_count": len(go),
        "no_go_count": len(no_go),
        "go_profiles": sorted({str(r["profile_name"]) for r in go}),
        "no_go_profiles": sorted({str(r["profile_name"]) for r in no_go}),
        "top_failed_hard_gates": failed_counter.most_common(10),
    }


def build_text_report(payload: dict) -> str:
    lines: List[str] = []
    lines.append(f"Weather Top30 readiness report @ {payload['generated_utc']}")
    lines.append(
        f"Inputs: matched={payload['inputs']['matched_files']} loaded={payload['inputs']['loaded_records']} "
        f"latest_profile_mode={payload['inputs']['latest_records']}"
    )
    for mode in ("strict", "quality", "unknown"):
        s = payload["summary"].get(mode) if isinstance(payload.get("summary"), dict) else None
        if not isinstance(s, dict):
            continue
        if int(s.get("count", 0)) <= 0:
            continue
        lines.append(
            f"{mode}: GO {s['go_count']}/{s['count']} | NO_GO {s['no_go_count']}/{s['count']}"
        )
        failed = s.get("top_failed_hard_gates") if isinstance(s.get("top_failed_hard_gates"), list) else []
        if failed:
            top_txt = ", ".join(f"{name}:{cnt}" for name, cnt in failed[:3])
            lines.append(f"  failed_gates: {top_txt}")
    lines.append("Per profile/mode:")
    rows = payload.get("latest_profile_mode_records")
    if isinstance(rows, list):
        for r in rows:
            if not isinstance(r, dict):
                continue
            failed = r.get("failed_hard_gates") if isinstance(r.get("failed_hard_gates"), list) else []
            fail_txt = ",".join(str(x) for x in failed) if failed else "-"
            lines.append(
                f"  {r['profile_name']} [{r['mode']}] => {r['decision']} "
                f"(failed={fail_txt}) generated={r['generated_utc']}"
            )
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Aggregate weather Top30 readiness decisions (observe-only).")
    p.add_argument("--glob", default="logs/*_top30_readiness_*latest.json", help="Input glob pattern")
    p.add_argument(
        "--mode",
        choices=("all", "strict", "quality", "unknown"),
        default="all",
        help="Keep only this mode before latest-per-profile aggregation",
    )
    p.add_argument("--profile", action="append", default=[], help="Optional profile filter (repeatable)")
    p.add_argument("--out-json", default="", help="Output JSON path (simple filename goes under logs/)")
    p.add_argument("--out-txt", default="", help="Output text path (simple filename goes under logs/)")
    p.add_argument("--pretty", action="store_true")
    args = p.parse_args()

    input_paths = resolve_inputs(args.glob)
    loaded: List[dict] = []
    for path in input_paths:
        r = load_record(path)
        if r is None:
            continue
        loaded.append(r)

    if args.mode != "all":
        loaded = [r for r in loaded if r["mode"] == args.mode]

    profile_filter = {str(x).strip() for x in (args.profile or []) if str(x).strip()}
    if profile_filter:
        loaded = [r for r in loaded if str(r["profile_name"]) in profile_filter]

    latest = latest_per_profile_mode(loaded)
    strict_summary = summarize_mode(latest, "strict")
    quality_summary = summarize_mode(latest, "quality")
    unknown_summary = summarize_mode(latest, "unknown")

    payload = {
        "generated_utc": now_utc().isoformat(),
        "meta": {"observe_only": True, "source": "scripts/report_weather_top30_readiness.py"},
        "inputs": {
            "glob": str(args.glob),
            "mode_filter": str(args.mode),
            "profile_filter": sorted(profile_filter),
            "matched_files": len(input_paths),
            "loaded_records": len(loaded),
            "latest_records": len(latest),
        },
        "summary": {
            "strict": strict_summary,
            "quality": quality_summary,
            "unknown": unknown_summary,
        },
        "latest_profile_mode_records": [
            {
                "profile_name": r["profile_name"],
                "mode": r["mode"],
                "decision": r["decision"],
                "generated_utc": r["generated_utc_iso"],
                "failed_hard_gates": list(r["failed_hard_gates"]),
                "row_count": r["row_count"],
                "both_ratio": r["both_ratio"],
                "median_net_yield_per_day": r["median_net_yield_per_day"],
                "top10_avg_max_profit": r["top10_avg_max_profit"],
                "path": r["path"],
            }
            for r in latest
        ],
    }

    out_json = resolve_out_path(args.out_json, "weather_top30_readiness_report_latest.json")
    out_txt = resolve_out_path(args.out_txt, "weather_top30_readiness_report_latest.txt")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_txt.parent.mkdir(parents=True, exist_ok=True)

    with out_json.open("w", encoding="utf-8") as f:
        if args.pretty:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        else:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    txt = build_text_report(payload)
    out_txt.write_text(txt + "\n", encoding="utf-8")

    print(f"[readiness-report] latest_records={len(latest)} strict_go={strict_summary['go_count']}/{strict_summary['count']}")
    print(f"[readiness-report] wrote {out_json}")
    print(f"[readiness-report] wrote {out_txt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
