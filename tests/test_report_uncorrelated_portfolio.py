from __future__ import annotations

import argparse
import json
from pathlib import Path

import report_uncorrelated_portfolio as mod


def _patch_minimal_pipeline(monkeypatch, tmp_path: Path, strategy_register: dict) -> None:
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "_load_strategy_register", lambda _path: strategy_register)
    monkeypatch.setattr(
        mod,
        "_build_strategy_records",
        lambda strategy_ids, logs, register, min_realized_days_for_corr: ([], {}, {}),
    )
    monkeypatch.setattr(mod, "_pairwise", lambda strategy_ids, corr_series, min_overlap_days: [])
    monkeypatch.setattr(mod, "_recommend_min_set", lambda strategy_records, pairwise, threshold_abs: {"recommended_min_set": []})
    monkeypatch.setattr(mod, "_risk_proxy_for_pair", lambda pair_for_risk, corr_series: None)
    monkeypatch.setattr(mod, "_monthly_portfolio_estimate", lambda pair_for_risk, strategy_records: None)


def test_main_sets_explicit_scope_mode(monkeypatch, tmp_path: Path):
    _patch_minimal_pipeline(monkeypatch, tmp_path, strategy_register={})
    out_json = tmp_path / "logs" / "out_explicit.json"
    monkeypatch.setattr(
        mod.argparse.ArgumentParser,
        "parse_args",
        lambda self: argparse.Namespace(
            strategy_ids="alpha,beta",
            corr_threshold_abs=0.30,
            min_overlap_days=2,
            min_realized_days_for_correlation=7,
            date_yyyymmdd="20260227",
            out_json=str(out_json),
            memo_out="",
            no_memo=True,
            pretty=False,
        ),
    )

    rc = mod.main()
    assert rc == 0
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["strategy_ids"] == ["alpha", "beta"]
    assert payload["meta"]["strategy_scope_mode"] == "explicit_strategy_ids"
    assert "explicit strategy_ids" in payload["meta"]["strategy_scope_text"]


def test_main_sets_adopted_scope_mode_when_strategy_ids_omitted(monkeypatch, tmp_path: Path):
    strategy_register = {
        "strategy_register": {
            "entries": [
                {"strategy_id": "weather", "status": "ADOPTED"},
                {"strategy_id": "no_longshot", "status": "REVIEW"},
                {"strategy_id": "eventpair", "status": "ADOPTED"},
            ]
        }
    }
    _patch_minimal_pipeline(monkeypatch, tmp_path, strategy_register=strategy_register)
    out_json = tmp_path / "logs" / "out_adopted.json"
    monkeypatch.setattr(
        mod.argparse.ArgumentParser,
        "parse_args",
        lambda self: argparse.Namespace(
            strategy_ids="",
            corr_threshold_abs=0.30,
            min_overlap_days=2,
            min_realized_days_for_correlation=7,
            date_yyyymmdd="20260227",
            out_json=str(out_json),
            memo_out="",
            no_memo=True,
            pretty=False,
        ),
    )

    rc = mod.main()
    assert rc == 0
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["strategy_ids"] == ["weather", "eventpair"]
    assert payload["meta"]["strategy_scope_mode"] == "adopted_from_strategy_register"
    assert "ADOPTED observe-only strategies" in payload["meta"]["strategy_scope_text"]
