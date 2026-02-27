#!/usr/bin/env python3
"""
Read canonical no-longshot monthly return KPI from strategy register snapshot.

Default source:
  logs/strategy_register_latest.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_repo_path(raw: str, default_rel: str) -> Path:
    s = (raw or "").strip()
    if not s:
        return repo_root() / default_rel
    p = Path(s)
    if p.is_absolute():
        return p
    return repo_root() / p


def load_json(path: Path) -> Dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("snapshot JSON root must be object")
    return raw


def _as_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        x = float(v)
    except Exception:
        return default
    if x != x or x in (float("inf"), float("-inf")):
        return default
    return x


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _fmt_ratio_pct(v: Any) -> str:
    x = _as_float(v, None)
    if x is None:
        return "n/a"
    return f"{x * 100.0:+.2f}%"


def _pick_text_or_ratio(block: Dict[str, Any], text_key: str, ratio_key: str) -> str:
    text_val = str(block.get(text_key) or "").strip()
    if text_val and text_val.lower() != "n/a":
        return text_val
    return _fmt_ratio_pct(block.get(ratio_key))


def build_kpi(payload: Dict[str, Any]) -> Dict[str, Any]:
    no = payload.get("no_longshot_status") if isinstance(payload.get("no_longshot_status"), dict) else {}
    gate = payload.get("realized_30d_gate") if isinstance(payload.get("realized_30d_gate"), dict) else {}

    return {
        "generated_utc": str(payload.get("generated_utc") or ""),
        "monthly_return_now_text": _pick_text_or_ratio(no, "monthly_return_now_text", "monthly_return_now_ratio"),
        "monthly_return_now_source": str(no.get("monthly_return_now_source") or ""),
        "monthly_return_now_new_condition_text": _pick_text_or_ratio(
            no,
            "monthly_return_now_new_condition_text",
            "monthly_return_now_new_condition_ratio",
        ),
        "monthly_return_now_new_condition_source": str(no.get("monthly_return_now_new_condition_source") or ""),
        "monthly_return_now_all_text": _pick_text_or_ratio(no, "monthly_return_now_all_text", "monthly_return_now_all_ratio"),
        "monthly_return_now_all_source": str(no.get("monthly_return_now_all_source") or ""),
        "rolling_30d_monthly_return_text": _pick_text_or_ratio(
            no,
            "rolling_30d_monthly_return_text",
            "rolling_30d_monthly_return_ratio",
        ),
        "rolling_30d_monthly_return_source": str(no.get("rolling_30d_monthly_return_source") or ""),
        "rolling_30d_resolved_trades": _as_int(no.get("rolling_30d_resolved_trades"), 0),
        "realized_30d_gate_decision": str(gate.get("decision") or ""),
        "realized_30d_gate_decision_3stage": str(gate.get("decision_3stage") or ""),
        "realized_30d_gate_decision_3stage_label_ja": str(gate.get("decision_3stage_label_ja") or ""),
    }


def render_pretty(kpi: Dict[str, Any]) -> str:
    lines = [
        f"generated_utc: {kpi.get('generated_utc') or '-'}",
        "monthly_return_now: {0} (source={1})".format(
            kpi.get("monthly_return_now_text") or "n/a",
            kpi.get("monthly_return_now_source") or "-",
        ),
        "monthly_return_now_new_condition: {0} (source={1})".format(
            kpi.get("monthly_return_now_new_condition_text") or "n/a",
            kpi.get("monthly_return_now_new_condition_source") or "-",
        ),
        "monthly_return_now_all: {0} (source={1})".format(
            kpi.get("monthly_return_now_all_text") or "n/a",
            kpi.get("monthly_return_now_all_source") or "-",
        ),
        "rolling_30d_monthly_return: {0} (source={1})".format(
            kpi.get("rolling_30d_monthly_return_text") or "n/a",
            kpi.get("rolling_30d_monthly_return_source") or "-",
        ),
        f"rolling_30d_resolved_trades: {kpi.get('rolling_30d_resolved_trades')}",
        "realized_30d_gate: {0} ({1}, {2})".format(
            kpi.get("realized_30d_gate_decision") or "-",
            kpi.get("realized_30d_gate_decision_3stage") or "-",
            kpi.get("realized_30d_gate_decision_3stage_label_ja") or "-",
        ),
    ]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Report canonical no-longshot monthly return from strategy register snapshot."
    )
    p.add_argument("--snapshot-json", default="logs/strategy_register_latest.json", help="Snapshot JSON path.")
    p.add_argument("--json", action="store_true", help="Print JSON object to stdout.")
    p.add_argument("--value-only", action="store_true", help="Print only monthly_return_now_text.")
    p.add_argument(
        "--expect-source-prefix",
        default="",
        help="If provided, fail when monthly_return_now_source does not start with this prefix.",
    )
    p.add_argument(
        "--min-resolved-trades",
        type=int,
        default=0,
        help="Fail when rolling_30d_resolved_trades is lower than this value.",
    )
    p.add_argument(
        "--fail-on-stage-not-final",
        action="store_true",
        help="Fail when realized_30d_gate.decision_3stage is not READY_FINAL.",
    )
    p.add_argument("--out-json", default="", help="Optional JSON file output path.")
    args = p.parse_args()

    snapshot_path = resolve_repo_path(str(args.snapshot_json), "logs/strategy_register_latest.json")
    if not snapshot_path.exists():
        print(f"[report-no-longshot-monthly] snapshot missing: {snapshot_path}", file=sys.stderr)
        return 1

    try:
        payload = load_json(snapshot_path)
    except Exception as e:
        print(f"[report-no-longshot-monthly] failed to read snapshot: {e}", file=sys.stderr)
        return 1

    kpi = build_kpi(payload)

    source = str(kpi.get("monthly_return_now_source") or "")
    if args.expect_source_prefix:
        prefix = str(args.expect_source_prefix)
        if not source.startswith(prefix):
            print(
                "[report-no-longshot-monthly] source mismatch: expected prefix "
                f"'{prefix}', got '{source or '-'}'",
                file=sys.stderr,
            )
            return 2

    resolved_trades = _as_int(kpi.get("rolling_30d_resolved_trades"), 0)
    if resolved_trades < int(args.min_resolved_trades):
        print(
            "[report-no-longshot-monthly] resolved trades below threshold: "
            f"{resolved_trades} < {int(args.min_resolved_trades)}",
            file=sys.stderr,
        )
        return 3

    if args.fail_on_stage_not_final:
        stage = str(kpi.get("realized_30d_gate_decision_3stage") or "")
        if stage != "READY_FINAL":
            print(
                "[report-no-longshot-monthly] stage not final: "
                f"{stage or '-'} != READY_FINAL",
                file=sys.stderr,
            )
            return 4

    if args.out_json:
        out_path = resolve_repo_path(str(args.out_json), str(args.out_json))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(kpi, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.value_only:
        print(str(kpi.get("monthly_return_now_text") or "n/a"))
        return 0

    if args.json:
        print(json.dumps(kpi, ensure_ascii=False, indent=2))
        return 0

    print(render_pretty(kpi))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
