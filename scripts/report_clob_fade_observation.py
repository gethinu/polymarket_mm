#!/usr/bin/env python3
"""
Summarize fade-observe metrics/log/state for a selected time window.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.request import Request, urlopen


TS_RE = r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
ENTRY_RE = re.compile(rf"^\[{TS_RE}\] entry ")
EXIT_RE = re.compile(rf"^\[{TS_RE}\] exit ")
HALT_RE = re.compile(rf"^\[{TS_RE}\] HALT: ")
ERROR_RE = re.compile(rf"^\[{TS_RE}\] error: ")


def _parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _mean(xs: list[float]) -> float:
    return float(statistics.mean(xs)) if xs else 0.0


def _p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    if len(xs) < 20:
        return float(max(xs))
    return float(statistics.quantiles(xs, n=20)[-1])


def _safe_print(s: str) -> None:
    try:
        print(s)
    except UnicodeEncodeError:
        try:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(s.encode(enc, errors="replace").decode(enc, errors="replace"))
        except Exception:
            pass


def _post_json(url: str, payload: dict, timeout_sec: float = 7.0) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "clob-fade-observe-report/1.0"},
        method="POST",
    )
    with urlopen(req, timeout=timeout_sec) as _:
        return


def _user_env_from_registry(name: str) -> str:
    if not sys.platform.startswith("win"):
        return ""
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            v, _t = winreg.QueryValueEx(k, name)
            return str(v or "").strip()
    except Exception:
        return ""


def _discord_url() -> str:
    return (
        os.getenv("CLOBBOT_DISCORD_WEBHOOK_URL")
        or _user_env_from_registry("CLOBBOT_DISCORD_WEBHOOK_URL")
        or os.getenv("DISCORD_WEBHOOK_URL")
        or _user_env_from_registry("DISCORD_WEBHOOK_URL")
        or ""
    ).strip()


@dataclass(frozen=True)
class MetricRow:
    ts: dt.datetime
    ts_ms: int
    token_id: str
    label: str
    mid: float
    spread: float
    imbalance: float
    zscore: float
    velocity_move: float
    consensus_score: float
    consensus_side: int
    consensus_agree: int
    position_side: int
    position_size: float
    entry_price: float
    unrealized_pnl: float
    realized_pnl: float
    trade_count: int
    win_count: int
    loss_count: int
    total_pnl: float
    day_pnl: float
    halted: bool


def iter_metrics(lines: Iterable[str]) -> Iterable[MetricRow]:
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        try:
            o = json.loads(line)
            ts = _parse_ts(str(o.get("ts") or ""))
            yield MetricRow(
                ts=ts,
                ts_ms=int(o.get("ts_ms") or int(ts.timestamp() * 1000.0)),
                token_id=str(o.get("token_id") or "").strip(),
                label=str(o.get("label") or "").strip(),
                mid=float(o.get("mid") or 0.0),
                spread=float(o.get("spread") or 0.0),
                imbalance=float(o.get("imbalance") or 0.0),
                zscore=float(o.get("zscore") or 0.0),
                velocity_move=float(o.get("velocity_move") or 0.0),
                consensus_score=float(o.get("consensus_score") or 0.0),
                consensus_side=int(o.get("consensus_side") or 0),
                consensus_agree=int(o.get("consensus_agree") or 0),
                position_side=int(o.get("position_side") or 0),
                position_size=float(o.get("position_size") or 0.0),
                entry_price=float(o.get("entry_price") or 0.0),
                unrealized_pnl=float(o.get("unrealized_pnl") or 0.0),
                realized_pnl=float(o.get("realized_pnl") or 0.0),
                trade_count=int(o.get("trade_count") or 0),
                win_count=int(o.get("win_count") or 0),
                loss_count=int(o.get("loss_count") or 0),
                total_pnl=float(o.get("total_pnl") or 0.0),
                day_pnl=float(o.get("day_pnl") or 0.0),
                halted=bool(o.get("halted") or False),
            )
        except Exception:
            continue


def _load_state(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return {}


def _count_events(log_file: str, since: dt.datetime, until: dt.datetime) -> dict:
    out = {"entries": 0, "exits": 0, "halts": 0, "errors": 0}
    if not log_file or not os.path.exists(log_file):
        return out
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                for name, rx in (("entries", ENTRY_RE), ("exits", EXIT_RE), ("halts", HALT_RE), ("errors", ERROR_RE)):
                    m = rx.match(line)
                    if not m:
                        continue
                    ts = _parse_ts(m.group("ts"))
                    if since <= ts <= until:
                        out[name] += 1
                    break
    except Exception:
        return out
    return out


def build_report(rows: list[MetricRow], state: dict, counts: dict, since: dt.datetime, until: dt.datetime) -> str:
    lines: list[str] = []
    lines.append(f"Window: {since:%Y-%m-%d %H:%M:%S} -> {until:%Y-%m-%d %H:%M:%S} (local)")
    if not rows:
        lines.append("No metrics samples found for the selected window.")
        lines.append(
            "Events(log): "
            f"entries={counts.get('entries', 0)} exits={counts.get('exits', 0)} "
            f"halts={counts.get('halts', 0)} errors={counts.get('errors', 0)}"
        )
        return "\n".join(lines)

    rows = sorted(rows, key=lambda r: r.ts_ms)
    ts0 = rows[0].ts
    ts1 = rows[-1].ts
    tokens = sorted({r.token_id for r in rows if r.token_id})
    mids = [r.mid for r in rows if r.mid > 0]
    spreads = [r.spread for r in rows if r.spread >= 0]
    abs_z = [abs(r.zscore) for r in rows]
    signal_hits = sum(1 for r in rows if r.consensus_side != 0)
    agree2 = sum(1 for r in rows if r.consensus_agree >= 2 and r.consensus_side != 0)
    open_now = sum(1 for r in rows[-len(tokens) :] if r.position_side != 0) if tokens else 0
    latest = rows[-1]

    lines.append(f"Samples: {len(rows)} | tokens: {len(tokens)}")
    lines.append(f"Observed: {ts0:%Y-%m-%d %H:%M:%S} -> {ts1:%Y-%m-%d %H:%M:%S}")
    lines.append(
        "Market: "
        f"mid(mean/min/max)={_mean(mids):.4f}/{min(mids) if mids else 0:.4f}/{max(mids) if mids else 0:.4f} "
        f"spread(mean/p95/max)={_mean(spreads):.4f}/{_p95(spreads):.4f}/{max(spreads) if spreads else 0:.4f}"
    )
    lines.append(
        "Signals: "
        f"consensus_side!=0 {signal_hits}/{len(rows)} ({(signal_hits/len(rows)):.1%}) | "
        f"agree>=2 {agree2}/{len(rows)} ({(agree2/len(rows)):.1%}) | "
        f"|z|(mean/p95)={_mean(abs_z):.2f}/{_p95(abs_z):.2f}"
    )
    lines.append(
        "Latest(total): "
        f"total_pnl={latest.total_pnl:+.4f} day_pnl={latest.day_pnl:+.4f} "
        f"halted={latest.halted} open_positions~{open_now}"
    )

    lines.append("Per token:")
    for tid in tokens:
        rts = [r for r in rows if r.token_id == tid]
        if not rts:
            continue
        mids2 = [r.mid for r in rts if r.mid > 0]
        spreads2 = [r.spread for r in rts if r.spread >= 0]
        sig = sum(1 for r in rts if r.consensus_side != 0)
        last = rts[-1]
        win_rate = (last.win_count / max(1, last.trade_count)) if last.trade_count > 0 else 0.0
        lines.append(
            f"- {last.label[:70]} ({tid[:10]}...)\n"
            f"  samples={len(rts)} signals={sig} mid={_mean(mids2):.4f} spread={_mean(spreads2):.4f} "
            f"pos={last.position_side:+d} unreal={last.unrealized_pnl:+.4f} realized={last.realized_pnl:+.4f} "
            f"trades={last.trade_count} winrate={win_rate:.1%}"
        )

    if state:
        halted = bool(state.get("halted") or False)
        halt_reason = str(state.get("halt_reason") or "")
        lines.append(
            "State: "
            f"day_key={state.get('day_key', '')} "
            f"entries={int(state.get('entries') or 0)} exits={int(state.get('exits') or 0)} "
            f"signals={int(state.get('signals_seen') or 0)} active_tokens={len(state.get('active_token_ids') or [])} "
            f"halted={halted}"
        )
        if halted and halt_reason:
            lines.append(f"Halt reason: {halt_reason}")
    else:
        lines.append("State: not found")

    lines.append(
        "Events(log): "
        f"entries={counts.get('entries', 0)} exits={counts.get('exits', 0)} "
        f"halts={counts.get('halts', 0)} errors={counts.get('errors', 0)}"
    )
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Summarize CLOB fade observe metrics/log/state")
    p.add_argument(
        "--metrics-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "clob-fade-observe-metrics.jsonl"),
        help="Path to metrics JSONL",
    )
    p.add_argument(
        "--log-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "clob-fade-observe.log"),
        help="Path to event log",
    )
    p.add_argument(
        "--state-file",
        default=str(Path(__file__).resolve().parents[1] / "logs" / "clob_fade_observe_state.json"),
        help="Path to runtime state JSON",
    )
    p.add_argument("--hours", type=float, default=24.0, help="Lookback window in hours")
    p.add_argument("--since", default="", help='Start timestamp "YYYY-MM-DD HH:MM:SS" (overrides --hours)')
    p.add_argument("--until", default="", help='End timestamp "YYYY-MM-DD HH:MM:SS" (default: now)')
    p.add_argument("--discord", action="store_true", help="Post report to Discord webhook")
    args = p.parse_args()

    now = dt.datetime.now()
    until: dt.datetime = _parse_ts(args.until) if args.until else now
    since: dt.datetime = _parse_ts(args.since) if args.since else (until - dt.timedelta(hours=float(args.hours)))

    rows: list[MetricRow] = []
    if os.path.exists(args.metrics_file):
        with open(args.metrics_file, "r", encoding="utf-8", errors="replace") as f:
            rows = [r for r in iter_metrics(f) if (since <= r.ts <= until)]

    state = _load_state(args.state_file)
    counts = _count_events(args.log_file, since, until)
    report = build_report(rows, state, counts, since, until)
    _safe_print(report)

    if args.discord:
        url = _discord_url()
        if not url:
            print("\nDiscord webhook not configured (CLOBBOT_DISCORD_WEBHOOK_URL).", file=sys.stderr)
            return 3
        content = report if len(report) <= 1900 else report[:1900] + "\n...(truncated)"
        try:
            _post_json(url, {"content": f"```text\n{content}\n```"})
        except Exception as e:
            code = getattr(e, "code", None)
            if isinstance(code, int):
                print(f"\nDiscord post failed: HTTP {code}", file=sys.stderr)
            else:
                print(f"\nDiscord post failed: {type(e).__name__}", file=sys.stderr)
            return 4

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
