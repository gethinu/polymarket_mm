#!/usr/bin/env python3
"""
Render one strategy register snapshot (JSON + HTML).

Observe-only helper:
  - reads canonical strategy register markdown (docs/llm/STRATEGY.md)
  - reads latest weather readiness decision snapshots (logs/*_top30_readiness_*latest.json)
  - reads clob runtime state and optional live process hints
  - writes one consolidated strategy snapshot under logs/
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import html
import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


DEFAULT_REALIZED_STRATEGY_ID = "weather_clob_arb_buckets_observe"
LEGACY_READINESS_PROFILE_ALIASES = {
    # Consolidated into weather_7acct_auto; keep older *_latest.json compatible.
    "weather_visual_test": "weather_7acct_auto",
}


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def logs_dir() -> Path:
    p = repo_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_path(raw: str, default_name: str) -> Path:
    if not raw.strip():
        return logs_dir() / default_name
    p = Path(raw)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs_dir() / p.name
    return repo_root() / p


def _as_float(v) -> Optional[float]:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n):
        return None
    return n


def _as_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_iso(ts: str) -> Optional[dt.datetime]:
    s = str(ts or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _read_json(path: Path) -> Optional[dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _extract_backticks(text: str) -> List[str]:
    return [m.group(1).strip() for m in re.finditer(r"`([^`]+)`", text or "") if m.group(1).strip()]


def _is_command_or_artifact(ref: str) -> bool:
    s = str(ref or "").strip().lower()
    if not s:
        return False
    if s.startswith("python ") or s.startswith("powershell "):
        return True
    if s.startswith("logs/") or s.startswith("scripts/") or s.startswith("docs/"):
        return True
    if "\\" in s:
        return True
    if s.endswith((".json", ".jsonl", ".csv", ".txt", ".md", ".html", ".ps1", ".py", ".log")):
        return True
    return False


def _extract_multiline_field(lines: List[str], label: str) -> str:
    marker = f"- {label}:"
    for i, raw in enumerate(lines):
        if not raw.startswith(marker):
            continue
        collected: List[str] = []
        head = raw[len(marker) :].strip()
        if head:
            collected.append(head)
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if nxt.startswith("- "):
                break
            t = nxt.strip()
            if not t:
                j += 1
                continue
            if t.startswith("- "):
                t = t[2:].strip()
            collected.append(t)
            j += 1
        return " ; ".join([x for x in collected if x]).strip()
    return ""


def parse_strategy_register(md_path: Path) -> dict:
    out: dict = {
        "source_path": str(md_path),
        "exists": md_path.exists(),
        "entries": [],
        "counts": {"ADOPTED": 0, "REJECTED": 0, "PENDING": 0, "UNKNOWN": 0},
    }
    if not md_path.exists():
        return out

    lines = md_path.read_text(encoding="utf-8").splitlines()
    section = ""
    raw_entries: List[dict] = []
    cur: Optional[dict] = None

    for line in lines:
        if line.startswith("## "):
            section = line[3:].strip()
            continue

        m = re.match(r"^\s*\d+\.\s+`([^`]+)`\s*$", line)
        if m:
            if cur is not None:
                raw_entries.append(cur)
            cur = {"strategy_id": m.group(1).strip(), "section": section, "lines": []}
            continue

        if cur is not None:
            cur["lines"].append(line.rstrip())

    if cur is not None:
        raw_entries.append(cur)

    parsed: List[dict] = []
    counts = {"ADOPTED": 0, "REJECTED": 0, "PENDING": 0, "UNKNOWN": 0}

    for row in raw_entries:
        strategy_id = str(row.get("strategy_id") or "").strip()
        section_name = str(row.get("section") or "").strip()
        body_lines: List[str] = [str(x) for x in (row.get("lines") or [])]

        status = "UNKNOWN"
        status_note = ""
        scope = ""
        decision_note = ""
        operational_gate = ""
        runtime_cmds: List[str] = []
        evidence_refs: List[str] = []

        in_runtime = False
        for raw_line in body_lines:
            s = raw_line.strip()
            if not s:
                continue

            if in_runtime:
                if raw_line.startswith("  - "):
                    refs = _extract_backticks(s)
                    if refs:
                        runtime_cmds.extend([x for x in refs if _is_command_or_artifact(x)])
                    continue
                if raw_line.startswith("- "):
                    in_runtime = False

            m_status = re.match(r"^-\s*Status:\s*`?([A-Za-z_]+)`?\s*(.*)$", s)
            if m_status:
                status = str(m_status.group(1) or "UNKNOWN").strip().upper()
                status_note = str(m_status.group(2) or "").strip()
                in_runtime = False
                continue

            if s.startswith("- Scope:"):
                scope = s[len("- Scope:") :].strip()
                in_runtime = False
                continue

            if s.startswith("- Decision note:"):
                decision_note = s[len("- Decision note:") :].strip()
                in_runtime = False
                continue

            if s.startswith("- Operational gate:"):
                operational_gate = s[len("- Operational gate:") :].strip()
                in_runtime = False
                continue

            if s.startswith("- Runtime:"):
                in_runtime = True
                continue

            if s.startswith("- Evidence snapshot"):
                refs = _extract_backticks(s)
                evidence_refs.extend([x for x in refs if _is_command_or_artifact(x)])
                in_runtime = False
                continue

            # collect file references from nested bullets below evidence
            if raw_line.startswith("  - "):
                refs = _extract_backticks(s)
                for ref in refs:
                    if _is_command_or_artifact(ref):
                        evidence_refs.append(ref)

        if not decision_note:
            decision_note = _extract_multiline_field(body_lines, "Decision note")
        if not operational_gate:
            operational_gate = _extract_multiline_field(body_lines, "Operational gate")

        status = status if status in counts else "UNKNOWN"
        counts[status] += 1

        parsed.append(
            {
                "strategy_id": strategy_id,
                "section": section_name,
                "status": status,
                "status_note": status_note,
                "scope": scope,
                "decision_note": decision_note,
                "operational_gate": operational_gate,
                "runtime_commands": runtime_cmds,
                "evidence_refs": sorted(set(evidence_refs)),
            }
        )

    out["entries"] = parsed
    out["counts"] = counts
    return out


def _extract_section_lines(md_path: Path, section_title: str) -> List[str]:
    if not md_path.exists():
        return []
    lines = md_path.read_text(encoding="utf-8").splitlines()
    want = str(section_title or "").strip().lower()
    capture = False
    out: List[str] = []
    for line in lines:
        if line.startswith("## "):
            title = line[3:].strip().lower()
            if capture and title != want:
                break
            capture = title == want
            continue
        if capture:
            out.append(line.rstrip())
    return out


def _extract_first_money(text: str) -> Optional[float]:
    s = str(text or "")
    m_dollar = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", s)
    if m_dollar:
        return _as_float(m_dollar.group(1))
    m_num = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
    if m_num:
        return _as_float(m_num.group(1))
    return None


def _extract_first_ratio_percent(text: str) -> Optional[float]:
    s = str(text or "")
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", s)
    if not m:
        return None
    v = _as_float(m.group(1))
    if v is None:
        return None
    return float(v) / 100.0


def parse_bankroll_policy(md_path: Path, strategy_entries: Optional[List[dict]] = None) -> dict:
    out: dict = {
        "source_path": str(md_path),
        "exists": md_path.exists(),
        "section_found": False,
        "initial_bankroll_usd": None,
        "allocation_mode": "unspecified",
        "allocation_policy": "",
        "live_max_daily_risk_ratio": None,
        "live_max_daily_risk_usd": None,
        "live_max_daily_risk_policy": "",
        "assumed_bankroll_policy_note": "",
        "adopted_strategy_count": 0,
        "adopted_strategy_ids": [],
        "default_adopted_allocations": [],
        "bullets": [],
    }
    if not md_path.exists():
        return out

    section_lines = _extract_section_lines(md_path, "Bankroll Policy")
    if not section_lines:
        return out
    out["section_found"] = True

    for raw in section_lines:
        s = str(raw or "").strip()
        if not s.startswith("- "):
            continue
        bullet = s[2:].strip()
        out["bullets"].append(bullet)
        lower = bullet.lower()

        if "initial bankroll" in lower and out["initial_bankroll_usd"] is None:
            out["initial_bankroll_usd"] = _extract_first_money(bullet)

        if "allocation" in lower and "ratio" in lower:
            out["allocation_policy"] = bullet
            if "equal-weight" in lower or "equal weight" in lower:
                out["allocation_mode"] = "equal_weight_adopted"

        if ("max risk" in lower or "daily risk" in lower) and not out["live_max_daily_risk_policy"]:
            out["live_max_daily_risk_policy"] = bullet
            out["live_max_daily_risk_ratio"] = _extract_first_ratio_percent(bullet)

        if "--assumed-bankroll-usd" in lower or "-assumedbankrollusd" in lower:
            out["assumed_bankroll_policy_note"] = bullet

    init_bankroll = _as_float(out.get("initial_bankroll_usd"))
    risk_ratio = _as_float(out.get("live_max_daily_risk_ratio"))
    if init_bankroll is not None and init_bankroll > 0 and risk_ratio is not None and risk_ratio >= 0:
        out["live_max_daily_risk_usd"] = float(init_bankroll * risk_ratio)

    entries = strategy_entries if isinstance(strategy_entries, list) else []
    adopted_ids = []
    for row in entries:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").upper() != "ADOPTED":
            continue
        sid = str(row.get("strategy_id") or "").strip()
        if sid:
            adopted_ids.append(sid)
    out["adopted_strategy_ids"] = adopted_ids
    out["adopted_strategy_count"] = len(adopted_ids)

    if out.get("allocation_mode") == "equal_weight_adopted" and adopted_ids:
        ratio = 1.0 / float(len(adopted_ids))
        alloc_rows: List[dict] = []
        for sid in adopted_ids:
            row = {
                "strategy_id": sid,
                "allocation_ratio": float(ratio),
                "allocation_pct": float(ratio * 100.0),
                "allocation_usd": float(init_bankroll * ratio) if init_bankroll is not None and init_bankroll > 0 else None,
            }
            alloc_rows.append(row)
        out["default_adopted_allocations"] = alloc_rows

    return out


def resolve_glob_inputs(pattern: str) -> List[Path]:
    pat = (pattern or "").strip() or "logs/*_top30_readiness_*latest.json"
    p = Path(pat)
    if p.is_absolute():
        if any(ch in pat for ch in ("*", "?", "[")):
            return sorted(Path(x) for x in glob.glob(pat))
        return [p] if p.exists() else []
    root = repo_root()
    if any(ch in pat for ch in ("*", "?", "[")):
        return sorted(Path(x) for x in glob.glob(str(root / pat)))
    q = root / p
    return [q] if q.exists() else []


def infer_readiness_mode(payload: dict, path: Path) -> str:
    thresholds = payload.get("thresholds") if isinstance(payload.get("thresholds"), dict) else {}
    req_exec = thresholds.get("require_execution_plan")
    if isinstance(req_exec, bool):
        return "strict" if req_exec else "quality"
    name = path.name.lower()
    if "strict" in name:
        return "strict"
    if "quality" in name:
        return "quality"
    return "unknown"


def readiness_failed_hard_gates(payload: dict) -> List[str]:
    gates = payload.get("gates") if isinstance(payload.get("gates"), list) else []
    failed: List[str] = []
    for g in gates:
        if not isinstance(g, dict):
            continue
        if bool(g.get("hard")) and not bool(g.get("passed")):
            failed.append(str(g.get("name") or "unknown"))
    return failed


def load_readiness_record(path: Path) -> Optional[dict]:
    payload = _read_json(path)
    if payload is None:
        return None
    if not isinstance(payload.get("gates"), list) or "decision" not in payload:
        return None

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    profile = str(meta.get("profile_name") or "").strip()
    if not profile:
        key = "_top30_readiness_"
        stem = path.stem
        profile = stem.split(key, 1)[0] if key in stem else stem
    profile = LEGACY_READINESS_PROFILE_ALIASES.get(profile, profile)

    decision = str(payload.get("decision") or "UNKNOWN").strip().upper()
    mode = infer_readiness_mode(payload, path)
    gen = _parse_iso(str(payload.get("generated_utc") or ""))
    if gen is None:
        gen = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)

    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    return {
        "profile_name": profile,
        "mode": mode,
        "decision": decision,
        "generated_utc": gen.isoformat(),
        "generated_dt": gen,
        "failed_hard_gates": readiness_failed_hard_gates(payload),
        "row_count": metrics.get("row_count"),
        "both_ratio": metrics.get("both_ratio"),
        "median_net_yield_per_day": metrics.get("median_net_yield_per_day"),
        "top10_avg_max_profit": metrics.get("top10_avg_max_profit"),
        "path": str(path),
    }


def latest_readiness(records: List[dict]) -> List[dict]:
    best: Dict[Tuple[str, str], dict] = {}
    for r in records:
        key = (str(r.get("profile_name") or ""), str(r.get("mode") or ""))
        prev = best.get(key)
        if prev is None or r["generated_dt"] > prev["generated_dt"]:
            best[key] = r
    out = list(best.values())
    out.sort(key=lambda x: (str(x.get("profile_name") or ""), str(x.get("mode") or "")))
    for r in out:
        r.pop("generated_dt", None)
    return out


def summarize_readiness(records: List[dict]) -> dict:
    def mode_summary(mode: str) -> dict:
        xs = [r for r in records if str(r.get("mode")) == mode]
        go = [r for r in xs if str(r.get("decision")) == "GO"]
        no_go = [r for r in xs if str(r.get("decision")) == "NO_GO"]
        return {
            "mode": mode,
            "count": len(xs),
            "go_count": len(go),
            "no_go_count": len(no_go),
            "go_profiles": sorted({str(r.get("profile_name") or "") for r in go if str(r.get("profile_name") or "")}),
            "no_go_profiles": sorted(
                {str(r.get("profile_name") or "") for r in no_go if str(r.get("profile_name") or "")}
            ),
        }

    return {"strict": mode_summary("strict"), "quality": mode_summary("quality"), "unknown": mode_summary("unknown")}


def load_clob_state(path: Path) -> dict:
    payload = _read_json(path)
    out: dict = {"path": str(path), "exists": path.exists(), "ok": payload is not None}
    if payload is None:
        return out
    for key in (
        "day",
        "executions_today",
        "notional_today",
        "consecutive_failures",
        "halted",
        "halt_reason",
        "start_pnl_total",
        "last_pnl_total",
        "last_pnl_check_ts",
    ):
        if key in payload:
            out[key] = payload.get(key)
    return out


def _parse_pct_text_to_ratio(text: str) -> Optional[float]:
    s = str(text or "").strip()
    if not s:
        return None
    if s.lower() in {"n/a", "na", "none", "null", "-"}:
        return None
    if s.endswith("%"):
        n = _as_float(s[:-1])
        return (n / 100.0) if n is not None else None
    return _as_float(s)


def _fmt_ratio_pct(ratio: Optional[float], digits: int = 2) -> str:
    if ratio is None:
        return "n/a"
    return f"{ratio:+.{max(0, int(digits))}%}"


def _gate_stage_label_ja(stage: str, min_days: int) -> str:
    s = str(stage or "").strip().upper()
    d = max(1, int(min_days))
    if s == "TENTATIVE":
        return f"{d}日暫定"
    if s == "INTERIM":
        return f"{d}日中間"
    if s == "FINAL":
        return f"{d}日確定"
    return "-"


def _gate_decision_label_ja(decision: str, tentative_days: int, interim_days: int, final_days: int) -> str:
    s = str(decision or "").strip().upper()
    if s == "PENDING_TENTATIVE":
        return f"{tentative_days}日暫定判定待ち"
    if s == "READY_TENTATIVE":
        return f"{tentative_days}日暫定判定"
    if s == "READY_INTERIM":
        return f"{interim_days}日中間判定"
    if s == "READY_FINAL":
        return f"{final_days}日確定判定"
    return "-"


def load_no_longshot_status(summary_path: Path, realized_latest_path: Path, monthly_txt_path: Path) -> dict:
    out: dict = {
        "summary_path": str(summary_path),
        "summary_exists": summary_path.exists(),
        "realized_latest_path": str(realized_latest_path),
        "realized_latest_exists": realized_latest_path.exists(),
        "monthly_txt_path": str(monthly_txt_path),
        "monthly_txt_exists": monthly_txt_path.exists(),
        "monthly_return_now_text": "n/a",
        "monthly_return_now_ratio": None,
        "monthly_return_now_source": "",
        "monthly_return_now_all_text": "n/a",
        "monthly_return_now_all_ratio": None,
        "monthly_return_now_all_source": "",
        "monthly_return_now_new_condition_text": "n/a",
        "monthly_return_now_new_condition_ratio": None,
        "monthly_return_now_new_condition_source": "",
        "rolling_30d_monthly_return_text": "n/a",
        "rolling_30d_monthly_return_ratio": None,
        "rolling_30d_monthly_return_source": "",
        "rolling_30d_monthly_return_all_text": "n/a",
        "rolling_30d_monthly_return_all_ratio": None,
        "rolling_30d_monthly_return_all_source": "",
        "rolling_30d_monthly_return_new_condition_text": "n/a",
        "rolling_30d_monthly_return_new_condition_ratio": None,
        "rolling_30d_monthly_return_new_condition_source": "",
        "rolling_30d_resolved_trades": None,
        "rolling_30d_resolved_trades_all": None,
        "rolling_30d_resolved_trades_new_condition": None,
        "open_positions": None,
        "open_positions_all": None,
        "open_positions_new_condition": None,
        "resolved_positions": None,
        "resolved_positions_all": None,
        "resolved_positions_new_condition": None,
        "realized_latest_rolling_30d_return_text": "n/a",
        "realized_latest_rolling_30d_return_ratio": None,
    }

    if summary_path.exists():
        try:
            text = summary_path.read_text(encoding="utf-8")
            line_map: Dict[str, str] = {}
            for raw in text.splitlines():
                m = re.match(r"^\s*-\s*([A-Za-z0-9_]+)\s*:\s*(.+?)\s*$", raw)
                if m:
                    line_map[str(m.group(1)).strip()] = str(m.group(2)).strip()
            monthly_now_text = str(line_map.get("monthly_return_now") or "n/a").strip()
            monthly_now_ratio = _parse_pct_text_to_ratio(monthly_now_text)
            monthly_now_src = str(line_map.get("monthly_return_now_source") or "").strip()
            monthly_now_all_text = str(line_map.get("monthly_return_now_all") or "n/a").strip()
            monthly_now_all_ratio = _parse_pct_text_to_ratio(monthly_now_all_text)
            monthly_now_all_src = str(line_map.get("monthly_return_now_all_source") or "").strip()
            monthly_now_new_text = str(line_map.get("monthly_return_now_new_condition") or "n/a").strip()
            monthly_now_new_ratio = _parse_pct_text_to_ratio(monthly_now_new_text)
            monthly_now_new_src = str(line_map.get("monthly_return_now_new_condition_source") or "").strip()
            roll_text = str(line_map.get("rolling_30d_monthly_return") or "n/a").strip()
            roll_ratio = _parse_pct_text_to_ratio(roll_text)
            roll_src = str(line_map.get("rolling_30d_monthly_return_source") or "").strip()
            roll_all_text = str(line_map.get("rolling_30d_monthly_return_all") or "n/a").strip()
            roll_all_ratio = _parse_pct_text_to_ratio(roll_all_text)
            roll_all_src = str(line_map.get("rolling_30d_monthly_return_all_source") or "").strip()
            roll_new_text = str(line_map.get("rolling_30d_monthly_return_new_condition") or "n/a").strip()
            roll_new_ratio = _parse_pct_text_to_ratio(roll_new_text)
            roll_new_src = str(line_map.get("rolling_30d_monthly_return_new_condition_source") or "").strip()
            out["monthly_return_now_text"] = monthly_now_text
            out["monthly_return_now_ratio"] = monthly_now_ratio
            out["monthly_return_now_source"] = monthly_now_src
            out["monthly_return_now_all_text"] = monthly_now_all_text
            out["monthly_return_now_all_ratio"] = monthly_now_all_ratio
            out["monthly_return_now_all_source"] = monthly_now_all_src
            out["monthly_return_now_new_condition_text"] = monthly_now_new_text
            out["monthly_return_now_new_condition_ratio"] = monthly_now_new_ratio
            out["monthly_return_now_new_condition_source"] = monthly_now_new_src
            out["rolling_30d_monthly_return_text"] = roll_text
            out["rolling_30d_monthly_return_ratio"] = roll_ratio
            out["rolling_30d_monthly_return_source"] = roll_src
            out["rolling_30d_monthly_return_all_text"] = roll_all_text
            out["rolling_30d_monthly_return_all_ratio"] = roll_all_ratio
            out["rolling_30d_monthly_return_all_source"] = roll_all_src
            out["rolling_30d_monthly_return_new_condition_text"] = roll_new_text
            out["rolling_30d_monthly_return_new_condition_ratio"] = roll_new_ratio
            out["rolling_30d_monthly_return_new_condition_source"] = roll_new_src
            out["rolling_30d_resolved_trades"] = _as_int(line_map.get("rolling_30d_resolved_trades"))
            out["rolling_30d_resolved_trades_all"] = _as_int(line_map.get("rolling_30d_resolved_trades_all"))
            out["rolling_30d_resolved_trades_new_condition"] = _as_int(
                line_map.get("rolling_30d_resolved_trades_new_condition")
            )
            out["open_positions"] = _as_int(line_map.get("open_positions"))
            out["open_positions_all"] = _as_int(line_map.get("open_positions_all"))
            out["open_positions_new_condition"] = _as_int(line_map.get("open_positions_new_condition"))
            out["resolved_positions"] = _as_int(line_map.get("resolved_positions"))
            out["resolved_positions_all"] = _as_int(line_map.get("resolved_positions_all"))
            out["resolved_positions_new_condition"] = _as_int(line_map.get("resolved_positions_new_condition"))

            # If dedicated new-condition tracker exists, prefer it for top-line KPI display.
            if monthly_now_new_ratio is not None:
                out["monthly_return_now_text"] = monthly_now_new_text
                out["monthly_return_now_ratio"] = monthly_now_new_ratio
                out["monthly_return_now_source"] = monthly_now_new_src or "realized_rolling_30d_new_condition"
            if roll_new_ratio is not None:
                out["rolling_30d_monthly_return_text"] = roll_new_text
                out["rolling_30d_monthly_return_ratio"] = roll_new_ratio
                out["rolling_30d_monthly_return_source"] = roll_new_src or "realized_rolling_30d_new_condition"
            if out.get("rolling_30d_resolved_trades_new_condition") is not None:
                out["rolling_30d_resolved_trades"] = out.get("rolling_30d_resolved_trades_new_condition")
            if out.get("open_positions_new_condition") is not None:
                out["open_positions"] = out.get("open_positions_new_condition")
            if out.get("resolved_positions_new_condition") is not None:
                out["resolved_positions"] = out.get("resolved_positions_new_condition")
        except Exception:
            pass

    if monthly_txt_path.exists():
        try:
            raw = monthly_txt_path.read_text(encoding="utf-8")
            m = re.search(r"monthly_return_pct_rolling_30d\s*=\s*([^\r\n]+)", raw, flags=re.IGNORECASE)
            if m:
                txt = str(m.group(1)).strip()
                ratio = _parse_pct_text_to_ratio(txt)
                source_now = str(out.get("rolling_30d_monthly_return_source") or "").strip().lower()
                should_override = source_now in {"", "realized_rolling_30d", "realized_rolling_30d_all"}
                if ratio is not None and should_override:
                    out["rolling_30d_monthly_return_text"] = _fmt_ratio_pct(ratio, digits=2)
                    out["rolling_30d_monthly_return_ratio"] = ratio
                    out["rolling_30d_monthly_return_source"] = "realized_rolling_30d_all"
                elif txt.lower() in {"n/a", "na", "none", "null", "-"} and should_override:
                    out["rolling_30d_monthly_return_text"] = "n/a"
                    out["rolling_30d_monthly_return_ratio"] = None
                    out["rolling_30d_monthly_return_source"] = "n/a"
        except Exception:
            pass

    realized = _read_json(realized_latest_path)
    if isinstance(realized, dict):
        try:
            metrics = realized.get("metrics") if isinstance(realized.get("metrics"), dict) else {}
            roll = metrics.get("rolling_30d") if isinstance(metrics.get("rolling_30d"), dict) else {}
            ret = _as_float(roll.get("return_pct"))
            out["realized_latest_rolling_30d_return_ratio"] = ret
            out["realized_latest_rolling_30d_return_text"] = _fmt_ratio_pct(ret, digits=2)
            if out.get("rolling_30d_resolved_trades") is None:
                out["rolling_30d_resolved_trades"] = _as_int(roll.get("resolved_trades"))
            if out.get("open_positions") is None:
                out["open_positions"] = _as_int(metrics.get("open_positions"))
            if out.get("resolved_positions") is None:
                out["resolved_positions"] = _as_int(metrics.get("resolved_positions"))
        except Exception:
            pass

    return out


def scan_live_processes(skip_scan: bool) -> dict:
    if skip_scan:
        return {"enabled": False, "ok": True, "count": 0, "rows": []}

    if os.name != "nt":
        return {"enabled": True, "ok": False, "count": 0, "rows": [], "error": "process scan currently supports Windows only"}

    cmd = (
        "Get-CimInstance Win32_Process "
        "| Where-Object { "
        "$_.Name -match '^python(\\.exe)?$' -and "
        "$_.CommandLine -like '*polymarket_clob_arb_realtime.py*' -and "
        "$_.CommandLine -like '*--execute*' "
        "} "
        "| Select-Object ProcessId,CreationDate,CommandLine "
        "| ConvertTo-Json -Depth 4 -Compress"
    )
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as exc:
        return {"enabled": True, "ok": False, "count": 0, "rows": [], "error": f"scan_failed: {exc}"}

    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip()[:500]
        return {"enabled": True, "ok": False, "count": 0, "rows": [], "error": f"powershell_exit_{p.returncode}: {msg}"}

    raw = (p.stdout or "").strip()
    if not raw:
        return {"enabled": True, "ok": True, "count": 0, "rows": []}

    try:
        obj = json.loads(raw)
    except Exception as exc:
        return {"enabled": True, "ok": False, "count": 0, "rows": [], "error": f"json_parse_failed: {exc}"}

    rows: List[dict]
    if isinstance(obj, list):
        rows = [x for x in obj if isinstance(x, dict)]
    elif isinstance(obj, dict):
        rows = [obj]
    else:
        rows = []

    slim: List[dict] = []
    for r in rows:
        slim.append(
            {
                "pid": int(r.get("ProcessId") or 0),
                "created": str(r.get("CreationDate") or ""),
                "command_line": str(r.get("CommandLine") or ""),
            }
        )
    return {"enabled": True, "ok": True, "count": len(slim), "rows": slim}


def _iter_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def _extract_day_key(row: dict) -> str:
    day = str(row.get("day") or row.get("date") or "").strip()
    if len(day) >= 10 and day[4:5] == "-" and day[7:8] == "-":
        return day[:10]
    ts = str(row.get("ts") or row.get("generated_utc") or row.get("captured_utc") or "").strip()
    if len(ts) >= 10 and ts[4:5] == "-" and ts[7:8] == "-":
        return ts[:10]
    return ""


def _extract_realized_value(row: dict) -> Optional[float]:
    for key in ("realized_pnl_usd", "pnl_realized_usd", "realized_pnl", "pnl_realized", "realized"):
        if key in row:
            n = _as_float(row.get(key))
            if n is not None:
                return n
    return None


def _extract_balance_value(row: dict) -> Optional[float]:
    for key in ("balance_usdc", "balance_usd", "bankroll_usd"):
        if key in row:
            n = _as_float(row.get(key))
            if n is not None and n > 0:
                return n
    return None


def _extract_strategy_id(row: dict) -> str:
    return str(row.get("strategy_id") or "").strip()


def _looks_like_cumulative_snapshot(path: Path, rows_sorted: List[dict]) -> bool:
    name = path.name.lower()
    if "clob_arb_realized_daily" in name:
        return True
    for r in rows_sorted:
        src = str(r.get("source") or "").strip().lower()
        if src.endswith("record_simmer_realized_daily.py"):
            return True
    return False


def _collect_day_rows(path: Path, strategy_id: str) -> Dict[str, dict]:
    day_rows: Dict[str, dict] = {}
    is_strategy_file = "strategy_realized_pnl_daily" in path.name.lower()
    target_sid = str(strategy_id or "").strip()

    for row in _iter_jsonl(path):
        if target_sid and is_strategy_file:
            row_sid = _extract_strategy_id(row)
            if row_sid != target_sid:
                continue

        day = _extract_day_key(row)
        if not day:
            continue
        pnl = _extract_realized_value(row)
        if pnl is None:
            continue

        day_rows[day] = {
            "day": day,
            "realized_value": float(pnl),
            "source": str(row.get("source") or "").strip(),
            "balance_usd": _extract_balance_value(row),
        }
    return day_rows


def load_realized_daily_series(strategy_id: str = DEFAULT_REALIZED_STRATEGY_ID) -> dict:
    strategy_file = logs_dir() / "strategy_realized_pnl_daily.jsonl"
    clob_file = logs_dir() / "clob_arb_realized_daily.jsonl"

    candidates: List[Path] = []
    preferred_rows: Dict[str, dict] = {}
    if strategy_file.exists():
        preferred_rows = _collect_day_rows(strategy_file, strategy_id=strategy_id)
    if preferred_rows:
        candidates = [strategy_file]
    else:
        for p in (clob_file, strategy_file):
            if p.exists():
                candidates.append(p)

    per_day: Dict[str, float] = {}
    used_files: List[str] = []
    source_modes: Dict[str, str] = {}
    balance_rows: List[dict] = []

    for path in candidates:
        day_rows = preferred_rows if (path == strategy_file and preferred_rows) else _collect_day_rows(path, strategy_id=strategy_id)

        if not day_rows:
            continue

        used_files.append(str(path))
        rows_sorted = [day_rows[d] for d in sorted(day_rows.keys())]
        is_cumulative = _looks_like_cumulative_snapshot(path, rows_sorted)
        source_modes[str(path)] = "cumulative_snapshot" if is_cumulative else "daily_realized"

        prev_val: Optional[float] = None
        for rec in rows_sorted:
            bal = _as_float(rec.get("balance_usd"))
            if bal is not None and bal > 0:
                balance_rows.append(
                    {
                        "day": str(rec.get("day") or ""),
                        "balance_usd": float(bal),
                        "path": str(path),
                    }
                )

            cur = _as_float(rec.get("realized_value"))
            if cur is None:
                continue

            if is_cumulative:
                if prev_val is None:
                    prev_val = cur
                    continue
                delta = cur - prev_val
                prev_val = cur
                per_day[str(rec.get("day"))] = per_day.get(str(rec.get("day")), 0.0) + float(delta)
            else:
                per_day[str(rec.get("day"))] = per_day.get(str(rec.get("day")), 0.0) + float(cur)

    bankroll = None
    bankroll_source = ""
    if balance_rows:
        balance_rows.sort(key=lambda x: str(x.get("day") or ""))
        last = balance_rows[-1]
        bankroll = _as_float(last.get("balance_usd"))
        if bankroll is not None:
            bankroll_source = f"{Path(str(last.get('path') or '')).name}:{str(last.get('day') or '')}"

    mode_set = {str(v) for v in source_modes.values() if str(v)}
    series_mode = "+".join(sorted(mode_set)) if mode_set else "none"

    return {
        "per_day": per_day,
        "strategy_id": str(strategy_id or "").strip(),
        "source_files": used_files,
        "source_modes": source_modes,
        "series_mode": series_mode,
        "bankroll_usd": bankroll,
        "bankroll_source": bankroll_source,
    }


def evaluate_realized_30d_gate(min_days: int, series: Optional[dict] = None) -> dict:
    s = series if isinstance(series, dict) else load_realized_daily_series()
    per_day_raw = s.get("per_day") if isinstance(s.get("per_day"), dict) else {}
    per_day: Dict[str, float] = {}
    for k, v in per_day_raw.items():
        n = _as_float(v)
        if n is not None:
            per_day[str(k)] = float(n)

    observed_days = len(per_day)
    final_days = max(1, int(min_days))
    tentative_days = min(7, final_days)
    interim_days = min(14, final_days)
    if interim_days < tentative_days:
        interim_days = tentative_days

    decision = "READY_FOR_JUDGMENT" if observed_days >= final_days else "PENDING_30D"
    reason = (
        f"observed_realized_days={observed_days} >= min_days={final_days}"
        if decision == "READY_FOR_JUDGMENT"
        else f"observed_realized_days={observed_days} < min_days={final_days}"
    )

    decision_3stage = "PENDING_TENTATIVE"
    stage_label = f"PRE_TENTATIVE_{tentative_days}D"
    stage_label_ja = f"{tentative_days}日暫定到達前"
    reason_3stage = f"observed_realized_days={observed_days} < tentative_days={tentative_days}"
    next_stage: Optional[dict] = {
        "stage": "TENTATIVE",
        "label": f"{tentative_days}d tentative",
        "label_ja": _gate_stage_label_ja("TENTATIVE", tentative_days),
        "min_days": int(tentative_days),
        "remaining_days": int(max(0, tentative_days - observed_days)),
    }

    if observed_days >= tentative_days:
        decision_3stage = "READY_TENTATIVE"
        stage_label = f"TENTATIVE_{tentative_days}D"
        stage_label_ja = _gate_stage_label_ja("TENTATIVE", tentative_days)
        reason_3stage = (
            f"observed_realized_days={observed_days} >= tentative_days={tentative_days}"
            f" and < interim_days={interim_days}"
        )
        next_stage = {
            "stage": "INTERIM",
            "label": f"{interim_days}d interim",
            "label_ja": _gate_stage_label_ja("INTERIM", interim_days),
            "min_days": int(interim_days),
            "remaining_days": int(max(0, interim_days - observed_days)),
        }

    if observed_days >= interim_days:
        decision_3stage = "READY_INTERIM"
        stage_label = f"INTERIM_{interim_days}D"
        stage_label_ja = _gate_stage_label_ja("INTERIM", interim_days)
        reason_3stage = (
            f"observed_realized_days={observed_days} >= interim_days={interim_days}"
            f" and < final_days={final_days}"
        )
        next_stage = {
            "stage": "FINAL",
            "label": f"{final_days}d final",
            "label_ja": _gate_stage_label_ja("FINAL", final_days),
            "min_days": int(final_days),
            "remaining_days": int(max(0, final_days - observed_days)),
        }

    if observed_days >= final_days:
        decision_3stage = "READY_FINAL"
        stage_label = f"FINAL_{final_days}D"
        stage_label_ja = _gate_stage_label_ja("FINAL", final_days)
        reason_3stage = f"observed_realized_days={observed_days} >= final_days={final_days}"
        next_stage = None

    source_files = s.get("source_files") if isinstance(s.get("source_files"), list) else []
    if not source_files:
        reason += "; realized daily artifact not found"
        reason_3stage += "; realized daily artifact not found"

    stages = [
        {
            "stage": "TENTATIVE",
            "label": f"{tentative_days}d tentative",
            "label_ja": _gate_stage_label_ja("TENTATIVE", tentative_days),
            "min_days": int(tentative_days),
            "reached": bool(observed_days >= tentative_days),
        },
        {
            "stage": "INTERIM",
            "label": f"{interim_days}d interim",
            "label_ja": _gate_stage_label_ja("INTERIM", interim_days),
            "min_days": int(interim_days),
            "reached": bool(observed_days >= interim_days),
        },
        {
            "stage": "FINAL",
            "label": f"{final_days}d final",
            "label_ja": _gate_stage_label_ja("FINAL", final_days),
            "min_days": int(final_days),
            "reached": bool(observed_days >= final_days),
        },
    ]

    total_realized = sum(per_day.values()) if per_day else 0.0
    return {
        "decision": decision,
        "decision_3stage": decision_3stage,
        "decision_3stage_label_ja": _gate_decision_label_ja(decision_3stage, tentative_days, interim_days, final_days),
        "stage_label": stage_label,
        "stage_label_ja": stage_label_ja,
        "reason": reason,
        "reason_3stage": reason_3stage,
        "next_stage": next_stage,
        "stages": stages,
        "stage_thresholds_days": {
            "tentative": int(tentative_days),
            "interim": int(interim_days),
            "final": int(final_days),
        },
        "strategy_id": str(s.get("strategy_id") or ""),
        "min_realized_days": int(final_days),
        "observed_realized_days": observed_days,
        "observed_total_realized_pnl_usd": float(total_realized),
        "series_mode": str(s.get("series_mode") or "none"),
        "source_files": source_files,
        "source_modes": (s.get("source_modes") if isinstance(s.get("source_modes"), dict) else {}),
    }


def summarize_realized_monthly_return(min_days: int, series: Optional[dict] = None) -> dict:
    s = series if isinstance(series, dict) else load_realized_daily_series()
    per_day_raw = s.get("per_day") if isinstance(s.get("per_day"), dict) else {}
    per_day: Dict[str, float] = {}
    for k, v in per_day_raw.items():
        n = _as_float(v)
        if n is not None:
            per_day[str(k)] = float(n)

    day_keys = sorted(per_day.keys())
    observed_days = len(day_keys)
    total_realized = float(sum(per_day.values())) if per_day else 0.0
    mean_daily = (total_realized / observed_days) if observed_days > 0 else None
    trailing_days = min(30, observed_days)
    trailing_sum = float(sum(per_day[d] for d in day_keys[-trailing_days:])) if trailing_days > 0 else 0.0
    latest_day = day_keys[-1] if day_keys else ""
    latest_daily = float(per_day.get(latest_day)) if latest_day in per_day else None

    bankroll = _as_float(s.get("bankroll_usd"))
    trailing_window_return = (trailing_sum / bankroll) if (bankroll is not None and bankroll > 0 and trailing_days > 0) else None
    rolling_30d_return = (trailing_sum / bankroll) if (bankroll is not None and bankroll > 0 and observed_days >= 30) else None
    max_drawdown_30d = None
    if bankroll is not None and bankroll > 0 and trailing_days > 0:
        eq = 1.0
        peak = 1.0
        worst_dd = 0.0
        for d in day_keys[-trailing_days:]:
            daily_ret = float(per_day.get(d, 0.0)) / float(bankroll)
            if daily_ret <= -1.0:
                eq = 0.0
            else:
                eq = eq * (1.0 + daily_ret)
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (eq / peak) - 1.0
                if dd < worst_dd:
                    worst_dd = dd
        max_drawdown_30d = float(worst_dd)

    projected_monthly = None
    if bankroll is not None and bankroll > 0 and mean_daily is not None:
        daily_ret = mean_daily / bankroll
        if daily_ret > -1.0:
            projected_monthly = (1.0 + daily_ret) ** 30.0 - 1.0

    decision = "READY_FOR_JUDGMENT" if observed_days >= int(min_days) else "INSUFFICIENT_DATA"
    if observed_days < int(min_days):
        reason = f"observed_realized_days={observed_days} < min_days={int(min_days)}"
    elif bankroll is None or bankroll <= 0:
        reason = "bankroll is unavailable; return ratio cannot be computed"
    else:
        reason = f"observed_realized_days={observed_days} >= min_days={int(min_days)}"

    return {
        "decision": decision,
        "reason": reason,
        "strategy_id": str(s.get("strategy_id") or ""),
        "min_realized_days": int(min_days),
        "observed_realized_days": observed_days,
        "series_mode": str(s.get("series_mode") or "none"),
        "source_files": (s.get("source_files") if isinstance(s.get("source_files"), list) else []),
        "source_modes": (s.get("source_modes") if isinstance(s.get("source_modes"), dict) else {}),
        "bankroll_usd": bankroll,
        "bankroll_source": str(s.get("bankroll_source") or ""),
        "total_realized_pnl_usd": float(total_realized),
        "latest_day": latest_day,
        "daily_realized_pnl_usd_latest": float(latest_daily) if latest_daily is not None else None,
        "mean_daily_realized_pnl_usd": float(mean_daily) if mean_daily is not None else None,
        "trailing_window_days": int(trailing_days),
        "trailing_window_realized_pnl_usd": float(trailing_sum),
        "projected_monthly_return_ratio": float(projected_monthly) if projected_monthly is not None else None,
        "projected_monthly_return_text": _fmt_ratio_pct(projected_monthly, digits=2),
        "trailing_window_return_ratio": float(trailing_window_return) if trailing_window_return is not None else None,
        "trailing_window_return_text": _fmt_ratio_pct(trailing_window_return, digits=2),
        "rolling_30d_return_ratio": float(rolling_30d_return) if rolling_30d_return is not None else None,
        "rolling_30d_return_text": _fmt_ratio_pct(rolling_30d_return, digits=2),
        "max_drawdown_30d_ratio": float(max_drawdown_30d) if max_drawdown_30d is not None else None,
        "max_drawdown_30d_text": _fmt_ratio_pct(max_drawdown_30d, digits=2),
    }


def build_kpi_core(no_longshot: dict, realized_monthly: dict) -> dict:
    no = no_longshot if isinstance(no_longshot, dict) else {}
    rm = realized_monthly if isinstance(realized_monthly, dict) else {}
    daily = _as_float(rm.get("daily_realized_pnl_usd_latest"))
    daily_day = str(rm.get("latest_day") or "")
    monthly_now_text = str(no.get("monthly_return_now_text") or rm.get("projected_monthly_return_text") or "n/a")
    monthly_now_source = str(no.get("monthly_return_now_source") or "realized_monthly_return.projected_monthly_return_text")
    max_dd_ratio = _as_float(rm.get("max_drawdown_30d_ratio"))
    max_dd_text = str(rm.get("max_drawdown_30d_text") or _fmt_ratio_pct(max_dd_ratio, digits=2))

    daily_text = "n/a"
    if daily is not None:
        daily_text = f"{daily:+.4f}"
    return {
        "daily_realized_pnl_usd": float(daily) if daily is not None else None,
        "daily_realized_pnl_usd_text": daily_text,
        "daily_realized_pnl_day": daily_day,
        "monthly_return_now_text": monthly_now_text,
        "monthly_return_now_source": monthly_now_source,
        "max_drawdown_30d_ratio": float(max_dd_ratio) if max_dd_ratio is not None else None,
        "max_drawdown_30d_text": max_dd_text,
        "source": "logs/strategy_register_latest.json",
    }


def render_html_snapshot(payload: dict) -> str:
    generated = html.escape(str(payload.get("generated_utc") or ""))
    strat = payload.get("strategy_register") if isinstance(payload.get("strategy_register"), dict) else {}
    counts = strat.get("counts") if isinstance(strat.get("counts"), dict) else {}
    strategies = strat.get("entries") if isinstance(strat.get("entries"), list) else []
    bankroll_policy = payload.get("bankroll_policy") if isinstance(payload.get("bankroll_policy"), dict) else {}
    readiness = payload.get("readiness") if isinstance(payload.get("readiness"), dict) else {}
    readiness_rows = readiness.get("latest_records") if isinstance(readiness.get("latest_records"), list) else []
    readiness_summary = readiness.get("summary") if isinstance(readiness.get("summary"), dict) else {}
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    live = runtime.get("live_processes") if isinstance(runtime.get("live_processes"), dict) else {}
    clob = runtime.get("clob_state") if isinstance(runtime.get("clob_state"), dict) else {}
    gate = payload.get("realized_30d_gate") if isinstance(payload.get("realized_30d_gate"), dict) else {}
    no_longshot = payload.get("no_longshot_status") if isinstance(payload.get("no_longshot_status"), dict) else {}
    realized_monthly = (
        payload.get("realized_monthly_return") if isinstance(payload.get("realized_monthly_return"), dict) else {}
    )
    kpi_core = payload.get("kpi_core") if isinstance(payload.get("kpi_core"), dict) else {}

    weather_view_specs = [
        (
            "weather_consensus_overview_latest.html",
            "Weather consensus overview (cross-profile)",
        ),
        (
            "weather_7acct_auto_consensus_snapshot_latest.html",
            "Weather consensus snapshot: weather_7acct_auto",
        ),
    ]
    weather_view_rows: List[str] = []
    for file_name, label in weather_view_specs:
        p = logs_dir() / file_name
        exists = p.exists()
        badge = '<span class="chip ok">AVAILABLE</span>' if exists else '<span class="chip bad">MISSING</span>'
        mtime = "-"
        if exists:
            try:
                mtime = dt.datetime.fromtimestamp(p.stat().st_mtime, tz=dt.timezone.utc).isoformat()
            except Exception:
                mtime = "-"
        link_html = f'<a href="{html.escape(file_name)}" target="_blank" rel="noopener">{html.escape(file_name)}</a>'
        weather_view_rows.append(
            f"<tr><td>{html.escape(label)}</td><td>{badge}</td><td>{link_html}</td><td>{html.escape(mtime)}</td></tr>"
        )

    def chip(status: str) -> str:
        s = (status or "").upper()
        cls = "unk"
        if s in {"ADOPTED", "GO", "READY_FOR_JUDGMENT", "READY_FINAL"}:
            cls = "ok"
        elif s in {"REJECTED", "NO_GO"}:
            cls = "bad"
        elif s in {
            "PENDING",
            "PENDING_30D",
            "PENDING_TENTATIVE",
            "READY_TENTATIVE",
            "READY_INTERIM",
        }:
            cls = "wait"
        return f'<span class="chip {cls}">{html.escape(s or "-")}</span>'

    strategy_rows: List[str] = []
    for r in strategies:
        if not isinstance(r, dict):
            continue
        sid_raw = str(r.get("strategy_id") or "")
        sid = html.escape(str(r.get("strategy_id") or ""))
        section = html.escape(str(r.get("section") or ""))
        status = str(r.get("status") or "")
        scope = html.escape(str(r.get("scope") or ""))
        note = html.escape(str(r.get("decision_note") or ""))
        runtime_cmds = r.get("runtime_commands") if isinstance(r.get("runtime_commands"), list) else []
        cmd = html.escape(str(runtime_cmds[0])) if runtime_cmds else "-"
        strategy_metric = "-"
        if sid_raw == "weather_clob_arb_buckets_observe":
            m_now = str(realized_monthly.get("projected_monthly_return_text") or "n/a")
            m_roll = str(realized_monthly.get("rolling_30d_return_text") or "n/a")
            m_days = str(realized_monthly.get("observed_realized_days") if realized_monthly.get("observed_realized_days") is not None else "-")
            m_gate = str(gate.get("decision_3stage") or gate.get("decision") or "-")
            strategy_metric = f"realized_monthly_now={m_now}; roll30={m_roll}; observed_days={m_days}; gate={m_gate}"
        if sid_raw == "no_longshot_daily_observe":
            m_now = str(no_longshot.get("monthly_return_now_text") or "n/a")
            m_roll = str(no_longshot.get("rolling_30d_monthly_return_text") or "n/a")
            m_src = str(no_longshot.get("monthly_return_now_source") or "-")
            m_now_all = str(no_longshot.get("monthly_return_now_all_text") or "n/a")
            m_now_new = str(no_longshot.get("monthly_return_now_new_condition_text") or "n/a")
            strategy_metric = f"monthly_now={m_now}; roll30={m_roll}; src={m_src}; monthly_now_new={m_now_new}; monthly_now_all={m_now_all}"
        metric = html.escape(strategy_metric)
        strategy_rows.append(
            f"<tr><td><code>{sid}</code></td><td>{chip(status)}</td><td>{section}</td><td>{scope}</td><td>{metric}</td><td><code>{cmd}</code></td><td>{note}</td></tr>"
        )

    readiness_rows_html: List[str] = []
    for r in readiness_rows:
        if not isinstance(r, dict):
            continue
        profile = html.escape(str(r.get("profile_name") or ""))
        mode = html.escape(str(r.get("mode") or ""))
        decision = str(r.get("decision") or "")
        gen = html.escape(str(r.get("generated_utc") or ""))
        row_count = html.escape(str(r.get("row_count") if r.get("row_count") is not None else "-"))
        both = html.escape(str(r.get("both_ratio") if r.get("both_ratio") is not None else "-"))
        yld = html.escape(
            str(r.get("median_net_yield_per_day") if r.get("median_net_yield_per_day") is not None else "-")
        )
        readiness_rows_html.append(
            f"<tr><td>{profile}</td><td>{mode}</td><td>{chip(decision)}</td><td>{row_count}</td><td>{both}</td><td>{yld}</td><td>{gen}</td></tr>"
        )

    strict = readiness_summary.get("strict") if isinstance(readiness_summary.get("strict"), dict) else {}
    quality = readiness_summary.get("quality") if isinstance(readiness_summary.get("quality"), dict) else {}

    clob_day = html.escape(str(clob.get("day") or "-"))
    clob_exec = html.escape(str(clob.get("executions_today") if clob.get("executions_today") is not None else "-"))
    clob_notional = html.escape(str(clob.get("notional_today") if clob.get("notional_today") is not None else "-"))
    clob_halt = html.escape(str(clob.get("halted") if clob.get("halted") is not None else "-"))
    live_count = int(live.get("count") or 0)

    live_rows_html: List[str] = []
    for r in (live.get("rows") if isinstance(live.get("rows"), list) else []):
        if not isinstance(r, dict):
            continue
        pid = html.escape(str(r.get("pid") or ""))
        created = html.escape(str(r.get("created") or ""))
        cmd = html.escape(str(r.get("command_line") or ""))
        live_rows_html.append(f"<tr><td>{pid}</td><td>{created}</td><td><code>{cmd}</code></td></tr>")

    gate_dec = str(gate.get("decision") or "")
    gate_dec_stage = str(gate.get("decision_3stage") or gate_dec)
    gate_dec_stage_ja = html.escape(str(gate.get("decision_3stage_label_ja") or "-"))
    gate_stage_label = html.escape(str(gate.get("stage_label") or "-"))
    gate_stage_label_ja = html.escape(str(gate.get("stage_label_ja") or "-"))
    gate_days = html.escape(str(gate.get("observed_realized_days") if gate.get("observed_realized_days") is not None else "-"))
    gate_realized = html.escape(
        str(gate.get("observed_total_realized_pnl_usd") if gate.get("observed_total_realized_pnl_usd") is not None else "-")
    )
    gate_reason_stage = html.escape(str(gate.get("reason_3stage") or gate.get("reason") or ""))
    gate_next = gate.get("next_stage") if isinstance(gate.get("next_stage"), dict) else {}
    gate_next_txt = "-"
    if gate_next:
        next_label = str(gate_next.get("label") or gate_next.get("stage") or "-")
        next_label_ja = str(gate_next.get("label_ja") or "")
        rem = gate_next.get("remaining_days")
        rem_txt = str(rem) if rem is not None else "-"
        if next_label_ja:
            gate_next_txt = f"{next_label_ja} / {next_label} (remaining_days={rem_txt})"
        else:
            gate_next_txt = f"{next_label} (remaining_days={rem_txt})"
    gate_next_html = html.escape(gate_next_txt)
    gate_sources = gate.get("source_files") if isinstance(gate.get("source_files"), list) else []
    gate_src_html = "".join(f"<li><code>{html.escape(str(x))}</code></li>" for x in gate_sources) or "<li>-</li>"
    gate_stage_rows: List[str] = []
    for stage in (gate.get("stages") if isinstance(gate.get("stages"), list) else []):
        if not isinstance(stage, dict):
            continue
        s_name = html.escape(str(stage.get("stage") or "-"))
        s_label = html.escape(str(stage.get("label") or "-"))
        s_label_ja = html.escape(str(stage.get("label_ja") or "-"))
        s_min = html.escape(str(stage.get("min_days") if stage.get("min_days") is not None else "-"))
        s_reached = "READY_FINAL" if bool(stage.get("reached")) else "PENDING_TENTATIVE"
        gate_stage_rows.append(
            f"<tr><td>{s_name}</td><td>{s_label_ja}</td><td>{s_label}</td><td>{s_min}</td><td>{chip(s_reached)}</td></tr>"
        )
    no_monthly_now = html.escape(str(no_longshot.get("monthly_return_now_text") or "n/a"))
    no_monthly_all = html.escape(str(no_longshot.get("monthly_return_now_all_text") or "n/a"))
    no_monthly_new = html.escape(str(no_longshot.get("monthly_return_now_new_condition_text") or "n/a"))
    no_roll30 = html.escape(str(no_longshot.get("rolling_30d_monthly_return_text") or "n/a"))
    no_roll30_all = html.escape(str(no_longshot.get("rolling_30d_monthly_return_all_text") or "n/a"))
    no_roll30_new = html.escape(str(no_longshot.get("rolling_30d_monthly_return_new_condition_text") or "n/a"))
    no_open = html.escape(str(no_longshot.get("open_positions") if no_longshot.get("open_positions") is not None else "-"))
    no_open_all = html.escape(str(no_longshot.get("open_positions_all") if no_longshot.get("open_positions_all") is not None else "-"))
    no_open_new = html.escape(str(no_longshot.get("open_positions_new_condition") if no_longshot.get("open_positions_new_condition") is not None else "-"))
    no_resolved = html.escape(
        str(no_longshot.get("resolved_positions") if no_longshot.get("resolved_positions") is not None else "-")
    )
    no_resolved_all = html.escape(
        str(no_longshot.get("resolved_positions_all") if no_longshot.get("resolved_positions_all") is not None else "-")
    )
    no_resolved_new = html.escape(
        str(no_longshot.get("resolved_positions_new_condition") if no_longshot.get("resolved_positions_new_condition") is not None else "-")
    )
    no_source = html.escape(str(no_longshot.get("monthly_return_now_source") or "-"))
    no_source_all = html.escape(str(no_longshot.get("monthly_return_now_all_source") or "-"))
    no_source_new = html.escape(str(no_longshot.get("monthly_return_now_new_condition_source") or "-"))
    kpi_daily = html.escape(str(kpi_core.get("daily_realized_pnl_usd_text") or "n/a"))
    kpi_monthly = html.escape(str(kpi_core.get("monthly_return_now_text") or "n/a"))
    kpi_maxdd = html.escape(str(kpi_core.get("max_drawdown_30d_text") or "n/a"))
    clob_monthly_now = html.escape(str(realized_monthly.get("projected_monthly_return_text") or "n/a"))
    clob_roll30 = html.escape(str(realized_monthly.get("rolling_30d_return_text") or "n/a"))
    clob_obs_days = html.escape(
        str(realized_monthly.get("observed_realized_days") if realized_monthly.get("observed_realized_days") is not None else "-")
    )
    clob_bankroll = html.escape(
        str(realized_monthly.get("bankroll_usd") if realized_monthly.get("bankroll_usd") is not None else "-")
    )
    clob_series_mode = html.escape(str(realized_monthly.get("series_mode") or "none"))
    clob_strategy_id = html.escape(str(realized_monthly.get("strategy_id") or "-"))
    clob_reason = html.escape(str(realized_monthly.get("reason") or ""))
    clob_mean_daily = html.escape(
        str(realized_monthly.get("mean_daily_realized_pnl_usd") if realized_monthly.get("mean_daily_realized_pnl_usd") is not None else "-")
    )
    clob_trailing_days = html.escape(
        str(realized_monthly.get("trailing_window_days") if realized_monthly.get("trailing_window_days") is not None else "-")
    )
    clob_trailing_pnl = html.escape(
        str(realized_monthly.get("trailing_window_realized_pnl_usd") if realized_monthly.get("trailing_window_realized_pnl_usd") is not None else "-")
    )
    clob_bankroll_source = html.escape(str(realized_monthly.get("bankroll_source") or "-"))
    clob_src_files = (
        realized_monthly.get("source_files") if isinstance(realized_monthly.get("source_files"), list) else []
    )
    clob_src_html = "".join(f"<li><code>{html.escape(str(x))}</code></li>" for x in clob_src_files) or "<li>-</li>"
    bp_initial = _as_float(bankroll_policy.get("initial_bankroll_usd"))
    bp_initial_txt = f"${bp_initial:.2f}" if bp_initial is not None else "-"
    bp_mode = html.escape(str(bankroll_policy.get("allocation_mode") or "-"))
    bp_count = int(bankroll_policy.get("adopted_strategy_count") or 0)
    bp_risk_ratio = _as_float(bankroll_policy.get("live_max_daily_risk_ratio"))
    bp_risk_ratio_txt = f"{bp_risk_ratio * 100.0:.2f}%" if bp_risk_ratio is not None else "-"
    bp_risk_usd = _as_float(bankroll_policy.get("live_max_daily_risk_usd"))
    bp_risk_usd_txt = f"${bp_risk_usd:.2f}" if bp_risk_usd is not None else "-"
    bp_allocation_policy = html.escape(str(bankroll_policy.get("allocation_policy") or "-"))
    bp_risk_policy = html.escape(str(bankroll_policy.get("live_max_daily_risk_policy") or "-"))
    bp_assumed_note = html.escape(str(bankroll_policy.get("assumed_bankroll_policy_note") or "-"))
    bp_alloc_rows: List[str] = []
    for row in (bankroll_policy.get("default_adopted_allocations") if isinstance(bankroll_policy.get("default_adopted_allocations"), list) else []):
        if not isinstance(row, dict):
            continue
        sid = html.escape(str(row.get("strategy_id") or "-"))
        ratio = _as_float(row.get("allocation_ratio"))
        pct = _as_float(row.get("allocation_pct"))
        usd = _as_float(row.get("allocation_usd"))
        ratio_txt = f"{ratio:.6f}" if ratio is not None else "-"
        pct_txt = f"{pct:.2f}%" if pct is not None else "-"
        usd_txt = f"${usd:.2f}" if usd is not None else "-"
        bp_alloc_rows.append(f"<tr><td><code>{sid}</code></td><td>{ratio_txt}</td><td>{pct_txt}</td><td>{usd_txt}</td></tr>")
    bp_alloc_rows_html = "".join(bp_alloc_rows) if bp_alloc_rows else '<tr><td colspan="4">no default allocation rows</td></tr>'

    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Strategy Register Snapshot</title>
  <style>
    :root {{
      --bg0: #041018;
      --bg1: #0a2333;
      --card: rgba(7, 23, 35, 0.86);
      --line: #1e5a79;
      --text: #d7f2ff;
      --muted: #7aa5bf;
      --ok: #00d58f;
      --bad: #ff6f6f;
      --wait: #ffc857;
      --unk: #8aa2b3;
      --accent: #41d7ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      font-family: "Consolas", "Cascadia Mono", monospace;
      background:
        radial-gradient(1200px 520px at 92% -12%, rgba(94, 179, 75, 0.25), transparent 62%),
        radial-gradient(900px 500px at -8% -20%, rgba(49, 150, 228, 0.28), transparent 58%),
        linear-gradient(180deg, var(--bg1), var(--bg0));
    }}
    .wrap {{ max-width: 1500px; margin: 0 auto; padding: 20px; }}
    .head h1 {{ margin: 0 0 6px 0; letter-spacing: .06em; }}
    .meta {{ color: var(--muted); margin-bottom: 14px; }}
    .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    .card {{
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--card);
      padding: 12px 14px;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.22);
    }}
    .k {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .07em; }}
    .v {{ font-size: 28px; line-height: 1.15; margin-top: 4px; }}
    .section {{ margin-top: 14px; }}
    .section h2 {{ margin: 0 0 8px 0; font-size: 18px; color: var(--accent); letter-spacing: .04em; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }}
    th, td {{ border-bottom: 1px solid rgba(30, 90, 121, 0.55); padding: 8px 10px; text-align: left; font-size: 12px; vertical-align: top; }}
    th {{ color: #8ac4df; background: rgba(10, 37, 56, 0.88); text-transform: uppercase; letter-spacing: .05em; }}
    tr:last-child td {{ border-bottom: none; }}
    code {{ color: #c3f6ff; }}
    a {{ color: #7fe5ff; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .chip {{
      display: inline-block;
      border-radius: 999px;
      border: 1px solid;
      padding: 2px 8px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: .05em;
    }}
    .ok {{ color: var(--ok); border-color: rgba(0, 213, 143, .6); background: rgba(0, 213, 143, .1); }}
    .bad {{ color: var(--bad); border-color: rgba(255, 111, 111, .55); background: rgba(255, 111, 111, .08); }}
    .wait {{ color: var(--wait); border-color: rgba(255, 200, 87, .55); background: rgba(255, 200, 87, .1); }}
    .unk {{ color: var(--unk); border-color: rgba(138, 162, 179, .5); background: rgba(138, 162, 179, .1); }}
    ul {{ margin: 6px 0 0 18px; padding: 0; }}
    .two {{ display: grid; gap: 12px; grid-template-columns: 1fr 1fr; }}
    @media (max-width: 1000px) {{ .two {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="head">
      <h1>STRATEGY REGISTER SNAPSHOT</h1>
      <div class="meta">generated_utc: {generated}</div>
    </div>

    <div class="grid">
      <div class="card"><div class="k">adopted</div><div class="v">{int(counts.get("ADOPTED") or 0)}</div></div>
      <div class="card"><div class="k">rejected</div><div class="v">{int(counts.get("REJECTED") or 0)}</div></div>
      <div class="card"><div class="k">pending</div><div class="v">{int(counts.get("PENDING") or 0)}</div></div>
      <div class="card"><div class="k">readiness strict go/total</div><div class="v">{int(strict.get("go_count") or 0)} / {int(strict.get("count") or 0)}</div></div>
      <div class="card"><div class="k">readiness quality go/total</div><div class="v">{int(quality.get("go_count") or 0)} / {int(quality.get("count") or 0)}</div></div>
      <div class="card"><div class="k">live clob processes</div><div class="v">{live_count}</div></div>
      <div class="card"><div class="k">clob realized monthly(now)</div><div class="v">{clob_monthly_now}</div></div>
      <div class="card"><div class="k">clob realized rolling_30d</div><div class="v">{clob_roll30}</div></div>
      <div class="card"><div class="k">no-longshot monthly_now</div><div class="v">{no_monthly_now}</div></div>
      <div class="card"><div class="k">no-longshot rolling_30d</div><div class="v">{no_roll30}</div></div>
      <div class="card"><div class="k">kpi_core daily realized pnl</div><div class="v">{kpi_daily}</div></div>
      <div class="card"><div class="k">kpi_core monthly return</div><div class="v">{kpi_monthly}</div></div>
      <div class="card"><div class="k">kpi_core max drawdown 30d</div><div class="v">{kpi_maxdd}</div></div>
      <div class="card"><div class="k">realized gate 7/14/30</div><div class="v">{chip(gate_dec_stage)}</div><div class="k" style="text-transform:none; letter-spacing:0.03em; margin-top:6px;">{gate_dec_stage_ja}</div></div>
    </div>

    <div class="section">
      <h2>Strategy Register</h2>
      <table>
        <thead><tr><th>strategy_id</th><th>status</th><th>section</th><th>scope</th><th>metric</th><th>runtime</th><th>decision_note</th></tr></thead>
        <tbody>{''.join(strategy_rows) if strategy_rows else '<tr><td colspan="7">no strategy entries</td></tr>'}</tbody>
      </table>
    </div>

    <div class="section">
      <h2>Bankroll Policy</h2>
      <table>
        <thead><tr><th>metric</th><th>value</th></tr></thead>
        <tbody>
          <tr><td>initial_bankroll_usd</td><td>{bp_initial_txt}</td></tr>
          <tr><td>allocation_mode</td><td>{bp_mode}</td></tr>
          <tr><td>adopted_strategy_count</td><td>{bp_count}</td></tr>
          <tr><td>allocation_policy</td><td>{bp_allocation_policy}</td></tr>
          <tr><td>live_max_daily_risk_ratio</td><td>{bp_risk_ratio_txt}</td></tr>
          <tr><td>live_max_daily_risk_usd</td><td>{bp_risk_usd_txt}</td></tr>
          <tr><td>live_max_daily_risk_policy</td><td>{bp_risk_policy}</td></tr>
          <tr><td>assumed_bankroll_policy_note</td><td>{bp_assumed_note}</td></tr>
        </tbody>
      </table>
      <table style="margin-top:10px;">
        <thead><tr><th>strategy_id</th><th>allocation_ratio</th><th>allocation_pct</th><th>allocation_usd</th></tr></thead>
        <tbody>{bp_alloc_rows_html}</tbody>
      </table>
    </div>

    <div class="section two">
      <div>
        <h2>Readiness Latest</h2>
        <table>
          <thead><tr><th>profile</th><th>mode</th><th>decision</th><th>rows</th><th>both_ratio</th><th>median_yield/day</th><th>generated_utc</th></tr></thead>
          <tbody>{''.join(readiness_rows_html) if readiness_rows_html else '<tr><td colspan="7">no readiness records</td></tr>'}</tbody>
        </table>
      </div>
      <div>
        <h2>Runtime Snapshot</h2>
        <table>
          <thead><tr><th>metric</th><th>value</th></tr></thead>
          <tbody>
            <tr><td>clob_state.day</td><td>{clob_day}</td></tr>
            <tr><td>clob_state.executions_today</td><td>{clob_exec}</td></tr>
            <tr><td>clob_state.notional_today</td><td>{clob_notional}</td></tr>
            <tr><td>clob_state.halted</td><td>{clob_halt}</td></tr>
            <tr><td>live_processes.count</td><td>{live_count}</td></tr>
            <tr><td>clob_realized.projected_monthly_now</td><td>{clob_monthly_now}</td></tr>
            <tr><td>clob_realized.rolling_30d</td><td>{clob_roll30}</td></tr>
            <tr><td>clob_realized.strategy_id</td><td>{clob_strategy_id}</td></tr>
            <tr><td>clob_realized.observed_days</td><td>{clob_obs_days}</td></tr>
            <tr><td>clob_realized.gate_legacy</td><td>{chip(gate_dec)}</td></tr>
            <tr><td>clob_realized.gate_stage</td><td>{chip(gate_dec_stage)}</td></tr>
            <tr><td>clob_realized.bankroll_usd</td><td>{clob_bankroll}</td></tr>
            <tr><td>no_longshot.monthly_return_now</td><td>{no_monthly_now}</td></tr>
            <tr><td>no_longshot.monthly_return_source</td><td>{no_source}</td></tr>
            <tr><td>no_longshot.monthly_return_now_new_condition</td><td>{no_monthly_new}</td></tr>
            <tr><td>no_longshot.monthly_return_source_new_condition</td><td>{no_source_new}</td></tr>
            <tr><td>no_longshot.monthly_return_now_all</td><td>{no_monthly_all}</td></tr>
            <tr><td>no_longshot.monthly_return_source_all</td><td>{no_source_all}</td></tr>
            <tr><td>no_longshot.rolling_30d</td><td>{no_roll30}</td></tr>
            <tr><td>no_longshot.rolling_30d_new_condition</td><td>{no_roll30_new}</td></tr>
            <tr><td>no_longshot.rolling_30d_all</td><td>{no_roll30_all}</td></tr>
            <tr><td>no_longshot.open_positions</td><td>{no_open}</td></tr>
            <tr><td>no_longshot.open_positions_new_condition</td><td>{no_open_new}</td></tr>
            <tr><td>no_longshot.open_positions_all</td><td>{no_open_all}</td></tr>
            <tr><td>no_longshot.resolved_positions</td><td>{no_resolved}</td></tr>
            <tr><td>no_longshot.resolved_positions_new_condition</td><td>{no_resolved_new}</td></tr>
            <tr><td>no_longshot.resolved_positions_all</td><td>{no_resolved_all}</td></tr>
          </tbody>
        </table>
        <table style="margin-top:10px;">
          <thead><tr><th>pid</th><th>created</th><th>command</th></tr></thead>
          <tbody>{''.join(live_rows_html) if live_rows_html else '<tr><td colspan="3">no live process detected</td></tr>'}</tbody>
        </table>
      </div>
    </div>

    <div class="section">
      <h2>Weather Views</h2>
      <table>
        <thead><tr><th>label</th><th>status</th><th>link</th><th>last_modified_utc</th></tr></thead>
        <tbody>{''.join(weather_view_rows) if weather_view_rows else '<tr><td colspan="4">no weather views</td></tr>'}</tbody>
      </table>
    </div>

    <div class="section">
      <h2>Realized PnL Gate (7d / 14d / 30d)</h2>
      <table>
        <thead><tr><th>legacy_decision</th><th>stage_decision</th><th>stage_decision_ja</th><th>stage_label</th><th>stage_label_ja</th><th>observed_days</th><th>next_stage</th><th>observed_total_realized_pnl_usd</th><th>reason</th></tr></thead>
        <tbody><tr><td>{chip(gate_dec)}</td><td>{chip(gate_dec_stage)}</td><td>{gate_dec_stage_ja}</td><td>{gate_stage_label}</td><td>{gate_stage_label_ja}</td><td>{gate_days}</td><td>{gate_next_html}</td><td>{gate_realized}</td><td>{gate_reason_stage}</td></tr></tbody>
      </table>
      <table style="margin-top:10px;">
        <thead><tr><th>stage</th><th>label_ja</th><th>label</th><th>min_days</th><th>reached</th></tr></thead>
        <tbody>{''.join(gate_stage_rows) if gate_stage_rows else '<tr><td colspan="5">no stage rows</td></tr>'}</tbody>
      </table>
      <ul>{gate_src_html}</ul>
    </div>

    <div class="section">
      <h2>CLOB Realized Monthly Return</h2>
      <table>
        <thead><tr><th>metric</th><th>value</th></tr></thead>
        <tbody>
          <tr><td>projected_monthly_return_pct_now</td><td>{clob_monthly_now}</td></tr>
          <tr><td>rolling_30d_return_pct</td><td>{clob_roll30}</td></tr>
          <tr><td>strategy_id</td><td>{clob_strategy_id}</td></tr>
          <tr><td>observed_realized_days</td><td>{clob_obs_days}</td></tr>
          <tr><td>mean_daily_realized_pnl_usd</td><td>{clob_mean_daily}</td></tr>
          <tr><td>trailing_window_days</td><td>{clob_trailing_days}</td></tr>
          <tr><td>trailing_window_realized_pnl_usd</td><td>{clob_trailing_pnl}</td></tr>
          <tr><td>bankroll_usd</td><td>{clob_bankroll}</td></tr>
          <tr><td>bankroll_source</td><td>{clob_bankroll_source}</td></tr>
          <tr><td>series_mode</td><td>{clob_series_mode}</td></tr>
          <tr><td>reason</td><td>{clob_reason}</td></tr>
        </tbody>
      </table>
      <ul>{clob_src_html}</ul>
    </div>
  </div>
</body>
</html>
"""


