from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import judge_simmer_ab_decision as mod


def _row(
    *,
    since: str,
    until: str,
    ts: str,
    data_sufficient: bool,
    base_turnover: float,
    cand_turnover: float,
    base_hold_sec: float,
    cand_hold_sec: float,
    base_expectancy: float,
    cand_expectancy: float,
    base_errors: int,
    cand_errors: int,
    base_halts: int,
    cand_halts: int,
) -> dict:
    return {
        "since": since,
        "until": until,
        "ts": ts,
        "data_sufficient": data_sufficient,
        "decision": "PASS" if data_sufficient else "INSUFFICIENT",
        "baseline": {
            "turnover_per_day": base_turnover,
            "median_hold_sec": base_hold_sec,
            "expectancy_est": base_expectancy,
            "errors": base_errors,
            "halts": base_halts,
        },
        "candidate": {
            "turnover_per_day": cand_turnover,
            "median_hold_sec": cand_hold_sec,
            "expectancy_est": cand_expectancy,
            "errors": cand_errors,
            "halts": cand_halts,
        },
    }


def test_judge_provisional_go_when_all_conditions_pass():
    rows = [
        _row(
            since="2026-03-10 00:00:00",
            until="2026-03-11 00:00:00",
            ts="2026-03-11 00:05:00",
            data_sufficient=True,
            base_turnover=10.0,
            cand_turnover=12.0,
            base_hold_sec=60.0,
            cand_hold_sec=40.0,
            base_expectancy=1.0,
            cand_expectancy=1.0,
            base_errors=2,
            cand_errors=1,
            base_halts=1,
            cand_halts=1,
        ),
        _row(
            since="2026-03-11 00:00:00",
            until="2026-03-12 00:00:00",
            ts="2026-03-12 00:05:00",
            data_sufficient=True,
            base_turnover=8.0,
            cand_turnover=8.0,
            base_hold_sec=50.0,
            cand_hold_sec=35.0,
            base_expectancy=0.5,
            cand_expectancy=0.6,
            base_errors=1,
            cand_errors=1,
            base_halts=0,
            cand_halts=0,
        ),
    ]

    result = mod.judge(
        rows=rows,
        min_days=2,
        expectancy_ratio_threshold=0.9,
        decision_date=dt.date(2026, 3, 22),
        today=dt.date(2026, 3, 21),
    )

    assert result["decision"] == "GO"
    assert result["summary"]["decision_stage"] == "PROVISIONAL"
    assert result["summary"]["min_days_pass"] is True
    assert result["summary"]["interim_milestones"]["tentative_7d"]["decision"] == "PENDING"
    assert result["summary"]["interim_milestones"]["intermediate_14d"]["decision"] == "PENDING"
    assert result["conditions"]["turnover_day_ge_baseline"]["pass"] is True
    assert result["conditions"]["median_hold_shorter_than_baseline"]["pass"] is True
    assert result["conditions"]["expectancy_not_worse_than_10pct"]["pass"] is True
    assert result["conditions"]["error_halt_frequency_not_higher"]["pass"] is True
    assert str(result["recommended_action"]).startswith("PROVISIONAL_GO:")


def test_judge_final_no_go_when_min_days_not_met():
    rows = [
        _row(
            since="2026-03-20 00:00:00",
            until="2026-03-21 00:00:00",
            ts="2026-03-21 00:05:00",
            data_sufficient=True,
            base_turnover=10.0,
            cand_turnover=11.0,
            base_hold_sec=60.0,
            cand_hold_sec=30.0,
            base_expectancy=1.0,
            cand_expectancy=1.0,
            base_errors=0,
            cand_errors=0,
            base_halts=0,
            cand_halts=0,
        )
    ]

    result = mod.judge(
        rows=rows,
        min_days=3,
        expectancy_ratio_threshold=0.9,
        decision_date=dt.date(2026, 3, 22),
        today=dt.date(2026, 3, 22),
    )

    assert result["decision"] == "NO_GO"
    assert result["summary"]["decision_stage"] == "FINAL"
    assert result["summary"]["min_days_pass"] is False
    assert result["summary"]["final_decision_ready"] is False
    assert str(result["recommended_action"]).startswith(
        "FINAL_NO_GO: Decision date reached without minimum data days."
    )


