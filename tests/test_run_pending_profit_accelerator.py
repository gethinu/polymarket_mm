"""Tests for run_pending_profit_accelerator helper logic."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_pending_profit_accelerator as mod


def _base_args() -> argparse.Namespace:
    return argparse.Namespace(
        python_exe="python",
        lag15_run_seconds=900,
        lag15_poll_sec=1.0,
        lag15_summary_every_sec=30.0,
        lag15_metrics_sample_sec=2.0,
        lag15_shares=25.0,
        lag15_entry_edge_cents=2.0,
        lag15_alert_edge_cents=0.4,
        lag15_allowed_side_mode="both",
        lag15_regime_mode="prefer",
        lag15_regime_short_lookback_sec=1800.0,
        lag15_regime_long_lookback_sec=7200.0,
        lag15_regime_short_threshold_pct=0.0015,
        lag15_regime_long_threshold_pct=0.0030,
        lag15_regime_opposite_edge_penalty_cents=4.0,
        lag15_fair_model="hybrid",
        lag15_drift_trend_lookback_sec=300.0,
        lag15_drift_max_adjustment=0.10,
        lag15_drift_trend_reference_move_pct=0.0010,
        lag15_drift_open_gap_reference_pct=0.0015,
        lag15_entry_price_min=0.0,
        lag15_entry_price_max=1.0,
        lag15_up_entry_price_min=-1.0,
        lag15_up_entry_price_max=-1.0,
        lag15_down_entry_price_min=-1.0,
        lag15_down_entry_price_max=-1.0,
        lag15_require_aligned_momentum=False,
        lag15_require_reversal=False,
        lag15_reversal_lookback_sec=180.0,
        lag15_reversal_min_move_usd=15.0,
        lag15_min_remaining_sec=45.0,
        lag15_max_remaining_sec=0.0,
        lag15_max_spread_cents=8.0,
        lag15_min_ask_depth=5.0,
        yesno_window_minutes="5,15",
        yesno_windows_back=3,
        yesno_windows_forward=3,
        yesno_min_edge_cents=0.3,
        yesno_summary_every_sec=30.0,
        yesno_run_seconds=900,
        yesno_metrics_log_all_candidates=False,
        replay_miss_penalty=0.005,
        replay_stale_grace_sec=2.0,
        replay_stale_penalty_per_sec=0.001,
        replay_max_worst_stale_sec=10.0,
        replay_min_gap_ms_per_event=5000,
        replay_scales="0.1,0.25,0.5,0.75,1.0",
        replay_bootstrap_iters=2000,
        replay_require_threshold_pass=True,
        pretty=False,
    )


def test_build_lag15_observe_cmd_has_observe_only_flags():
    args = _base_args()
    p = mod.build_lag15_paths("20260307_000000")
    cmd = mod.build_lag15_observe_cmd(args, p)
    text = " ".join(cmd)
    assert "polymarket_btc15m_lag_observe.py" in text
    assert "--no-max-one-entry-per-window" in cmd
    assert "--execute" not in cmd
    assert str(p.metrics) in cmd


def test_classify_lag15_collect_more_when_trades_below_gate():
    payload = {
        "fee_adjusted": {
            "trade_count": 3,
            "net_pnl": 10.0,
            "max_drawdown": 1.2,
            "win_rate": 0.5,
        }
    }
    out = mod.classify_lag15(payload, min_trades=20)
    assert out["status"] == "COLLECT_MORE"
    assert out["trade_count"] == 3


def test_build_lag15_observe_cmd_includes_redesign_filters():
    args = _base_args()
    args.lag15_allowed_side_mode = "down"
    args.lag15_regime_mode = "strict"
    args.lag15_fair_model = "drift"
    args.lag15_entry_price_min = 0.05
    args.lag15_entry_price_max = 0.35
    args.lag15_up_entry_price_max = 0.70
    args.lag15_down_entry_price_max = 0.25
    args.lag15_require_aligned_momentum = True
    args.lag15_require_reversal = True
    args.lag15_reversal_lookback_sec = 240.0
    args.lag15_reversal_min_move_usd = 18.0
    args.lag15_max_remaining_sec = 540.0
    p = mod.build_lag15_paths("20260307_000000")
    cmd = mod.build_lag15_observe_cmd(args, p)
    text = " ".join(cmd)
    assert "--allowed-side-mode down" in text
    assert "--regime-mode strict" in text
    assert "--fair-model drift" in text
    assert "--entry-price-min 0.05" in text
    assert "--entry-price-max 0.35" in text
    assert "--up-entry-price-max 0.7" in text
    assert "--down-entry-price-max 0.25" in text
    assert "--require-aligned-momentum" in cmd
    assert "--require-reversal" in cmd
    assert "--max-remaining-sec 540.0" in text


def test_classify_yesno_candidate_when_growth_positive_and_samples_enough():
    payload = {
        "data": {"sample_count": 80},
        "kelly": {
            "full_fraction_estimate": 0.35,
            "scales": [
                {"scale_of_full_kelly": 0.25, "expected_log_growth": 0.0002},
                {"scale_of_full_kelly": 0.50, "expected_log_growth": 0.0009},
            ],
        },
    }
    out = mod.classify_yesno_replay(payload, min_samples=30)
    assert out["status"] == "CANDIDATE"
    assert out["sample_count"] == 80
    assert out["best_scale_of_full_kelly"] == 0.50


def test_build_overall_decision_prioritizes_error():
    decision, reason = mod.build_overall_decision(
        children_ok=False,
        lag15_status="CANDIDATE",
        yesno_status="CANDIDATE",
    )
    assert decision == "ERROR"
    assert "failed" in reason


def test_classify_step_level_treats_no_sample_replay_as_warn():
    assert mod.classify_step_level("yesno_replay", 2) == "warn"
    assert mod.classify_step_level("lag15_eval", 1) == "error"


def test_parse_args_default_run_tag_latest(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", ["run_pending_profit_accelerator.py"])
    args = mod.parse_args()
    assert args.run_tag == "latest"
    assert args.skip_lag15 is False


def test_parse_args_can_enable_lag15_with_no_skip(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_pending_profit_accelerator.py", "--no-skip-lag15"],
    )
    args = mod.parse_args()
    assert args.skip_lag15 is False


def test_build_yesno_replay_cmd_can_disable_threshold_requirement():
    args = _base_args()
    args.replay_require_threshold_pass = False
    p = mod.build_yesno_paths("20260307_000000")
    cmd = mod.build_yesno_replay_cmd(args, p)
    assert "--require-threshold-pass" not in cmd