def main() -> int:
    p = argparse.ArgumentParser(description="Render strategy register snapshot (observe-only).")
    p.add_argument("--strategy-md", default="docs/llm/STRATEGY.md", help="Strategy register markdown path")
    p.add_argument("--readiness-glob", default="logs/*_top30_readiness_*latest.json", help="Readiness JSON glob")
    p.add_argument("--clob-state-file", default="logs/clob_arb_state.json", help="CLOB arb state JSON path")
    p.add_argument("--min-realized-days", type=int, default=30, help="Required realized-PnL days for gate")
    p.add_argument(
        "--realized-strategy-id",
        default=DEFAULT_REALIZED_STRATEGY_ID,
        help="Target strategy id for realized monthly/gate evaluation",
    )
    p.add_argument("--skip-process-scan", action="store_true", help="Skip live process scan")
    p.add_argument("--out-json", default="", help="Output JSON path (simple filename goes under logs/)")
    p.add_argument("--out-html", default="", help="Output HTML path (simple filename goes under logs/)")
    p.add_argument("--pretty", action="store_true")
    args = p.parse_args()

    strategy_path = resolve_path(str(args.strategy_md), "STRATEGY.md")
    readiness_paths = resolve_glob_inputs(str(args.readiness_glob))
    readiness_loaded: List[dict] = []
    for rp in readiness_paths:
        rec = load_readiness_record(rp)
        if rec is not None:
            readiness_loaded.append(rec)
    readiness_latest_rows = latest_readiness(readiness_loaded)
    min_realized_days = max(1, int(args.min_realized_days))
    realized_strategy_id = str(args.realized_strategy_id or "").strip() or DEFAULT_REALIZED_STRATEGY_ID
    realized_series = load_realized_daily_series(strategy_id=realized_strategy_id)
    strategy_register = parse_strategy_register(strategy_path)
    bankroll_policy = parse_bankroll_policy(
        strategy_path,
        strategy_entries=(strategy_register.get("entries") if isinstance(strategy_register.get("entries"), list) else []),
    )
    no_longshot_status = load_no_longshot_status(
        logs_dir() / "no_longshot_daily_summary.txt",
        logs_dir() / "no_longshot_realized_latest.json",
        logs_dir() / "no_longshot_monthly_return_latest.txt",
    )
    realized_30d_gate = evaluate_realized_30d_gate(min_realized_days, series=realized_series)
    realized_monthly = summarize_realized_monthly_return(min_realized_days, series=realized_series)
    kpi_core = build_kpi_core(no_longshot_status, realized_monthly)

    payload = {
        "generated_utc": now_utc().isoformat(),
        "meta": {"observe_only": True, "source": "scripts/render_strategy_register_snapshot.py"},
        "inputs": {
            "strategy_md": str(strategy_path),
            "readiness_glob": str(args.readiness_glob),
            "matched_readiness_files": len(readiness_paths),
            "loaded_readiness_records": len(readiness_loaded),
            "latest_readiness_records": len(readiness_latest_rows),
            "clob_state_file": str(resolve_path(str(args.clob_state_file), "clob_arb_state.json")),
            "realized_strategy_id": realized_strategy_id,
        },
        "strategy_register": strategy_register,
        "bankroll_policy": bankroll_policy,
        "readiness": {
            "latest_records": readiness_latest_rows,
            "summary": summarize_readiness(readiness_latest_rows),
        },
        "runtime": {
            "clob_state": load_clob_state(resolve_path(str(args.clob_state_file), "clob_arb_state.json")),
            "live_processes": scan_live_processes(bool(args.skip_process_scan)),
        },
        "kpi_core": kpi_core,
        "no_longshot_status": no_longshot_status,
        "realized_30d_gate": realized_30d_gate,
        "realized_monthly_return": realized_monthly,
    }

    out_json = resolve_path(str(args.out_json), "strategy_register_latest.json")
    out_html = resolve_path(str(args.out_html), "strategy_register_latest.html")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_html.parent.mkdir(parents=True, exist_ok=True)

    with out_json.open("w", encoding="utf-8") as f:
        if args.pretty:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        else:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")

    out_html.write_text(render_html_snapshot(payload), encoding="utf-8")
    print(f"[strategy-register] wrote json: {out_json}")
    print(f"[strategy-register] wrote html: {out_html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
