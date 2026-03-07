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
            "summary": {
                "runs_window": 0,
                "episodes": 0,
                "dte_filter": {
                    "enabled": True,
                    "max_dte_days": 14.0,
                    "episodes_after": 4,
                    "episodes_before": 20,
                    "dte_known_before": 18,
                    "dte_unknown_before": 2,
                },
                "event_count": 0,
                "ev_usd_p90": 0,
                "edge_cents_median": 0,
                "stake_usd_median_raw": 0,
                "stake_usd_median_effective": 0,
            },
            "decision": {
                "decision": "NO_GO",
                "decision_lockup_adjusted": "NO_GO",
                "projected_monthly_return_lockup_adjusted": 0.04,
                "reasons": [],
                "lockup_adjusted_reasons": [],
            },
            "selected_threshold": {
                "base_scenario": {"projected_monthly_return": 0, "capture_ratio": 0.35, "monthly_return_pct": 0.0},
                "hold_to_resolution_proxy": {
                    "hold_days_median": 20.0,
                    "hold_days_mean": 25.0,
                    "hold_days_p90": 40.0,
                    "capital_required_steady_state_usd": 250.0,
                    "opportunities_per_day_lockup_capped": 0.5,
                    "lockup_multiplier": 0.1,
                },
                "lockup_base_scenario": {"capture_ratio": 0.35, "monthly_return_pct": 4.0},
            },
            "settings": {
                "assumed_bankroll_usd": 100.0,
                "assumed_bankroll_source": "cli_arg",
                "max_stake_usd": 10.0,
            },
        }
    )
    assert "Assumed bankroll=$100.00 (source=cli_arg)" in txt
    assert "DTE filter: <= 14.00d | episodes kept=4/20 (known=18 unknown_drop=2)" in txt
    assert "Stake median raw/effective=$0.00/$0.00 | cap=$10.00" in txt
    assert "hold-to-resolution proxy: dte median=20.00d mean=25.00d p90=40.00d" in txt
    assert "lockup-adjusted base capture=35% => projected monthly=+4.00%" in txt
    assert "Lockup-adjusted decision: NO_GO | projected_monthly=+4.00%" in txt


def test_apply_stake_cap_to_ev_scales_ev_linearly():
    ev, stake = mod.apply_stake_cap_to_ev(18.0, 60.0, 10.0)
    assert ev == 3.0
    assert stake == 10.0


def test_hold_to_resolution_proxy_caps_entries_by_capital():
    out = mod.hold_to_resolution_proxy(
        effective_stakes=[5.0, 5.0],
        hold_days=[10.0, 20.0],
        opportunities_per_day=6.0,
        assumed_bankroll_usd=60.0,
    )
    assert out["coverage_count"] == 2
    assert out["capital_days_per_entry_mean"] == 75.0
    assert out["capital_required_steady_state_usd"] == 450.0
    assert out["opportunities_per_day_lockup_capped"] == 0.8
    assert out["capital_constrained"] is True


def test_filter_episodes_by_max_dte_keeps_only_shorter_known_rows():
    ref = mod.parse_ts("2026-03-07 00:00:00")
    episodes = [
        mod.Episode(
            key="m1:YES",
            market_id="m1",
            event_slug="e1",
            question="Q1",
            event_class="x",
            side="YES",
            start_ts=ref,
            end_ts=ref,
            samples=1,
            run_count=1,
            edge_cents_max=6.0,
            edge_cents_median=6.0,
            confidence_mean=0.9,
            selected_price_median=0.2,
            stake_usd_median=5.0,
            ev_usd_median=1.0,
            dte_median=7.0,
        ),
        mod.Episode(
            key="m2:YES",
            market_id="m2",
            event_slug="e2",
            question="Q2",
            event_class="x",
            side="YES",
            start_ts=ref,
            end_ts=ref,
            samples=1,
            run_count=1,
            edge_cents_max=6.0,
            edge_cents_median=6.0,
            confidence_mean=0.9,
            selected_price_median=0.2,
            stake_usd_median=5.0,
            ev_usd_median=1.0,
            dte_median=30.0,
        ),
        mod.Episode(
            key="m3:YES",
            market_id="m3",
            event_slug="e3",
            question="Q3",
            event_class="x",
            side="YES",
            start_ts=ref,
            end_ts=ref,
            samples=1,
            run_count=1,
            edge_cents_max=6.0,
            edge_cents_median=6.0,
            confidence_mean=0.9,
            selected_price_median=0.2,
            stake_usd_median=5.0,
            ev_usd_median=1.0,
            dte_median=float("nan"),
        ),
    ]
    out, meta = mod.filter_episodes_by_max_dte(episodes, 14.0)
    assert [e.market_id for e in out] == ["m1"]
    assert meta["enabled"] is True
    assert meta["episodes_before"] == 3
    assert meta["episodes_after"] == 1
    assert meta["dte_known_before"] == 2
    assert meta["dte_unknown_before"] == 1
