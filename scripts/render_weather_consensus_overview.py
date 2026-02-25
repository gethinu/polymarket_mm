#!/usr/bin/env python3
"""
Render a cross-profile weather consensus overview HTML.

Observe-only utility:
  - reads logs/<profile>_consensus_watchlist_latest.json
  - compares overlap / rank shifts across profiles
  - writes logs/weather_consensus_overview_latest.html by default
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


def now_iso_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def logs_dir() -> Path:
    out = repo_root() / "logs"
    out.mkdir(parents=True, exist_ok=True)
    return out


def resolve_path(raw: str, default_name: str) -> Path:
    if not raw.strip():
        return logs_dir() / default_name
    p = Path(raw)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs_dir() / p.name
    return repo_root() / p


def as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def as_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        out = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return out


def fmt_float(v: Optional[float], digits: int = 2) -> str:
    if v is None:
        return "-"
    return f"{v:.{digits}f}"


def esc(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)


@dataclass
class Row:
    key: str
    rank: int
    market_id: str
    question: str
    score_total: Optional[float]
    yes_price: Optional[float]
    no_price: Optional[float]
    side_hint: str
    liquidity_num: Optional[float]
    volume_24h: Optional[float]
    hours_to_end: Optional[float]


@dataclass
class ProfileSnapshot:
    profile_name: str
    path: Path
    generated_utc: str
    rendered_utc: str
    input_counts: Dict[str, Any]
    rows: List[Row]


def normalize_rows(payload: dict, top_n: int) -> List[Row]:
    raw_rows = payload.get("top")
    if not isinstance(raw_rows, list):
        return []
    out: List[Row] = []
    seen: Dict[str, int] = {}
    for i, r in enumerate(raw_rows):
        if not isinstance(r, dict):
            continue
        market_id = str(r.get("market_id") or r.get("id") or "").strip()
        question = str(r.get("question") or r.get("label") or "").strip()
        key = market_id or question or f"row:{i + 1}"
        if key in seen:
            continue
        seen[key] = 1
        out.append(
            Row(
                key=key,
                rank=as_int(r.get("rank"), i + 1),
                market_id=market_id,
                question=question,
                score_total=as_float(r.get("score_total"), None),
                yes_price=as_float(r.get("yes_price"), None),
                no_price=as_float(r.get("no_price"), None),
                side_hint=str(r.get("side_hint") or r.get("side") or "").strip().lower(),
                liquidity_num=as_float(r.get("liquidity_num"), None),
                volume_24h=as_float(r.get("volume_24h"), None),
                hours_to_end=as_float(r.get("hours_to_end"), None),
            )
        )
    out.sort(key=lambda x: x.rank)
    if top_n > 0:
        out = out[:top_n]
    return out


def load_profile(profile_name: str, top_n: int) -> ProfileSnapshot:
    in_path = logs_dir() / f"{profile_name}_consensus_watchlist_latest.json"
    payload = json.loads(in_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be object")
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    generated = str(payload.get("generated_utc") or "")
    rendered = now_iso_utc()
    input_counts = meta.get("input_counts") if isinstance(meta.get("input_counts"), dict) else {}
    return ProfileSnapshot(
        profile_name=profile_name,
        path=in_path,
        generated_utc=generated,
        rendered_utc=rendered,
        input_counts=input_counts,
        rows=normalize_rows(payload, top_n=top_n),
    )


def row_map(rows: List[Row]) -> Dict[str, Row]:
    return {r.key: r for r in rows}


def compare_profiles(left: ProfileSnapshot, right: ProfileSnapshot, max_shifts: int = 15) -> dict:
    left_map = row_map(left.rows)
    right_map = row_map(right.rows)
    left_keys = set(left_map.keys())
    right_keys = set(right_map.keys())
    overlap = left_keys.intersection(right_keys)

    shifts: List[dict] = []
    for k in overlap:
        lr = left_map[k].rank
        rr = right_map[k].rank
        shifts.append(
            {
                "key": k,
                "question": left_map[k].question or right_map[k].question,
                "left_rank": lr,
                "right_rank": rr,
                "delta": rr - lr,
                "abs_delta": abs(rr - lr),
            }
        )
    shifts.sort(key=lambda x: (x["abs_delta"], x["left_rank"]), reverse=True)

    left_only = [left_map[k] for k in left_keys.difference(right_keys)]
    right_only = [right_map[k] for k in right_keys.difference(left_keys)]
    left_only.sort(key=lambda x: x.rank)
    right_only.sort(key=lambda x: x.rank)

    return {
        "left_profile": left.profile_name,
        "right_profile": right.profile_name,
        "left_count": len(left_keys),
        "right_count": len(right_keys),
        "overlap_count": len(overlap),
        "overlap_ratio_left": (len(overlap) / max(1, len(left_keys))),
        "overlap_ratio_right": (len(overlap) / max(1, len(right_keys))),
        "left_only": left_only[:10],
        "right_only": right_only[:10],
        "rank_shifts": shifts[:max_shifts],
    }


def render_profile_card(snapshot: ProfileSnapshot) -> str:
    top1 = snapshot.rows[0] if snapshot.rows else None
    c_no = snapshot.input_counts.get("no_longshot_rows")
    c_late = snapshot.input_counts.get("lateprob_rows")
    c_merged = snapshot.input_counts.get("merged_rows")
    top1_line = (
        f"#{top1.rank} {esc(top1.question)} (score={fmt_float(top1.score_total, 2)})"
        if top1
        else "-"
    )
    return f"""
    <article class="card">
      <h3>{esc(snapshot.profile_name)}</h3>
      <div class="kv"><span>generated_utc</span><b>{esc(snapshot.generated_utc or "-")}</b></div>
      <div class="kv"><span>rows</span><b>{len(snapshot.rows)}</b></div>
      <div class="kv"><span>input no_longshot / lateprob / merged</span><b>{esc(c_no)} / {esc(c_late)} / {esc(c_merged)}</b></div>
      <div class="kv"><span>source</span><b>{esc(snapshot.path.name)}</b></div>
      <div class="top1">{top1_line}</div>
    </article>
    """


def render_top_table(snapshot: ProfileSnapshot, rows: int = 10) -> str:
    body: List[str] = []
    for r in snapshot.rows[:rows]:
        body.append(
            "<tr>"
            f"<td>{r.rank}</td>"
            f"<td>{esc(r.market_id)}</td>"
            f"<td>{fmt_float(r.score_total, 2)}</td>"
            f"<td>{fmt_float(r.yes_price, 4)}</td>"
            f"<td>{fmt_float(r.no_price, 4)}</td>"
            f"<td>{esc(r.side_hint)}</td>"
            f"<td>{fmt_float(r.hours_to_end, 2)}</td>"
            f"<td class='q'>{esc(r.question)}</td>"
            "</tr>"
        )
    if not body:
        body.append("<tr><td colspan='8'>no rows</td></tr>")
    return f"""
    <section class="panel">
      <h3>{esc(snapshot.profile_name)} Top {rows}</h3>
      <table>
        <thead>
          <tr><th>rank</th><th>market_id</th><th>score</th><th>yes</th><th>no</th><th>side</th><th>h_to_end</th><th>question</th></tr>
        </thead>
        <tbody>
          {''.join(body)}
        </tbody>
      </table>
    </section>
    """


def render_compare_panel(comp: dict) -> str:
    shifts: List[str] = []
    for s in comp.get("rank_shifts", []):
        shifts.append(
            "<tr>"
            f"<td>{esc(s.get('left_rank'))}</td>"
            f"<td>{esc(s.get('right_rank'))}</td>"
            f"<td>{esc(s.get('delta'))}</td>"
            f"<td class='q'>{esc(s.get('question'))}</td>"
            "</tr>"
        )
    if not shifts:
        shifts.append("<tr><td colspan='4'>no overlap rows</td></tr>")

    left_only = comp.get("left_only", [])
    right_only = comp.get("right_only", [])

    left_list = "".join(f"<li>#{r.rank} {esc(r.question)}</li>" for r in left_only) or "<li>-</li>"
    right_list = "".join(f"<li>#{r.rank} {esc(r.question)}</li>" for r in right_only) or "<li>-</li>"

    return f"""
    <section class="panel">
      <h3>Compare: {esc(comp['left_profile'])} vs {esc(comp['right_profile'])}</h3>
      <div class="compare-grid">
        <div class="card compact">
          <div class="kv"><span>left_count</span><b>{comp['left_count']}</b></div>
          <div class="kv"><span>right_count</span><b>{comp['right_count']}</b></div>
          <div class="kv"><span>overlap_count</span><b>{comp['overlap_count']}</b></div>
          <div class="kv"><span>overlap_ratio_left</span><b>{fmt_float(comp['overlap_ratio_left'] * 100.0, 1)}%</b></div>
          <div class="kv"><span>overlap_ratio_right</span><b>{fmt_float(comp['overlap_ratio_right'] * 100.0, 1)}%</b></div>
        </div>
        <div class="card compact">
          <div class="mini-title">{esc(comp['left_profile'])} only (top 10)</div>
          <ul>{left_list}</ul>
        </div>
        <div class="card compact">
          <div class="mini-title">{esc(comp['right_profile'])} only (top 10)</div>
          <ul>{right_list}</ul>
        </div>
      </div>
      <table>
        <thead><tr><th>left_rank</th><th>right_rank</th><th>delta</th><th>question</th></tr></thead>
        <tbody>{''.join(shifts)}</tbody>
      </table>
    </section>
    """


def render_html(snapshots: List[ProfileSnapshot], missing: List[str], top_n: int) -> str:
    generated = now_iso_utc()
    cards = "".join(render_profile_card(s) for s in snapshots)

    compare_panels: List[str] = []
    if len(snapshots) >= 2:
        base = snapshots[0]
        for other in snapshots[1:]:
            compare_panels.append(render_compare_panel(compare_profiles(base, other)))

    top_tables = "".join(render_top_table(s, rows=min(10, max(1, top_n))) for s in snapshots)

    set_list = [set(row_map(s.rows).keys()) for s in snapshots]
    common_all = len(set.intersection(*set_list)) if set_list else 0
    union_all = len(set.union(*set_list)) if set_list else 0

    missing_html = ""
    if missing:
        items = "".join(f"<li>{esc(m)}</li>" for m in missing)
        missing_html = f"""
        <section class="panel warn">
          <h3>Missing Profiles</h3>
          <ul>{items}</ul>
        </section>
        """

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Weather Consensus Overview</title>
  <style>
    :root {{
      --bg: #0f1520;
      --bg2: #101b2b;
      --card: #152131;
      --line: #2a405a;
      --text: #e6eef8;
      --muted: #9bb0c6;
      --accent: #5ad3a3;
      --warn: #f0b35a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      background:
        radial-gradient(1100px 700px at 8% -20%, #1a2a42 0%, transparent 55%),
        radial-gradient(900px 500px at 110% 0%, #1f304b 0%, transparent 45%),
        linear-gradient(180deg, var(--bg2), var(--bg));
      font: 14px/1.45 "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    }}
    .wrap {{ max-width: 1420px; margin: 0 auto; padding: 20px 16px 28px; }}
    h1 {{ margin: 0 0 6px; font-size: 24px; }}
    h2 {{ margin: 0 0 10px; font-size: 18px; }}
    h3 {{ margin: 0 0 10px; font-size: 16px; }}
    .sub {{ color: var(--muted); font-size: 12px; margin-bottom: 14px; }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }}
    .card, .panel {{
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--card);
      padding: 12px;
    }}
    .compact {{ padding: 10px; }}
    .panel {{ margin-top: 12px; }}
    .warn {{ border-color: var(--warn); }}
    .kv {{ display: flex; justify-content: space-between; gap: 8px; margin: 2px 0; }}
    .kv span {{ color: var(--muted); }}
    .top1 {{
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px solid var(--line);
      color: var(--accent);
      font-size: 12px;
    }}
    .mini-title {{ color: var(--muted); margin-bottom: 6px; }}
    .compare-grid {{
      display: grid;
      grid-template-columns: 1.1fr 1fr 1fr;
      gap: 10px;
      margin-bottom: 10px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      border: 1px solid var(--line);
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 6px 7px;
      text-align: left;
      vertical-align: top;
    }}
    thead th {{
      background: rgba(90, 211, 163, 0.10);
      color: var(--accent);
      position: sticky;
      top: 0;
      z-index: 1;
    }}
    .q {{ min-width: 340px; }}
    ul {{ margin: 0; padding-left: 16px; }}
    li {{ margin: 4px 0; font-size: 12px; color: var(--muted); }}
    @media (max-width: 980px) {{
      .compare-grid {{ grid-template-columns: 1fr; }}
      .q {{ min-width: 220px; }}
    }}
  </style>
</head>
<body>
  <main class="wrap">
    <h1>Weather Consensus Overview</h1>
    <div class="sub">
      generated_utc={esc(generated)} | top_n={top_n} | profiles_loaded={len(snapshots)} | common_across_all={common_all} | union={union_all}
    </div>

    {missing_html}

    <section class="panel">
      <h2>Profile Summary</h2>
      <div class="summary">{cards}</div>
    </section>

    {''.join(compare_panels)}

    <section class="panel">
      <h2>Top Rows</h2>
      {top_tables}
    </section>
  </main>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Render cross-profile weather consensus overview HTML")
    p.add_argument(
        "--profile",
        action="append",
        default=[],
        help="Profile name (repeatable). default: weather_7acct_auto + weather_visual_test",
    )
    p.add_argument("--top-n", type=int, default=30, help="Rows per profile to compare")
    p.add_argument(
        "--out-html",
        default="logs/weather_consensus_overview_latest.html",
        help="Output HTML path (simple filename goes under logs/)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    profiles_raw = [str(x).strip() for x in (args.profile or []) if str(x).strip()]
    profiles: List[str] = []
    seen: Dict[str, int] = {}
    for p in profiles_raw:
        if p in seen:
            continue
        seen[p] = 1
        profiles.append(p)
    if not profiles:
        profiles = ["weather_7acct_auto", "weather_visual_test"]

    loaded: List[ProfileSnapshot] = []
    missing: List[str] = []
    for p in profiles:
        path = logs_dir() / f"{p}_consensus_watchlist_latest.json"
        if not path.exists():
            missing.append(f"{p}: missing ({path.name})")
            continue
        try:
            loaded.append(load_profile(p, top_n=max(1, int(args.top_n))))
        except Exception as exc:
            missing.append(f"{p}: parse_error ({exc})")

    if not loaded:
        print("No profile snapshots loaded.")
        for m in missing:
            print(f" - {m}")
        return 2

    out_path = resolve_path(str(args.out_html), "weather_consensus_overview_latest.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(loaded, missing, top_n=max(1, int(args.top_n))), encoding="utf-8")
    print(f"[weather-overview] wrote html: {out_path}")
    if missing:
        print(f"[weather-overview] warnings: {len(missing)} profile(s) missing or invalid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
