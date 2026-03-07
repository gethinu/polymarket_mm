#!/usr/bin/env python3
"""Realtime observe-only dashboard for event-driven artifacts."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import parse_qs, urlparse


LOG_TS_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


def parse_ts(s: str) -> dt.datetime:
    # event-driven artifacts emit naive timestamps in UTC.
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)


def hhmmss(ts_ms: int) -> str:
    try:
        return dt.datetime.fromtimestamp(ts_ms / 1000.0).strftime("%H:%M:%S")
    except Exception:
        return ""


def read_tail_lines(path: str, max_lines: int, max_bytes: int = 8 * 1024 * 1024) -> List[str]:
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


def as_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def mean(xs: List[float], default: float = 0.0) -> float:
    return (sum(xs) / len(xs)) if xs else default


def median(xs: List[float], default: float = 0.0) -> float:
    if not xs:
        return default
    ys = sorted(xs)
    n = len(ys)
    m = n // 2
    return float(ys[m]) if n % 2 else float((ys[m - 1] + ys[m]) / 2.0)


@dataclass
class SignalRow:
    ts_ms: int
    ts: str
    side: str
    edge_cents: float
    confidence: float
    stake: float
    event_class: str
    event_slug: str
    market_id: str
    question: str


@dataclass
class MetricRow:
    ts_ms: int
    ts: str
    scanned: int
    eligible: int
    event_count: int
    candidate: int
    suppressed: int
    written: int
    runtime_sec: float


@dataclass
class Config:
    host: str
    port: int
    signals_file: str
    metrics_file: str
    log_file: str
    profit_json: str
    summary_txt: str
    window_minutes: float
    tail_lines: int
    max_signals: int
    refresh_ms: int


class DashboardHTTPServer(ThreadingHTTPServer):
    cfg: Config


def parse_signal(line: str) -> Optional[SignalRow]:
    try:
        o = json.loads(line)
    except Exception:
        return None
    if not isinstance(o, dict):
        return None
    ts_raw = str(o.get("ts") or "").strip()
    if not ts_raw:
        return None
    try:
        ts_ms = int(parse_ts(ts_raw).timestamp() * 1000.0)
    except Exception:
        return None
    side = str(o.get("side") or "").strip().upper()
    return SignalRow(
        ts_ms=ts_ms,
        ts=ts_raw,
        side=side if side in {"YES", "NO"} else "-",
        edge_cents=as_float(o.get("edge_cents"), 0.0),
        confidence=as_float(o.get("confidence"), 0.0),
        stake=as_float(o.get("suggested_stake_usd"), 0.0),
        event_class=str(o.get("event_class") or "") or "unclassified",
        event_slug=str(o.get("event_slug") or ""),
        market_id=str(o.get("market_id") or ""),
        question=str(o.get("question") or ""),
    )


def parse_metric(line: str) -> Optional[MetricRow]:
    try:
        o = json.loads(line)
    except Exception:
        return None
    if not isinstance(o, dict):
        return None
    ts_raw = str(o.get("ts") or "").strip()
    if not ts_raw:
        return None
    try:
        ts_ms = int(parse_ts(ts_raw).timestamp() * 1000.0)
    except Exception:
        return None
    return MetricRow(
        ts_ms=ts_ms,
        ts=ts_raw,
        scanned=as_int(o.get("scanned"), 0),
        eligible=as_int(o.get("eligible_count"), 0),
        event_count=as_int(o.get("event_count"), 0),
        candidate=as_int(o.get("candidate_count"), 0),
        suppressed=as_int(o.get("suppressed_count"), 0),
        written=as_int(o.get("top_written"), 0),
        runtime_sec=as_float(o.get("runtime_sec"), 0.0),
    )


def load_signals(path: str, since_ms: int, tail_lines: int) -> List[SignalRow]:
    rows: List[SignalRow] = []
    for ln in read_tail_lines(path, max_lines=max(200, tail_lines)):
        r = parse_signal(ln)
        if r and r.ts_ms >= since_ms:
            rows.append(r)
    rows.sort(key=lambda x: x.ts_ms)
    return rows


def load_metrics(path: str, since_ms: int, tail_lines: int) -> List[MetricRow]:
    rows: List[MetricRow] = []
    for ln in read_tail_lines(path, max_lines=max(200, tail_lines)):
        r = parse_metric(ln)
        if r and r.ts_ms >= since_ms:
            rows.append(r)
    rows.sort(key=lambda x: x.ts_ms)
    return rows


def load_profit(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def load_json_obj(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def load_jsonl_rows(path: str, tail_lines: int) -> List[dict]:
    rows: List[dict] = []
    for ln in read_tail_lines(path, max_lines=max(80, tail_lines)):
        s = str(ln or "").strip()
        if not s:
            continue
        try:
            raw = json.loads(s)
        except Exception:
            continue
        if isinstance(raw, dict):
            rows.append(raw)
    return rows


def load_text(path: str, limit: int = 2500) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        txt = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""
    return txt if len(txt) <= limit else (txt[:limit] + "\n...(truncated)")


def log_events(path: str, since_ts: float, tail_lines: int) -> List[dict]:
    out: List[dict] = []
    for ln in read_tail_lines(path, max_lines=max(120, tail_lines)):
        s = ln.strip()
        if not s:
            continue
        m = LOG_TS_RE.search(s)
        ts_txt = ""
        if m:
            ts_txt = str(m.group("ts") or "")
            try:
                if parse_ts(ts_txt).timestamp() < float(since_ts):
                    continue
            except Exception:
                pass
        kind = "info"
        lo = s.lower()
        if s.startswith("#"):
            kind = "signal"
        elif "error" in lo:
            kind = "error"
        elif "warn" in lo:
            kind = "warn"
        elif "run=" in lo and "written=" in lo:
            kind = "run"
        out.append({"ts": ts_txt[-8:], "kind": kind, "text": s[:240]})
    return out[-60:]


def build_snapshot(cfg: Config, minutes: Optional[float]) -> dict:
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_local = dt.datetime.now()
    win = float(minutes if minutes is not None else cfg.window_minutes)
    win = max(5.0, min(7 * 24 * 60.0, win))
    since_utc = now_utc - dt.timedelta(minutes=win)
    since_ms = int(since_utc.timestamp() * 1000.0)

    signals = load_signals(cfg.signals_file, since_ms, cfg.tail_lines)
    metrics = load_metrics(cfg.metrics_file, since_ms, cfg.tail_lines)
    events = log_events(cfg.log_file, since_utc.timestamp(), cfg.tail_lines)
    profit = load_profit(cfg.profit_json)
    summary = load_text(cfg.summary_txt)
    base_dir = Path(cfg.profit_json).resolve().parent
    live_state = load_json_obj(str(base_dir / "event_driven_live_state.json"))
    live_execs = load_jsonl_rows(str(base_dir / "event_driven_live_executions.jsonl"), cfg.tail_lines)

    cand_sum = sum(max(0, m.candidate) for m in metrics)
    write_sum = sum(max(0, m.written) for m in metrics)
    supp_sum = sum(max(0, m.suppressed) for m in metrics)
    yes = sum(1 for s in signals if s.side == "YES")
    no = sum(1 for s in signals if s.side == "NO")

    edges = [s.edge_cents for s in signals]
    confs = [s.confidence for s in signals]
    latest = metrics[-1] if metrics else None
    age_sec = max(0, int(now_utc.timestamp() - latest.ts_ms / 1000.0)) if latest else -1

    cls_counts = {}
    for s in signals:
        cls_counts[s.event_class] = cls_counts.get(s.event_class, 0) + 1
    classes = [{"name": k, "count": v} for k, v in sorted(cls_counts.items(), key=lambda kv: kv[1], reverse=True)]

    recent_signals = []
    for s in sorted(signals, key=lambda x: x.ts_ms, reverse=True)[: max(1, cfg.max_signals)]:
        recent_signals.append(
            {
                "ts": s.ts[-8:],
                "side": s.side,
                "edge_cents": s.edge_cents,
                "confidence": s.confidence,
                "stake": s.stake,
                "event_class": s.event_class,
                "question": s.question,
            }
        )

    decision = profit.get("decision") if isinstance(profit.get("decision"), dict) else {}
    selected = profit.get("selected_threshold") if isinstance(profit.get("selected_threshold"), dict) else {}
    settings = profit.get("settings") if isinstance(profit.get("settings"), dict) else {}
    base_scenario = selected.get("base_scenario") if isinstance(selected.get("base_scenario"), dict) else {}
    reasons = decision.get("reasons") if isinstance(decision.get("reasons"), list) else []

    live_positions_raw = live_state.get("positions") if isinstance(live_state.get("positions"), list) else []
    open_positions = [row for row in live_positions_raw if isinstance(row, dict) and str(row.get("status") or "") == "open"]
    open_rows = []
    for row in open_positions[:6]:
        entry_ts = str(row.get("entry_utc") or "").strip()
        try:
            age_hours = max(0.0, (now_utc - dt.datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))).total_seconds() / 3600.0) if entry_ts else 0.0
        except Exception:
            age_hours = 0.0
        open_rows.append(
            {
                "question": str(row.get("question") or "")[:160],
                "side": str(row.get("side") or "").upper(),
                "size_shares": as_float(row.get("size_shares"), 0.0),
                "entry_price": as_float(row.get("entry_price"), 0.0),
                "notional_usd": as_float(row.get("notional_usd"), 0.0),
                "requested_notional_usd": as_float(row.get("requested_notional_usd"), 0.0),
                "age_hours": age_hours,
                "status": str(row.get("status") or ""),
            }
        )

    recent_live = []
    for row in live_execs[-10:]:
        recent_live.append(
            {
                "ts": str(row.get("ts_utc") or "")[11:19],
                "status": str(row.get("status") or ""),
                "side": str(row.get("side") or "").upper(),
                "question": str(row.get("question") or "")[:120],
                "filled_size_shares": as_float(row.get("filled_size_shares"), 0.0),
                "filled_notional_usd": as_float(row.get("filled_notional_usd"), 0.0),
                "filled_avg_price": as_float(row.get("filled_avg_price"), 0.0),
                "reason": str(row.get("reason") or "")[:120],
            }
        )

    last_fill = {}
    for row in reversed(live_execs):
        if str(row.get("status") or "") == "filled":
            last_fill = {
                "ts": str(row.get("ts_utc") or "")[11:19],
                "side": str(row.get("side") or "").upper(),
                "filled_size_shares": as_float(row.get("filled_size_shares"), 0.0),
                "filled_notional_usd": as_float(row.get("filled_notional_usd"), 0.0),
                "filled_avg_price": as_float(row.get("filled_avg_price"), 0.0),
                "question": str(row.get("question") or "")[:120],
            }
            break

    return {
        "generated_at": now_local.strftime("%Y-%m-%d %H:%M:%S"),
        "window_minutes": win,
        "totals": {
            "runs": len(metrics),
            "signals": len(signals),
            "yes": yes,
            "no": no,
            "unique_events": len({s.event_slug for s in signals if s.event_slug}),
            "unique_markets": len({s.market_id for s in signals if s.market_id}),
            "write_rate": (write_sum / max(1, cand_sum)),
            "suppressed_rate": (supp_sum / max(1, cand_sum)),
            "avg_runtime_sec": mean([m.runtime_sec for m in metrics], 0.0),
            "edge_mean_cents": mean(edges, 0.0),
            "edge_median_cents": median(edges, 0.0),
            "confidence_mean": mean(confs, 0.0),
            "latest_age_sec": age_sec,
            "latest_written": latest.written if latest else 0,
            "latest_candidate": latest.candidate if latest else 0,
            "latest_suppressed": latest.suppressed if latest else 0,
        },
        "series": {
            "metrics_ts": [hhmmss(m.ts_ms) for m in metrics[-260:]],
            "candidate": [m.candidate for m in metrics[-260:]],
            "written": [m.written for m in metrics[-260:]],
            "suppressed": [m.suppressed for m in metrics[-260:]],
            "signal_ts": [hhmmss(s.ts_ms) for s in signals[-260:]],
            "signal_edge": [s.edge_cents for s in signals[-260:]],
            "signal_conf": [s.confidence for s in signals[-260:]],
        },
        "classes": classes,
        "recent_signals": recent_signals,
        "recent_events": events,
        "profit": {
            "decision": str(decision.get("decision") or "N/A"),
            "observe_only_note": "Projected EV only. Not realized PnL or live win rate.",
            "projected_monthly_profit_usd": as_float(base_scenario.get("monthly_profit_usd"), 0.0),
            "projected_monthly_return": as_float(decision.get("projected_monthly_return"), 0.0),
            "target_monthly_return": as_float(decision.get("target_monthly_return"), 0.0),
            "assumed_bankroll_usd": as_float(settings.get("assumed_bankroll_usd"), 0.0),
            "max_stake_usd": as_float(settings.get("max_stake_usd"), 0.0),
            "selected_threshold_cents": as_float(selected.get("threshold_cents"), 0.0),
            "selected_hit_ratio": as_float(selected.get("hit_ratio"), 0.0),
            "selected_episodes_hit": as_int(selected.get("episodes_hit"), 0),
            "selected_episodes_total": as_int(selected.get("episodes_total"), 0),
            "selected_unique_events": as_int(selected.get("unique_events"), 0),
            "selected_opp_per_day": as_float(selected.get("opportunities_per_day_capped"), 0.0),
            "reasons": [str(x)[:180] for x in reasons[:8]],
        },
        "live": {
            "open_positions": len(open_positions),
            "daily_notional_usd": as_float(live_state.get("daily_notional_usd"), 0.0),
            "last_run_mode": str(((live_state.get("last_run") if isinstance(live_state.get("last_run"), dict) else {}) or {}).get("mode") or ""),
            "last_run_attempted": as_int(((live_state.get("last_run") if isinstance(live_state.get("last_run"), dict) else {}) or {}).get("attempted"), 0),
            "last_run_submitted": as_int(((live_state.get("last_run") if isinstance(live_state.get("last_run"), dict) else {}) or {}).get("submitted"), 0),
            "open_rows": open_rows,
            "recent_execs": list(reversed(recent_live)),
            "last_fill": last_fill,
            "exit_note": "Live exit / resolution checks run when execute_event_driven_live.py runs. Current local task policy is daily, not realtime.",
        },
        "summary_text": summary,
    }


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Event-Driven Ops Deck</title>
<style>
:root{--bg:#081018;--panel:#112130;--line:#244a62;--txt:#d8e6f0;--muted:#8ca7ba;--up:#1fd08f;--down:#ff6b78;--flat:#95b2c5;--c1:#5bd3ff;--c2:#ffb84d}
*{box-sizing:border-box}body{margin:0;padding:16px;background:linear-gradient(180deg,#04090d,var(--bg));font-family:Segoe UI,system-ui,sans-serif;color:var(--txt)}
.wrap{max-width:1500px;margin:0 auto}.top{display:flex;justify-content:space-between;align-items:end;gap:8px;margin-bottom:10px}
h1{margin:0;font-size:24px;letter-spacing:.06em;text-transform:uppercase}p{margin:4px 0 0;color:var(--muted);font-size:13px}
.ctl{display:flex;gap:8px;align-items:center;font:12px Consolas,monospace;color:var(--muted)}select,.badge{background:#0e1b27;color:var(--txt);border:1px solid var(--line);border-radius:10px;padding:6px 8px}
.stats{display:grid;grid-template-columns:repeat(8,minmax(120px,1fr));gap:8px;margin-bottom:10px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:8px}
.k{font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}.v{margin-top:5px;font:700 17px Consolas,monospace}.up{color:var(--up)}.down{color:var(--down)}.neutral{color:var(--flat)}
.grid{display:grid;grid-template-columns:1.2fr 1.2fr 1fr;gap:10px;margin-bottom:10px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:9px}
h3{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:#a3bece}canvas{width:100%;height:220px;border:1px solid #204357;border-radius:9px;background:#0b1620;display:block}
.notice{margin:0 0 10px;border:1px solid rgba(255,184,77,.45);background:rgba(255,184,77,.08);border-radius:12px;padding:9px 10px;font:12px Consolas,monospace;color:#ffd28b}
.rows{max-height:260px;overflow:auto;display:grid;gap:6px}.row{display:flex;justify-content:space-between;border:1px solid #21465c;border-radius:8px;padding:6px 8px;font:12px Consolas,monospace}
table{width:100%;border-collapse:collapse;font:11px Consolas,monospace}.tw{max-height:320px;overflow:auto;border:1px solid #21455b;border-radius:9px}
th,td{padding:6px 7px;border-bottom:1px solid #1d3f54;text-align:left;vertical-align:top}th{position:sticky;top:0;background:#122333;color:#9fb9cb;font-size:10px;text-transform:uppercase}
.ev{max-height:320px;overflow:auto;display:grid;gap:6px}.evt{border:1px solid #21475d;border-radius:8px;padding:6px 8px;font:11px Consolas,monospace}.evt.signal{border-color:rgba(31,208,143,.5)}.evt.run{border-color:rgba(91,211,255,.5)}.evt.error,.evt.warn{border-color:rgba(255,107,120,.6)}
pre{margin:0;max-height:220px;overflow:auto;border:1px solid #21455b;border-radius:9px;padding:8px;background:#0b1620;font:11px Consolas,monospace;white-space:pre-wrap}
.meta{margin-top:6px;color:var(--muted);font:11px Consolas,monospace;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
@media (max-width:1200px){.stats{grid-template-columns:repeat(4,minmax(120px,1fr))}.grid{grid-template-columns:1fr}}@media (max-width:700px){.stats{grid-template-columns:repeat(2,minmax(120px,1fr))}}
</style></head><body><div class="wrap">
<header class="top"><div><h1>Event-Driven Ops Deck</h1><p>observe-only / signal flow + profit-window monitor</p></div>
<div class="ctl"><label for="win">window</label><select id="win"><option value="30">30m</option><option value="60">60m</option><option value="180" selected>180m</option><option value="360">360m</option><option value="720">720m</option><option value="1440">1440m</option></select><span id="hb" class="badge">waiting...</span></div></header>
<section class="notice">This page separates two things: observe-only signal flow / projected EV, and actual live position state from the guarded micro-live helper.</section>
<section class="stats">
<div class="card"><div class="k">Runs</div><div id="stRuns" class="v">-</div></div>
<div class="card"><div class="k">Signals (YES/NO)</div><div id="stSignals" class="v">-</div></div>
<div class="card"><div class="k">Write Rate</div><div id="stWrite" class="v">-</div></div>
<div class="card"><div class="k">Suppressed Rate</div><div id="stSupp" class="v">-</div></div>
<div class="card"><div class="k">Observe Gate</div><div id="stDec" class="v">-</div></div>
<div class="card"><div class="k">Projected EV / Month</div><div id="stProj" class="v">-</div></div>
<div class="card"><div class="k">Episode Pass / Basis</div><div id="stUniq" class="v">-</div></div>
<div class="card"><div class="k">Last Run Age</div><div id="stAge" class="v">-</div></div>
</section>
<section class="grid"><article class="panel"><h3>Live Position State</h3><div id="liveState" class="ev"></div></article><article class="panel"><h3>Open Live Positions</h3><div id="liveOpen" class="ev"></div></article><article class="panel"><h3>Recent Live Executions</h3><div id="liveExec" class="ev"></div></article></section>
<section class="grid"><article class="panel"><h3>Run Flow (cand / write / supp)</h3><canvas id="cRun"></canvas></article><article class="panel"><h3>Signal Edge + Conf</h3><canvas id="cSig"></canvas></article><article class="panel"><h3>Class Breakdown</h3><div id="classes" class="rows"></div></article></section>
<section class="grid"><article class="panel"><h3>Recent Signals</h3><div class="tw"><table><thead><tr><th>ts</th><th>side</th><th>edge</th><th>conf</th><th>stake</th><th>class</th><th>question</th></tr></thead><tbody id="sigRows"></tbody></table></div></article>
<article class="panel"><h3>Recent Log Events</h3><div id="events" class="ev"></div></article>
<article class="panel"><h3>Projected Profit Window (Observe Only)</h3><div id="profit" class="ev"></div></article></section>
<section class="panel"><h3>Daily Summary Snapshot</h3><pre id="summary">-</pre><div class="meta"><span id="mA">metrics: -</span><span id="mB">profit: -</span><span id="mC">refresh: __REFRESH_MS__ms</span></div></section>
</div>
<script>
const REFRESH_MS=__REFRESH_MS__, el=(id)=>document.getElementById(id);
const esc=(s)=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'","&#39;");
const pct=(v)=>(Number(v||0)*100).toFixed(1)+'%';
const age=(sec)=>{const s=Math.max(0,Number(sec||0)); if(s<60)return Math.floor(s)+'s'; if(s<3600)return Math.floor(s/60)+'m'; return (s/3600).toFixed(1)+'h';};
function draw(canvas, lines){const dpr=Math.max(1,window.devicePixelRatio||1),w=canvas.clientWidth||800,h=canvas.clientHeight||220; canvas.width=Math.floor(w*dpr); canvas.height=Math.floor(h*dpr); const c=canvas.getContext('2d'); c.setTransform(dpr,0,0,dpr,0,0); c.clearRect(0,0,w,h);
 const all=[]; for(const l of lines){for(const x of (l.v||[]))all.push(Number(x||0));} if(all.length<2){c.fillStyle='#8ca7ba'; c.font='12px Consolas'; c.fillText('waiting for metrics...',16,22); return;}
 let mn=Math.min(...all), mx=Math.max(...all); if(Math.abs(mx-mn)<1e-9)mx=mn+1; const p=22,pw=w-p*2,ph=h-p*2,xa=(i,n)=>p+pw*i/Math.max(1,n-1),ya=(v)=>p+((mx-v)/(mx-mn))*ph;
 c.strokeStyle='rgba(140,167,186,.25)'; c.lineWidth=1; for(let i=0;i<=4;i++){const y=p+ph*i/4; c.beginPath(); c.moveTo(p,y); c.lineTo(p+pw,y); c.stroke();}
 for(const l of lines){const arr=l.v||[]; if(arr.length<2)continue; c.strokeStyle=l.c||'#ccc'; c.lineWidth=l.w||1.7; c.beginPath(); for(let i=0;i<arr.length;i++){const x=xa(i,arr.length),y=ya(Number(arr[i]||0)); if(i===0)c.moveTo(x,y); else c.lineTo(x,y);} c.stroke();}}
function render(s){const t=s.totals||{}, p=s.profit||{}, live=s.live||{}, cls=Array.isArray(s.classes)?s.classes:[], rs=Array.isArray(s.recent_signals)?s.recent_signals:[], ev=Array.isArray(s.recent_events)?s.recent_events:[];
 el('stRuns').textContent=String(Number(t.runs||0)); el('stSignals').textContent=`${Number(t.signals||0)} (${Number(t.yes||0)}/${Number(t.no||0)})`;
 el('stWrite').textContent=pct(t.write_rate||0); el('stWrite').className=`v ${(t.write_rate||0)>0.05?'up':'neutral'}`;
 el('stSupp').textContent=pct(t.suppressed_rate||0); el('stSupp').className=`v ${(t.suppressed_rate||0)>0.95?'down':'neutral'}`;
 const d=String(p.decision||'N/A').toUpperCase(); el('stDec').textContent=d; el('stDec').className=`v ${d==='GO'?'up':d==='NO_GO'?'down':'neutral'}`;
 el('stProj').textContent=`$${Number(p.projected_monthly_profit_usd||0).toFixed(2)} / ${pct(p.projected_monthly_return||0)}`; el('stProj').className=`v ${((p.projected_monthly_return||0)-(p.target_monthly_return||0))>=0?'up':'down'}`;
 const epHit=Number(p.selected_episodes_hit||0), epTotal=Number(p.selected_episodes_total||0), bk=Number(p.assumed_bankroll_usd||0), cap=Number(p.max_stake_usd||0);
 el('stUniq').textContent=`${epHit}/${epTotal} | $${bk.toFixed(0)}/$${cap.toFixed(0)}`; el('stAge').textContent=(Number(t.latest_age_sec||-1)>=0)?age(t.latest_age_sec):'-'; el('stAge').className=`v ${(t.latest_age_sec||0)>600?'down':'neutral'}`;
 const lf=live.last_fill||{}, openRows=Array.isArray(live.open_rows)?live.open_rows:[], liveExecs=Array.isArray(live.recent_execs)?live.recent_execs:[];
 el('liveState').innerHTML=`<div class="evt"><b>open positions</b>: ${Number(live.open_positions||0)} | <b>daily deployed</b>: $${Number(live.daily_notional_usd||0).toFixed(2)}</div><div class="evt"><b>last live run</b>: mode=${esc(live.last_run_mode||'-')} attempted=${Number(live.last_run_attempted||0)} filled=${Number(live.last_run_submitted||0)}</div><div class="evt"><b>last fill</b>: ${lf.ts?esc(lf.ts):'-'} ${esc(lf.side||'-')} ${Number(lf.filled_size_shares||0).toFixed(2)} @ ${Number(lf.filled_avg_price||0).toFixed(3)} = $${Number(lf.filled_notional_usd||0).toFixed(2)}</div><div class="evt"><b>exit cadence</b>: ${esc(live.exit_note||'-')}</div>`;
 el('liveOpen').innerHTML=openRows.length?openRows.map(x=>`<div class="evt"><div><b>${esc(x.side||'-')}</b> ${Number(x.size_shares||0).toFixed(2)} @ ${Number(x.entry_price||0).toFixed(3)} = $${Number(x.notional_usd||0).toFixed(2)}</div><div>${esc(String(x.question||''))}</div><div>requested=$${Number(x.requested_notional_usd||0).toFixed(2)} | age=${Number(x.age_hours||0).toFixed(1)}h | status=${esc(x.status||'-')}</div></div>`).join(''):'<div class="evt">no open live positions</div>';
 el('liveExec').innerHTML=liveExecs.length?liveExecs.map(x=>`<div class="evt ${esc(x.status||'')}"><div>${esc(x.ts||'-')} | ${esc(String(x.status||'-').toUpperCase())} | ${esc(x.side||'-')}</div><div>${esc(String(x.question||''))}</div><div>${Number(x.filled_size_shares||0)>0?`filled ${Number(x.filled_size_shares||0).toFixed(2)} @ ${Number(x.filled_avg_price||0).toFixed(3)} = $${Number(x.filled_notional_usd||0).toFixed(2)}`:esc(x.reason||'no fill')}</div></div>`).join(''):'<div class="evt">no live executions yet</div>';
 draw(el('cRun'),[{v:s.series?.candidate,c:'#5bd3ff',w:2.1},{v:s.series?.written,c:'#1fd08f',w:1.8},{v:s.series?.suppressed,c:'#ff6b78',w:1.4}]);
 draw(el('cSig'),[{v:s.series?.signal_edge,c:'#ffb84d',w:2.0},{v:(s.series?.signal_conf||[]).map(x=>Number(x||0)*30),c:'#5bd3ff',w:1.3}]);
 el('classes').innerHTML=cls.length?cls.map(x=>`<div class="row"><span>${esc(x.name)}</span><b>${Number(x.count||0)}</b></div>`).join(''):'<div class="row"><span>-</span><b>0</b></div>';
 el('sigRows').innerHTML=rs.length?rs.map(x=>`<tr><td>${esc(x.ts||'')}</td><td class="${x.side==='YES'?'up':'down'}">${esc(x.side||'-')}</td><td class="${Number(x.edge_cents||0)>=0?'up':'down'}">${(Number(x.edge_cents||0)>=0?'+':'')+Number(x.edge_cents||0).toFixed(2)}c</td><td>${pct(x.confidence||0)}</td><td>$${Number(x.stake||0).toFixed(2)}</td><td>${esc(x.event_class||'-')}</td><td>${esc(String(x.question||'').slice(0,140))}</td></tr>`).join(''):'<tr><td colspan="7">no signals in window</td></tr>';
 el('events').innerHTML=ev.length?ev.slice().reverse().map(x=>`<div class="evt ${esc(x.kind||'')}"><div>${esc(x.ts||'-')} | ${esc(String(x.kind||'info').toUpperCase())}</div><div>${esc(x.text||'')}</div></div>`).join(''):'<div class="evt">no recent log events</div>';
 const reasons=Array.isArray(p.reasons)?p.reasons:[]; el('profit').innerHTML=`<div class="evt"><b>observe-only</b>: ${esc(p.observe_only_note||'Projected EV only')}</div><div class="evt"><b>gate</b>: ${esc(d)} | <b>threshold</b>: ${Number(p.selected_threshold_cents||0).toFixed(2)}c</div><div class="evt"><b>projected EV</b>: $${Number(p.projected_monthly_profit_usd||0).toFixed(2)} / ${pct(p.projected_monthly_return||0)} | <b>target</b>: ${pct(p.target_monthly_return||0)}</div><div class="evt"><b>basis</b>: bankroll=$${bk.toFixed(2)} | stake_cap=$${cap.toFixed(2)}</div><div class="evt"><b>episode pass</b>: ${epHit}/${epTotal} = ${pct(p.selected_hit_ratio||0)} | <b>events</b>: ${Number(p.selected_unique_events||0)} | <b>opp/day</b>: ${Number(p.selected_opp_per_day||0).toFixed(2)}</div>${reasons.map(r=>`<div class="evt">${esc(r)}</div>`).join('')||'<div class="evt">no decision reasons</div>'}`;
 el('summary').textContent=String(s.summary_text||'-'); el('mA').textContent=`metrics: avg_runtime=${Number(t.avg_runtime_sec||0).toFixed(2)}s edge_median=${(Number(t.edge_median_cents||0)>=0?'+':'')+Number(t.edge_median_cents||0).toFixed(2)}c conf_mean=${pct(t.confidence_mean||0)}`;
 el('mB').textContent=`projected_ev: $${Number(p.projected_monthly_profit_usd||0).toFixed(2)} / ${pct(p.projected_monthly_return||0)} (not realized)`; el('hb').textContent=`updated ${s.generated_at||'-'} | window ${Number(s.window_minutes||0)}m`;}
async function tick(){try{const m=Number(el('win').value||180); const r=await fetch(`/api/snapshot?minutes=${encodeURIComponent(m)}`,{cache:'no-store'}); if(!r.ok)throw new Error(`HTTP ${r.status}`); render(await r.json());}catch(err){el('hb').textContent=`error ${String(err).slice(0,80)}`;}}
el('win').addEventListener('change',tick); window.addEventListener('resize',tick); tick(); setInterval(tick,Math.max(700,REFRESH_MS));
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def log_message(self, fmt: str, *args) -> None:
        return

    def send_bytes(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code: int, payload: dict) -> None:
        self.send_bytes(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        cfg = self.server.cfg
        p = urlparse(self.path)
        if p.path in {"/", "/index.html"}:
            html = HTML.replace("__REFRESH_MS__", str(int(cfg.refresh_ms)))
            self.send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if p.path == "/api/snapshot":
            q = parse_qs(p.query)
            m = None
            try:
                raw = (q.get("minutes") or [""])[0]
                if raw:
                    m = float(raw)
            except Exception:
                m = None
            self.send_json(200, build_snapshot(cfg, m))
            return
        self.send_json(404, {"error": "not found"})


def parse_args():
    logs = Path(__file__).resolve().parents[1] / "logs"
    p = argparse.ArgumentParser(description="Realtime dashboard for event-driven observe artifacts")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8788)
    p.add_argument("--signals-file", default=str(logs / "event-driven-observe-signals.jsonl"))
    p.add_argument("--metrics-file", default=str(logs / "event-driven-observe-metrics.jsonl"))
    p.add_argument("--log-file", default=str(logs / "event-driven-observe.log"))
    p.add_argument("--profit-json", default=str(logs / "event_driven_profit_window_latest.json"))
    p.add_argument("--summary-txt", default=str(logs / "event_driven_daily_summary.txt"))
    p.add_argument("--window-minutes", type=float, default=180.0)
    p.add_argument("--tail-lines", type=int, default=12000)
    p.add_argument("--max-signals", type=int, default=18)
    p.add_argument("--refresh-ms", type=int, default=1500)
    return p.parse_args()


def main() -> int:
    a = parse_args()
    cfg = Config(
        host=str(a.host),
        port=int(a.port),
        signals_file=str(a.signals_file),
        metrics_file=str(a.metrics_file),
        log_file=str(a.log_file),
        profit_json=str(a.profit_json),
        summary_txt=str(a.summary_txt),
        window_minutes=float(a.window_minutes),
        tail_lines=max(200, int(a.tail_lines)),
        max_signals=max(1, int(a.max_signals)),
        refresh_ms=max(600, int(a.refresh_ms)),
    )
    server = DashboardHTTPServer((cfg.host, cfg.port), Handler)
    server.cfg = cfg
    print(f"Event-Driven Dashboard: http://{cfg.host}:{cfg.port}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
