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


def _fake_market(mid: str = "mkt1"):
    return {
        "id": mid,
        "outcomes": ["YES", "NO"],
        "clobTokenIds": ["tok_yes", "tok_no"],
        "outcomePrices": ["0.18", "0.82"],
    }


class _FakeClient:
    """Records create/post_order calls; post_order returns a controllable response."""

    def __init__(self, response):
        self._response = response
        self.created = []
        self.posted = []

    def create_order(self, args):
        self.created.append(args)
        return {"order": "built"}

    def post_order(self, order, orderType=None, post_only=False):
        self.posted.append({"orderType": orderType, "post_only": post_only})
        return self._response


def _live_argv(tmp_path: Path, screen_csv: Path, **extra) -> list[str]:
    argv = [
        "execute_no_longshot_live.py",
        "--execute",
        "--confirm-live",
        "YES",
        "--screen-csv",
        str(screen_csv),
        "--state-file",
        str(tmp_path / "logs" / "state.json"),
        "--exec-log-file",
        str(tmp_path / "logs" / "exec.jsonl"),
        "--log-file",
        str(tmp_path / "logs" / "run.log"),
    ]
    for k, v in extra.items():
        argv.extend(["--" + k.replace("_", "-"), str(v)])
    return argv


def test_main_refuses_live_without_confirm(monkeypatch):
    monkeypatch.setattr(mod.sys, "argv", ["execute_no_longshot_live.py", "--execute"])
    assert mod.main() == 2


def test_main_live_submits_and_records_position(monkeypatch, tmp_path: Path):
    screen_csv = tmp_path / "logs" / "screen.csv"
    _write_screen_csv(
        screen_csv,
        [{"market_id": "mkt1", "question": "Will X?", "yes_price": "0.18",
          "no_price": "0.82", "liquidity_num": "10000", "volume_24h": "5000",
          "net_yield_per_day": "0.001"}],
    )
    client = _FakeClient({"orderID": "oid123", "success": True})
    monkeypatch.setattr(mod, "fetch_market_by_id", lambda mid, timeout_sec: _fake_market(mid))
    monkeypatch.setattr(mod, "build_clob_client_from_env", lambda **kw: client)
    monkeypatch.setattr(
        mod.sys, "argv",
        _live_argv(tmp_path, screen_csv, max_new_orders=1, order_size_shares=5,
                   max_daily_notional_usd=10, max_entry_no_price=0.84),
    )
    assert mod.main() == 0
    assert len(client.posted) == 1
    state = mod.read_json(tmp_path / "logs" / "state.json", {})
    assert int(state["last_run"]["submitted"]) == 1
    assert float(state["daily_notional_usd"]) > 0.0
    positions = state.get("positions")
    assert len(positions) == 1
    assert positions[0]["status"] == "open"
    assert positions[0]["order_id"] == "oid123"
    # limit price must never exceed the configured max entry NO price
    assert float(positions[0]["entry_no_price"]) <= 0.84


def test_main_live_blocks_on_daily_notional_cap(monkeypatch, tmp_path: Path):
    screen_csv = tmp_path / "logs" / "screen.csv"
    _write_screen_csv(
        screen_csv,
        [{"market_id": "mkt1", "question": "Will X?", "yes_price": "0.18",
          "no_price": "0.82", "liquidity_num": "10000", "volume_24h": "5000",
          "net_yield_per_day": "0.001"}],
    )
    client = _FakeClient({"orderID": "oid123", "success": True})
    monkeypatch.setattr(mod, "fetch_market_by_id", lambda mid, timeout_sec: _fake_market(mid))
    monkeypatch.setattr(mod, "build_clob_client_from_env", lambda **kw: client)
    # cap (2.0) is below one order's notional (~0.83 * 5 = 4.15) => must NOT submit
    monkeypatch.setattr(
        mod.sys, "argv",
        _live_argv(tmp_path, screen_csv, max_new_orders=1, order_size_shares=5,
                   max_daily_notional_usd=2, max_entry_no_price=0.84),
    )
    assert mod.main() == 0
    assert len(client.posted) == 0
    state = mod.read_json(tmp_path / "logs" / "state.json", {})
    assert int(state["last_run"]["submitted"]) == 0
    assert float(state["daily_notional_usd"]) == 0.0
    assert state.get("positions") == []


def test_main_live_rejected_order_records_no_position(monkeypatch, tmp_path: Path):
    screen_csv = tmp_path / "logs" / "screen.csv"
    _write_screen_csv(
        screen_csv,
        [{"market_id": "mkt1", "question": "Will X?", "yes_price": "0.18",
          "no_price": "0.82", "liquidity_num": "10000", "volume_24h": "5000",
          "net_yield_per_day": "0.001"}],
    )
    client = _FakeClient({"error": "not enough liquidity"})
    monkeypatch.setattr(mod, "fetch_market_by_id", lambda mid, timeout_sec: _fake_market(mid))
    monkeypatch.setattr(mod, "build_clob_client_from_env", lambda **kw: client)
    monkeypatch.setattr(
        mod.sys, "argv",
        _live_argv(tmp_path, screen_csv, max_new_orders=1, order_size_shares=5,
                   max_daily_notional_usd=10, max_entry_no_price=0.84),
    )
    assert mod.main() == 0
    state = mod.read_json(tmp_path / "logs" / "state.json", {})
    assert int(state["last_run"]["submitted"]) == 0
    assert int(state["last_run"]["errors"]) == 1
    assert state.get("positions") == []
    assert float(state["daily_notional_usd"]) == 0.0


def test_main_resets_daily_notional_on_day_rollover(monkeypatch, tmp_path: Path):
    screen_csv = tmp_path / "logs" / "screen.csv"
    _write_screen_csv(screen_csv, [])
    state_file = tmp_path / "logs" / "state.json"
    mod.write_json(state_file, {"day": "2020-01-01", "daily_notional_usd": 999.0, "positions": []}, pretty=False)
    monkeypatch.setattr(
        mod.sys, "argv",
        [
            "execute_no_longshot_live.py",
            "--screen-csv", str(screen_csv),
            "--state-file", str(state_file),
            "--exec-log-file", str(tmp_path / "logs" / "exec.jsonl"),
            "--log-file", str(tmp_path / "logs" / "run.log"),
        ],
    )
    assert mod.main() == 0
    state = mod.read_json(state_file, {})
    assert float(state["daily_notional_usd"]) == 0.0


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

