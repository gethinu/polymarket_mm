#!/usr/bin/env python3
"""
Run observe-only intake-to-cohort flow in one command:
  link_intake JSON -> extract users -> analyze cohort
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_tag() -> str:
    return now_utc().strftime("%Y%m%d_%H%M%S")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def logs_dir() -> Path:
    p = repo_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_input_path(raw: str) -> Path:
    p = Path(str(raw or "").strip())
    if p.is_absolute():
        return p
    return repo_root() / p


def sanitize_profile_name(raw: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", str(raw or "").strip())
    s = s.strip("_")
    return s or "link_intake_auto"


def run_cmd(cmd: List[str]) -> None:
    print("[run]", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=repo_root(), check=True)


def count_users(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())


def copy_latest(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def load_json(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def parse_args():
    p = argparse.ArgumentParser(description="Run link-intake JSON -> user extraction -> cohort analysis (observe-only)")
    p.add_argument("intake_json", help="Input intake JSON (e.g., logs/link_intake_*.json)")
    p.add_argument("--profile-name", default="link_intake_auto", help="Output file prefix")
    p.add_argument(
        "--min-confidence",
        default="medium",
        choices=["none", "low", "medium", "high"],
        help="Minimum confidence passed to extract_link_intake_users.py",
    )
    p.add_argument(
        "--resolve-profiles",
        dest="resolve_profiles",
        action="store_true",
        help="Resolve profile URLs/handles to wallets during extraction",
    )
    p.add_argument(
        "--no-resolve-profiles",
        dest="resolve_profiles",
        action="store_false",
        help="Disable profile->wallet resolution during extraction",
    )
    p.set_defaults(resolve_profiles=True)
    p.add_argument("--limit", type=int, default=500, help="Pass-through Data API page size for cohort analysis")
    p.add_argument("--max-trades", type=int, default=2500, help="Pass-through max trades per user for cohort analysis")
    p.add_argument("--sleep-sec", type=float, default=0.0, help="Pass-through sleep between paginated fetches")
    p.add_argument(
        "--weather-keywords",
        default="weather,temperature,highest temperature,lowest temperature,precipitation,rain,snow,wind,humidity,forecast",
        help="Pass-through weather keywords for cohort analysis",
    )
    p.add_argument("--min-low-price", type=float, default=0.15, help="Pass-through low-price BUY threshold")
    p.add_argument("--max-high-price", type=float, default=0.85, help="Pass-through high-price BUY threshold")
    p.add_argument("--top-markets", type=int, default=8, help="Pass-through top markets kept per user")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON outputs where supported")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    intake_path = resolve_input_path(args.intake_json)
    if not intake_path.exists():
        print(f"Input intake JSON not found: {intake_path}")
        return 2

    profile = sanitize_profile_name(args.profile_name)
    stamp = utc_tag()
    logs = logs_dir()

    users_txt = logs / f"{profile}_link_intake_users_{stamp}.txt"
    users_json = logs / f"{profile}_link_intake_users_{stamp}.json"
    cohort_json = logs / f"{profile}_link_intake_cohort_{stamp}.json"
    summary_json = logs / f"{profile}_link_intake_summary_{stamp}.json"

    users_txt_latest = logs / f"{profile}_link_intake_users_latest.txt"
    users_json_latest = logs / f"{profile}_link_intake_users_latest.json"
    cohort_json_latest = logs / f"{profile}_link_intake_cohort_latest.json"
    summary_json_latest = logs / f"{profile}_link_intake_summary_latest.json"

    extract_cmd = [
        sys.executable,
        str(repo_root() / "scripts" / "extract_link_intake_users.py"),
        str(intake_path),
        "--min-confidence",
        str(args.min_confidence),
        "--out-user-file",
        str(users_txt),
        "--out-json",
        str(users_json),
    ]
    if args.resolve_profiles:
        extract_cmd.append("--resolve-profiles")
    if args.pretty:
        extract_cmd.append("--pretty")

    run_cmd(extract_cmd)
    user_count = count_users(users_txt)

    status = "ok"
    cohort_payload = {}
    cohort_ok = False
    cohort_cmd: List[str] = []

    if user_count <= 0:
        status = "no_users_extracted"
    else:
        cohort_cmd = [
            sys.executable,
            str(repo_root() / "scripts" / "analyze_trader_cohort.py"),
            "--user-file",
            str(users_txt),
            "--limit",
            str(int(args.limit)),
            "--max-trades",
            str(int(args.max_trades)),
            "--sleep-sec",
            str(float(args.sleep_sec)),
            "--weather-keywords",
            str(args.weather_keywords),
            "--min-low-price",
            str(float(args.min_low_price)),
            "--max-high-price",
            str(float(args.max_high_price)),
            "--top-markets",
            str(int(args.top_markets)),
            "--out",
            str(cohort_json),
        ]
        if args.pretty:
            cohort_cmd.append("--pretty")
        run_cmd(cohort_cmd)
        cohort_payload = load_json(cohort_json)
        cohort_ok = bool(((cohort_payload.get("cohort") or {}).get("ok")) if isinstance(cohort_payload, dict) else False)
        if not cohort_ok:
            status = "cohort_not_ok"

    summary_payload = {
        "meta": {
            "generated_at_utc": now_utc().isoformat(),
            "tool": "run_link_intake_cohort.py",
            "observe_only": True,
            "status": status,
            "profile_name": profile,
            "input_intake_json": str(intake_path),
        },
        "params": {
            "min_confidence": str(args.min_confidence),
            "resolve_profiles": bool(args.resolve_profiles),
            "limit": int(args.limit),
            "max_trades": int(args.max_trades),
            "sleep_sec": float(args.sleep_sec),
            "weather_keywords": str(args.weather_keywords),
            "min_low_price": float(args.min_low_price),
            "max_high_price": float(args.max_high_price),
            "top_markets": int(args.top_markets),
        },
        "commands": {
            "extract": extract_cmd,
            "cohort": cohort_cmd,
        },
        "outputs": {
            "users_file": str(users_txt),
            "users_summary_json": str(users_json),
            "cohort_json": str(cohort_json) if cohort_cmd else "",
            "summary_json": str(summary_json),
            "users_file_latest": str(users_txt_latest),
            "users_summary_json_latest": str(users_json_latest),
            "cohort_json_latest": str(cohort_json_latest),
            "summary_json_latest": str(summary_json_latest),
        },
        "stats": {
            "extracted_user_count": int(user_count),
            "cohort_ok": bool(cohort_ok),
            "resolved_user_count": int(((cohort_payload.get("meta") or {}).get("resolved_user_count") or 0))
            if cohort_payload
            else 0,
            "failed_user_count": int(((cohort_payload.get("meta") or {}).get("failed_user_count") or 0))
            if cohort_payload
            else 0,
        },
    }

    summary_json.parent.mkdir(parents=True, exist_ok=True)
    with summary_json.open("w", encoding="utf-8") as f:
        if args.pretty:
            json.dump(summary_payload, f, indent=2, ensure_ascii=False)
        else:
            json.dump(summary_payload, f, separators=(",", ":"), ensure_ascii=False)

    copy_latest(users_txt, users_txt_latest)
    copy_latest(users_json, users_json_latest)
    if cohort_cmd:
        copy_latest(cohort_json, cohort_json_latest)
    copy_latest(summary_json, summary_json_latest)

    print()
    print(f"status={status}")
    print(f"users_extracted={user_count}")
    print(f"cohort_ok={cohort_ok}")
    print(f"users_file={users_txt}")
    print(f"cohort_json={cohort_json if cohort_cmd else '(skipped)'}")
    print(f"summary_json={summary_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
