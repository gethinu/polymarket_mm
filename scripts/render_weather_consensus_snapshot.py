#!/usr/bin/env python3
"""
Render weather consensus watchlist JSON into a visual HTML snapshot.

Observe-only helper:
  - reads output from scripts/build_weather_consensus_watchlist.py
  - writes a standalone HTML snapshot under logs/
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
from pathlib import Path
from typing import Dict, List, Optional


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_path(raw: str, default_name: str) -> Path:
    logs = repo_root() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if not raw.strip():
        return logs / default_name
    p = Path(raw)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs / p.name
    return repo_root() / p


def _opt_float(v) -> Optional[float]:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n):
        return None
    return n


def _fmt_opt(v: Optional[float], digits: int = 4) -> str:
    if v is None:
        return "N/A"
    return f"{v:.{digits}f}"


def load_ab_report(profile_name: str, baseline_name: str) -> dict:
    path = repo_root() / "logs" / f"{profile_name}_ab_vs_{baseline_name}_latest.json"
    out: Dict[str, object] = {
        "available": False,
        "baseline_name": baseline_name,
        "path": str(path),
    }
    if not path.exists():
        return out
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        out["error"] = f"parse_error: {exc}"
        return out
    if not isinstance(raw, dict):
        out["error"] = "invalid_root_type"
        return out

    overlap = raw.get("overlap") if isinstance(raw.get("overlap"), dict) else {}
    metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
    yield_block = metrics.get("yield_per_day") if isinstance(metrics.get("yield_per_day"), dict) else {}
    liq_block = metrics.get("liquidity") if isinstance(metrics.get("liquidity"), dict) else {}
    c_y = (yield_block.get("consensus") if isinstance(yield_block.get("consensus"), dict) else {})
    b_y = (yield_block.get("baseline") if isinstance(yield_block.get("baseline"), dict) else {})
    c_l = (liq_block.get("consensus") if isinstance(liq_block.get("consensus"), dict) else {})
    b_l = (liq_block.get("baseline") if isinstance(liq_block.get("baseline"), dict) else {})
    cautions = raw.get("cautions") if isinstance(raw.get("cautions"), list) else []
    signals = raw.get("signals") if isinstance(raw.get("signals"), list) else []

    out.update(
        {
            "available": True,
            "assessment": str(raw.get("assessment") or "unknown"),
            "generated_utc": str(raw.get("generated_utc") or ""),
            "overlap_count": int(as_float(overlap.get("count"), 0.0)),
            "overlap_ratio_vs_consensus": _opt_float(overlap.get("ratio_vs_consensus")),
            "yield_consensus_median": _opt_float(c_y.get("median")),
            "yield_baseline_median": _opt_float(b_y.get("median")),
            "liq_consensus_median": _opt_float(c_l.get("median")),
            "liq_baseline_median": _opt_float(b_l.get("median")),
            "cautions_count": len(cautions),
            "signals_count": len(signals),
        }
    )
    return out


def as_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _bar_width(v: float, vmax: float) -> float:
    if vmax <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * v / vmax))


def _fmt(v: float, digits: int = 2) -> str:
    if not math.isfinite(v):
        return "-"
    return f"{v:.{digits}f}"


def render_html(payload: dict, profile_name: str, top_n: int) -> str:
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    top = payload.get("top") if isinstance(payload.get("top"), list) else []
    top_rows: List[dict] = [r for r in top if isinstance(r, dict)][: max(1, int(top_n))]

    scoring = meta.get("scoring") if isinstance(meta.get("scoring"), dict) else {}
    filters = meta.get("filters") if isinstance(meta.get("filters"), dict) else {}
    inputs = meta.get("input_counts") if isinstance(meta.get("input_counts"), dict) else {}

    max_score = max([as_float(r.get("score_total"), 0.0) for r in top_rows] + [1.0])
    max_liq = max([as_float(r.get("liquidity_num"), 0.0) for r in top_rows] + [1.0])
    max_vol = max([as_float(r.get("volume_24h"), 0.0) for r in top_rows] + [1.0])
    max_yield = max([as_float(r.get("net_yield_per_day"), 0.0) for r in top_rows] + [1.0])

    both_cnt = 0
    no_only_cnt = 0
    late_only_cnt = 0
    neither_cnt = 0
    side_yes_cnt = 0
    side_no_cnt = 0
    hours_vals: List[float] = []

    for r in top_rows:
        in_no = bool(r.get("in_no_longshot"))
        in_late = bool(r.get("in_lateprob"))
        if in_no and in_late:
            both_cnt += 1
        elif in_no:
            no_only_cnt += 1
        elif in_late:
            late_only_cnt += 1
        else:
            neither_cnt += 1

        side_val = str(r.get("side_hint") or "").strip().lower()
        if side_val == "yes":
            side_yes_cnt += 1
        elif side_val == "no":
            side_no_cnt += 1

        h = as_float(r.get("hours_to_end"), math.nan)
        if math.isfinite(h) and h >= 0:
            hours_vals.append(h)

    rows_html: List[str] = []
    for r in top_rows:
        rank = int(as_float(r.get("rank"), 0.0))
        market_id_raw = str(r.get("market_id") or "")
        end_iso_raw = str(r.get("end_iso") or "")
        question_raw = str(r.get("question") or "")
        question = html.escape(question_raw)
        question_attr = html.escape(question_raw, quote=True)
        score = as_float(r.get("score_total"), 0.0)
        yes_price = as_float(r.get("yes_price"), math.nan)
        liq = as_float(r.get("liquidity_num"), 0.0)
        vol = as_float(r.get("volume_24h"), 0.0)
        side_raw = str(r.get("side_hint") or "").strip().lower()
        side = html.escape(side_raw)
        entry_price = as_float(r.get("entry_price"), math.nan)
        max_profit = as_float(r.get("max_profit"), math.nan)
        net_yield_day = as_float(r.get("net_yield_per_day"), math.nan)
        hours_to_end = as_float(r.get("hours_to_end"), math.nan)
        in_no = bool(r.get("in_no_longshot"))
        in_late = bool(r.get("in_lateprob"))

        if in_no and in_late:
            source_tag = "both"
            source_cls = "src-both"
        elif in_no:
            source_tag = "no-longshot"
            source_cls = "src-no"
        elif in_late:
            source_tag = "lateprob"
            source_cls = "src-late"
        else:
            source_tag = "none"
            source_cls = "src-none"

        score_w = _bar_width(score, max_score)
        liq_w = _bar_width(liq, max_liq)
        vol_w = _bar_width(vol, max_vol)
        yld_w = _bar_width(max(0.0, net_yield_day), max_yield)

        side_cls = "side-none"
        if side_raw == "yes":
            side_cls = "side-yes"
        elif side_raw == "no":
            side_cls = "side-no"

        rows_html.append(
            f"""
            <tr
              data-rank="{rank}"
              data-marketid="{html.escape(market_id_raw, quote=True)}"
              data-endiso="{html.escape(end_iso_raw, quote=True)}"
              data-question="{question_attr}"
              data-score="{score:.8f}"
              data-yes="{yes_price:.8f}"
              data-entry="{entry_price:.8f}"
              data-maxprofit="{max_profit:.8f}"
              data-yield="{net_yield_day:.8f}"
              data-hours="{hours_to_end:.8f}"
              data-side="{html.escape(side_raw, quote=True)}"
              data-source="{html.escape(source_tag, quote=True)}"
              data-liq="{liq:.8f}"
              data-vol="{vol:.8f}"
            >
              <td class="rank">{rank}</td>
              <td class="q">{question}</td>
              <td class="num">
                <div class="val">{_fmt(score, 2)}</div>
                <div class="bar"><span style="width:{score_w:.1f}%"></span></div>
              </td>
              <td class="num">{_fmt(yes_price, 4)}</td>
              <td class="num">{_fmt(entry_price, 4)}</td>
              <td class="num">{_fmt(max_profit, 4)}</td>
              <td class="num">
                <div class="val">{_fmt(net_yield_day, 4)}</div>
                <div class="bar yld"><span style="width:{yld_w:.1f}%"></span></div>
              </td>
              <td class="num">{_fmt(hours_to_end, 2)}</td>
              <td class="num"><span class="chip {side_cls}">{side if side else "-"}</span></td>
              <td class="num"><span class="chip {source_cls}">{html.escape(source_tag)}</span></td>
              <td class="num">
                <div class="val">{_fmt(liq, 0)}</div>
                <div class="bar liq"><span style="width:{liq_w:.1f}%"></span></div>
              </td>
              <td class="num">
                <div class="val">{_fmt(vol, 0)}</div>
                <div class="bar vol"><span style="width:{vol_w:.1f}%"></span></div>
              </td>
            </tr>
            """
        )

    score_mode = html.escape(str(scoring.get("score_mode") or ""))
    weights = scoring.get("weights") if isinstance(scoring.get("weights"), dict) else {}
    weights_txt = ", ".join(f"{k}={as_float(v):.3f}" for k, v in weights.items()) if weights else "-"

    min_liq = _fmt(as_float(filters.get("min_liquidity"), math.nan), 0)
    min_vol = _fmt(as_float(filters.get("min_volume_24h"), math.nan), 0)

    generated_utc = html.escape(str(payload.get("generated_utc") or ""))
    now_tag = now_utc().isoformat()
    profile = html.escape(profile_name)
    profile_js = json.dumps(profile_name)
    no_cnt = int(as_float(inputs.get("no_longshot_rows"), 0.0))
    late_cnt = int(as_float(inputs.get("lateprob_rows"), 0.0))
    merged_cnt = int(as_float(inputs.get("merged_rows"), 0.0))
    hours_min = min(hours_vals) if hours_vals else math.nan
    hours_max = max(hours_vals) if hours_vals else math.nan
    yld_vals = [as_float(r.get("net_yield_per_day"), math.nan) for r in top_rows]
    yld_vals = [x for x in yld_vals if math.isfinite(x)]
    yld_max = max(yld_vals) if yld_vals else math.nan
    yld_med = sorted(yld_vals)[len(yld_vals) // 2] if yld_vals else math.nan

    ab_no = load_ab_report(profile_name=str(profile_name), baseline_name="no_longshot")
    ab_late = load_ab_report(profile_name=str(profile_name), baseline_name="lateprob")

    def render_ab_card(rep: dict, label: str) -> str:
        available = bool(rep.get("available"))
        if not available:
            err = str(rep.get("error") or "report not found")
            path = html.escape(str(rep.get("path") or ""))
            return (
                f"""
                <article class="ab-card">
                  <div class="ab-head">
                    <div class="ab-title">consensus vs {html.escape(label)}</div>
                    <span class="ab-chip unavailable">N/A</span>
                  </div>
                  <div class="ab-meta">{html.escape(err)}</div>
                  <div class="ab-meta ab-path">{path}</div>
                </article>
                """
            )

        assessment = str(rep.get("assessment") or "unknown").strip().lower()
        chip_cls = "neutral"
        if assessment == "favorable":
            chip_cls = "good"
        elif assessment == "mixed":
            chip_cls = "warn"
        elif assessment == "weak":
            chip_cls = "bad"

        overlap_ratio = _fmt_opt(_opt_float(rep.get("overlap_ratio_vs_consensus")), 3)
        yield_c = _fmt_opt(_opt_float(rep.get("yield_consensus_median")), 6)
        yield_b = _fmt_opt(_opt_float(rep.get("yield_baseline_median")), 6)
        liq_c = _fmt_opt(_opt_float(rep.get("liq_consensus_median")), 1)
        liq_b = _fmt_opt(_opt_float(rep.get("liq_baseline_median")), 1)
        cautions = int(as_float(rep.get("cautions_count"), 0.0))
        signals = int(as_float(rep.get("signals_count"), 0.0))
        generated = html.escape(str(rep.get("generated_utc") or "-"))

        return (
            f"""
            <article class="ab-card">
              <div class="ab-head">
                <div class="ab-title">consensus vs {html.escape(label)}</div>
                <span class="ab-chip {chip_cls}">{html.escape(assessment)}</span>
              </div>
              <div class="ab-meta">generated_utc={generated}</div>
              <div class="ab-grid-kv">
                <div>overlap_ratio</div><div>{overlap_ratio}</div>
                <div>yield_med</div><div>{yield_c} / {yield_b}</div>
                <div>liq_med</div><div>{liq_c} / {liq_b}</div>
                <div>signals / cautions</div><div>{signals} / {cautions}</div>
              </div>
            </article>
            """
        )

    ab_cards_html = render_ab_card(ab_no, "no_longshot") + render_ab_card(ab_late, "lateprob")

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{profile} consensus snapshot</title>
  <style>
    :root {{
      --bg: #0f1419;
      --card: #141b22;
      --ink: #e7edf4;
      --muted: #9eb0c3;
      --line: #2b3642;
      --score: #5ec7b7;
      --liq: #79a8ff;
      --vol: #f0a35a;
      --accent: #7ad26b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: radial-gradient(1200px 700px at 10% -20%, #1a2430 0%, #0f1419 50%, #0d1116 100%);
      color: var(--ink);
      font: 14px/1.45 "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    }}
    .wrap {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 22px 18px 28px;
    }}
    h1 {{
      margin: 0 0 4px;
      font-size: 23px;
      letter-spacing: 0.2px;
    }}
    .sub {{
      margin: 0 0 14px;
      color: var(--muted);
      font-size: 12px;
    }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(165px, 1fr));
      gap: 10px;
      margin: 0 0 12px;
    }}
    .ab-section {{
      margin: 0 0 12px;
    }}
    .ab-title-row {{
      margin: 0 0 6px;
      color: var(--muted);
      font-size: 11px;
      letter-spacing: 0.5px;
      text-transform: uppercase;
    }}
    .ab-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 10px;
    }}
    .ab-card {{
      background: linear-gradient(160deg, rgba(255,255,255,0.02), rgba(255,255,255,0.0)), var(--card);
      border: 1px solid var(--line);
      border-radius: 11px;
      padding: 10px 12px;
    }}
    .ab-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 6px;
    }}
    .ab-title {{
      font-size: 12px;
      color: var(--ink);
      letter-spacing: 0.2px;
    }}
    .ab-chip {{
      display: inline-block;
      min-width: 72px;
      padding: 2px 8px;
      border-radius: 999px;
      text-align: center;
      font-size: 11px;
      font-weight: 700;
      text-transform: lowercase;
      border: 1px solid transparent;
    }}
    .ab-chip.good {{ color: #96e6bb; background: rgba(75, 199, 124, 0.15); border-color: rgba(75, 199, 124, 0.45); }}
    .ab-chip.warn {{ color: #ffd4a5; background: rgba(240, 163, 90, 0.15); border-color: rgba(240, 163, 90, 0.45); }}
    .ab-chip.bad {{ color: #ffb7b7; background: rgba(255, 90, 90, 0.14); border-color: rgba(255, 90, 90, 0.40); }}
    .ab-chip.neutral, .ab-chip.unavailable {{ color: #b7c5d6; background: rgba(255,255,255,0.06); border-color: rgba(255,255,255,0.16); }}
    .ab-meta {{
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 6px;
      word-break: break-all;
    }}
    .ab-path {{
      font-size: 10px;
      opacity: 0.85;
    }}
    .ab-grid-kv {{
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 4px 10px;
      font-size: 12px;
    }}
    .ab-grid-kv > div:nth-child(odd) {{
      color: var(--muted);
    }}
    .ab-grid-kv > div:nth-child(even) {{
      text-align: right;
      color: var(--ink);
      font-weight: 600;
    }}
    .card {{
      background: linear-gradient(160deg, rgba(255,255,255,0.02), rgba(255,255,255,0.0)), var(--card);
      border: 1px solid var(--line);
      border-radius: 11px;
      padding: 10px 12px;
      min-height: 76px;
    }}
    .k {{
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}
    .v {{
      margin-top: 5px;
      font-size: 18px;
      font-weight: 700;
    }}
    .v.small {{ font-size: 12px; font-weight: 600; color: var(--ink); line-height: 1.35; }}
    .table-wrap {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 12px;
      overflow: auto;
    }}
    .controls {{
      margin: 0 0 10px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px 12px;
      align-items: center;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(255,255,255,0.02);
    }}
    .ctl-group {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .ctl-label {{
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}
    .flt-btn {{
      border: 1px solid rgba(255,255,255,0.14);
      background: rgba(255,255,255,0.05);
      color: var(--ink);
      border-radius: 999px;
      padding: 2px 10px;
      font-size: 12px;
      line-height: 1.4;
      cursor: pointer;
    }}
    .flt-btn:hover {{
      background: rgba(255,255,255,0.10);
    }}
    .flt-btn.active {{
      border-color: rgba(122, 210, 107, 0.75);
      background: rgba(122, 210, 107, 0.20);
      color: #d6ffd0;
    }}
    .preset-btn.active {{
      border-color: rgba(132, 194, 255, 0.78);
      background: rgba(132, 194, 255, 0.20);
      color: #d8ecff;
    }}
    .search {{
      border: 1px solid rgba(255,255,255,0.16);
      background: rgba(0,0,0,0.20);
      color: var(--ink);
      border-radius: 8px;
      padding: 5px 8px;
      min-width: 260px;
      outline: none;
    }}
    .search:focus {{
      border-color: rgba(132, 194, 255, 0.8);
      box-shadow: 0 0 0 2px rgba(132, 194, 255, 0.15);
    }}
    .num-filter {{
      border: 1px solid rgba(255,255,255,0.16);
      background: rgba(0,0,0,0.20);
      color: var(--ink);
      border-radius: 8px;
      padding: 5px 8px;
      width: 120px;
      outline: none;
    }}
    .num-filter:focus {{
      border-color: rgba(132, 194, 255, 0.8);
      box-shadow: 0 0 0 2px rgba(132, 194, 255, 0.15);
    }}
    .meta {{
      color: var(--muted);
      font-size: 12px;
    }}
    .hint {{
      color: #8ea3b8;
      font-size: 11px;
      letter-spacing: 0.1px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: auto;
      min-width: 1320px;
    }}
    thead th {{
      background: #111821;
      color: var(--muted);
      font-size: 11px;
      letter-spacing: 0.5px;
      text-transform: uppercase;
      text-align: left;
      border-bottom: 1px solid var(--line);
      padding: 10px 8px;
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    thead th.sortable {{
      cursor: pointer;
      user-select: none;
    }}
    thead th.sortable:hover {{
      color: #d2deea;
      background: #13202b;
    }}
    .sort-ind {{
      display: inline-block;
      min-width: 10px;
      margin-left: 5px;
      color: #84c2ff;
    }}
    tbody td {{
      border-bottom: 1px solid rgba(255,255,255,0.05);
      padding: 8px;
      vertical-align: top;
    }}
    tbody tr:hover {{ background: rgba(255,255,255,0.03); }}
    td.rank {{ width: 50px; text-align: right; color: var(--accent); font-weight: 700; }}
    td.q {{ width: 46%; color: var(--ink); }}
    td.num {{ width: 12%; text-align: right; white-space: nowrap; }}
    .val {{ margin-bottom: 3px; }}
    .bar {{
      width: 100%;
      height: 5px;
      background: rgba(255,255,255,0.08);
      border-radius: 99px;
      overflow: hidden;
    }}
    .bar span {{
      display: block;
      height: 100%;
      background: var(--score);
    }}
    .bar.liq span {{ background: var(--liq); }}
    .bar.vol span {{ background: var(--vol); }}
    .bar.yld span {{ background: var(--accent); }}
    .chip {{
      display: inline-block;
      min-width: 56px;
      padding: 1px 8px;
      border-radius: 999px;
      text-align: center;
      font-size: 11px;
      font-weight: 700;
      border: 1px solid transparent;
      text-transform: lowercase;
      letter-spacing: 0.1px;
    }}
    .chip.side-yes {{ color: #96e6bb; background: rgba(75, 199, 124, 0.15); border-color: rgba(75, 199, 124, 0.45); }}
    .chip.side-no {{ color: #ffd4a5; background: rgba(240, 163, 90, 0.15); border-color: rgba(240, 163, 90, 0.45); }}
    .chip.side-none {{ color: #b7c5d6; background: rgba(255,255,255,0.06); border-color: rgba(255,255,255,0.16); }}
    .chip.src-both {{ color: #8fdfff; background: rgba(85, 181, 255, 0.16); border-color: rgba(85, 181, 255, 0.45); }}
    .chip.src-no {{ color: #c8d8ff; background: rgba(121, 168, 255, 0.14); border-color: rgba(121, 168, 255, 0.42); }}
    .chip.src-late {{ color: #ffe2b8; background: rgba(240, 163, 90, 0.13); border-color: rgba(240, 163, 90, 0.40); }}
    .chip.src-none {{ color: #b7c5d6; background: rgba(255,255,255,0.06); border-color: rgba(255,255,255,0.16); }}
    @media (max-width: 980px) {{
      .cards {{ grid-template-columns: repeat(2, minmax(150px, 1fr)); }}
      td.q {{ width: auto; }}
      .search {{ min-width: 0; width: 100%; }}
      .num-filter {{ width: 100px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>{profile} Consensus Snapshot</h1>
    <p class="sub">generated_utc={generated_utc} | rendered_utc={html.escape(now_tag)}</p>

    <section class="cards">
      <article class="card">
        <div class="k">Rows</div>
        <div class="v">{len(top_rows)}</div>
      </article>
      <article class="card">
        <div class="k">Input Counts</div>
        <div class="v small">no_longshot={no_cnt}<br/>lateprob={late_cnt}<br/>merged={merged_cnt}</div>
      </article>
      <article class="card">
        <div class="k">Source Overlap</div>
        <div class="v small">both={both_cnt}<br/>no_only={no_only_cnt}<br/>late_only={late_only_cnt}<br/>none={neither_cnt}</div>
      </article>
      <article class="card">
        <div class="k">Side Mix</div>
        <div class="v small">yes={side_yes_cnt}<br/>no={side_no_cnt}</div>
      </article>
      <article class="card">
        <div class="k">Yield/day</div>
        <div class="v small">max={_fmt(yld_max, 4)}<br/>median={_fmt(yld_med, 4)}</div>
      </article>
      <article class="card">
        <div class="k">Hours To End</div>
        <div class="v small">min={_fmt(hours_min, 2)}<br/>max={_fmt(hours_max, 2)}</div>
      </article>
      <article class="card">
        <div class="k">Filters</div>
        <div class="v small">min_liquidity={min_liq}<br/>min_volume_24h={min_vol}</div>
      </article>
      <article class="card">
        <div class="k">Scoring</div>
        <div class="v small">mode={score_mode}<br/>{html.escape(weights_txt)}</div>
      </article>
    </section>

    <section class="ab-section">
      <div class="ab-title-row">A/B dryrun (observe-only)</div>
      <div class="ab-grid">
        {ab_cards_html}
      </div>
    </section>

    <section class="controls">
      <div class="ctl-group">
        <span class="ctl-label">Preset</span>
        <button type="button" class="flt-btn preset-btn active" data-preset="all">all</button>
        <button type="button" class="flt-btn preset-btn" data-preset="edge24">edge24</button>
        <button type="button" class="flt-btn preset-btn" data-preset="tight18">tight18</button>
      </div>
      <div class="ctl-group">
        <span class="ctl-label">Side</span>
        <button type="button" class="flt-btn active" data-filter-kind="side" data-filter-value="all">all</button>
        <button type="button" class="flt-btn" data-filter-kind="side" data-filter-value="yes">yes</button>
        <button type="button" class="flt-btn" data-filter-kind="side" data-filter-value="no">no</button>
      </div>
      <div class="ctl-group">
        <span class="ctl-label">Source</span>
        <button type="button" class="flt-btn active" data-filter-kind="source" data-filter-value="all">all</button>
        <button type="button" class="flt-btn" data-filter-kind="source" data-filter-value="both">both</button>
        <button type="button" class="flt-btn" data-filter-kind="source" data-filter-value="no-longshot">no-longshot</button>
        <button type="button" class="flt-btn" data-filter-kind="source" data-filter-value="lateprob">lateprob</button>
        <button type="button" class="flt-btn" data-filter-kind="source" data-filter-value="none">none</button>
      </div>
      <div class="ctl-group">
        <span class="ctl-label">Search</span>
        <input id="q-filter" class="search" type="search" placeholder="question contains..." />
      </div>
      <div class="ctl-group">
        <span class="ctl-label">Min Yield/Day</span>
        <input id="flt-min-yield" class="num-filter" type="number" step="0.001" placeholder="0.050" />
      </div>
      <div class="ctl-group">
        <span class="ctl-label">Max Hours</span>
        <input id="flt-max-hours" class="num-filter" type="number" step="0.1" placeholder="24" />
      </div>
      <div class="ctl-group">
        <span class="ctl-label">Min Score</span>
        <input id="flt-min-score" class="num-filter" type="number" step="0.1" placeholder="50" />
      </div>
      <div class="ctl-group">
        <button type="button" id="flt-reset" class="flt-btn">reset</button>
        <button type="button" id="copy-filter-link" class="flt-btn">copy link</button>
        <button type="button" id="export-visible-csv" class="flt-btn">export visible csv</button>
        <span id="flt-meta" class="meta"></span>
        <span id="flt-stats" class="meta"></span>
        <span class="hint">shortcuts: / search, r reset, e export, c copy</span>
      </div>
    </section>

    <section class="table-wrap">
      <table data-sortable="1">
        <thead>
          <tr>
            <th class="sortable" data-sort-key="rank" data-sort-type="num" data-sort-default="asc" style="width:50px;text-align:right;">Rank<span class="sort-ind"></span></th>
            <th class="sortable" data-sort-key="question" data-sort-type="text" data-sort-default="asc">Question<span class="sort-ind"></span></th>
            <th class="sortable" data-sort-key="score" data-sort-type="num" data-sort-default="desc" style="width:14%;text-align:right;">Score<span class="sort-ind"></span></th>
            <th class="sortable" data-sort-key="yes" data-sort-type="num" data-sort-default="desc" style="width:8%;text-align:right;">YES<span class="sort-ind"></span></th>
            <th class="sortable" data-sort-key="entry" data-sort-type="num" data-sort-default="desc" style="width:8%;text-align:right;">Entry<span class="sort-ind"></span></th>
            <th class="sortable" data-sort-key="maxprofit" data-sort-type="num" data-sort-default="desc" style="width:8%;text-align:right;">MaxProfit<span class="sort-ind"></span></th>
            <th class="sortable" data-sort-key="yield" data-sort-type="num" data-sort-default="desc" style="width:10%;text-align:right;">Yield/Day<span class="sort-ind"></span></th>
            <th class="sortable" data-sort-key="hours" data-sort-type="num" data-sort-default="asc" style="width:8%;text-align:right;">Hours<span class="sort-ind"></span></th>
            <th class="sortable" data-sort-key="side" data-sort-type="text" data-sort-default="asc" style="width:7%;text-align:right;">Side<span class="sort-ind"></span></th>
            <th class="sortable" data-sort-key="source" data-sort-type="text" data-sort-default="asc" style="width:9%;text-align:right;">Source<span class="sort-ind"></span></th>
            <th class="sortable" data-sort-key="liq" data-sort-type="num" data-sort-default="desc" style="width:10%;text-align:right;">Liquidity<span class="sort-ind"></span></th>
            <th class="sortable" data-sort-key="vol" data-sort-type="num" data-sort-default="desc" style="width:10%;text-align:right;">Volume24h<span class="sort-ind"></span></th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows_html)}
        </tbody>
      </table>
    </section>
  </div>
  <script>
    (function () {{
      const table = document.querySelector('table[data-sortable="1"]');
      if (!table) return;

      const tbody = table.querySelector('tbody');
      const headers = Array.from(table.querySelectorAll('th.sortable'));
      const rows = Array.from(tbody.querySelectorAll('tr'));
      const filterButtons = Array.from(document.querySelectorAll('.flt-btn[data-filter-kind]'));
      const presetButtons = Array.from(document.querySelectorAll('.preset-btn[data-preset]'));
      const filterInput = document.getElementById('q-filter');
      const minYieldInput = document.getElementById('flt-min-yield');
      const maxHoursInput = document.getElementById('flt-max-hours');
      const minScoreInput = document.getElementById('flt-min-score');
      const resetBtn = document.getElementById('flt-reset');
      const copyLinkBtn = document.getElementById('copy-filter-link');
      const exportBtn = document.getElementById('export-visible-csv');
      const filterMeta = document.getElementById('flt-meta');
      const filterStats = document.getElementById('flt-stats');
      const profileName = {profile_js};
      const validSide = new Set(['all', 'yes', 'no']);
      const validSource = new Set(['all', 'both', 'no-longshot', 'lateprob', 'none']);
      const validPreset = new Set(['all', 'edge24', 'tight18', 'custom']);
      const validDir = new Set(['asc', 'desc']);
      const defaultSortKey = 'rank';
      const defaultSortDir = 'asc';
      let activeKey = 'rank';
      let activeDir = 'asc';
      let activePreset = 'all';
      let activeSide = 'all';
      let activeSource = 'all';
      let activeQuery = '';
      let activeMinYield = NaN;
      let activeMaxHours = NaN;
      let activeMinScore = NaN;
      let suppressUrlSync = false;

      const parseNum = (v) => {{
        const n = Number(v);
        return Number.isFinite(n) ? n : NaN;
      }};
      const parseThreshold = (v) => {{
        const s = String(v ?? '').trim();
        if (!s) return NaN;
        const n = Number(s);
        return Number.isFinite(n) ? n : NaN;
      }};
      const isTypingTarget = (el) => {{
        if (!el) return false;
        const tag = String(el.tagName || '').toLowerCase();
        return tag === 'input' || tag === 'textarea' || tag === 'select' || !!el.isContentEditable;
      }};
      const sortTypeForKey = (key) => {{
        const th = headers.find((h) => (h.dataset.sortKey || '') === key);
        return th ? (th.dataset.sortType || 'num') : 'num';
      }};

      function syncUrlFromState() {{
        if (suppressUrlSync) return;

        const params = new URLSearchParams();
        if (activePreset !== 'all') params.set('preset', activePreset);
        if (activeSide !== 'all') params.set('side', activeSide);
        if (activeSource !== 'all') params.set('source', activeSource);
        if (activeQuery) params.set('q', activeQuery);
        if (Number.isFinite(activeMinYield)) params.set('min_y', activeMinYield.toString());
        if (Number.isFinite(activeMaxHours)) params.set('max_h', activeMaxHours.toString());
        if (Number.isFinite(activeMinScore)) params.set('min_s', activeMinScore.toString());
        if (activeKey !== defaultSortKey || activeDir !== defaultSortDir) {{
          params.set('sort', activeKey);
          params.set('dir', activeDir);
        }}

        const hash = params.toString();
        const nextUrl = window.location.pathname + window.location.search + (hash ? ('#' + hash) : '');
        history.replaceState(null, '', nextUrl);
      }}

      function applyStateFromUrl() {{
        const raw = String(window.location.hash || '');
        if (!raw || raw === '#') {{
          return {{ key: defaultSortKey, dir: defaultSortDir }};
        }}

        const params = new URLSearchParams(raw.startsWith('#') ? raw.slice(1) : raw);
        const urlPreset = String(params.get('preset') || '').trim().toLowerCase();
        const urlSide = String(params.get('side') || '').trim().toLowerCase();
        const urlSource = String(params.get('source') || '').trim().toLowerCase();
        const urlQueryRaw = String(params.get('q') || '').trim();
        const urlMinYield = parseThreshold(params.get('min_y'));
        const urlMaxHours = parseThreshold(params.get('max_h'));
        const urlMinScore = parseThreshold(params.get('min_s'));
        const urlSort = String(params.get('sort') || '').trim();
        const urlDir = String(params.get('dir') || '').trim().toLowerCase();
        const hasNumeric =
          Number.isFinite(urlMinYield) ||
          Number.isFinite(urlMaxHours) ||
          Number.isFinite(urlMinScore);

        if (validPreset.has(urlPreset)) activePreset = urlPreset;
        if (validSide.has(urlSide)) activeSide = urlSide;
        if (validSource.has(urlSource)) activeSource = urlSource;
        activeQuery = urlQueryRaw.toLowerCase();

        if (activePreset === 'edge24') {{
          activeMinYield = 0.05;
          activeMaxHours = 24.0;
          activeMinScore = 50.0;
        }} else if (activePreset === 'tight18') {{
          activeMinYield = 0.08;
          activeMaxHours = 18.0;
          activeMinScore = 65.0;
        }} else if (activePreset === 'custom' || hasNumeric) {{
          activePreset = 'custom';
          activeMinYield = urlMinYield;
          activeMaxHours = urlMaxHours;
          activeMinScore = urlMinScore;
        }} else {{
          activePreset = 'all';
          activeMinYield = NaN;
          activeMaxHours = NaN;
          activeMinScore = NaN;
        }}

        if (filterInput) filterInput.value = urlQueryRaw;
        setNumericInput(minYieldInput, activeMinYield, 3);
        setNumericInput(maxHoursInput, activeMaxHours, 1);
        setNumericInput(minScoreInput, activeMinScore, 1);

        const hasSortKey = headers.some((h) => (h.dataset.sortKey || '') === urlSort);
        const outKey = hasSortKey ? urlSort : defaultSortKey;
        const outDir = validDir.has(urlDir) ? urlDir : defaultSortDir;
        return {{ key: outKey, dir: outDir }};
      }}

      function updateIndicators() {{
        for (const th of headers) {{
          const span = th.querySelector('.sort-ind');
          if (!span) continue;
          if (th.dataset.sortKey === activeKey) {{
            span.textContent = activeDir === 'asc' ? '▲' : '▼';
          }} else {{
            span.textContent = '';
          }}
        }}
      }}

      function updateFilterButtons() {{
        for (const b of filterButtons) {{
          const kind = b.dataset.filterKind || '';
          const value = b.dataset.filterValue || '';
          const active =
            (kind === 'side' && value === activeSide) ||
            (kind === 'source' && value === activeSource);
          b.classList.toggle('active', active);
        }}
      }}

      function updatePresetButtons() {{
        for (const b of presetButtons) {{
          const value = b.dataset.preset || '';
          b.classList.toggle('active', value === activePreset);
        }}
      }}

      function setNumericInput(el, value, digits) {{
        if (!el) return;
        el.value = Number.isFinite(value) ? value.toFixed(digits) : '';
      }}

      function applyPreset(name) {{
        if (name === 'edge24') {{
          activeMinYield = 0.05;
          activeMaxHours = 24.0;
          activeMinScore = 50.0;
        }} else if (name === 'tight18') {{
          activeMinYield = 0.08;
          activeMaxHours = 18.0;
          activeMinScore = 65.0;
        }} else {{
          activeMinYield = NaN;
          activeMaxHours = NaN;
          activeMinScore = NaN;
          name = 'all';
        }}

        activePreset = name;
        setNumericInput(minYieldInput, activeMinYield, 3);
        setNumericInput(maxHoursInput, activeMaxHours, 1);
        setNumericInput(minScoreInput, activeMinScore, 1);
        updatePresetButtons();
        applyFilters();
      }}

      function rowMatches(row) {{
        const side = String(row.dataset.side || '');
        const source = String(row.dataset.source || '');
        const q = String(row.dataset.question || '').toLowerCase();
        const y = parseNum(row.dataset.yield);
        const h = parseNum(row.dataset.hours);
        const sc = parseNum(row.dataset.score);

        if (activeSide !== 'all' && side !== activeSide) return false;
        if (activeSource !== 'all' && source !== activeSource) return false;
        if (activeQuery && !q.includes(activeQuery)) return false;
        if (Number.isFinite(activeMinYield) && (Number.isNaN(y) || y < activeMinYield)) return false;
        if (Number.isFinite(activeMaxHours) && (Number.isNaN(h) || h > activeMaxHours)) return false;
        if (Number.isFinite(activeMinScore) && (Number.isNaN(sc) || sc < activeMinScore)) return false;
        return true;
      }}

      function applyFilters() {{
        let visible = 0;
        let sumYield = 0.0;
        let cntYield = 0;
        const yieldVals = [];
        let sumScore = 0.0;
        let cntScore = 0;
        const scoreVals = [];
        let minHours = Infinity;
        let maxHours = -Infinity;
        let sideYes = 0;
        let sideNo = 0;
        let srcBoth = 0;
        let srcNo = 0;
        let srcLate = 0;
        let srcNone = 0;
        for (const row of rows) {{
          const ok = rowMatches(row);
          row.style.display = ok ? '' : 'none';
          if (ok) {{
            visible += 1;
            const y = parseNum(row.dataset.yield);
            const sc = parseNum(row.dataset.score);
            const h = parseNum(row.dataset.hours);
            const side = String(row.dataset.side || '');
            const src = String(row.dataset.source || '');
            if (Number.isFinite(y)) {{
              sumYield += y;
              cntYield += 1;
              yieldVals.push(y);
            }}
            if (Number.isFinite(sc)) {{
              sumScore += sc;
              cntScore += 1;
              scoreVals.push(sc);
            }}
            if (Number.isFinite(h)) {{
              minHours = Math.min(minHours, h);
              maxHours = Math.max(maxHours, h);
            }}
            if (side === 'yes') sideYes += 1;
            if (side === 'no') sideNo += 1;
            if (src === 'both') srcBoth += 1;
            if (src === 'no-longshot') srcNo += 1;
            if (src === 'lateprob') srcLate += 1;
            if (src === 'none') srcNone += 1;
          }}
        }}
        if (filterMeta) {{
          const parts = ['visible ' + visible + ' / ' + rows.length];
          if (activePreset !== 'all') parts.push('preset=' + activePreset);
          if (Number.isFinite(activeMinYield)) parts.push('minYield>=' + activeMinYield.toFixed(3));
          if (Number.isFinite(activeMaxHours)) parts.push('maxHours<=' + activeMaxHours.toFixed(1));
          if (Number.isFinite(activeMinScore)) parts.push('minScore>=' + activeMinScore.toFixed(1));
          filterMeta.textContent = parts.join(' | ');
        }}
        if (filterStats) {{
          yieldVals.sort((a, b) => a - b);
          scoreVals.sort((a, b) => a - b);
          const midY = yieldVals.length ? yieldVals[Math.floor(yieldVals.length / 2)] : NaN;
          const midS = scoreVals.length ? scoreVals[Math.floor(scoreVals.length / 2)] : NaN;
          const parts = [];
          if (cntYield > 0) parts.push('avgYield=' + (sumYield / cntYield).toFixed(4));
          if (Number.isFinite(midY)) parts.push('medYield=' + midY.toFixed(4));
          if (cntScore > 0) parts.push('avgScore=' + (sumScore / cntScore).toFixed(1));
          if (Number.isFinite(midS)) parts.push('medScore=' + midS.toFixed(1));
          if (minHours !== Infinity) parts.push('hours=' + minHours.toFixed(1) + '..' + maxHours.toFixed(1));
          parts.push('side yes=' + sideYes + ' no=' + sideNo);
          parts.push('src both=' + srcBoth + ' no=' + srcNo + ' late=' + srcLate + ' none=' + srcNone);
          filterStats.textContent = parts.join(' | ');
        }}
        syncUrlFromState();
      }}

      function compareRows(a, b, key, type, dir) {{
        let out = 0;
        if (type === 'num') {{
          const av = parseNum(a.dataset[key]);
          const bv = parseNum(b.dataset[key]);
          const aNan = Number.isNaN(av);
          const bNan = Number.isNaN(bv);
          if (aNan && bNan) {{
            out = 0;
          }} else if (aNan) {{
            out = 1;
          }} else if (bNan) {{
            out = -1;
          }} else {{
            out = av - bv;
          }}
        }} else {{
          const av = String(a.dataset[key] || '');
          const bv = String(b.dataset[key] || '');
          out = av.localeCompare(bv);
        }}
        return dir === 'asc' ? out : -out;
      }}

      function sortBy(key, type, forcedDir) {{
        if (forcedDir) {{
          activeDir = forcedDir;
        }} else if (activeKey === key) {{
          activeDir = activeDir === 'asc' ? 'desc' : 'asc';
        }} else {{
          const th = headers.find((h) => h.dataset.sortKey === key);
          activeDir = (th && th.dataset.sortDefault) ? th.dataset.sortDefault : 'desc';
        }}
        activeKey = key;

        const rows = Array.from(tbody.querySelectorAll('tr'));
        rows.sort((a, b) => compareRows(a, b, key, type, activeDir));
        for (const row of rows) tbody.appendChild(row);
        updateIndicators();
        applyFilters();
      }}

      function restoreFromUrlState() {{
        suppressUrlSync = true;
        const state = applyStateFromUrl();
        updatePresetButtons();
        updateFilterButtons();
        sortBy(state.key, sortTypeForKey(state.key), state.dir);
        suppressUrlSync = false;
        applyFilters();
      }}

      for (const th of headers) {{
        th.addEventListener('click', () => {{
          const key = th.dataset.sortKey || '';
          const type = th.dataset.sortType || 'num';
          if (!key) return;
          sortBy(key, type);
        }});
      }}

      for (const b of presetButtons) {{
        b.addEventListener('click', () => {{
          const name = b.dataset.preset || 'all';
          applyPreset(name);
        }});
      }}

      for (const b of filterButtons) {{
        b.addEventListener('click', () => {{
          const kind = b.dataset.filterKind || '';
          const value = b.dataset.filterValue || 'all';
          if (kind === 'side') activeSide = value;
          if (kind === 'source') activeSource = value;
          updateFilterButtons();
          applyFilters();
        }});
      }}

      if (filterInput) {{
        filterInput.addEventListener('input', () => {{
          activeQuery = String(filterInput.value || '').trim().toLowerCase();
          applyFilters();
        }});
      }}
      if (minYieldInput) {{
        minYieldInput.addEventListener('input', () => {{
          activeMinYield = parseThreshold(minYieldInput.value);
          activePreset =
            !Number.isFinite(activeMinYield) &&
            !Number.isFinite(activeMaxHours) &&
            !Number.isFinite(activeMinScore)
              ? 'all'
              : 'custom';
          updatePresetButtons();
          applyFilters();
        }});
      }}
      if (maxHoursInput) {{
        maxHoursInput.addEventListener('input', () => {{
          activeMaxHours = parseThreshold(maxHoursInput.value);
          activePreset =
            !Number.isFinite(activeMinYield) &&
            !Number.isFinite(activeMaxHours) &&
            !Number.isFinite(activeMinScore)
              ? 'all'
              : 'custom';
          updatePresetButtons();
          applyFilters();
        }});
      }}
      if (minScoreInput) {{
        minScoreInput.addEventListener('input', () => {{
          activeMinScore = parseThreshold(minScoreInput.value);
          activePreset =
            !Number.isFinite(activeMinYield) &&
            !Number.isFinite(activeMaxHours) &&
            !Number.isFinite(activeMinScore)
              ? 'all'
              : 'custom';
          updatePresetButtons();
          applyFilters();
        }});
      }}

      if (resetBtn) {{
        resetBtn.addEventListener('click', () => {{
          activePreset = 'all';
          activeSide = 'all';
          activeSource = 'all';
          activeQuery = '';
          activeMinYield = NaN;
          activeMaxHours = NaN;
          activeMinScore = NaN;
          if (filterInput) filterInput.value = '';
          if (minYieldInput) minYieldInput.value = '';
          if (maxHoursInput) maxHoursInput.value = '';
          if (minScoreInput) minScoreInput.value = '';
          updatePresetButtons();
          updateFilterButtons();
          applyFilters();
        }});
      }}

      if (copyLinkBtn) {{
        copyLinkBtn.addEventListener('click', async () => {{
          if (!navigator.clipboard || !navigator.clipboard.writeText) return;
          try {{
            await navigator.clipboard.writeText(window.location.href);
          }} catch (_err) {{
            return;
          }}
          if (filterMeta) {{
            filterMeta.textContent = 'link copied';
            window.setTimeout(() => applyFilters(), 900);
          }}
        }});
      }}

      if (exportBtn) {{
        exportBtn.addEventListener('click', () => {{
          const orderedRows = Array.from(tbody.querySelectorAll('tr'));
          const visibleRows = orderedRows.filter((row) => row.style.display !== 'none');
          const headers = [
            'rank',
            'market_id',
            'end_iso',
            'question',
            'score',
            'yes_price',
            'entry_price',
            'max_profit',
            'net_yield_per_day',
            'hours_to_end',
            'side',
            'source',
            'liquidity',
            'volume_24h',
          ];

          const csvEscape = (v) => {{
            const s = String(v ?? '');
            return /[",\\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
          }};

          const lines = [headers.join(',')];
          for (const row of visibleRows) {{
            const vals = [
              row.dataset.rank || '',
              row.dataset.marketid || '',
              row.dataset.endiso || '',
              row.dataset.question || '',
              row.dataset.score || '',
              row.dataset.yes || '',
              row.dataset.entry || '',
              row.dataset.maxprofit || '',
              row.dataset.yield || '',
              row.dataset.hours || '',
              row.dataset.side || '',
              row.dataset.source || '',
              row.dataset.liq || '',
              row.dataset.vol || '',
            ];
            lines.push(vals.map(csvEscape).join(','));
          }}

          const blob = new Blob([lines.join('\\n')], {{ type: 'text/csv;charset=utf-8;' }});
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          const ts = new Date().toISOString().replace(/[:.]/g, '-');
          a.href = url;
          a.download = profileName + '_visible_' + ts + '.csv';
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          URL.revokeObjectURL(url);
        }});
      }}

      window.addEventListener('keydown', (ev) => {{
        if (ev.defaultPrevented || ev.ctrlKey || ev.metaKey || ev.altKey) return;
        const key = String(ev.key || '').toLowerCase();
        const activeEl = document.activeElement;

        if (key === '/' && !isTypingTarget(activeEl)) {{
          if (!filterInput) return;
          ev.preventDefault();
          filterInput.focus();
          filterInput.select();
          return;
        }}

        if (key === 'escape' && activeEl === filterInput && filterInput) {{
          if (!filterInput.value) return;
          ev.preventDefault();
          filterInput.value = '';
          activeQuery = '';
          applyFilters();
          return;
        }}

        if (isTypingTarget(activeEl)) return;
        if (key === 'r' && resetBtn) {{
          ev.preventDefault();
          resetBtn.click();
          return;
        }}
        if (key === 'e' && exportBtn) {{
          ev.preventDefault();
          exportBtn.click();
          return;
        }}
        if (key === 'c' && copyLinkBtn) {{
          ev.preventDefault();
          copyLinkBtn.click();
          return;
        }}
      }});
      window.addEventListener('hashchange', () => restoreFromUrlState());
      restoreFromUrlState();
    }})();
  </script>
</body>
</html>
"""


