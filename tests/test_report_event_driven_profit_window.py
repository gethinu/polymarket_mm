from __future__ import annotations

import json
from pathlib import Path

import report_event_driven_profit_window as mod


def _ensure_dirs(root: Path) -> None:
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "llm").mkdir(parents=True, exist_ok=True)


def test_resolve_assumed_bankroll_uses_cli_arg(tmp_path: Path):
    _ensure_dirs(tmp_path)
    value, source = mod.resolve_assumed_bankroll_usd(tmp_path, 250.0)
    assert value == 250.0
    assert source == "cli_arg"


def test_resolve_assumed_bankroll_prefers_snapshot(tmp_path: Path):
    _ensure_dirs(tmp_path)
    payload = {"bankroll_policy": {"initial_bankroll_usd": 180.0}}
    (tmp_path / "logs" / "strategy_register_latest.json").write_text(
        json.dumps(payload, ensure_ascii=True),
        encoding="utf-8",
    )

    value, source = mod.resolve_assumed_bankroll_usd(tmp_path, None)
    assert value == 180.0
    assert "strategy_register_latest.json" in source


def test_resolve_assumed_bankroll_falls_back_to_strategy_md(tmp_path: Path):
    _ensure_dirs(tmp_path)
    (tmp_path / "docs" / "llm" / "STRATEGY.md").write_text(
        "\n".join(
            [
                "# Strategy",
                "",
                "## Bankroll Policy",
                "- Initial bankroll: $220",
                "",
                "## Adopted Strategies",
            ]
        ),
        encoding="utf-8",
    )

    value, source = mod.resolve_assumed_bankroll_usd(tmp_path, None)
    assert value == 220.0
    assert source == "docs/llm/STRATEGY.md:Bankroll Policy.initial_bankroll"


def test_resolve_assumed_bankroll_uses_hardcoded_default_when_missing(tmp_path: Path):
    _ensure_dirs(tmp_path)
    value, source = mod.resolve_assumed_bankroll_usd(tmp_path, None)
    assert value == mod.DEFAULT_ASSUMED_BANKROLL_USD
    assert source.startswith("hardcoded_default:")


def test_build_text_report_includes_assumed_bankroll_source():
    txt = mod.build_text_report(
        {
            "window": {"hours": 24},
            "summary": {"runs_window": 0, "episodes": 0, "event_count": 0, "ev_usd_p90": 0, "edge_cents_median": 0},
            "decision": {"decision": "NO_GO", "reasons": []},
            "selected_threshold": {"base_scenario": {"projected_monthly_return": 0}},
            "settings": {"assumed_bankroll_usd": 100.0, "assumed_bankroll_source": "cli_arg"},
        }
    )
    assert "Assumed bankroll=$100.00 (source=cli_arg)" in txt
