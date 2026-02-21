#!/usr/bin/env python3
"""
Observe-only side router for polymarket_clob_fade_observe.py.

Reads long/short (and optional both) canary state JSON files and writes a
runtime control JSON that can hot-reload into the main fade observer.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from polymarket_clob_arb_scanner import as_float


def now_ts() -> float:
    return time.time()


def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Logger:
    def __init__(self, log_file: Optional[str]):
        self.log_file = Path(log_file) if log_file else None
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, msg: str) -> None:
        if not self.log_file:
            return
        with self.log_file.open("a", encoding="utf-8", newline="\n") as f:
            f.write(msg + "\n")

    def info(self, msg: str) -> None:
        try:
            print(msg)
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))
        self._append(msg)


@dataclass
class VariantStats:
    name: str
    path: str
    age_sec: float
    entries: int
    exits: int
    trades: int
    wins: int
    realized: float
    unrealized: float
    total: float
    day_pnl: float
    ok: bool
    reason: str

    @property
    def winrate(self) -> float:
        if self.trades <= 0:
            return 0.0
        return float(self.wins) / float(self.trades)


def _load_variant_state(path: Path, name: str, stale_sec: float) -> VariantStats:
    if not path.exists():
        return VariantStats(
            name=name,
            path=str(path),
            age_sec=1e9,
            entries=0,
            exits=0,
            trades=0,
            wins=0,
            realized=0.0,
            unrealized=0.0,
            total=0.0,
            day_pnl=0.0,
            ok=False,
            reason="missing",
        )
    try:
        mtime = float(path.stat().st_mtime)
    except Exception:
        mtime = 0.0
    age = max(0.0, now_ts() - mtime) if mtime > 0 else 1e9

    raw = None
    last_err: Optional[Exception] = None
    for _ in range(4):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            break
        except Exception as e:
            last_err = e
            time.sleep(0.05)
    if raw is None:
        return VariantStats(
            name=name,
            path=str(path),
            age_sec=age,
            entries=0,
            exits=0,
            trades=0,
            wins=0,
            realized=0.0,
            unrealized=0.0,
            total=0.0,
            day_pnl=0.0,
            ok=False,
            reason=f"parse-error:{last_err}",
        )

    realized = 0.0
    unrealized = 0.0
    trades = 0
    wins = 0
    for tok in (raw.get("token_states") or {}).values():
        if not isinstance(tok, dict):
            continue
        realized += as_float(tok.get("realized_pnl"), 0.0)
        side = int(tok.get("position_side") or 0)
        size = as_float(tok.get("position_size"), 0.0)
        last_mid = as_float(tok.get("last_mid"), 0.0)
        entry_price = as_float(tok.get("entry_price"), 0.0)
        if side != 0 and size > 0 and last_mid > 0 and entry_price > 0:
            unrealized += float(side) * (last_mid - entry_price) * size
        else:
            unrealized += as_float(tok.get("unrealized_pnl"), 0.0)
        trades += int(tok.get("trade_count") or 0)
        wins += int(tok.get("win_count") or 0)

    total = realized + unrealized
    day_anchor = as_float(raw.get("day_anchor_total_pnl"), 0.0)
    day_pnl = total - day_anchor
    entries = int(raw.get("entries") or 0)
    exits = int(raw.get("exits") or 0)

    ok = True
    reason = "ok"
    if stale_sec > 0 and age > stale_sec:
        ok = False
        reason = f"stale>{stale_sec:g}s"

    return VariantStats(
        name=name,
        path=str(path),
        age_sec=age,
        entries=entries,
        exits=exits,
        trades=trades,
        wins=wins,
        realized=realized,
        unrealized=unrealized,
        total=total,
        day_pnl=day_pnl,
        ok=ok,
        reason=reason,
    )


def _to_dict(v: Optional[VariantStats]) -> dict:
    if v is None:
        return {"ok": False, "reason": "disabled"}
    return {
        "ok": bool(v.ok),
        "reason": v.reason,
        "age_sec": round(float(v.age_sec), 3),
        "entries": int(v.entries),
        "exits": int(v.exits),
        "trades": int(v.trades),
        "wins": int(v.wins),
        "winrate": round(float(v.winrate), 6),
        "realized": round(float(v.realized), 6),
        "unrealized": round(float(v.unrealized), 6),
        "total": round(float(v.total), 6),
        "day_pnl": round(float(v.day_pnl), 6),
        "path": v.path,
    }


def _eligible(v: Optional[VariantStats], min_exits: int) -> bool:
    return bool(v and v.ok and int(v.exits) >= int(min_exits))


def _metric_value(v: VariantStats, metric: str) -> float:
    m = str(metric or "per_exit").strip().lower()
    if m == "cumulative":
        return float(v.realized)
    denom = max(1, int(v.exits))
    return float(v.realized) / float(denom)


def _metric_label(metric: str) -> str:
    m = str(metric or "per_exit").strip().lower()
    if m == "cumulative":
        return "realized"
    return "realized/exits"


def _choose_side(
    long_v: Optional[VariantStats],
    short_v: Optional[VariantStats],
    both_v: Optional[VariantStats],
    current_side: str,
    last_switch_ts: float,
    min_exits: int,
    decision_metric: str,
    switch_margin_usd: float,
    both_keep_margin_usd: float,
    hold_side_sec: float,
) -> Tuple[str, str]:
    long_ok = _eligible(long_v, min_exits=min_exits)
    short_ok = _eligible(short_v, min_exits=min_exits)
    both_ok = _eligible(both_v, min_exits=min_exits) if both_v else False

    choice = "both"
    reason = "fallback both"

    metric_name = _metric_label(decision_metric)

    if long_ok and short_ok:
        long_metric = _metric_value(long_v, decision_metric)
        short_metric = _metric_value(short_v, decision_metric)
        diff = float(long_metric) - float(short_metric)
        if diff >= float(switch_margin_usd):
            choice = "long"
            reason = (
                f"long lead {metric_name} {long_metric:+.6f} vs short {short_metric:+.6f} "
                f"(diff={diff:+.4f})"
            )
        elif diff <= -float(switch_margin_usd):
            choice = "short"
            reason = (
                f"short lead {metric_name} {short_metric:+.6f} vs long {long_metric:+.6f} "
                f"(diff={diff:+.4f})"
            )
        else:
            choice = "both"
            reason = f"long/short {metric_name} diff too small ({diff:+.4f})"
    elif long_ok:
        choice = "long"
        reason = "short ineligible; use long"
    elif short_ok:
        choice = "short"
        reason = "long ineligible; use short"
    elif both_ok:
        choice = "both"
        reason = "only both eligible"

    if both_ok and choice in ("long", "short"):
        ref = long_v if choice == "long" else short_v
        if ref is not None:
            both_metric = _metric_value(both_v, decision_metric)
            ref_metric = _metric_value(ref, decision_metric)
            if (float(both_metric) - float(ref_metric)) >= float(both_keep_margin_usd):
                choice = "both"
                reason = (
                    f"both outperform keeps base ({metric_name}: both={both_metric:+.6f}, "
                    f"{ref.name}={ref_metric:+.6f})"
                )

    if current_side in ("both", "long", "short") and choice != current_side and float(hold_side_sec) > 0:
        elapsed = now_ts() - float(last_switch_ts or 0.0)
        if elapsed < float(hold_side_sec):
            remain = max(0.0, float(hold_side_sec) - elapsed)
            return current_side, f"hold {current_side} ({remain:.0f}s left); candidate={choice}; {reason}"
    return choice, reason


def _write_control(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    tmp.replace(path)


def parse_args():
    script_dir = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Observe-only side router for CLOB fade monitor")
    p.add_argument(
        "--long-state",
        default=str(script_dir.parent / "logs" / "clob_fade_observe_profit_long_canary_state.json"),
        help="Long canary state JSON",
    )
    p.add_argument(
        "--short-state",
        default=str(script_dir.parent / "logs" / "clob_fade_observe_profit_short_canary_state.json"),
        help="Short canary state JSON",
    )
    p.add_argument(
        "--both-state",
        default=str(script_dir.parent / "logs" / "clob_fade_observe_profit_live_v3_state.json"),
        help="Optional both-side baseline state JSON",
    )
    p.add_argument(
        "--out-control",
        default=str(script_dir.parent / "logs" / "clob_fade_runtime_control.json"),
        help="Output control JSON for polymarket_clob_fade_observe.py --control-file",
    )
    p.add_argument("--stale-sec", type=float, default=240.0, help="Ignore canary files older than this")
    p.add_argument("--min-exits", type=int, default=6, help="Min exits required for a canary to be eligible")
    p.add_argument(
        "--decision-metric",
        choices=("per_exit", "cumulative"),
        default="per_exit",
        help="Metric used for side comparison (default: realized per exit)",
    )
    p.add_argument(
        "--switch-margin-usd",
        type=float,
        default=0.0008,
        help="Required lead in selected decision metric before switching long<->short",
    )
    p.add_argument(
        "--both-keep-margin-usd",
        type=float,
        default=0.0010,
        help="Keep both if both baseline outperforms chosen side by this margin (selected metric)",
    )
    p.add_argument("--hold-side-sec", type=float, default=900.0, help="Minimum hold time before changing side")
    p.add_argument("--poll-sec", type=float, default=30.0, help="Polling interval (seconds)")
    p.add_argument("--run-seconds", type=int, default=0, help="Stop after N seconds (0=forever)")
    p.add_argument("--once", action="store_true", help="Run one cycle and exit")
    p.add_argument(
        "--log-file",
        default=str(script_dir.parent / "logs" / "fade-side-router.log"),
        help="Router event log path",
    )
    return p.parse_args()


def run(args) -> int:
    logger = Logger(args.log_file)
    logger.info("Fade Side Router (observe-only)")
    logger.info("=" * 60)
    metric_name = _metric_label(args.decision_metric)
    logger.info(
        f"min_exits={int(args.min_exits)} stale={float(args.stale_sec):g}s "
        f"metric={metric_name} "
        f"switch_margin={float(args.switch_margin_usd):g} both_keep_margin={float(args.both_keep_margin_usd):g} "
        f"hold={float(args.hold_side_sec):g}s poll={float(args.poll_sec):g}s"
    )

    long_state = Path(args.long_state)
    short_state = Path(args.short_state)
    both_state = Path(args.both_state) if str(args.both_state).strip() else None
    out_control = Path(args.out_control)

    current_side = "both"
    # Allow immediate decision on startup; hold timer applies after first switch.
    last_switch_ts = now_ts() - max(0.0, float(args.hold_side_sec))
    t0 = now_ts()

    while True:
        long_v = _load_variant_state(long_state, "long", stale_sec=float(args.stale_sec))
        short_v = _load_variant_state(short_state, "short", stale_sec=float(args.stale_sec))
        both_v = _load_variant_state(both_state, "both", stale_sec=float(args.stale_sec)) if both_state else None

        decided_side, reason = _choose_side(
            long_v=long_v,
            short_v=short_v,
            both_v=both_v,
            current_side=current_side,
            last_switch_ts=last_switch_ts,
            min_exits=int(args.min_exits),
            decision_metric=str(args.decision_metric),
            switch_margin_usd=float(args.switch_margin_usd),
            both_keep_margin_usd=float(args.both_keep_margin_usd),
            hold_side_sec=float(args.hold_side_sec),
        )
        if decided_side != current_side:
            prev = current_side
            current_side = decided_side
            last_switch_ts = now_ts()
            logger.info(f"[{iso_now()}] switch {prev}->{current_side} | {reason}")

        payload = {
            "generated_at": iso_now(),
            "source": "fade_side_router",
            "reason": reason,
            "overrides": {
                "allowed_sides": current_side,
            },
            "decision_metric": metric_name,
            "variants": {
                "long": _to_dict(long_v),
                "short": _to_dict(short_v),
                "both": _to_dict(both_v) if both_v else {"ok": False, "reason": "disabled"},
            },
        }
        _write_control(out_control, payload)

        long_metric = _metric_value(long_v, args.decision_metric)
        short_metric = _metric_value(short_v, args.decision_metric)
        both_metric = _metric_value(both_v, args.decision_metric) if both_v else 0.0
        logger.info(
            f"[{iso_now()}] decision={current_side} "
            f"| long={long_metric:+.6f}({long_v.realized:+.4f}/ex{long_v.exits})/{long_v.reason} "
            f"| short={short_metric:+.6f}({short_v.realized:+.4f}/ex{short_v.exits})/{short_v.reason} "
            f"| both={both_metric:+.6f}({(both_v.realized if both_v else 0.0):+.4f})"
        )

        if args.once:
            break
        if args.run_seconds and (now_ts() - t0) >= int(args.run_seconds):
            logger.info("run-seconds reached. stopping.")
            break
        time.sleep(max(1.0, float(args.poll_sec)))
    return 0


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
