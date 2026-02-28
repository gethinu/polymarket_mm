#!/usr/bin/env python3
"""
Check PENDING release conditions for one strategy (observe-only by default).

Default behavior:
- Read strategy status from logs/strategy_register_latest.json
- Evaluate release locks only (execution-edge, full_kelly)
- Print one-line summary + JSON payload

Optional apply mode:
- If release_ready and current status is PENDING, update docs/llm/STRATEGY.md
  status line for the target strategy to ADOPTED.
- Then refresh strategy register snapshot and implementation ledger.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


DEFAULT_STRATEGY_ID = "gamma_eventpair_exec_edge_filter_observe"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def logs_dir() -> Path:
    p = repo_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_path(raw: str, default_name: str) -> Path:
    if not str(raw or "").strip():
        return logs_dir() / default_name
    p = Path(str(raw))
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs_dir() / p.name
    return repo_root() / p


def read_json(path: Path) -> Optional[dict]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def write_json(path: Path, payload: dict, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        if pretty:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        else:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def _as_float(v: object, default: float = math.nan) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except Exception:
        return default


def _as_bool(v: object) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return s in {"1", "true", "yes", "y", "on"}


def _parse_ts_ms(row: dict) -> int:
    ts_ms = int(_as_float(row.get("ts_ms"), 0.0))
    if ts_ms > 0:
        return ts_ms
    ts = str(row.get("ts") or "").strip()
    if not ts:
        return 0
    try:
        return int(dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp() * 1000.0)
    except Exception:
        return 0


def _dedupe_key(row: dict) -> str:
    return (
        str(row.get("event_key") or "").strip()
        or str(row.get("market_id") or "").strip()
        or str(row.get("event_id") or "").strip()
        or str(row.get("title") or "").strip()
    )


def _iter_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = str(line or "").strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def _choose_latest_existing(paths: Iterable[Path]) -> Optional[Path]:
    xs = [p for p in paths if p.exists() and p.is_file()]
    if not xs:
        return None
    xs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return xs[0]


def _extract_entry(snapshot: dict, strategy_id: str) -> Optional[dict]:
    sr = snapshot.get("strategy_register") if isinstance(snapshot.get("strategy_register"), dict) else {}
    entries = sr.get("entries") if isinstance(sr.get("entries"), list) else []
    sid = str(strategy_id or "").strip()
    for row in entries:
        if not isinstance(row, dict):
            continue
        if str(row.get("strategy_id") or "").strip() == sid:
            return row
    return None


def _collect_candidate_files(entry: dict, pattern: str) -> List[Path]:
    refs = entry.get("evidence_refs") if isinstance(entry.get("evidence_refs"), list) else []
    out: List[Path] = []
    rx = re.compile(pattern)
    for ref in refs:
        s = str(ref or "").strip()
        if not s:
            continue
        if not rx.search(s):
            continue
        out.append(resolve_path(s, s))
    return out


def _resolve_monthly_and_replay_paths(
    *,
    entry: dict,
    monthly_override: str,
    replay_override: str,
) -> Tuple[Optional[Path], Optional[Path]]:
    monthly_path = None
    replay_path = None
    if str(monthly_override or "").strip():
        monthly_path = resolve_path(str(monthly_override), "monthly.json")
    if str(replay_override or "").strip():
        replay_path = resolve_path(str(replay_override), "replay.json")

    if monthly_path is None:
        monthly_candidates = _collect_candidate_files(entry, r"clob-arb-eventpair-monthly-estimate.*\.json$")
        monthly_path = _choose_latest_existing(monthly_candidates)
    if replay_path is None:
        replay_candidates = _collect_candidate_files(entry, r"clob-arb-kelly-replay-eventpair.*\.json$")
        replay_path = _choose_latest_existing(replay_candidates)
    return monthly_path, replay_path


def _load_metrics_paths_from_monthly(monthly_payload: dict) -> List[Path]:
    keys = [
        "source_metrics_files",
        "source_metrics_file",
    ]
    rows: List[str] = []
    for k in keys:
        v = monthly_payload.get(k)
        if isinstance(v, list):
            rows.extend([str(x or "").strip() for x in v if str(x or "").strip()])
        elif isinstance(v, str) and v.strip():
            rows.append(v.strip())
    out: List[Path] = []
    for raw in rows:
        out.append(resolve_path(raw, raw))
    dedup = []
    seen = set()
    for p in out:
        s = str(p.resolve()) if p.exists() else str(p)
        if s in seen:
            continue
        seen.add(s)
        dedup.append(p)
    return dedup


def _compute_execution_edge(
    *,
    metrics_files: List[Path],
    min_gap_ms_per_event: int,
    max_worst_stale_sec: float,
    conservative_cost_cents: float,
) -> dict:
    rows_read = 0
    sample_count = 0
    edge_sum = 0.0
    edge_pos_count = 0
    edge_adj_sum = 0.0
    edge_adj_pos_count = 0
    last_ts_by_key: Dict[str, int] = {}
    cost_penalty_usd = max(0.0, float(conservative_cost_cents)) / 100.0

    files_existing = [p for p in metrics_files if p.exists() and p.is_file()]
    for path in files_existing:
        for row in _iter_jsonl(path):
            rows_read += 1
            if not _as_bool(row.get("passes_raw_threshold")):
                continue

            ts_ms = _parse_ts_ms(row)
            if min_gap_ms_per_event > 0 and ts_ms > 0:
                key = _dedupe_key(row)
                if key:
                    last_ts = int(last_ts_by_key.get(key, 0) or 0)
                    if last_ts > 0 and (ts_ms - last_ts) < int(min_gap_ms_per_event):
                        continue
                    last_ts_by_key[key] = ts_ms

            stale = _as_float(row.get("worst_book_stale_sec"), math.nan)
            if math.isfinite(stale) and max_worst_stale_sec > 0 and stale > max_worst_stale_sec:
                continue

            edge = _as_float(row.get("net_edge_exec_est"), math.nan)
            if not math.isfinite(edge):
                edge = _as_float(row.get("net_edge_raw"), math.nan)
            if not math.isfinite(edge):
                continue

            sample_count += 1
            edge_sum += float(edge)
            if edge > 0:
                edge_pos_count += 1

            edge_adj = float(edge) - cost_penalty_usd
            edge_adj_sum += edge_adj
            if edge_adj > 0:
                edge_adj_pos_count += 1

    mean_edge = (edge_sum / sample_count) if sample_count > 0 else None
    mean_edge_adj = (edge_adj_sum / sample_count) if sample_count > 0 else None
    pos_ratio = (float(edge_pos_count) / float(sample_count)) if sample_count > 0 else None
    pos_ratio_adj = (float(edge_adj_pos_count) / float(sample_count)) if sample_count > 0 else None
    return {
        "metrics_files": [str(p) for p in files_existing],
        "rows_read": int(rows_read),
        "sample_count": int(sample_count),
        "mean_execution_edge_usd": mean_edge,
        "positive_ratio": pos_ratio,
        "mean_execution_edge_usd_conservative": mean_edge_adj,
        "positive_ratio_conservative": pos_ratio_adj,
        "conservative_cost_cents": float(conservative_cost_cents),
    }


def _update_strategy_status_line(
    *,
    strategy_md: Path,
    strategy_id: str,
    new_status_line: str,
) -> bool:
    if not strategy_md.exists():
        return False
    lines = strategy_md.read_text(encoding="utf-8").splitlines()
    header_idx = None
    pat = re.compile(r"^\s*\d+\.\s+`([^`]+)`\s*$")
    for i, line in enumerate(lines):
        m = pat.match(line.strip())
        if not m:
            continue
        if str(m.group(1) or "").strip() == str(strategy_id).strip():
            header_idx = i
            break
    if header_idx is None:
        return False

    j = header_idx + 1
    while j < len(lines):
        s = lines[j].strip()
        if s.startswith("## "):
            break
        if pat.match(s):
            break
        if s.startswith("- Status:"):
            prefix = lines[j][: len(lines[j]) - len(lines[j].lstrip())]
            lines[j] = f"{prefix}{new_status_line}"
            strategy_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True
        j += 1
    return False


def _run_refresh_commands() -> List[dict]:
    cmds = [
        [sys.executable, "scripts/render_strategy_register_snapshot.py", "--pretty"],
        [sys.executable, "scripts/render_implementation_ledger.py"],
    ]
    rows = []
    for cmd in cmds:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root()),
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        rows.append(
            {
                "command": " ".join(cmd),
                "returncode": int(proc.returncode),
                "stdout": str(proc.stdout or "").strip(),
                "stderr": str(proc.stderr or "").strip(),
                "ok": proc.returncode == 0,
            }
        )
        if proc.returncode != 0:
            break
    return rows


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Check PENDING release conditions for one strategy (observe-only by default).")
    p.add_argument("--strategy", default=DEFAULT_STRATEGY_ID, help="Strategy id to evaluate.")
    p.add_argument("--snapshot-json", default="logs/strategy_register_latest.json", help="Strategy register snapshot JSON.")
    p.add_argument("--strategy-md", default="docs/llm/STRATEGY.md", help="Canonical strategy markdown (used by --apply).")
    p.add_argument("--monthly-json", default="", help="Override monthly-estimate JSON path.")
    p.add_argument("--replay-json", default="", help="Override Kelly replay JSON path.")
    p.add_argument("--min-gap-ms-per-event", type=int, default=5000, help="Per-event dedupe window for execution-edge aggregation.")
    p.add_argument("--max-worst-stale-sec", type=float, default=10.0, help="Skip metrics rows above this stale-book cap (0=disabled).")
    p.add_argument("--conservative-costs", action="store_true", help="Require conservative cost-adjusted execution-edge > 0.")
    p.add_argument(
        "--conservative-cost-cents",
        type=float,
        default=2.0,
        help="Cost buffer (cents) subtracted from net execution edge when --conservative-costs is enabled.",
    )
    p.add_argument("--apply", action="store_true", help="If release-ready, update strategy status to ADOPTED and refresh artifacts.")
    p.add_argument("--out-json", default="", help="Optional output JSON path (simple filename goes under logs/).")
    p.add_argument("--pretty", action="store_true")
    return p.parse_args()


def run_check(args: argparse.Namespace) -> Tuple[int, dict]:
    snapshot_path = resolve_path(str(args.snapshot_json), "strategy_register_latest.json")
    strategy_md_path = resolve_path(str(args.strategy_md), "STRATEGY.md")
    strategy_id = str(args.strategy or "").strip()
    if not strategy_id:
        return 20, {
            "ok": False,
            "error": "invalid_strategy_id",
            "reason_codes": ["invalid_strategy_id"],
        }

    snapshot = read_json(snapshot_path)
    if snapshot is None:
        return 21, {
            "ok": False,
            "strategy": strategy_id,
            "snapshot_json": str(snapshot_path),
            "error": "snapshot_missing_or_invalid",
            "reason_codes": ["snapshot_missing_or_invalid"],
        }

    entry = _extract_entry(snapshot, strategy_id)
    if entry is None:
        return 22, {
            "ok": False,
            "strategy": strategy_id,
            "snapshot_json": str(snapshot_path),
            "error": "strategy_not_found",
            "reason_codes": ["strategy_not_found"],
        }

    status = str(entry.get("status") or "UNKNOWN").strip().upper()
    monthly_path, replay_path = _resolve_monthly_and_replay_paths(
        entry=entry,
        monthly_override=str(args.monthly_json or ""),
        replay_override=str(args.replay_json or ""),
    )
    reason_codes: List[str] = []
    release_check = "HOLD"
    release_ready = False

    full_kelly = None
    mean_edge = None
    mean_edge_cons = None
    check_execution_edge_positive = False
    check_full_kelly_positive = False
    check_execution_edge_positive_cons = False
    edge_stats = {
        "metrics_files": [],
        "rows_read": 0,
        "sample_count": 0,
        "mean_execution_edge_usd": None,
        "positive_ratio": None,
        "mean_execution_edge_usd_conservative": None,
        "positive_ratio_conservative": None,
        "conservative_cost_cents": float(args.conservative_cost_cents or 0.0),
    }

    if status != "PENDING":
        release_check = "NOOP"
        reason_codes.append("status_not_pending")
    else:
        monthly_payload = read_json(monthly_path) if monthly_path is not None else None
        replay_payload = read_json(replay_path) if replay_path is not None else None
        if monthly_payload is None:
            reason_codes.append("missing_metric")
        if replay_payload is None:
            reason_codes.append("missing_metric")

        if isinstance(replay_payload, dict):
            kelly = replay_payload.get("kelly") if isinstance(replay_payload.get("kelly"), dict) else {}
            full_kelly = _as_float(kelly.get("full_fraction_estimate"), math.nan)
            if not math.isfinite(full_kelly):
                full_kelly = None

        metrics_files = _load_metrics_paths_from_monthly(monthly_payload or {})
        edge_stats = _compute_execution_edge(
            metrics_files=metrics_files,
            min_gap_ms_per_event=max(0, int(args.min_gap_ms_per_event or 0)),
            max_worst_stale_sec=float(args.max_worst_stale_sec or 0.0),
            conservative_cost_cents=float(args.conservative_cost_cents or 0.0),
        )
        mean_edge = edge_stats.get("mean_execution_edge_usd")
        mean_edge_cons = edge_stats.get("mean_execution_edge_usd_conservative")

        check_execution_edge_positive = bool(mean_edge is not None and float(mean_edge) > 0)
        check_full_kelly_positive = bool(full_kelly is not None and float(full_kelly) > 0)
        check_execution_edge_positive_cons = bool(
            mean_edge_cons is not None and float(mean_edge_cons) > 0
        )

        if mean_edge is None:
            reason_codes.append("missing_metric")
        elif not check_execution_edge_positive:
            reason_codes.append("execution_edge_non_positive")
        if full_kelly is None:
            reason_codes.append("missing_metric")
        elif not check_full_kelly_positive:
            reason_codes.append("full_kelly_non_positive")
        if bool(args.conservative_costs) and not check_execution_edge_positive_cons:
            reason_codes.append("conservative_execution_edge_non_positive")

        if (
            check_execution_edge_positive
            and check_full_kelly_positive
            and ((not bool(args.conservative_costs)) or check_execution_edge_positive_cons)
        ):
            release_check = "RELEASE_READY"
            release_ready = True
        else:
            release_check = "HOLD"

    out: dict = {
        "ok": True,
        "generated_utc": now_utc().isoformat(),
        "strategy": strategy_id,
        "current_status": status,
        "release_check": release_check,
        "release_ready": bool(release_ready),
        "execution_edge": mean_edge,
        "full_kelly": full_kelly,
        "checks": {
            "execution_edge_positive": bool(check_execution_edge_positive),
            "full_kelly_positive": bool(check_full_kelly_positive),
            "conservative_execution_edge_positive": bool(check_execution_edge_positive_cons),
            "conservative_costs_enabled": bool(args.conservative_costs),
        },
        "reason_codes": sorted(set(reason_codes)),
        "sources": {
            "snapshot_json": str(snapshot_path),
            "strategy_md": str(strategy_md_path),
            "monthly_json": str(monthly_path) if monthly_path is not None else "",
            "replay_json": str(replay_path) if replay_path is not None else "",
            "metrics_files": edge_stats.get("metrics_files", []),
        },
        "metrics_summary": edge_stats,
        "apply": {
            "requested": bool(args.apply),
            "applied": False,
            "status_line_updated": False,
            "refresh_commands": [],
            "error": "",
        },
    }

    exit_code = 0 if release_check in {"RELEASE_READY", "NOOP"} else 10

    if bool(args.apply):
        if release_check != "RELEASE_READY":
            out["apply"]["error"] = "apply_skipped_not_release_ready"
        elif status != "PENDING":
            out["apply"]["error"] = "apply_skipped_status_not_pending"
        else:
            today = dt.datetime.now().date().isoformat()
            new_status_line = f"- Status: `ADOPTED` (auto-promoted by check_pending_release.py on {today}, observe-only)."
            updated = _update_strategy_status_line(
                strategy_md=strategy_md_path,
                strategy_id=strategy_id,
                new_status_line=new_status_line,
            )
            out["apply"]["status_line_updated"] = bool(updated)
            if not updated:
                out["apply"]["error"] = "strategy_status_line_not_updated"
                exit_code = 23
            else:
                cmd_rows = _run_refresh_commands()
                out["apply"]["refresh_commands"] = cmd_rows
                all_ok = all(bool(r.get("ok")) for r in cmd_rows)
                out["apply"]["applied"] = bool(all_ok)
                if not all_ok:
                    out["apply"]["error"] = "refresh_failed"
                    exit_code = 24
                else:
                    # reflect status change in current payload for operator readability
                    out["current_status"] = "ADOPTED"

    return exit_code, out


def _fmt_num(v: object) -> str:
    n = _as_float(v, math.nan)
    if not math.isfinite(n):
        return "n/a"
    return f"{n:.6f}"


def main() -> int:
    args = parse_args()
    code, payload = run_check(args)

    summary_reason = ",".join(payload.get("reason_codes") or []) if isinstance(payload.get("reason_codes"), list) else ""
    summary = (
        f"{str(payload.get('strategy') or '')} | "
        f"status={str(payload.get('current_status') or '')} | "
        f"release_check={str(payload.get('release_check') or '')} | "
        f"execution_edge={_fmt_num(payload.get('execution_edge'))} | "
        f"full_kelly={_fmt_num(payload.get('full_kelly'))} | "
        f"reason={summary_reason or '-'}"
    )
    print(summary)
    if bool(args.pretty):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    out_json = str(args.out_json or "").strip()
    if out_json:
        out_path = resolve_path(out_json, "pending_release_check_latest.json")
        write_json(out_path, payload, pretty=bool(args.pretty))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
