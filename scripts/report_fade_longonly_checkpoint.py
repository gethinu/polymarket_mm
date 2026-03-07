#!/usr/bin/env python3
"""
Build a 24h intermediate checkpoint for the local long-only fade profile.

Observe-only utility. It does not place orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


TS_FMT = "%Y-%m-%d %H:%M:%S"
ENTRY_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] entry ")
EXIT_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] exit (?P<reason>[a-z_]+) "
    r"pnl=(?P<pnl>[+\-]?\d+(?:\.\d+)?) "
)


def _parse_local_ts(raw: str) -> Optional[dt.datetime]:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, TS_FMT)
    except Exception:
        return None


def _read_tail_lines(path: Path, max_bytes: int) -> list[str]:
    if not path.exists():
        return []
    size = path.stat().st_size
    if size <= 0:
        return []
    seek = max(0, size - max(1024, int(max_bytes)))
    with path.open("rb") as f:
        f.seek(seek)
        raw = f.read().decode("utf-8", errors="replace")
    lines = raw.splitlines()
    if seek > 0 and lines:
        lines = lines[1:]
    return lines


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


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _iter_metric_rows(lines: Iterable[str]) -> Iterable[dict]:
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        ts = _parse_local_ts(str(row.get("ts") or ""))
        if ts is None:
            continue
        total = row.get("total_pnl")
        if total is None:
            continue
        ts_ms = _to_int(row.get("ts_ms"), int(ts.timestamp() * 1000.0))
        yield {
            "ts": ts,
            "ts_text": str(row.get("ts") or ""),
            "ts_ms": ts_ms,
            "total_pnl": _to_float(total),
            "day_pnl": _to_float(row.get("day_pnl")),
            "realized_total": _to_float(row.get("realized_total")),
            "unrealized_total": _to_float(row.get("unrealized_total")),
            "open_positions_total": _to_int(row.get("open_positions_total")),
        }


def _count_open_positions_from_state(state: dict) -> int:
    token_states = state.get("token_states")
    if not isinstance(token_states, dict):
        return 0
    count = 0
    for row in token_states.values():
        if not isinstance(row, dict):
            continue
        side = _to_int(row.get("position_side"))
        size = _to_float(row.get("position_size"))
        if side != 0 and size > 0:
            count += 1
    return count


def _rate(numer: int, denom: int) -> Optional[float]:
    if denom <= 0:
        return None
    return float(numer) / float(denom)


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    return f"{(100.0 * v):.1f}%"


def _fmt_num(v: Optional[float], prec: int = 4) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.{prec}f}"


def _supervisor_job_start_ts_ms(supervisor_state: dict, job_name: str) -> Optional[int]:
    jobs = supervisor_state.get("jobs")
    if not isinstance(jobs, list):
        return None
    for row in jobs:
        if not isinstance(row, dict):
            continue
        if str(row.get("name") or "") != str(job_name):
            continue
        ts = _to_int(row.get("last_start_ts"), 0)
        if ts <= 0:
            return None
        return ts
    return None


def _supervisor_job_command(supervisor_state: dict, job_name: str) -> list[str]:
    jobs = supervisor_state.get("jobs")
    if not isinstance(jobs, list):
        return []
    for row in jobs:
        if not isinstance(row, dict):
            continue
        if str(row.get("name") or "") != str(job_name):
            continue
        cmd = row.get("command")
        if isinstance(cmd, list):
            return [str(x) for x in cmd]
        return []
    return []


def _extract_arg_from_cmd(command: list[str], arg_name: str) -> Optional[str]:
    key = str(arg_name or "").strip()
    if not key:
        return None
    for idx, token in enumerate(command):
        if token != key:
            continue
        next_idx = idx + 1
        if next_idx < len(command):
            return str(command[next_idx])
        return None
    return None


def _max_drawdown_from_rows(rows: list[dict], origin_total: Optional[float] = None) -> Optional[float]:
    if not rows:
        return None
    if origin_total is None:
        origin_total = float(rows[0]["total_pnl"])
    peak = 0.0
    dd = 0.0
    for row in rows:
        equity = float(row["total_pnl"]) - float(origin_total)
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > dd:
            dd = drawdown
    return float(dd)


def _window_event_counts(log_lines: Iterable[str], since: dt.datetime, until: dt.datetime) -> dict:
    out = {
        "entries": 0,
        "exits": 0,
        "wins": 0,
        "losses": 0,
        "nonpositive_exits": 0,
        "net_pnl_usd": 0.0,
        "gross_profit_usd": 0.0,
        "gross_loss_usd": 0.0,
        "profit_factor": None,
        "exit_reasons": {},
    }
    reasons: Dict[str, int] = {}
    for line in log_lines:
        m_entry = ENTRY_RE.match(line)
        if m_entry:
            ts = _parse_local_ts(m_entry.group("ts"))
            if ts is not None and since <= ts <= until:
                out["entries"] += 1
            continue
        m_exit = EXIT_RE.match(line)
        if not m_exit:
            continue
        ts = _parse_local_ts(m_exit.group("ts"))
        if ts is None or ts < since or ts > until:
            continue
        out["exits"] += 1
        reason = str(m_exit.group("reason") or "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1

        pnl = _to_float(m_exit.group("pnl"), 0.0)
        out["net_pnl_usd"] += pnl
        if pnl > 1e-12:
            out["wins"] += 1
            out["gross_profit_usd"] += pnl
        else:
            out["nonpositive_exits"] += 1
            if pnl < -1e-12:
                out["losses"] += 1
                out["gross_loss_usd"] += abs(pnl)

    gross_profit = float(out["gross_profit_usd"])
    gross_loss = float(out["gross_loss_usd"])
    if gross_loss > 1e-12:
        out["profit_factor"] = gross_profit / gross_loss
    elif gross_profit > 1e-12:
        out["profit_factor"] = float("inf")
    else:
        out["profit_factor"] = 0.0

    out["exit_reasons"] = dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])))
    return out


def _pnl_per_trade_cents_per_share(pnl_per_trade_usd: Optional[float], position_size_shares: Optional[float]) -> Optional[float]:
    if pnl_per_trade_usd is None:
        return None
    size = _to_float(position_size_shares, 0.0)
    if size <= 0:
        return None
    return (100.0 * float(pnl_per_trade_usd)) / float(size)


def _pnl_per_trade_usd(net_pnl_usd: Any, exits: Any) -> Optional[float]:
    n = _to_float(net_pnl_usd, 0.0)
    d = _to_int(exits, 0)
    if d <= 0:
        return None
    return float(n) / float(d)


def build_checkpoint(args) -> tuple[dict, str]:
    now_local = dt.datetime.now()
    now_utc = dt.datetime.now(dt.timezone.utc)
    since = now_local - dt.timedelta(hours=float(args.hours))

    metrics_file = Path(args.metrics_file)
    state_file = Path(args.state_file)
    log_file = Path(args.log_file)
    baseline_file = Path(args.baseline_json)
    supervisor_state_file = Path(args.supervisor_state_file)

    baseline = _load_json(baseline_file)
    baseline_started_local_raw = str(baseline.get("started_at_local") or "").strip()
    baseline_start_local = _parse_local_ts(str(args.baseline_start_local or "").strip())
    if baseline_start_local is None:
        baseline_start_local = _parse_local_ts(baseline_started_local_raw)

    metric_lines = _read_tail_lines(metrics_file, max_bytes=int(args.tail_bytes))
    metric_rows = list(_iter_metric_rows(metric_lines))
    if not metric_rows:
        raise RuntimeError(f"no metric rows parsed from {metrics_file}")

    latest = metric_rows[-1]
    first_in_window = next((r for r in metric_rows if r["ts"] >= since), metric_rows[0])
    delta_in_window = float(latest["total_pnl"]) - float(first_in_window["total_pnl"])

    supervisor_state = _load_json(supervisor_state_file)
    start_ts_ms = _supervisor_job_start_ts_ms(supervisor_state, args.supervisor_job_name)
    job_cmd = _supervisor_job_command(supervisor_state, args.supervisor_job_name)
    job_position_size = _to_float(_extract_arg_from_cmd(job_cmd, "--position-size-shares"), 0.0)
    first_since_start = None
    rows_since_start: list[dict] = []
    if start_ts_ms is not None:
        rows_since_start = [r for r in metric_rows if int(r["ts_ms"]) >= int(start_ts_ms)]
        first_since_start = rows_since_start[0] if rows_since_start else None
    delta_since_start = None
    if first_since_start is not None:
        delta_since_start = float(latest["total_pnl"]) - float(first_since_start["total_pnl"])
    dd_in_window = _max_drawdown_from_rows([r for r in metric_rows if r["ts"] >= since] or [first_in_window, latest], origin_total=float(first_in_window["total_pnl"]))
    dd_since_start = _max_drawdown_from_rows(rows_since_start, origin_total=float(first_since_start["total_pnl"])) if first_since_start is not None else None

    baseline_total = baseline.get("total_pnl_usd")
    rows_since_baseline: list[dict] = []
    if baseline_start_local is not None:
        rows_since_baseline = [r for r in metric_rows if r["ts"] >= baseline_start_local]
    first_since_baseline = rows_since_baseline[0] if rows_since_baseline else None
    baseline_origin_total = _to_float(baseline_total) if baseline_total is not None else None
    delta_since_baseline = None
    if baseline_origin_total is not None:
        delta_since_baseline = float(latest["total_pnl"]) - float(baseline_origin_total)
    elif first_since_baseline is not None:
        delta_since_baseline = float(latest["total_pnl"]) - float(first_since_baseline["total_pnl"])
    dd_since_baseline = None
    if rows_since_baseline:
        if baseline_origin_total is not None:
            dd_since_baseline = _max_drawdown_from_rows(rows_since_baseline, origin_total=float(baseline_origin_total))
        elif first_since_baseline is not None:
            dd_since_baseline = _max_drawdown_from_rows(rows_since_baseline, origin_total=float(first_since_baseline["total_pnl"]))

    state = _load_json(state_file)
    log_lines = _read_tail_lines(log_file, max_bytes=max(2 * 1024 * 1024, int(args.tail_bytes) // 2))
    events = _window_event_counts(log_lines, since=since, until=now_local)
    events_since_start = (
        _window_event_counts(log_lines, since=dt.datetime.fromtimestamp(float(start_ts_ms) / 1000.0), until=now_local)
        if start_ts_ms is not None
        else {"entries": 0, "exits": 0, "wins": 0, "losses": 0, "nonpositive_exits": 0, "net_pnl_usd": 0.0, "gross_profit_usd": 0.0, "gross_loss_usd": 0.0, "profit_factor": None, "exit_reasons": {}}
    )
    events_since_baseline = (
        _window_event_counts(log_lines, since=baseline_start_local, until=now_local)
        if baseline_start_local is not None
        else {"entries": 0, "exits": 0, "wins": 0, "losses": 0, "nonpositive_exits": 0, "net_pnl_usd": 0.0, "gross_profit_usd": 0.0, "gross_loss_usd": 0.0, "profit_factor": None, "exit_reasons": {}}
    )
    exits = _to_int(events.get("exits"))
    reasons = events.get("exit_reasons") if isinstance(events.get("exit_reasons"), dict) else {}
    timeout_count = _to_int(reasons.get("timeout"))
    tp_count = _to_int(reasons.get("tp"))
    sl_count = _to_int(reasons.get("sl"))
    trail_count = _to_int(reasons.get("trail"))
    breakeven_count = _to_int(reasons.get("breakeven"))
    pnl_per_trade_in_window = _pnl_per_trade_usd(events.get("net_pnl_usd"), exits)

    exits_since_start = _to_int(events_since_start.get("exits"))
    reasons_since_start = (
        events_since_start.get("exit_reasons")
        if isinstance(events_since_start.get("exit_reasons"), dict)
        else {}
    )
    timeout_since_start = _to_int(reasons_since_start.get("timeout"))
    tp_since_start = _to_int(reasons_since_start.get("tp"))
    sl_since_start = _to_int(reasons_since_start.get("sl"))
    trail_since_start = _to_int(reasons_since_start.get("trail"))
    breakeven_since_start = _to_int(reasons_since_start.get("breakeven"))
    pnl_per_trade_since_start = _pnl_per_trade_usd(
        events_since_start.get("net_pnl_usd"),
        exits_since_start,
    )
    exits_since_baseline = _to_int(events_since_baseline.get("exits"))
    reasons_since_baseline = (
        events_since_baseline.get("exit_reasons")
        if isinstance(events_since_baseline.get("exit_reasons"), dict)
        else {}
    )
    timeout_since_baseline = _to_int(reasons_since_baseline.get("timeout"))
    tp_since_baseline = _to_int(reasons_since_baseline.get("tp"))
    sl_since_baseline = _to_int(reasons_since_baseline.get("sl"))
    trail_since_baseline = _to_int(reasons_since_baseline.get("trail"))
    breakeven_since_baseline = _to_int(reasons_since_baseline.get("breakeven"))
    pnl_per_trade_since_baseline = _pnl_per_trade_usd(
        events_since_baseline.get("net_pnl_usd"),
        exits_since_baseline,
    )

    baseline_entries = baseline.get("entries")
    baseline_signals = baseline.get("signals_seen")
    delta_vs_baseline = None
    if baseline_total is not None:
        delta_vs_baseline = float(latest["total_pnl"]) - _to_float(baseline_total)

    long_trades = _to_int(state.get("long_trades"))
    long_wins = _to_int(state.get("long_wins"))
    long_win_rate = _rate(long_wins, long_trades)
    open_from_state = _count_open_positions_from_state(state)

    payload = {
        "generated_local": now_local.strftime(TS_FMT),
        "generated_utc": now_utc.isoformat(),
        "window_hours": float(args.hours),
        "window_start_local": since.strftime(TS_FMT),
        "source_files": {
            "metrics_file": str(metrics_file),
            "state_file": str(state_file),
            "log_file": str(log_file),
            "baseline_json": str(baseline_file),
            "supervisor_state_file": str(supervisor_state_file),
        },
        "runtime": {
            "supervisor_job_name": str(args.supervisor_job_name),
            "supervisor_job_last_start_ts_ms": start_ts_ms,
            "job_position_size_shares": (job_position_size if job_position_size > 0 else None),
            "baseline_start_local_effective": baseline_start_local.strftime(TS_FMT) if baseline_start_local is not None else None,
        },
        "baseline": {
            "baseline_tag": baseline.get("baseline_tag"),
            "baseline_started_local": baseline.get("started_at_local"),
            "baseline_total_pnl_usd": _to_float(baseline_total) if baseline_total is not None else None,
            "baseline_entries": _to_int(baseline_entries) if baseline_entries is not None else None,
            "baseline_signals": _to_int(baseline_signals) if baseline_signals is not None else None,
        },
        "current": {
            "ts_local": latest["ts_text"],
            "total_pnl_usd": float(latest["total_pnl"]),
            "day_pnl_usd": float(latest["day_pnl"]),
            "realized_total_usd": float(latest["realized_total"]),
            "unrealized_total_usd": float(latest["unrealized_total"]),
            "open_positions_total": _to_int(latest["open_positions_total"]),
            "open_positions_total_from_state": int(open_from_state),
            "entries_total": _to_int(state.get("entries")),
            "exits_total": _to_int(state.get("exits")),
            "signals_total": _to_int(state.get("signals_seen")),
            "long_trades": long_trades,
            "long_wins": long_wins,
            "long_win_rate": long_win_rate,
        },
        "delta": {
            "vs_baseline_total_pnl_usd": delta_vs_baseline,
            "vs_baseline_entries": (_to_int(state.get("entries")) - _to_int(baseline_entries)) if baseline_entries is not None else None,
            "vs_baseline_signals": (_to_int(state.get("signals_seen")) - _to_int(baseline_signals)) if baseline_signals is not None else None,
            "in_window_total_pnl_change_usd": delta_in_window,
            "in_window_max_drawdown_usd": dd_in_window,
            "in_window_pnl_per_trade_usd": pnl_per_trade_in_window,
            "in_window_pnl_per_trade_cents_per_share": _pnl_per_trade_cents_per_share(pnl_per_trade_in_window, job_position_size if job_position_size > 0 else None),
            "since_supervisor_start_total_pnl_change_usd": delta_since_start,
            "since_supervisor_start_max_drawdown_usd": dd_since_start,
            "since_supervisor_start_pnl_per_trade_usd": pnl_per_trade_since_start,
            "since_supervisor_start_pnl_per_trade_cents_per_share": _pnl_per_trade_cents_per_share(pnl_per_trade_since_start, job_position_size if job_position_size > 0 else None),
            "since_baseline_total_pnl_change_usd": delta_since_baseline,
            "since_baseline_max_drawdown_usd": dd_since_baseline,
            "since_baseline_pnl_per_trade_usd": pnl_per_trade_since_baseline,
            "since_baseline_pnl_per_trade_cents_per_share": _pnl_per_trade_cents_per_share(
                pnl_per_trade_since_baseline,
                job_position_size if job_position_size > 0 else None,
            ),
        },
        "window_events": {
            "entries": _to_int(events.get("entries")),
            "exits": exits,
            "exit_reasons": reasons,
            "wins": _to_int(events.get("wins")),
            "losses": _to_int(events.get("losses")),
            "nonpositive_exits": _to_int(events.get("nonpositive_exits")),
            "net_pnl_usd": _to_float(events.get("net_pnl_usd")),
            "gross_profit_usd": _to_float(events.get("gross_profit_usd")),
            "gross_loss_usd": _to_float(events.get("gross_loss_usd")),
            "profit_factor": events.get("profit_factor"),
            "pnl_per_trade_usd": pnl_per_trade_in_window,
            "pnl_per_trade_cents_per_share": _pnl_per_trade_cents_per_share(
                pnl_per_trade_in_window,
                job_position_size if job_position_size > 0 else None,
            ),
            "timeout_rate": _rate(timeout_count, exits),
            "tp_rate": _rate(tp_count, exits),
            "sl_rate": _rate(sl_count, exits),
            "trail_rate": _rate(trail_count, exits),
            "breakeven_rate": _rate(breakeven_count, exits),
        },
        "since_baseline": {
            "entries": _to_int(events_since_baseline.get("entries")),
            "exits": exits_since_baseline,
            "exit_reasons": reasons_since_baseline,
            "wins": _to_int(events_since_baseline.get("wins")),
            "losses": _to_int(events_since_baseline.get("losses")),
            "nonpositive_exits": _to_int(events_since_baseline.get("nonpositive_exits")),
            "net_pnl_usd": _to_float(events_since_baseline.get("net_pnl_usd")),
            "gross_profit_usd": _to_float(events_since_baseline.get("gross_profit_usd")),
            "gross_loss_usd": _to_float(events_since_baseline.get("gross_loss_usd")),
            "profit_factor": events_since_baseline.get("profit_factor"),
            "pnl_per_trade_usd": pnl_per_trade_since_baseline,
            "pnl_per_trade_cents_per_share": _pnl_per_trade_cents_per_share(
                pnl_per_trade_since_baseline,
                job_position_size if job_position_size > 0 else None,
            ),
            "timeout_rate": _rate(timeout_since_baseline, exits_since_baseline),
            "tp_rate": _rate(tp_since_baseline, exits_since_baseline),
            "sl_rate": _rate(sl_since_baseline, exits_since_baseline),
            "trail_rate": _rate(trail_since_baseline, exits_since_baseline),
            "breakeven_rate": _rate(breakeven_since_baseline, exits_since_baseline),
        },
        "since_supervisor_start": {
            "entries": _to_int(events_since_start.get("entries")),
            "exits": exits_since_start,
            "exit_reasons": reasons_since_start,
            "wins": _to_int(events_since_start.get("wins")),
            "losses": _to_int(events_since_start.get("losses")),
            "nonpositive_exits": _to_int(events_since_start.get("nonpositive_exits")),
            "net_pnl_usd": _to_float(events_since_start.get("net_pnl_usd")),
            "gross_profit_usd": _to_float(events_since_start.get("gross_profit_usd")),
            "gross_loss_usd": _to_float(events_since_start.get("gross_loss_usd")),
            "profit_factor": events_since_start.get("profit_factor"),
            "pnl_per_trade_usd": pnl_per_trade_since_start,
            "pnl_per_trade_cents_per_share": _pnl_per_trade_cents_per_share(
                pnl_per_trade_since_start,
                job_position_size if job_position_size > 0 else None,
            ),
            "timeout_rate": _rate(timeout_since_start, exits_since_start),
            "tp_rate": _rate(tp_since_start, exits_since_start),
            "sl_rate": _rate(sl_since_start, exits_since_start),
            "trail_rate": _rate(trail_since_start, exits_since_start),
            "breakeven_rate": _rate(breakeven_since_start, exits_since_start),
        },
    }

    txt_lines = [
        f"Fade Long-Only Checkpoint ({now_local.strftime(TS_FMT)} local)",
        f"Window: {since.strftime(TS_FMT)} -> {now_local.strftime(TS_FMT)} ({args.hours:.1f}h)",
        f"Current total/day: {_fmt_num(payload['current']['total_pnl_usd'])} / {_fmt_num(payload['current']['day_pnl_usd'])}",
        f"Current realized/unrealized: {_fmt_num(payload['current']['realized_total_usd'])} / {_fmt_num(payload['current']['unrealized_total_usd'])}",
        f"Window delta total_pnl: {_fmt_num(payload['delta']['in_window_total_pnl_change_usd'])}",
        f"Window pnl/trade(USD): {_fmt_num(payload['delta']['in_window_pnl_per_trade_usd'])}",
        f"Window pnl/trade(c/share): {_fmt_num(payload['delta']['in_window_pnl_per_trade_cents_per_share'])}",
        f"Since baseline delta: {_fmt_num(payload['delta']['since_baseline_total_pnl_change_usd'])}",
        f"Since baseline pnl/trade(c/share): {_fmt_num(payload['delta']['since_baseline_pnl_per_trade_cents_per_share'])}",
        f"Since baseline PF/DD: {_fmt_num(payload['since_baseline']['profit_factor'], prec=2)} / {_fmt_num(payload['delta']['since_baseline_max_drawdown_usd'])}",
        f"Since baseline exits ({exits_since_baseline}): timeout={_fmt_pct(payload['since_baseline']['timeout_rate'])} "
        f"tp={_fmt_pct(payload['since_baseline']['tp_rate'])} sl={_fmt_pct(payload['since_baseline']['sl_rate'])}",
        f"Since supervisor start delta: {_fmt_num(payload['delta']['since_supervisor_start_total_pnl_change_usd'])}",
        f"Since start pnl/trade(USD): {_fmt_num(payload['delta']['since_supervisor_start_pnl_per_trade_usd'])}",
        f"Since start pnl/trade(c/share): {_fmt_num(payload['delta']['since_supervisor_start_pnl_per_trade_cents_per_share'])}",
        f"Since start PF/DD: {_fmt_num(payload['since_supervisor_start']['profit_factor'], prec=2)} / {_fmt_num(payload['delta']['since_supervisor_start_max_drawdown_usd'])}",
        f"Exit mix ({exits} exits): timeout={_fmt_pct(payload['window_events']['timeout_rate'])} "
        f"tp={_fmt_pct(payload['window_events']['tp_rate'])} sl={_fmt_pct(payload['window_events']['sl_rate'])} "
        f"trail={_fmt_pct(payload['window_events']['trail_rate'])} breakeven={_fmt_pct(payload['window_events']['breakeven_rate'])}",
        f"Since start exits ({exits_since_start}): timeout={_fmt_pct(payload['since_supervisor_start']['timeout_rate'])} "
        f"tp={_fmt_pct(payload['since_supervisor_start']['tp_rate'])} sl={_fmt_pct(payload['since_supervisor_start']['sl_rate'])}",
        f"Long winrate: {_fmt_pct(payload['current']['long_win_rate'])} ({long_wins}/{long_trades})",
        f"Entries/Exits/Signals total: {payload['current']['entries_total']} / {payload['current']['exits_total']} / {payload['current']['signals_total']}",
        f"Baseline delta total_pnl: {_fmt_num(payload['delta']['vs_baseline_total_pnl_usd'])}",
    ]
    return payload, "\n".join(txt_lines) + "\n"


def parse_args():
    repo = Path(__file__).resolve().parents[1]
    logs = repo / "logs"
    p = argparse.ArgumentParser(description="Report local fade long-only 24h checkpoint (observe-only)")
    p.add_argument("--hours", type=float, default=24.0, help="Lookback window in hours for event mix and pnl delta")
    p.add_argument(
        "--baseline-json",
        default=str(logs / "fade_longonly_24h_baseline_latest.json"),
        help="Baseline snapshot JSON path",
    )
    p.add_argument(
        "--baseline-start-local",
        default="",
        help='Optional baseline start override "YYYY-MM-DD HH:MM:SS" (defaults to baseline-json.started_at_local)',
    )
    p.add_argument(
        "--metrics-file",
        default=str(logs / "clob-fade-observe-profit-long-canary-metrics.jsonl"),
        help="Fade long-canary metrics JSONL path",
    )
    p.add_argument(
        "--state-file",
        default=str(logs / "clob_fade_observe_profit_long_canary_state.json"),
        help="Fade long-canary state JSON path",
    )
    p.add_argument(
        "--log-file",
        default=str(logs / "clob-fade-observe-profit-long-canary.log"),
        help="Fade long-canary log path",
    )
    p.add_argument(
        "--supervisor-state-file",
        default=str(logs / "fade_observe_supervisor_state.json"),
        help="Fade supervisor state JSON path",
    )
    p.add_argument(
        "--supervisor-job-name",
        default="fade_long_canary",
        help="Job name in supervisor state for since-start delta",
    )
    p.add_argument(
        "--out-json",
        default=str(logs / "fade_longonly_24h_eval_current_latest.json"),
        help="Output JSON path",
    )
    p.add_argument(
        "--out-txt",
        default=str(logs / "fade_longonly_24h_eval_current_latest.txt"),
        help="Output text summary path",
    )
    p.add_argument(
        "--tail-bytes",
        type=int,
        default=160 * 1024 * 1024,
        help="Bytes to read from tail of large logs/metrics files",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    payload, summary = build_checkpoint(args)

    out_json = Path(args.out_json)
    out_txt = Path(args.out_txt)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out_txt.write_text(summary, encoding="utf-8")

    print(summary, end="")
    print(f"out_json={out_json}")
    print(f"out_txt={out_txt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