def main() -> int:
    p = argparse.ArgumentParser(description="Render weather consensus watchlist JSON to HTML snapshot")
    p.add_argument(
        "--consensus-json",
        default="logs/weather_7acct_auto_consensus_watchlist_latest.json",
        help="Input JSON from build_weather_consensus_watchlist.py",
    )
    p.add_argument("--profile-name", default="weather_7acct_auto", help="Output HTML filename prefix")
    p.add_argument("--top-n", type=int, default=30, help="Rows rendered in snapshot")
    p.add_argument("--out-html", default="", help="Output HTML path (simple filename goes under logs/)")
    args = p.parse_args()

    in_path = resolve_path(args.consensus_json, f"{args.profile_name}_consensus_watchlist_latest.json")
    out_path = resolve_path(args.out_html, f"{args.profile_name}_consensus_snapshot_latest.html")

    if not in_path.exists():
        print(f"Consensus JSON not found: {in_path}")
        return 2
    try:
        payload = json.loads(in_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Failed to parse JSON: {exc}")
        return 2
    if not isinstance(payload, dict):
        print("Input JSON root must be object.")
        return 2

    html_doc = render_html(payload=payload, profile_name=str(args.profile_name), top_n=int(args.top_n))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc, encoding="utf-8")
    print(f"[snapshot] wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
