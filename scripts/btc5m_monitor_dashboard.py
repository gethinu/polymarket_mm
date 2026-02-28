#!/usr/bin/env python3
"""
Realtime dashboard for BTC short-window observe scripts.

Supports:
- scripts/polymarket_btc5m_lag_observe.py (observe-only)
- scripts/polymarket_btc5m_panic_observe.py (observe-only)

This dashboard is observe-only visualization. It never places orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse


ENTER_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] paper ENTER ")
SETTLE_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] settle ")
HALT_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] HALT: ")
WARN_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] warn: ")
ERROR_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] error: ")
SUMMARY_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] summary ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def to_hhmmss(ts_ms: int) -> str:
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


def load_state(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def parse_metrics_line(line: str) -> Optional[dict]:
    try:
        o = json.loads(line)
    except Exception:
        return None
    if not isinstance(o, dict):
        return None
    ts_ms = int(o.get("ts_ms") or 0)
    if ts_ms <= 0:
        return None
    o["_ts_ms"] = ts_ms
    return o


def load_metrics(metrics_file: str, since_ms: int, tail_lines: int) -> List[dict]:
    lines = read_tail_lines(metrics_file, max_lines=max(200, int(tail_lines)))
    rows: List[dict] = []
    for ln in lines:
        row = parse_metrics_line(ln)
        if not row:
            continue
        if int(row.get("_ts_ms") or 0) < since_ms:
            continue
        rows.append(row)
    rows.sort(key=lambda r: int(r.get("_ts_ms") or 0))
    return rows


def _event_kind(line: str) -> str:
    if ENTER_RE.search(line):
        return "enter"
    if SETTLE_RE.search(line):
        return "settle"
    if HALT_RE.search(line):
        return "halt"
    if ERROR_RE.search(line):
        return "error"
    if WARN_RE.search(line):
        return "warn"
    if SUMMARY_RE.search(line):
        return "summary"
    return "info"


def load_events(log_file: str, tail_lines: int) -> List[dict]:
    out: List[dict] = []
    for ln in read_tail_lines(log_file, max_lines=max(200, int(tail_lines))):
        s = ln.strip()
        if not s:
            continue
        out.append({"kind": _event_kind(s), "text": s})
    return out[-200:]


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _as_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def build_series(rows: List[dict]) -> dict:
    ts: List[str] = []
    pnl_total: List[float] = []
    day_pnl: List[float] = []
    best_edge: List[float] = []
    spot: List[float] = []
    remaining: List[float] = []

    for r in rows:
        ts_ms = _as_int(r.get("_ts_ms"), 0)
        ts.append(to_hhmmss(ts_ms))
        pnl_total.append(_as_float(r.get("pnl_total_usd"), 0.0))
        day_pnl.append(_as_float(r.get("day_pnl_usd"), 0.0))

        be = r.get("best_edge")
        if be is None:
            be = r.get("edge_up") if abs(_as_float(r.get("edge_up"), 0.0)) >= abs(_as_float(r.get("edge_down"), 0.0)) else r.get("edge_down")
        best_edge.append(_as_float(be, 0.0) if be is not None else 0.0)

        spot.append(_as_float(r.get("spot_btc_usd"), 0.0))
        remaining.append(_as_float(r.get("remaining_sec"), 0.0))

    return {
        "ts": ts,
        "pnl_total_usd": pnl_total,
        "day_pnl_usd": day_pnl,
        "best_edge": best_edge,
        "spot_btc_usd": spot,
        "remaining_sec": remaining,
    }


def compute_win_rate(state: dict) -> float:
    wins = _as_int(state.get("wins"), 0)
    losses = _as_int(state.get("losses"), 0)
    pushes = _as_int(state.get("pushes"), 0)
    denom = max(1, wins + losses + pushes)
    return float(wins) / float(denom)


def default_runtime_paths(mode: str, window_minutes: int) -> Tuple[str, str, str]:
    m = int(window_minutes)
    mode_norm = str(mode or "").strip().lower()
    if mode_norm not in {"lag", "panic"}:
        mode_norm = "lag"

    base = f"btc{m}m-{mode_norm}-observe"
    logs = _repo_root() / "logs"
    log_file = str(logs / f"{base}.log")
    state_file = str(logs / f"{base.replace('-', '_')}_state.json")
    metrics_file = str(logs / f"{base}-metrics.jsonl")
    return log_file, state_file, metrics_file


@dataclass
class Config:
    host: str
    port: int
    mode: str
    window_minutes: int
    log_file: str
    state_file: str
    metrics_file: str
    window_lookback_minutes: float
    tail_lines: int
    refresh_ms: int


class DashboardHTTPServer(ThreadingHTTPServer):
    cfg: Config


def build_snapshot(cfg: Config, *, window_minutes: Optional[float]) -> dict:
    now_ms = int(dt.datetime.now().timestamp() * 1000.0)
    lookback = float(window_minutes if window_minutes is not None else cfg.window_lookback_minutes)
    lookback = max(1.0, min(24 * 60.0, lookback))
    since_ms = now_ms - int(lookback * 60.0 * 1000.0)

    rows = load_metrics(cfg.metrics_file, since_ms=since_ms, tail_lines=cfg.tail_lines)
    latest = rows[-1] if rows else {}
    state = load_state(cfg.state_file)
    series = build_series(rows)
    events = load_events(cfg.log_file, tail_lines=cfg.tail_lines)

    return {
        "ok": True,
        "meta": {
            "mode": cfg.mode,
            "window_minutes": cfg.window_minutes,
            "lookback_minutes": lookback,
            "metrics_file": cfg.metrics_file,
            "state_file": cfg.state_file,
            "log_file": cfg.log_file,
            "rows": len(rows),
        },
        "latest": latest,
        "state": state,
        "stats": {
            "total_pnl_usd": _as_float(state.get("pnl_total_usd"), _as_float(latest.get("pnl_total_usd"), 0.0)),
            "day_pnl_usd": _as_float(latest.get("day_pnl_usd"), 0.0),
            "win_rate": compute_win_rate(state),
            "trades_closed": _as_int(state.get("trades_closed"), _as_int(latest.get("trades_closed"), 0)),
            "halted": bool(state.get("halted") or False),
            "halt_reason": str(state.get("halt_reason") or ""),
            "current_window_slug": str(state.get("current_window_slug") or latest.get("window_slug") or ""),
            "remaining_sec": _as_float(latest.get("remaining_sec"), 0.0),
            "spot_btc_usd": latest.get("spot_btc_usd"),
            "window_open_btc_usd": latest.get("window_open_btc_usd"),
            "active_position": latest.get("active_position") if isinstance(latest.get("active_position"), dict) else state.get("active_position"),
        },
        "series": series,
        "events": events,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>BTC Ops Deck</title>
  <style>
    :root{
      --bg:#070c10;
      --panel:rgba(14,29,42,.86);
      --border:#1d3a4f;
      --text:#cfe4ef;
      --muted:#7f9cae;
      --up:#2ed573;
      --down:#ff4757;
      --warn:#ffa502;
      --accent:#39a0ff;
      --ink:#0a1118;
      --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
    }
    html,body{height:100%;}
    body{
      margin:0;
      background: radial-gradient(1200px 600px at 20% 0%, rgba(57,160,255,.14), transparent 60%),
                  radial-gradient(900px 500px at 90% 20%, rgba(46,213,115,.10), transparent 55%),
                  var(--bg);
      color:var(--text);
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
    }
    .shell{max-width:1240px;margin:14px auto;padding:0 14px;}
    header{
      display:flex;align-items:flex-end;justify-content:space-between;gap:10px;flex-wrap:wrap;
      margin-bottom:10px;
    }
    .title h1{margin:0;font-size:20px;letter-spacing:.4px;}
    .title p{margin:2px 0 0 0;color:var(--muted);font-size:12px;}
    .controls{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:12px;font-family:var(--mono);}
    select{
      background:rgba(10,20,28,.75);border:1px solid var(--border);border-radius:8px;
      color:var(--text);padding:6px 8px;font-family:var(--mono);font-size:12px;
    }
    .badge{
      padding:6px 10px;border-radius:999px;border:1px solid var(--border);
      background: rgba(10,20,28,.55);font-family:var(--mono);font-size:12px;color:var(--muted);
    }
    .stats{
      display:grid;
      grid-template-columns: repeat(6, minmax(150px, 1fr));
      gap:8px;
      margin-bottom:10px;
    }
    .stat{
      background:var(--panel);
      border:1px solid var(--border);
      border-radius:12px;
      padding:10px 10px 8px 10px;
    }
    .stat .k{font-size:11px;color:var(--muted);font-family:var(--mono);text-transform:uppercase;letter-spacing:.6px;}
    .stat .v{font-size:18px;margin-top:6px;font-family:var(--mono);font-weight:600;}
    .up{color:var(--up);}
    .down{color:var(--down);}
    .warn{color:var(--warn);}
    .muted{color:var(--muted);}
    .grid{
      display:grid;
      grid-template-columns: 2fr 1fr;
      gap:10px;
    }
    .panel{
      background:var(--panel);
      border:1px solid var(--border);
      border-radius:14px;
      padding:10px;
      overflow:hidden;
    }
    .panel h3{
      margin:0 0 8px 0;
      font-size:12px;
      color:var(--muted);
      font-family:var(--mono);
      text-transform:uppercase;
      letter-spacing:.6px;
    }
    canvas{width:100%;height:280px;}
    .events{
      height:310px;
      overflow:auto;
      font-family:var(--mono);
      font-size:11px;
      line-height:1.35;
      background: rgba(8,16,23,.35);
      border:1px solid rgba(29,58,79,.8);
      border-radius:12px;
      padding:8px;
    }
    .evt{padding:6px 8px;border-radius:10px;margin-bottom:6px;border:1px solid rgba(29,58,79,.65);}
    .evt.enter{background: rgba(46,213,115,.10);}
    .evt.settle{background: rgba(57,160,255,.10);}
    .evt.halt{background: rgba(255,71,87,.10);}
    .evt.error{background: rgba(255,71,87,.08);}
    .evt.warn{background: rgba(255,165,2,.10);}
    .evt.summary{background: rgba(127,156,174,.08);}
    .evt.info{background: rgba(14,29,42,.35);}
    .footer{
      margin-top:10px;color:var(--muted);font-size:11px;font-family:var(--mono);
      display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;
    }
    @media (max-width: 1100px){
      .stats{grid-template-columns: repeat(3, minmax(150px, 1fr));}
      .grid{grid-template-columns: 1fr;}
      canvas{height:260px;}
      .events{height:260px;}
    }
    @media (max-width: 700px){
      .stats{grid-template-columns: repeat(2, minmax(150px, 1fr));}
    }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div class="title">
        <h1>BTC Ops Deck</h1>
        <p>observe-only / BTC short-window monitor / realtime chart + execution log</p>
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
        <span class="badge" id="heartbeat">waiting...</span>
      </div>
    </header>

    <section class="stats">
      <div class="stat"><div class="k">Total PnL</div><div class="v" id="stTotal">-</div></div>
      <div class="stat"><div class="k">Day PnL</div><div class="v" id="stDay">-</div></div>
      <div class="stat"><div class="k">Win Rate</div><div class="v" id="stWin">-</div></div>
      <div class="stat"><div class="k">Trades Closed</div><div class="v" id="stTrades">-</div></div>
      <div class="stat"><div class="k">Window / Rem</div><div class="v" id="stWindow">-</div></div>
      <div class="stat"><div class="k">Spot / Open</div><div class="v" id="stSpot">-</div></div>
    </section>

    <section class="grid">
      <div class="panel">
        <h3>Equity Curve (Total PnL)</h3>
        <canvas id="pnlCanvas"></canvas>
      </div>
      <div class="panel">
        <h3>Execution Log (tail)</h3>
        <div class="events" id="events"></div>
      </div>
    </section>

    <div class="footer">
      <span id="metaA">metrics: -</span>
      <span id="metaB">state: -</span>
      <span id="metaC">refresh: __REFRESH_MS__ms</span>
    </div>
  </div>

  <script>
    const REFRESH_MS = __REFRESH_MS__;
    const el = (id) => document.getElementById(id);
    const palette = {
      up: getComputedStyle(document.documentElement).getPropertyValue('--up').trim(),
      down: getComputedStyle(document.documentElement).getPropertyValue('--down').trim(),
      accent: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(),
      muted: getComputedStyle(document.documentElement).getPropertyValue('--muted').trim()
    };

    function fmtSigned(n, digits=4) {
      const v = Number(n || 0);
      const s = (v >= 0 ? '+' : '') + v.toFixed(digits);
      return s;
    }

    function fmtPct(x) {
      const v = Math.max(0, Math.min(1, Number(x || 0)));
      return (v * 100).toFixed(1) + '%';
    }

    function clsSigned(n) {
      const v = Number(n || 0);
      if (v > 0) return 'up';
      if (v < 0) return 'down';
      return 'muted';
    }

    function drawLineChart(canvas, xs, ys) {
      const dpr = Math.max(1, window.devicePixelRatio || 1);
      const w = canvas.clientWidth || 900;
      const h = canvas.clientHeight || 240;
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      const c = canvas.getContext('2d');
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
      c.clearRect(0, 0, w, h);

      if (!ys || ys.length < 2) {
        c.fillStyle = palette.muted;
        c.font = '12px JetBrains Mono';
        c.fillText('waiting for metrics...', 16, 22);
        return;
      }

      const pad = 24;
      const plotW = w - pad * 2;
      const plotH = h - pad * 2;
      let vmin = Math.min.apply(null, ys);
      let vmax = Math.max.apply(null, ys);
      if (!isFinite(vmin) || !isFinite(vmax)) return;
      if (Math.abs(vmax - vmin) < 1e-9) vmax = vmin + 1;

      c.strokeStyle = 'rgba(111,157,182,0.22)';
      c.lineWidth = 1;
      for (let i = 0; i <= 4; i++) {
        const y = pad + (plotH * i / 4);
        c.beginPath(); c.moveTo(pad, y); c.lineTo(pad + plotW, y); c.stroke();
      }

      const xAt = (i, n) => pad + (plotW * i / Math.max(1, n - 1));
      const yAt = (v) => pad + ((vmax - v) / (vmax - vmin)) * plotH;

      c.strokeStyle = palette.accent;
      c.lineWidth = 2.2;
      c.beginPath();
      for (let i = 0; i < ys.length; i++) {
        const x = xAt(i, ys.length);
        const y = yAt(ys[i]);
        if (i === 0) c.moveTo(x, y); else c.lineTo(x, y);
      }
      c.stroke();

      // Last point callout
      const lx = xAt(ys.length - 1, ys.length);
      const ly = yAt(ys[ys.length - 1]);
      c.fillStyle = 'rgba(10,17,24,.92)';
      c.strokeStyle = 'rgba(29,58,79,.9)';
      c.lineWidth = 1;
      c.beginPath(); c.arc(lx, ly, 3.6, 0, Math.PI * 2); c.fill(); c.stroke();
    }

    function renderEvents(events) {
      const wrap = el('events');
      if (!wrap) return;
      wrap.innerHTML = '';
      (events || []).slice().reverse().forEach((e) => {
        const div = document.createElement('div');
        div.className = 'evt ' + String(e.kind || 'info');
        div.textContent = String(e.text || '');
        wrap.appendChild(div);
      });
    }

    function render(snapshot) {
      const st = snapshot?.stats || {};
      const meta = snapshot?.meta || {};
      const series = snapshot?.series || {};

      const total = Number(st.total_pnl_usd || 0);
      el('stTotal').textContent = fmtSigned(total, 4);
      el('stTotal').className = 'v ' + clsSigned(total);

      const day = Number(st.day_pnl_usd || 0);
      el('stDay').textContent = fmtSigned(day, 4);
      el('stDay').className = 'v ' + clsSigned(day);

      el('stWin').textContent = fmtPct(st.win_rate || 0);
      el('stTrades').textContent = String(st.trades_closed || 0);

      const rem = Number(st.remaining_sec || 0);
      const slug = String(st.current_window_slug || '-');
      el('stWindow').textContent = `${slug} / ${rem.toFixed(1)}s`;

      const spot = st.spot_btc_usd;
      const op = st.window_open_btc_usd;
      const spotTxt = (spot === null || spot === undefined) ? 'n/a' : Number(spot).toFixed(2);
      const opTxt = (op === null || op === undefined) ? 'n/a' : Number(op).toFixed(2);
      el('stSpot').textContent = `${spotTxt} / ${opTxt}`;

      renderEvents(snapshot?.events || []);

      el('metaA').textContent = `metrics: ${meta.metrics_file || '-'}`;
      el('metaB').textContent = `state: ${meta.state_file || '-'}`;

      drawLineChart(el('pnlCanvas'), series.ts || [], series.pnl_total_usd || []);

      if (st.halted) {
        el('heartbeat').textContent = `HALTED ${String(st.halt_reason || '').slice(0, 64)}`;
        el('heartbeat').style.color = getComputedStyle(document.documentElement).getPropertyValue('--down');
      } else {
        el('heartbeat').textContent = `ok rows=${meta.rows || 0}`;
        el('heartbeat').style.color = getComputedStyle(document.documentElement).getPropertyValue('--muted');
      }
    }

    async function tick() {
      try {
        const winSel = el('windowSelect')?.value || '60';
        const resp = await fetch(`/api/snapshot?minutes=${encodeURIComponent(winSel)}`, {cache: 'no-store'});
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        render(data);
      } catch (err) {
        el('heartbeat').textContent = `error ${String(err).slice(0, 80)}`;
      }
    }

    el('windowSelect').addEventListener('change', () => tick());
    window.addEventListener('resize', () => tick());
    tick();
    setInterval(tick, Math.max(700, REFRESH_MS));
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def log_message(self, fmt: str, *args) -> None:
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
            html = HTML_TEMPLATE.replace("__REFRESH_MS__", str(int(cfg.refresh_ms)))
            self._send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return

        if parsed.path == "/healthz":
            self._send_json(200, {"ok": True})
            return

        if parsed.path == "/api/snapshot":
            q = parse_qs(parsed.query)
            minutes = None
            raw_minutes = (q.get("minutes") or [""])[0]
            if raw_minutes:
                try:
                    minutes = float(raw_minutes)
                except Exception:
                    minutes = None
            snap = build_snapshot(cfg, window_minutes=minutes)
            self._send_json(200, snap)
            return

        self._send_json(404, {"error": "not found"})


def parse_args():
    p = argparse.ArgumentParser(description="Realtime dashboard for BTC short-window observe logs (observe-only)")
    p.add_argument("--host", default="127.0.0.1", help="Bind host")
    p.add_argument("--port", type=int, default=8791, help="Bind port")
    p.add_argument("--mode", choices=("lag", "panic"), default="lag", help="Default runtime file preset")
    p.add_argument("--window-minutes", type=int, choices=(5, 15), default=5, help="Preset window size (5 or 15)")
    p.add_argument("--log-file", default="", help="Input log file (defaults by --mode/--window-minutes)")
    p.add_argument("--state-file", default="", help="Input state JSON (defaults by --mode/--window-minutes)")
    p.add_argument("--metrics-file", default="", help="Input metrics JSONL (defaults by --mode/--window-minutes)")
    p.add_argument("--window-lookback-minutes", type=float, default=60.0, help="Default dashboard lookback window")
    p.add_argument("--tail-lines", type=int, default=5000, help="Tail lines to scan per refresh")
    p.add_argument("--refresh-ms", type=int, default=1500, help="Browser refresh interval hint in milliseconds")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    d_log, d_state, d_metrics = default_runtime_paths(args.mode, args.window_minutes)
    cfg = Config(
        host=str(args.host),
        port=int(args.port),
        mode=str(args.mode),
        window_minutes=int(args.window_minutes),
        log_file=str(args.log_file or d_log),
        state_file=str(args.state_file or d_state),
        metrics_file=str(args.metrics_file or d_metrics),
        window_lookback_minutes=float(args.window_lookback_minutes),
        tail_lines=max(200, int(args.tail_lines)),
        refresh_ms=max(500, int(args.refresh_ms)),
    )

    server = DashboardHTTPServer((cfg.host, cfg.port), DashboardHandler)
    server.cfg = cfg

    print("BTC Monitor Dashboard (observe-only)")
    print("=" * 44)
    print(f"URL: http://{cfg.host}:{cfg.port}")
    print(f"preset: mode={cfg.mode} window={cfg.window_minutes}m")
    print(f"metrics: {cfg.metrics_file}")
    print(f"state:   {cfg.state_file}")
    print(f"log:     {cfg.log_file}")
    print("Press Ctrl+C to stop")

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