def test_judge_final_no_go_when_condition_fails():
    rows = [
        _row(
            since="2026-03-15 00:00:00",
            until="2026-03-16 00:00:00",
            ts="2026-03-16 00:05:00",
            data_sufficient=True,
            base_turnover=10.0,
            cand_turnover=12.0,
            base_hold_sec=30.0,
            cand_hold_sec=50.0,
            base_expectancy=1.0,
            cand_expectancy=0.95,
            base_errors=0,
            cand_errors=0,
            base_halts=0,
            cand_halts=0,
        ),
        _row(
            since="2026-03-16 00:00:00",
            until="2026-03-17 00:00:00",
            ts="2026-03-17 00:05:00",
            data_sufficient=True,
            base_turnover=11.0,
            cand_turnover=11.0,
            base_hold_sec=35.0,
            cand_hold_sec=40.0,
            base_expectancy=0.9,
            cand_expectancy=0.9,
            base_errors=1,
            cand_errors=0,
            base_halts=0,
            cand_halts=0,
        ),
    ]

    result = mod.judge(
        rows=rows,
        min_days=2,
        expectancy_ratio_threshold=0.9,
        decision_date=dt.date(2026, 3, 22),
        today=dt.date(2026, 3, 22),
    )

    assert result["decision"] == "NO_GO"
    assert result["summary"]["decision_stage"] == "FINAL"
    assert result["summary"]["min_days_pass"] is True
    assert result["summary"]["final_decision_ready"] is True
    assert result["conditions"]["median_hold_shorter_than_baseline"]["pass"] is False
    assert str(result["recommended_action"]).startswith("FINAL_NO_GO: Re-tune candidate parameters")


def test_judge_prefers_data_sufficient_row_per_day():
    rows = [
        _row(
            since="2026-03-10 00:00:00",
            until="2026-03-11 00:00:00",
            ts="2026-03-11 00:30:00",
            data_sufficient=False,
            base_turnover=100.0,
            cand_turnover=0.0,
            base_hold_sec=10.0,
            cand_hold_sec=90.0,
            base_expectancy=1.0,
            cand_expectancy=-1.0,
            base_errors=0,
            cand_errors=100,
            base_halts=0,
            cand_halts=100,
        ),
        _row(
            since="2026-03-10 00:00:00",
            until="2026-03-11 00:00:00",
            ts="2026-03-11 00:05:00",
            data_sufficient=True,
            base_turnover=10.0,
            cand_turnover=10.0,
            base_hold_sec=60.0,
            cand_hold_sec=30.0,
            base_expectancy=1.0,
            cand_expectancy=1.0,
            base_errors=0,
            cand_errors=0,
            base_halts=0,
            cand_halts=0,
        ),
    ]

    result = mod.judge(
        rows=rows,
        min_days=1,
        expectancy_ratio_threshold=0.9,
        decision_date=dt.date(2026, 3, 22),
        today=dt.date(2026, 3, 21),
    )

    assert result["summary"]["daily_rows"] == 1
    assert result["summary"]["data_sufficient_days"] == 1
    assert result["decision"] == "GO"


def test_interim_milestones_reached_and_go_when_conditions_pass():
    rows = []
    for i in range(14):
        day0 = dt.datetime(2026, 3, 1) + dt.timedelta(days=i)
        day1 = day0 + dt.timedelta(days=1)
        rows.append(
            _row(
                since=day0.strftime("%Y-%m-%d %H:%M:%S"),
                until=day1.strftime("%Y-%m-%d %H:%M:%S"),
                ts=(day1 + dt.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
                data_sufficient=True,
                base_turnover=10.0,
                cand_turnover=12.0,
                base_hold_sec=60.0,
                cand_hold_sec=40.0,
                base_expectancy=1.0,
                cand_expectancy=1.0,
                base_errors=0,
                cand_errors=0,
                base_halts=0,
                cand_halts=0,
            )
        )

    result = mod.judge(
        rows=rows,
        min_days=25,
        expectancy_ratio_threshold=0.9,
        decision_date=dt.date(2026, 3, 22),
        today=dt.date(2026, 3, 15),
    )

    m7 = result["summary"]["interim_milestones"]["tentative_7d"]
    m14 = result["summary"]["interim_milestones"]["intermediate_14d"]
    assert m7["reached"] is True
    assert m14["reached"] is True
    assert m7["decision"] == "GO"
    assert m14["decision"] == "GO"


def test_cli_fail_on_final_no_go_exit_code(tmp_path: Path):
    history_path = tmp_path / "history.jsonl"
    rows = [
        _row(
            since="2026-03-20 00:00:00",
            until="2026-03-21 00:00:00",
            ts="2026-03-21 00:05:00",
            data_sufficient=False,
            base_turnover=0.0,
            cand_turnover=0.0,
            base_hold_sec=0.0,
            cand_hold_sec=0.0,
            base_expectancy=0.0,
            cand_expectancy=0.0,
            base_errors=0,
            cand_errors=0,
            base_halts=0,
            cand_halts=0,
        )
    ]
    history_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    script = Path(__file__).resolve().parents[1] / "scripts" / "judge_simmer_ab_decision.py"
    cmd = [
        sys.executable,
        str(script),
        "--history-file",
        str(history_path),
        "--min-days",
        "25",
        "--decision-date",
        "2026-03-22",
        "--today",
        "2026-03-22",
        "--fail-on-final-no-go",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 6
    assert "Exit Gate: FINAL_NO_GO" in proc.stdout
