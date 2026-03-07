#!/usr/bin/env python3
"""
Capture an observe-only baseline snapshot for fade checkpoint evaluation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any


TS_FMT = "%Y-%m-%d %H:%M:%S"


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _sum_state_pnl(state: dict) -> tuple[float, float, int]:
    token_states = state.get("token_states")
    if not isinstance(token_states, dict):
        return (0.0, 0.0, 0)
    realized = 0.0
    unrealized = 0.0
    open_positions = 0
    for row in token_states.values():
        if not isinstance(row, dict):
            continue
        realized += _to_float(row.get("realized_pnl"))
        unrealized += _to_float(row.get("unrealized_pnl"))
        side = _to_int(row.get("position_side"))
        size = _to_float(row.get("position_size"))
        if side != 0 and size > 0.0:
            open_positions += 1
    return (float(realized), float(unrealized), int(open_positions))


def build_payload(args) -> dict:
    now_local = dt.datetime.now()
    now_utc = dt.datetime.now(dt.timezone.utc)
    eval_at_local = now_local + dt.timedelta(hours=float(args.eval_after_hours))
    eval_at_utc = now_utc + dt.timedelta(hours=float(args.eval_after_hours))
    tag = str(args.baseline_tag or "").strip() or now_local.strftime("%Y%m%d_%H%M%S")

    state_file = Path(args.state_file)
    state = _load_state(state_file)
    realized_sum, unrealized_sum, open_positions = _sum_state_pnl(state)
    total_pnl = realized_sum + unrealized_sum
    day_anchor_total = _to_float(state.get("day_anchor_total_pnl"))
    day_pnl = total_pnl - day_anchor_total

    return {
        "phase": str(args.phase),
        "baseline_tag": tag,
        "started_at_local": now_local.strftime(TS_FMT),
        "started_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target_eval_after_local": eval_at_local.strftime(TS_FMT),
        "target_eval_after_utc": eval_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state_file": str(state_file),
        "total_pnl_usd": float(total_pnl),
        "realized_pnl_usd": float(realized_sum),
        "unrealized_pnl_usd": float(unrealized_sum),
        "open_positions_total": int(open_positions),
        "day_key": str(state.get("day_key") or ""),
        "day_anchor_total_pnl": float(day_anchor_total),
        "day_pnl_usd": float(day_pnl),
        "entries": _to_int(state.get("entries")),
        "exits": _to_int(state.get("exits")),
        "signals_seen": _to_int(state.get("signals_seen")),
        "long_trades": _to_int(state.get("long_trades")),
        "long_wins": _to_int(state.get("long_wins")),
        "short_trades": _to_int(state.get("short_trades")),
        "short_wins": _to_int(state.get("short_wins")),
        "halted": bool(state.get("halted") or False),
    }


def parse_args():
    repo = Path(__file__).resolve().parents[1]
    logs = repo / "logs"
    p = argparse.ArgumentParser(description="Capture fade checkpoint baseline snapshot (observe-only)")
    p.add_argument("--phase", required=True, help="Baseline phase tag (e.g. fade_regime_both_redesign)")
    p.add_argument("--state-file", required=True, help="Fade state JSON path")
    p.add_argument("--baseline-tag", default="", help="Optional explicit baseline tag")
    p.add_argument("--eval-after-hours", type=float, default=24.0, help="Target evaluation time offset in hours")
    p.add_argument(
        "--out-json",
        default=str(logs / "fade_checkpoint_baseline_latest.json"),
        help="Output baseline JSON path",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_payload(args)

    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"phase={payload['phase']} baseline_tag={payload['baseline_tag']}")
    print(f"state_file={payload['state_file']}")
    print(f"total_pnl_usd={payload['total_pnl_usd']:+.4f} entries={int(payload['entries'])} exits={int(payload['exits'])}")
    print(f"out_json={out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

