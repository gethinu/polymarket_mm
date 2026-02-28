#!/usr/bin/env python3
"""
Realtime dashboard for polymarket_clob_fade_observe.py outputs.

This is observe-only visualization. It never places orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse


ENTRY_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] entry ")
EXIT_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] exit ")
HALT_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] HALT: ")
ERROR_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] error: ")
SUMMARY_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] summary")


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def to_iso_ms(ts_ms: int) -> str:
    try:
        return dt.datetime.fromtimestamp(ts_ms / 1000.0).strftime("%H:%M:%S")
    except Exception:
        return ""


def read_tail_lines(path: str, max_lines: int, max_bytes: int = 4 * 1024 * 1024) -> List[str]:
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            seek = max(0, size - max(1024, int(max_bytes)))
            f.seek(seek, os.SEEK_SET)
            raw = f.read().decode("utf-8", errors="replace")
    except Exception:
        return []

    lines = raw.splitlines()
    if seek > 0 and lines:
        lines = lines[1:]
    if max_lines > 0 and len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines


@dataclass
class MetricRow:
    ts_ms: int
    ts: str
    token_id: str
    label: str
    mid: float
    spread: float
    imbalance: float
    zscore: float
    velocity_move: float
    bot_zscore: float
    bot_velocity: float
    bot_imbalance: float
    bot_extreme: float
    consensus_score: float
    consensus_side: int
    consensus_agree: int
    position_side: int
    position_size: float
    disabled_until_ts: float
    disable_reason: str
    entry_price: float
    unrealized_pnl: float
    realized_pnl: float
    trade_count: int
    win_count: int
    loss_count: int
    total_pnl: float
    day_pnl: float
    halted: bool


@dataclass
class Config:
    host: str
    port: int
    primary_label: str
    metrics_file: str
    state_file: str
    log_file: str
    secondary_label: str
    secondary_metrics_file: str
    secondary_state_file: str
    secondary_log_file: str
    detail_default: str
    window_minutes: float
    tail_lines: int
    max_tokens: int
    refresh_ms: int


class DashboardHTTPServer(ThreadingHTTPServer):
    cfg: Config


def parse_metric_row(line: str) -> Optional[MetricRow]:
    try:
        o = json.loads(line)
    except Exception:
        return None

    ts_raw = str(o.get("ts") or "").strip()
    ts_ms = int(o.get("ts_ms") or 0)
    if ts_ms <= 0 and ts_raw:
        try:
            ts_ms = int(parse_ts(ts_raw).timestamp() * 1000.0)
        except Exception:
            ts_ms = 0
    if ts_ms <= 0:
        return None

    return MetricRow(
        ts_ms=ts_ms,
        ts=ts_raw,
        token_id=str(o.get("token_id") or "").strip(),
        label=str(o.get("label") or "").strip(),
        mid=float(o.get("mid") or 0.0),
        spread=float(o.get("spread") or 0.0),
        imbalance=float(o.get("imbalance") or 0.0),
        zscore=float(o.get("zscore") or 0.0),
        velocity_move=float(o.get("velocity_move") or 0.0),
        bot_zscore=float(o.get("bot_zscore") or 0.0),
        bot_velocity=float(o.get("bot_velocity") or 0.0),
        bot_imbalance=float(o.get("bot_imbalance") or 0.0),
        bot_extreme=float(o.get("bot_extreme") or 0.0),
        consensus_score=float(o.get("consensus_score") or 0.0),
        consensus_side=int(o.get("consensus_side") or 0),
        consensus_agree=int(o.get("consensus_agree") or 0),
        position_side=int(o.get("position_side") or 0),
        position_size=float(o.get("position_size") or 0.0),
        disabled_until_ts=float(o.get("disabled_until_ts") or 0.0),
        disable_reason=str(o.get("disable_reason") or ""),
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


def load_metric_rows(metrics_file: str, since_ms: int, tail_lines: int) -> List[MetricRow]:
    lines = read_tail_lines(metrics_file, max_lines=max(200, int(tail_lines)))
    rows: List[MetricRow] = []
    for ln in lines:
        row = parse_metric_row(ln)
        if not row:
            continue
        if row.ts_ms < since_ms:
            continue
        rows.append(row)
    rows.sort(key=lambda r: r.ts_ms)
    return rows


def load_state(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = json.load(f)
            if isinstance(raw, dict):
                return raw
    except Exception:
        return {}
    return {}


def _state_open_stats(state: dict) -> Dict[str, object]:
    entries = int((state or {}).get("entries") or 0)
    exits = int((state or {}).get("exits") or 0)
    entry_exit_gap = entries - exits
    out: Dict[str, object] = {
        "available": False,
        "open_total": 0,
        "open_active": 0,
        "open_inactive": 0,
        "entry_exit_gap": entry_exit_gap,
        "consistency_ok": True,
        "warning": "",
    }
    if not isinstance(state, dict):
        return out

    token_states = state.get("token_states")
    if not isinstance(token_states, dict):
        return out

    active_ids = {str(x) for x in (state.get("active_token_ids") or []) if str(x)}
    open_total = 0
    open_active = 0
    for tid, raw in token_states.items():
        if not isinstance(raw, dict):
            continue
        try:
            side = int(raw.get("position_side") or 0)
        except Exception:
            side = 0
        try:
            size = float(raw.get("position_size") or 0.0)
        except Exception:
            size = 0.0
        if side == 0 or size <= 0:
            continue
        open_total += 1
        tok_id = str(raw.get("token_id") or tid)
        if tok_id in active_ids:
            open_active += 1

    open_inactive = max(0, open_total - open_active)
    consistency_ok = (entry_exit_gap == open_total)
    warnings: List[str] = []
    if open_inactive > 0:
        warnings.append(f"inactive_open={open_inactive}")
    if not consistency_ok:
        warnings.append(f"entries_minus_exits={entry_exit_gap} open_total={open_total}")

    out["available"] = True
    out["open_total"] = open_total
    out["open_active"] = open_active
    out["open_inactive"] = open_inactive
    out["consistency_ok"] = consistency_ok
    out["warning"] = ", ".join(warnings)
    return out


def collect_log_stats(log_file: str, since: dt.datetime, until: dt.datetime, tail_lines: int) -> Dict[str, object]:
    out = {
        "entries": 0,
        "exits": 0,
        "halts": 0,
        "errors": 0,
        "summaries": 0,
        "recent": [],
    }
    lines = read_tail_lines(log_file, max_lines=max(200, int(tail_lines)))
    recent: List[dict] = []
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        etype = ""
        ts: Optional[dt.datetime] = None
        for name, rx in (
            ("entry", ENTRY_RE),
            ("exit", EXIT_RE),
            ("halt", HALT_RE),
            ("error", ERROR_RE),
            ("summary", SUMMARY_RE),
        ):
            m = rx.match(line)
            if not m:
                continue
            try:
                ts = parse_ts(m.group("ts"))
            except Exception:
                ts = None
            etype = name
            break

        if not etype or not ts or ts < since or ts > until:
            continue

        if etype == "entry":
            out["entries"] += 1
        elif etype == "exit":
            out["exits"] += 1
        elif etype == "halt":
            out["halts"] += 1
        elif etype == "error":
            out["errors"] += 1
        elif etype == "summary":
            out["summaries"] += 1

        if etype in {"entry", "exit", "halt", "error"}:
            recent.append({"ts": ts.strftime("%H:%M:%S"), "type": etype, "text": line[:220]})

    out["recent"] = recent[-24:]
    return out


def _calc_drawdown(values: List[float]) -> tuple[float, float]:
    if not values:
        return (0.0, 0.0)
    peak = float(values[0])
    max_dd = 0.0
    for raw in values:
        v = float(raw)
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    now_dd = max(0.0, peak - float(values[-1]))
    return (max_dd, now_dd)


def _build_single_snapshot(
    metrics_file: str,
    state_file: str,
    log_file: str,
    window_minutes: float,
    tail_lines: int,
    max_tokens: int,
) -> dict:
    now = dt.datetime.now()
    win = float(window_minutes)
    win = max(1.0, min(24 * 60.0, win))
    since = now - dt.timedelta(minutes=win)
    since_ms = int(since.timestamp() * 1000.0)

    rows = load_metric_rows(metrics_file, since_ms=since_ms, tail_lines=tail_lines)
    state = load_state(state_file)
    logs = collect_log_stats(log_file, since=since, until=now, tail_lines=tail_lines)

    by_token: Dict[str, List[MetricRow]] = {}
    for r in rows:
        if not r.token_id:
            continue
        by_token.setdefault(r.token_id, []).append(r)

    latest_rows: List[MetricRow] = []
    for tid, arr in by_token.items():
        if arr:
            latest_rows.append(arr[-1])

    latest_rows.sort(key=lambda x: abs(float(x.consensus_score)), reverse=True)
    selected = latest_rows[: max(1, int(max_tokens))]

    ts_points: List[int] = []
    ts_total: List[float] = []
    ts_day: List[float] = []
    ts_signals: List[int] = []
    seen_ts: Dict[int, int] = {}
    sig_count: Dict[int, int] = {}

    for r in rows:
        sig_count[r.ts_ms] = sig_count.get(r.ts_ms, 0) + (1 if r.consensus_side != 0 else 0)

    for r in rows:
        if r.ts_ms in seen_ts:
            continue
        seen_ts[r.ts_ms] = 1
        ts_points.append(r.ts_ms)
        ts_total.append(float(r.total_pnl))
        ts_day.append(float(r.day_pnl))
        ts_signals.append(int(sig_count.get(r.ts_ms, 0)))

    tokens_payload: List[dict] = []
    for r in selected:
        arr = by_token.get(r.token_id) or []
        arr_tail = arr[-120:]
        mids = [float(x.mid) for x in arr_tail]
        scores = [float(x.consensus_score) for x in arr_tail]
        zscores = [float(x.zscore) for x in arr_tail]
        imbs = [float(x.imbalance) for x in arr_tail]
        timestamps = [to_iso_ms(int(x.ts_ms)) for x in arr_tail]
        win_rate = (float(r.win_count) / max(1, int(r.trade_count))) if int(r.trade_count) > 0 else 0.0
        tokens_payload.append(
            {
                "token_id": r.token_id,
                "label": r.label,
                "mid": r.mid,
                "spread": r.spread,
                "imbalance": r.imbalance,
                "zscore": r.zscore,
                "velocity_move": r.velocity_move,
                "bot_zscore": r.bot_zscore,
                "bot_velocity": r.bot_velocity,
                "bot_imbalance": r.bot_imbalance,
                "bot_extreme": r.bot_extreme,
                "consensus_score": r.consensus_score,
                "consensus_side": r.consensus_side,
                "consensus_agree": r.consensus_agree,
                "position_side": r.position_side,
                "position_size": r.position_size,
                "is_disabled": (float(r.disabled_until_ts or 0.0) > now.timestamp()),
                "disable_reason": str(r.disable_reason or ""),
                "entry_price": r.entry_price,
                "unrealized_pnl": r.unrealized_pnl,
                "realized_pnl": r.realized_pnl,
                "trade_count": r.trade_count,
                "win_rate": win_rate,
                "history_ts": timestamps,
                "history_mid": mids,
                "history_score": scores,
                "history_z": zscores,
                "history_imb": imbs,
            }
        )

    latest_total = float(ts_total[-1]) if ts_total else 0.0
    latest_day = float(ts_day[-1]) if ts_day else 0.0
    max_drawdown, now_drawdown = _calc_drawdown(ts_total)
    halted = bool(state.get("halted") or False) or bool((selected[0].halted if selected else False))
    selected_open_positions = sum(1 for r in selected if int(r.position_side) != 0)
    open_stats = _state_open_stats(state)
    if bool(open_stats.get("available")):
        open_positions = int(open_stats.get("open_total") or 0)
        open_positions_active = int(open_stats.get("open_active") or 0)
        open_positions_inactive = int(open_stats.get("open_inactive") or 0)
        open_consistency_ok = bool(open_stats.get("consistency_ok"))
    else:
        open_positions = selected_open_positions
        open_positions_active = selected_open_positions
        open_positions_inactive = 0
        open_consistency_ok = True
    entry_exit_gap = int(open_stats.get("entry_exit_gap") or 0)
    consistency_warning = str(open_stats.get("warning") or "")
    active_signals = sum(1 for r in selected if int(r.consensus_side) != 0)
    long_trades = int(state.get("long_trades") or 0)
    short_trades = int(state.get("short_trades") or 0)
    long_wins = int(state.get("long_wins") or 0)
    short_wins = int(state.get("short_wins") or 0)
    trade_count = max(0, long_trades + short_trades)
    win_count = max(0, long_wins + short_wins)
    if trade_count <= 0:
        trade_count = max(0, sum(max(0, int(x.trade_count or 0)) for x in latest_rows))
    if win_count <= 0:
        win_count = max(0, sum(max(0, int(x.win_count or 0)) for x in latest_rows))
    win_rate = (float(win_count) / float(trade_count)) if trade_count > 0 else 0.0

    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "window_minutes": win,
        "totals": {
            "total_pnl": latest_total,
            "day_pnl": latest_day,
            "open_positions": open_positions,
            "open_positions_active": open_positions_active,
            "open_positions_inactive": open_positions_inactive,
            "open_consistency_ok": bool(open_consistency_ok),
            "entry_exit_gap": int(entry_exit_gap),
            "active_signals": active_signals,
            "tracked_tokens": len(selected),
            "halted": halted,
            "trade_count": trade_count,
            "win_count": win_count,
            "win_rate": win_rate,
            "max_drawdown": float(max_drawdown),
            "drawdown_now": float(now_drawdown),
        },
        "series": {
            "ts": [to_iso_ms(x) for x in ts_points[-240:]],
            "total_pnl": ts_total[-240:],
            "day_pnl": ts_day[-240:],
            "signal_count": ts_signals[-240:],
        },
        "events": logs,
        "state": {
            "day_key": str(state.get("day_key") or ""),
            "entries": int(state.get("entries") or 0),
            "exits": int(state.get("exits") or 0),
            "signals_seen": int(state.get("signals_seen") or 0),
            "universe_refresh_count": int(state.get("universe_refresh_count") or 0),
            "halted": bool(state.get("halted") or False),
            "halt_reason": str(state.get("halt_reason") or ""),
            "open_positions_total": int(open_positions),
            "open_positions_active": int(open_positions_active),
            "open_positions_inactive": int(open_positions_inactive),
            "entry_exit_gap": int(entry_exit_gap),
            "consistency_warning": consistency_warning,
        },
        "tokens": tokens_payload,
    }


def _secondary_enabled(cfg: Config) -> bool:
    return bool(str(cfg.secondary_metrics_file or "").strip())


def _variant_summary(source: str, label: str, snap: dict) -> dict:
    totals = snap.get("totals") or {}
    state = snap.get("state") or {}
    events = snap.get("events") or {}
    entries = int(state.get("entries") or 0)
    exits = int(state.get("exits") or 0)
    if entries <= 0 and int(events.get("entries") or 0) > 0:
        entries = int(events.get("entries") or 0)
    if exits <= 0 and int(events.get("exits") or 0) > 0:
        exits = int(events.get("exits") or 0)
    return {
        "source": source,
        "label": (label or source.upper()),
        "generated_at": str(snap.get("generated_at") or ""),
        "totals": {
            "total_pnl": float(totals.get("total_pnl") or 0.0),
            "day_pnl": float(totals.get("day_pnl") or 0.0),
            "trade_count": int(totals.get("trade_count") or 0),
            "win_count": int(totals.get("win_count") or 0),
            "win_rate": float(totals.get("win_rate") or 0.0),
            "max_drawdown": float(totals.get("max_drawdown") or 0.0),
            "drawdown_now": float(totals.get("drawdown_now") or 0.0),
            "open_positions": int(totals.get("open_positions") or 0),
            "open_positions_active": int(totals.get("open_positions_active") or 0),
            "open_positions_inactive": int(totals.get("open_positions_inactive") or 0),
            "open_consistency_ok": bool(totals.get("open_consistency_ok", True)),
            "entry_exit_gap": int(totals.get("entry_exit_gap") or 0),
            "active_signals": int(totals.get("active_signals") or 0),
            "halted": bool(totals.get("halted") or False),
        },
        "entries": entries,
        "exits": exits,
        "signals_seen": int(state.get("signals_seen") or 0),
        "halt_reason": str(state.get("halt_reason") or ""),
        "consistency_warning": str(state.get("consistency_warning") or ""),
        "summaries": int(events.get("summaries") or 0),
    }


def build_snapshot(cfg: Config, window_minutes: Optional[float] = None, detail_source: Optional[str] = None) -> dict:
    win = float(window_minutes if window_minutes is not None else cfg.window_minutes)
    primary = _build_single_snapshot(
        metrics_file=cfg.metrics_file,
        state_file=cfg.state_file,
        log_file=cfg.log_file,
        window_minutes=win,
        tail_lines=cfg.tail_lines,
        max_tokens=cfg.max_tokens,
    )

    secondary = None
    if _secondary_enabled(cfg):
        secondary = _build_single_snapshot(
            metrics_file=cfg.secondary_metrics_file,
            state_file=cfg.secondary_state_file,
            log_file=cfg.secondary_log_file,
            window_minutes=win,
            tail_lines=cfg.tail_lines,
            max_tokens=cfg.max_tokens,
        )

    detail = str(detail_source or cfg.detail_default or "primary").strip().lower()
    if detail not in {"primary", "secondary"}:
        detail = "primary"
    if detail == "secondary" and not secondary:
        detail = "primary"

    detail_snap = secondary if (detail == "secondary" and secondary) else primary
    out = dict(detail_snap)
    variants = [_variant_summary("primary", cfg.primary_label, primary)]
    if secondary:
        variants.append(_variant_summary("secondary", cfg.secondary_label, secondary))
    out["variants"] = variants
    out["dual_mode"] = bool(secondary)
    out["detail_source"] = detail
    out["detail_label"] = (
        cfg.secondary_label if (detail == "secondary" and secondary) else cfg.primary_label
    ) or detail.upper()
    return out


HTML_TEMPLATE = r"""
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Fade Ops Deck</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg:#071017;
      --bg-soft:#0e1c26;
      --panel:#101f2b;
      --panel-2:#132636;
      --txt:#d4e4ef;
      --muted:#7f9cae;
      --line:#224055;
      --up:#14d38a;
      --down:#ff6b6b;
      --flat:#8fb3c9;
      --sig:#ffb347;
      --accent:#42d5ff;
    }
    * { box-sizing: border-box; }
    html, body { margin:0; padding:0; min-height:100%; }
    body {
      font-family: "Space Grotesk", sans-serif;
      color: var(--txt);
      background: radial-gradient(1200px 600px at 10% -20%, #16344a 0%, rgba(22,52,74,0) 55%),
                  radial-gradient(900px 500px at 90% -10%, #27492b 0%, rgba(39,73,43,0) 60%),
                  linear-gradient(180deg, #040b10 0%, var(--bg) 40%, #03080d 100%);
      padding: 22px;
    }
    .shell { max-width: 1480px; margin: 0 auto; }
    .top {
      display:flex;
      justify-content:space-between;
      align-items:flex-end;
      gap:16px;
      margin-bottom:14px;
      animation: fadeIn .45s ease-out;
    }
    .title h1 {
      margin:0;
      letter-spacing:0.08em;
      text-transform:uppercase;
      font-size: 28px;
      color:#ecf7ff;
    }
    .title p {
      margin:4px 0 0;
      color:var(--muted);
      font-size:13px;
    }
    .controls {
      display:flex;
      gap:10px;
      align-items:center;
      font-family:"JetBrains Mono", monospace;
      font-size:12px;
      color:var(--muted);
    }
    .controls select {
      background:var(--panel);
      color:var(--txt);
      border:1px solid var(--line);
      border-radius:10px;
      padding:6px 8px;
    }
    .detail-wrap {
      display:none;
      align-items:center;
      gap:8px;
    }
    .badge {
      border:1px solid var(--line);
      border-radius:999px;
      padding:6px 10px;
      font-family:"JetBrains Mono", monospace;
      font-size:12px;
      color:#9bb5c4;
      background: rgba(17, 33, 46, 0.6);
    }
    .stats {
      display:grid;
      grid-template-columns: repeat(8, minmax(120px, 1fr));
      gap:10px;
      margin-bottom:12px;
      padding:10px;
      border:1px solid #1b3345;
      border-radius:16px;
      background: rgba(5, 14, 21, 0.84);
      backdrop-filter: blur(6px);
      position: sticky;
      top: 8px;
      z-index: 18;
      animation: fadeIn .6s ease-out;
    }
    .variants {
      display:grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap:10px;
      margin-bottom:12px;
      animation: fadeIn .66s ease-out;
    }
    .variant {
      background: linear-gradient(180deg, rgba(17,33,46,0.9), rgba(12,24,34,0.9));
      border:1px solid var(--line);
      border-radius:12px;
      padding:9px 11px;
      font-family:"JetBrains Mono", monospace;
    }
    .variant .head {
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:8px;
      margin-bottom:6px;
    }
    .variant .name {
      color:#dceef9;
      letter-spacing:.04em;
      font-size:12px;
      text-transform:uppercase;
    }
    .variant .stamp {
      color:#88a8ba;
      font-size:10px;
    }
    .variant .row {
      display:flex;
      justify-content:space-between;
      gap:10px;
      color:#99b9cc;
      font-size:11px;
      line-height:1.5;
    }
    .variant .row b {
      color:#e7f7ff;
      font-weight:600;
    }
    .stat {
      background: linear-gradient(180deg, rgba(19,38,54,0.9), rgba(14,29,42,0.9));
      border:1px solid var(--line);
      border-radius:14px;
      padding:10px 12px;
      min-height:78px;
    }
    .stat .k { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.08em; }
    .stat .v {
      margin-top:6px;
      font-size:20px;
      font-weight:700;
      font-family:"JetBrains Mono", monospace;
      color:#ebf8ff;
    }
    .grid {
      display:grid;
      grid-template-columns: 2fr 1fr;
      gap:12px;
      margin-bottom:12px;
    }
    .panel {
      background: linear-gradient(180deg, rgba(17,33,46,0.92), rgba(12,24,34,0.9));
      border:1px solid var(--line);
      border-radius:15px;
      padding:12px;
      min-height:230px;
      animation: fadeIn .75s ease-out;
    }
    .panel h3 {
      margin:0 0 8px;
      font-size:12px;
      color:#9db8c8;
      letter-spacing: .08em;
      text-transform: uppercase;
      font-weight: 600;
    }
    #pnlCanvas {
      width:100%;
      height:240px;
      display:block;
      background: linear-gradient(180deg, rgba(6,13,18,0.2), rgba(6,13,18,0.45));
      border-radius:10px;
      border:1px solid #1a3447;
    }
    .events {
      max-height:260px;
      overflow:auto;
      display:flex;
      flex-direction:column;
      gap:7px;
      padding-right:4px;
    }
    .event {
      background: rgba(13,28,40,.7);
      border:1px solid #1c3b50;
      border-radius:11px;
      padding:8px;
      font-family:"JetBrains Mono", monospace;
      font-size:11px;
      line-height:1.35;
    }
    .event .meta { color:#8fb0c3; margin-bottom:4px; }
    .event.entry { border-color: rgba(20,211,138,.45); }
    .event.exit { border-color: rgba(66,213,255,.45); }
    .event.halt, .event.error { border-color: rgba(255,107,107,.55); }

    .tokens {
      display:grid;
      grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
      gap:12px;
      animation: fadeIn .9s ease-out;
    }
    .token {
      background: linear-gradient(165deg, rgba(17,34,48,0.95), rgba(10,22,31,0.95));
      border:1px solid #1f4157;
      border-radius:14px;
      padding:11px;
      position:relative;
      overflow:hidden;
    }
    .token::before {
      content:"";
      position:absolute;
      right:-40px;
      top:-55px;
      width:130px;
      height:130px;
      border-radius:50%;
      background: radial-gradient(circle, rgba(66,213,255,.18), rgba(66,213,255,0));
      pointer-events:none;
    }
    .token .head {
      display:flex;
      justify-content:space-between;
      gap:8px;
      margin-bottom:8px;
    }
    .token .name {
      font-size:13px;
      font-weight:600;
      line-height:1.25;
      color:#def0fa;
    }
    .pill {
      font-family:"JetBrains Mono", monospace;
      font-size:11px;
      border:1px solid #2c546c;
      color:#96bed4;
      background: rgba(13,27,38,0.75);
      border-radius:999px;
      padding:2px 8px;
      height:fit-content;
      white-space:nowrap;
    }
    .rows {
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap:7px 10px;
      margin-bottom:8px;
      font-family:"JetBrains Mono", monospace;
      font-size:11px;
      color:#9ab7c8;
    }
    .rows b { color:#e8f7ff; font-weight:600; }
    .up { color: var(--up) !important; }
    .down { color: var(--down) !important; }
    .neutral { color: var(--flat) !important; }

    .spark {
      width:100%;
      height:58px;
      display:block;
      border-radius:10px;
      border:1px solid #204258;
      background: rgba(7,15,21,.55);
      margin-bottom:8px;
    }
    .bot-grid {
      display:grid;
      grid-template-columns: repeat(4, 1fr);
      gap:6px;
    }
    .bot {
      border:1px solid #20465e;
      background: rgba(14,29,42,.85);
      border-radius:10px;
      padding:6px;
      font-family:"JetBrains Mono", monospace;
      font-size:10px;
      color:#8db0c3;
    }
    .bot .v { display:block; margin-top:4px; font-size:12px; }

    .footer {
      margin-top:10px;
      color:#7291a4;
      font-size:11px;
      font-family:"JetBrains Mono", monospace;
      display:flex;
      justify-content:space-between;
      gap:12px;
      flex-wrap:wrap;
    }

    @keyframes fadeIn {
      from { opacity:0; transform: translateY(10px); }
      to { opacity:1; transform: translateY(0); }
    }

    @media (max-width: 1000px) {
      .stats { grid-template-columns: repeat(4, minmax(120px, 1fr)); }
      .grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 720px) {
      .stats { grid-template-columns: repeat(2, minmax(120px, 1fr)); top: 4px; }
      .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header class="top">
      <div class="title">
        <h1>Fade Ops Deck</h1>
        <p>observe-only / multi-bot consensus / realtime monitor</p>
      </div>
      <div class="controls">
        <label for="windowSelect">window</label>
        <select id="windowSelect">
          <option value="15">15m</option>
          <option value="30">30m</option>
          <option value="60" selected>60m</option>
          <option value="180">180m</option>
          <option value="360">360m</option>
        </select>
        <span class="detail-wrap" id="detailWrap">
          <label for="detailSelect">detail</label>
          <select id="detailSelect"></select>
        </span>
        <span class="badge" id="heartbeat">waiting...</span>
      </div>
    </header>

    <section class="stats">
      <div class="stat"><div class="k">Total PnL</div><div class="v" id="stTotal">-</div></div>
      <div class="stat"><div class="k">Day PnL</div><div class="v" id="stDay">-</div></div>
      <div class="stat"><div class="k">Win Rate</div><div class="v" id="stWinRate">-</div></div>
      <div class="stat"><div class="k">Max DD / Now</div><div class="v" id="stDD">-</div></div>
      <div class="stat"><div class="k">Open Positions</div><div class="v" id="stOpen">-</div></div>
      <div class="stat"><div class="k">Active Signals</div><div class="v" id="stSignals">-</div></div>
      <div class="stat"><div class="k">Entries / Exits</div><div class="v" id="stEntries">-</div></div>
      <div class="stat"><div class="k">Guard</div><div class="v" id="stGuard">-</div></div>
    </section>
    <section class="variants" id="variants"></section>

    <section class="grid">
      <div class="panel">
        <h3>PNL + Signal Pulse</h3>
        <canvas id="pnlCanvas"></canvas>
      </div>
      <div class="panel">
        <h3>Recent Events</h3>
        <div class="events" id="events"></div>
      </div>
    </section>

    <section class="tokens" id="tokens"></section>

    <div class="footer">
      <span id="metaA">metrics: -</span>
      <span id="metaB">state: -</span>
      <span id="metaC">refresh: __REFRESH_MS__ms</span>
    </div>
  </div>

  <script>
    const REFRESH_MS = __REFRESH_MS__;
    let detailMode = "__DETAIL_DEFAULT__";
    const palette = {
      up: getComputedStyle(document.documentElement).getPropertyValue('--up').trim(),
      down: getComputedStyle(document.documentElement).getPropertyValue('--down').trim(),
      flat: getComputedStyle(document.documentElement).getPropertyValue('--flat').trim(),
      sig: getComputedStyle(document.documentElement).getPropertyValue('--sig').trim(),
      accent: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()
    };

    const el = (id) => document.getElementById(id);

    function fmtSigned(v) {
      const n = Number(v || 0);
      const s = (n >= 0 ? '+' : '') + n.toFixed(4);
      return s;
    }

    function fmtPct(v) {
      const n = Number(v || 0);
      const clamped = Math.max(0, Math.min(1, n));
      return (clamped * 100).toFixed(1) + '%';
    }

    function fmtDrawdown(v) {
      const n = Math.abs(Number(v || 0));
      return '-' + n.toFixed(4);
    }

    function clsSigned(v) {
      const n = Number(v || 0);
      if (n > 0) return 'up';
      if (n < 0) return 'down';
      return 'neutral';
    }

    function drawLineChart(canvas, xs, ysA, ysB, ysSig) {
      const dpr = Math.max(1, window.devicePixelRatio || 1);
      const w = canvas.clientWidth || 900;
      const h = canvas.clientHeight || 240;
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      const c = canvas.getContext('2d');
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
      c.clearRect(0, 0, w, h);

      if (!ysA || ysA.length < 2) {
        c.fillStyle = '#7f9cae';
        c.font = '12px JetBrains Mono';
        c.fillText('waiting for metrics...', 16, 22);
        return;
      }

      const pad = 24;
      const plotW = w - pad * 2;
      const plotH = h - pad * 2;
      const vals = ysA.concat(ysB || []);
      let vmin = Math.min.apply(null, vals);
      let vmax = Math.max.apply(null, vals);
      if (!isFinite(vmin) || !isFinite(vmax)) return;
      if (Math.abs(vmax - vmin) < 1e-9) {
        vmax = vmin + 1;
      }

      c.strokeStyle = 'rgba(111,157,182,0.25)';
      c.lineWidth = 1;
      for (let i = 0; i <= 4; i++) {
        const y = pad + (plotH * i / 4);
        c.beginPath(); c.moveTo(pad, y); c.lineTo(pad + plotW, y); c.stroke();
      }

      const xAt = (i, n) => pad + (plotW * i / Math.max(1, n - 1));
      const yAt = (v) => pad + ((vmax - v) / (vmax - vmin)) * plotH;

      function drawSeries(arr, color, width) {
        if (!arr || arr.length < 2) return;
        c.strokeStyle = color;
        c.lineWidth = width;
        c.beginPath();
        for (let i = 0; i < arr.length; i++) {
          const x = xAt(i, arr.length);
          const y = yAt(arr[i]);
          if (i === 0) c.moveTo(x, y); else c.lineTo(x, y);
        }
        c.stroke();
      }

      drawSeries(ysA, palette.accent, 2.2);
      drawSeries(ysB, '#8fd7a3', 1.6);

      if (ysSig && ysSig.length > 1) {
        const maxSig = Math.max(1, ...ysSig);
        c.strokeStyle = 'rgba(255,179,71,0.35)';
        c.lineWidth = 1.0;
        c.beginPath();
        for (let i = 0; i < ysSig.length; i++) {
          const x = xAt(i, ysSig.length);
          const y = pad + plotH - (ysSig[i] / maxSig) * (plotH * 0.25);
          if (i === 0) c.moveTo(x, y); else c.lineTo(x, y);
        }
        c.stroke();
      }

      c.fillStyle = '#9db8c8';
      c.font = '11px JetBrains Mono';
      c.fillText('total', pad + 4, pad + 12);
      c.fillStyle = '#8fd7a3';
      c.fillText('day', pad + 45, pad + 12);
      c.fillStyle = 'rgba(255,179,71,0.9)';
      c.fillText('signal pulse', pad + 76, pad + 12);
    }

    function drawSpark(canvas, values, color) {
      const dpr = Math.max(1, window.devicePixelRatio || 1);
      const w = canvas.clientWidth || 300;
      const h = canvas.clientHeight || 58;
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      const c = canvas.getContext('2d');
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
      c.clearRect(0, 0, w, h);
      if (!values || values.length < 2) return;

      let vmin = Math.min(...values);
      let vmax = Math.max(...values);
      if (Math.abs(vmax - vmin) < 1e-12) vmax = vmin + 1;
      const pad = 4;
      const xAt = (i, n) => pad + ((w - pad * 2) * i / Math.max(1, n - 1));
      const yAt = (v) => pad + ((vmax - v) / (vmax - vmin)) * (h - pad * 2);

      c.strokeStyle = color;
      c.lineWidth = 1.6;
      c.beginPath();
      for (let i = 0; i < values.length; i++) {
        const x = xAt(i, values.length);
        const y = yAt(values[i]);
        if (i === 0) c.moveTo(x, y); else c.lineTo(x, y);
      }
      c.stroke();
    }

    function botColor(v) {
      if (v > 0) return 'up';
      if (v < 0) return 'down';
      return 'neutral';
    }

    function tokenCard(tok, idx) {
      const side = Number(tok.position_side || 0);
      const sideLabel = side > 0 ? 'LONG' : side < 0 ? 'SHORT' : 'FLAT';
      const disabled = !!tok.is_disabled;
      const statusLabel = disabled ? 'DISABLED' : sideLabel;
      return `
        <article class="token" style="animation-delay:${Math.min(idx * 40, 300)}ms">
          <div class="head">
            <div class="name">${tok.label || tok.token_id}</div>
            <div class="pill ${disabled ? 'down' : ''}">${statusLabel}</div>
          </div>
          <div class="rows">
            <div>mid <b>${Number(tok.mid || 0).toFixed(4)}</b></div>
            <div>spread <b>${Number(tok.spread || 0).toFixed(4)}</b></div>
            <div>score <b class="${clsSigned(tok.consensus_score)}">${fmtSigned(tok.consensus_score)}</b></div>
            <div>agree <b>${Number(tok.consensus_agree || 0)}</b></div>
            <div>unreal <b class="${clsSigned(tok.unrealized_pnl)}">${fmtSigned(tok.unrealized_pnl)}</b></div>
            <div>realized <b class="${clsSigned(tok.realized_pnl)}">${fmtSigned(tok.realized_pnl)}</b></div>
            <div>trades <b>${Number(tok.trade_count || 0)}</b></div>
            <div>winrate <b>${(Number(tok.win_rate || 0) * 100).toFixed(1)}%</b></div>
            <div>status <b class="${disabled ? 'down' : 'neutral'}">${disabled ? 'disabled' : 'enabled'}</b></div>
            <div>reason <b>${disabled ? String(tok.disable_reason || '-').slice(0,26) : '-'}</b></div>
          </div>
          <canvas class="spark" id="spark-${idx}"></canvas>
          <div class="bot-grid">
            <div class="bot">zscore <span class="v ${botColor(tok.bot_zscore)}">${fmtSigned(tok.bot_zscore)}</span></div>
            <div class="bot">velocity <span class="v ${botColor(tok.bot_velocity)}">${fmtSigned(tok.bot_velocity)}</span></div>
            <div class="bot">imbalance <span class="v ${botColor(tok.bot_imbalance)}">${fmtSigned(tok.bot_imbalance)}</span></div>
            <div class="bot">extreme <span class="v ${botColor(tok.bot_extreme)}">${fmtSigned(tok.bot_extreme)}</span></div>
          </div>
        </article>
      `;
    }

    function variantCardHtml(v) {
      const t = v?.totals || {};
      const warnOpen = Number(t.open_positions_inactive || 0) > 0 || (t.open_consistency_ok === false);
      const guard = (t.halted || String(v?.halt_reason || '').length > 0) ? 'HALT' : (warnOpen ? 'WARN' : 'OK');
      return `
        <article class="variant">
          <div class="head">
            <div class="name">${v?.label || v?.source || '-'}</div>
            <div class="stamp">${v?.generated_at || '-'}</div>
          </div>
          <div class="row"><span>day</span><b class="${clsSigned(t.day_pnl || 0)}">${fmtSigned(t.day_pnl || 0)}</b></div>
          <div class="row"><span>total</span><b class="${clsSigned(t.total_pnl || 0)}">${fmtSigned(t.total_pnl || 0)}</b></div>
          <div class="row"><span>win / trades</span><b>${fmtPct(t.win_rate || 0)} / ${Number(t.trade_count || 0)}</b></div>
          <div class="row"><span>max DD / now</span><b class="${Number(t.max_drawdown || 0) > 0 ? 'down' : 'neutral'}">${fmtDrawdown(t.max_drawdown || 0)} / ${fmtDrawdown(t.drawdown_now || 0)}</b></div>
          <div class="row"><span>entries / exits</span><b>${Number(v?.entries || 0)} / ${Number(v?.exits || 0)}</b></div>
          <div class="row"><span>open(all/a/i)</span><b class="${warnOpen ? 'down' : 'neutral'}">${Number(t.open_positions || 0)} / ${Number(t.open_positions_active || 0)} / ${Number(t.open_positions_inactive || 0)}</b></div>
          <div class="row"><span>open / signals</span><b>${Number(t.open_positions || 0)} / ${Number(t.active_signals || 0)}</b></div>
          <div class="row"><span>entry-exit gap</span><b class="${(t.open_consistency_ok === false) ? 'down' : 'neutral'}">${Number(t.entry_exit_gap || 0)}</b></div>
          <div class="row"><span>guard</span><b class="${guard === 'OK' ? 'up' : 'down'}">${guard}</b></div>
        </article>
      `;
    }

    function syncDetailControl(snapshot) {
      const variants = Array.isArray(snapshot?.variants) ? snapshot.variants : [];
      const wrap = el('detailWrap');
      const sel = el('detailSelect');
      if (!wrap || !sel) return;
      if (variants.length < 2) {
        wrap.style.display = 'none';
        sel.innerHTML = '';
        detailMode = 'primary';
        return;
      }
      wrap.style.display = 'inline-flex';
      const options = variants.map((v) => `<option value="${v.source}">${v.label || v.source}</option>`).join('');
      if (sel.innerHTML !== options) {
        sel.innerHTML = options;
      }
      const fromSnap = String(snapshot?.detail_source || detailMode || 'primary');
      const exists = Array.from(sel.options).some((o) => o.value === fromSnap);
      sel.value = exists ? fromSnap : 'primary';
      detailMode = sel.value || 'primary';
    }

    function renderVariants(snapshot) {
      const variants = Array.isArray(snapshot?.variants) ? snapshot.variants : [];
      el('variants').innerHTML = variants.map(variantCardHtml).join('');
    }

    function render(snapshot) {
      const total = snapshot?.totals || {};
      const state = snapshot?.state || {};
      const events = snapshot?.events || {};
      const series = snapshot?.series || {};
      const tokens = snapshot?.tokens || [];

      syncDetailControl(snapshot);
      renderVariants(snapshot);

      el('stTotal').textContent = fmtSigned(total.total_pnl || 0);
      el('stTotal').className = `v ${clsSigned(total.total_pnl || 0)}`;
      el('stDay').textContent = fmtSigned(total.day_pnl || 0);
      el('stDay').className = `v ${clsSigned(total.day_pnl || 0)}`;
      const tradeCount = Number(total.trade_count || 0);
      const winRate = Number(total.win_rate || 0);
      el('stWinRate').textContent = fmtPct(winRate);
      if (tradeCount <= 0) {
        el('stWinRate').className = 'v neutral';
      } else if (winRate >= 0.55) {
        el('stWinRate').className = 'v up';
      } else if (winRate <= 0.45) {
        el('stWinRate').className = 'v down';
      } else {
        el('stWinRate').className = 'v neutral';
      }
      const maxDD = Number(total.max_drawdown || 0);
      const nowDD = Number(total.drawdown_now || 0);
      el('stDD').textContent = `${fmtDrawdown(maxDD)} / ${fmtDrawdown(nowDD)}`;
      el('stDD').className = `v ${maxDD > 0 ? 'down' : 'neutral'}`;
      const openTotal = Number(total.open_positions || 0);
      const openActive = Number(total.open_positions_active || 0);
      const openInactive = Number(total.open_positions_inactive || 0);
      const hasOpenWarn = (openInactive > 0) || (total.open_consistency_ok === false);
      el('stOpen').textContent = `${openTotal} (${openActive}/${openInactive})`;
      el('stOpen').className = `v ${hasOpenWarn ? 'down' : 'neutral'}`;
      el('stSignals').textContent = String(total.active_signals || 0);
      el('stEntries').textContent = `${state.entries || 0} / ${state.exits || 0}`;
      const hasWarn = hasOpenWarn || String(state.consistency_warning || '').length > 0;
      const guard = (total.halted || state.halted) ? 'HALT' : (hasWarn ? 'WARN' : 'OK');
      el('stGuard').textContent = guard;
      el('stGuard').className = `v ${guard === 'OK' ? 'up' : 'down'}`;

      drawLineChart(el('pnlCanvas'), series.ts || [], series.total_pnl || [], series.day_pnl || [], series.signal_count || []);

      const ev = (events.recent || []).slice().reverse();
      const evHtml = ev.length ? ev.map((x) => `
        <div class="event ${x.type}">
          <div class="meta">${x.ts} | ${x.type.toUpperCase()}</div>
          <div>${x.text}</div>
        </div>
      `).join('') : '<div class="event"><div class="meta">-</div><div>no recent events</div></div>';
      el('events').innerHTML = evHtml;

      el('tokens').innerHTML = tokens.map((t, i) => tokenCard(t, i)).join('');
      tokens.forEach((t, i) => {
        const canvas = document.getElementById(`spark-${i}`);
        if (!canvas) return;
        drawSpark(canvas, t.history_mid || [], palette.accent);
      });

      const guardTxt = (state.halted && state.halt_reason) ? `halt=${state.halt_reason}` : 'halt=none';
      const warnTxt = String(state.consistency_warning || '').trim();
      el('metaA').textContent = `metrics: tokens=${total.tracked_tokens || 0} summaries=${events.summaries || 0} entries=${events.entries || 0} exits=${events.exits || 0} variants=${(snapshot?.variants || []).length || 1}`;
      el('metaB').textContent = `state: signals_seen=${state.signals_seen || 0} universe_refresh=${state.universe_refresh_count || 0} ${guardTxt}${warnTxt ? ` warn=${warnTxt}` : ''}`;
      el('heartbeat').textContent = `updated ${snapshot.generated_at || '-'} | ${snapshot.detail_label || detailMode || '-'}`;
    }

    async function tick() {
      try {
        const winSel = Number(el('windowSelect').value || 60);
        const detailSel = el('detailSelect');
        const detail = (detailSel && detailSel.value) ? detailSel.value : detailMode;
        const resp = await fetch(`/api/snapshot?minutes=${encodeURIComponent(winSel)}&detail=${encodeURIComponent(detail || 'primary')}`, {cache: 'no-store'});
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        detailMode = String(data?.detail_source || detail || detailMode || 'primary');
        render(data);
      } catch (err) {
        el('heartbeat').textContent = `error ${String(err).slice(0, 80)}`;
      }
    }

    el('windowSelect').addEventListener('change', () => tick());
    el('detailSelect').addEventListener('change', () => tick());
    window.addEventListener('resize', () => {
      const cv = el('pnlCanvas');
      if (!cv) return;
      tick();
    });

    tick();
    setInterval(tick, Math.max(700, REFRESH_MS));
  </script>
</body>
</html>
"""

class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def log_message(self, fmt: str, *args) -> None:
        # Quiet by default to keep console readable.
        return

    def _send_bytes(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: dict) -> None:
        try:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except Exception:
            raw = b"{}"
        self._send_bytes(code, raw, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        cfg = self.server.cfg

        if parsed.path in {"/", "/index.html"}:
            html = HTML_TEMPLATE.replace("__REFRESH_MS__", str(int(cfg.refresh_ms))).replace(
                "__DETAIL_DEFAULT__", str(cfg.detail_default or "primary")
            )
            self._send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return

        if parsed.path == "/healthz":
            self._send_json(200, {"ok": True})
            return

        if parsed.path == "/api/snapshot":
            q = parse_qs(parsed.query)
            minutes = None
            detail = str((q.get("detail") or [""])[0] or "").strip().lower()
            raw_minutes = (q.get("minutes") or [""])[0]
            if raw_minutes:
                try:
                    minutes = float(raw_minutes)
                except Exception:
                    minutes = None
            snap = build_snapshot(cfg, window_minutes=minutes, detail_source=detail)
            self._send_json(200, snap)
            return

        self._send_json(404, {"error": "not found"})


def parse_args():
    script_dir = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Realtime dashboard for CLOB fade observe logs")
    p.add_argument("--host", default="127.0.0.1", help="Bind host")
    p.add_argument("--port", type=int, default=8787, help="Bind port")
    p.add_argument("--primary-label", default="PRIMARY", help="Display label for primary source")
    p.add_argument(
        "--metrics-file",
        default=str(script_dir.parent / "logs" / "clob-fade-observe-metrics.jsonl"),
        help="Input metrics JSONL from polymarket_clob_fade_observe.py",
    )
    p.add_argument(
        "--state-file",
        default=str(script_dir.parent / "logs" / "clob_fade_observe_state.json"),
        help="Input runtime state JSON from polymarket_clob_fade_observe.py",
    )
    p.add_argument(
        "--log-file",
        default=str(script_dir.parent / "logs" / "clob-fade-observe.log"),
        help="Input event log from polymarket_clob_fade_observe.py",
    )
    p.add_argument("--secondary-label", default="SECONDARY", help="Display label for secondary source")
    p.add_argument("--secondary-metrics-file", default="", help="Optional second metrics JSONL for dual view")
    p.add_argument("--secondary-state-file", default="", help="Optional second runtime state JSON for dual view")
    p.add_argument("--secondary-log-file", default="", help="Optional second event log for dual view")
    p.add_argument(
        "--detail-default",
        choices=("primary", "secondary"),
        default="primary",
        help="Default detail panel source when dual view is enabled",
    )
    p.add_argument("--window-minutes", type=float, default=60.0, help="Default dashboard lookback window")
    p.add_argument("--tail-lines", type=int, default=8000, help="Tail lines to scan per refresh")
    p.add_argument("--max-tokens", type=int, default=12, help="Max token cards to render")
    p.add_argument("--refresh-ms", type=int, default=1500, help="Browser refresh interval hint in milliseconds")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = Config(
        host=str(args.host),
        port=int(args.port),
        primary_label=str(args.primary_label or "PRIMARY"),
        metrics_file=str(args.metrics_file),
        state_file=str(args.state_file),
        log_file=str(args.log_file),
        secondary_label=str(args.secondary_label or "SECONDARY"),
        secondary_metrics_file=str(args.secondary_metrics_file or ""),
        secondary_state_file=str(args.secondary_state_file or ""),
        secondary_log_file=str(args.secondary_log_file or ""),
        detail_default=str(args.detail_default or "primary"),
        window_minutes=float(args.window_minutes),
        tail_lines=max(200, int(args.tail_lines)),
        max_tokens=max(1, int(args.max_tokens)),
        refresh_ms=max(500, int(args.refresh_ms)),
    )

    server = DashboardHTTPServer((cfg.host, cfg.port), DashboardHandler)
    server.cfg = cfg

    print("Fade Monitor Dashboard")
    print("=" * 40)
    print(f"URL: http://{cfg.host}:{cfg.port}")
    print(f"primary[{cfg.primary_label}]")
    print(f"  metrics: {cfg.metrics_file}")
    print(f"  state:   {cfg.state_file}")
    print(f"  log:     {cfg.log_file}")
    if _secondary_enabled(cfg):
        print(f"secondary[{cfg.secondary_label}]")
        print(f"  metrics: {cfg.secondary_metrics_file}")
        print(f"  state:   {cfg.secondary_state_file or '-'}")
        print(f"  log:     {cfg.secondary_log_file or '-'}")
        print(f"  detail-default: {cfg.detail_default}")
    print("Press Ctrl+C to stop")

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
