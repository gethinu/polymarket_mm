from __future__ import annotations

import sys

import polymarket_clob_arb_realtime as clob_arb


def test_parse_args_env_override_when_cli_default(monkeypatch):
    monkeypatch.setenv("CLOBBOT_MIN_EDGE_CENTS", "2.5")
    monkeypatch.setattr(sys, "argv", ["clob_arb"])
    args = clob_arb.parse_args()
    assert args.min_edge_cents == 2.5


def test_parse_args_cli_flag_wins_over_env(monkeypatch):
    monkeypatch.setenv("CLOBBOT_MIN_EDGE_CENTS", "2.5")
    monkeypatch.setattr(sys, "argv", ["clob_arb", "--min-edge-cents", "1.25"])
    args = clob_arb.parse_args()
    assert args.min_edge_cents == 1.25


def test_parse_args_env_execute_does_not_autoconfirm(monkeypatch):
    # Safety: CLOBBOT_EXECUTE must NOT auto-synthesize the live confirmation token.
    monkeypatch.delenv("CLOBBOT_CONFIRM_LIVE", raising=False)
    monkeypatch.setenv("CLOBBOT_EXECUTE", "1")
    monkeypatch.setattr(sys, "argv", ["clob_arb"])
    args = clob_arb.parse_args()
    assert args.execute is True
    assert args.confirm_live != "YES"


def test_parse_args_env_confirm_live_explicit(monkeypatch):
    # Live confirm only via an explicit, separate env var (or CLI), never auto-filled.
    monkeypatch.setenv("CLOBBOT_EXECUTE", "1")
    monkeypatch.setenv("CLOBBOT_CONFIRM_LIVE", "YES")
    monkeypatch.setattr(sys, "argv", ["clob_arb"])
    args = clob_arb.parse_args()
    assert args.execute is True
    assert args.confirm_live == "YES"

