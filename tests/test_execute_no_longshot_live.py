from __future__ import annotations

import csv
from pathlib import Path

import execute_no_longshot_live as mod


def _write_screen_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "market_id",
                "question",
                "yes_price",
                "no_price",
                "liquidity_num",
                "volume_24h",
                "net_yield_per_day",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_main_refuses_live_without_confirm(monkeypatch):
    monkeypatch.setattr(mod.sys, "argv", ["execute_no_longshot_live.py", "--execute"])
    assert mod.main() == 2


def test_main_observe_preview_writes_state_and_no_positions(monkeypatch, tmp_path: Path):
    screen_csv = tmp_path / "logs" / "screen.csv"
    state_file = tmp_path / "logs" / "state.json"
    exec_log = tmp_path / "logs" / "exec.jsonl"
    run_log = tmp_path / "logs" / "run.log"
    _write_screen_csv(
        screen_csv,
        [
            {
                "market_id": "mkt1",
                "question": "Will X happen?",
                "yes_price": "0.18",
                "no_price": "0.82",
                "liquidity_num": "10000",
                "volume_24h": "5000",
                "net_yield_per_day": "0.001",
            }
        ],
    )

    def _fake_fetch_market(mid: str, timeout_sec: float):
        assert mid == "mkt1"
        return {
            "id": "mkt1",
            "outcomes": ["YES", "NO"],
            "clobTokenIds": ["tok_yes", "tok_no"],
            "outcomePrices": ["0.18", "0.82"],
        }

    monkeypatch.setattr(mod, "fetch_market_by_id", _fake_fetch_market)
    monkeypatch.setattr(
        mod.sys,
        "argv",
        [
            "execute_no_longshot_live.py",
            "--screen-csv",
            str(screen_csv),
            "--state-file",
            str(state_file),
            "--exec-log-file",
            str(exec_log),
            "--log-file",
            str(run_log),
            "--max-new-orders",
            "1",
        ],
    )
    rc = mod.main()
    assert rc == 0

    state = mod.read_json(state_file, {})
    assert isinstance(state, dict)
    assert isinstance(state.get("last_run"), dict)
    assert int(state["last_run"]["attempted"]) == 1
    assert int(state["last_run"]["submitted"]) == 0
    assert int(state["last_run"]["open_positions"]) == 0
    assert state.get("positions") == []
    assert exec_log.exists()

