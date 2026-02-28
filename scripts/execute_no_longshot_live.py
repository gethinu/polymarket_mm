#!/usr/bin/env python3
"""
No-longshot live entry helper (observe-first).

Default behavior is observe-preview (no order submission). Live execution is
enabled only when both --execute and --confirm-live YES are specified.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sys
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from lib.clob_auth import build_clob_client_from_env
from polymarket_clob_arb_scanner import parse_json_string_field


GAMMA_API_BASE = "https://gamma-api.polymarket.com"
USER_AGENT = "Mozilla/5.0 (compatible; no-longshot-live/1.0)"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def day_key_utc() -> str:
    return now_utc().date().isoformat()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def logs_dir() -> Path:
    p = repo_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_path(raw: str, default_name: str) -> Path:
    s = str(raw or "").strip()
    if not s:
        return logs_dir() / default_name
    p = Path(s)
    if p.is_absolute():
        return p
    if len(p.parts) == 1:
        return logs_dir() / p.name
    return repo_root() / p


def as_float(v, default: Optional[float] = None) -> Optional[float]:
    try:
        n = float(v)
    except Exception:
        return default
    if n != n or n in (float("inf"), float("-inf")):
        return default
    return n


def q_cent_up(v: float) -> float:
    if not math.isfinite(v) or v <= 0:
        return 0.0
    return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def fetch_json(url: str, timeout_sec: float = 20.0, retries: int = 3) -> Optional[object]:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for i in range(max(1, retries)):
        try:
            with urlopen(req, timeout=timeout_sec) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError):
            if i >= retries - 1:
                return None
    return None


def fetch_market_by_id(market_id: str, timeout_sec: float) -> Optional[dict]:
    mid = str(market_id or "").strip()
    if not mid:
        return None
    url = f"{GAMMA_API_BASE}/markets?id={mid}"
    data = fetch_json(url, timeout_sec=timeout_sec, retries=3)
    if isinstance(data, list) and data:
        row = data[0]
        if isinstance(row, dict):
            return row
    return None


def extract_yes_no_prices(market: dict) -> Optional[Tuple[float, float]]:
    outcomes = [str(x).strip().lower() for x in parse_json_string_field(market.get("outcomes"))]
    prices_raw = parse_json_string_field(market.get("outcomePrices"))
    prices: List[float] = []
    for x in prices_raw:
        n = as_float(x)
        if n is None:
            return None
        prices.append(float(n))
    if len(outcomes) != 2 or len(prices) != 2:
        return None
    if "yes" not in outcomes or "no" not in outcomes:
        return None
    yes_i = outcomes.index("yes")
    return float(prices[yes_i]), float(prices[1 - yes_i])


def extract_no_token_id(market: dict) -> Optional[str]:
    token_ids = [str(x) for x in parse_json_string_field(market.get("clobTokenIds"))]
    outcomes = [str(x).strip().lower() for x in parse_json_string_field(market.get("outcomes"))]
    if len(token_ids) < 2:
        return None
    if outcomes and len(outcomes) == len(token_ids):
        for i, out in enumerate(outcomes):
            if out == "no":
                return token_ids[i]
    return token_ids[1]


class Logger:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def info(self, msg: str) -> None:
        line = f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        try:
            print(line)
        except UnicodeEncodeError:
            try:
                enc = getattr(sys.stdout, "encoding", None) or "utf-8"
                print(line.encode(enc, errors="replace").decode(enc, errors="replace"))
            except Exception:
                pass
        with self.log_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")


def read_json(path: Path, default_obj):
    if not path.exists():
        return default_obj
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default_obj
    return obj if isinstance(obj, type(default_obj)) else default_obj


def write_json(path: Path, obj, pretty: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        if pretty:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        else:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    tmp.replace(path)


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")


def load_screen_rows(path: Path) -> List[dict]:
    if not path.exists():
        return []
    rows: List[dict] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if isinstance(row, dict):
                    rows.append(row)
    except Exception:
        return []
    return rows


def unique_rows_by_market(rows: List[dict]) -> List[dict]:
    out: List[dict] = []
    seen = set()
    for row in rows:
        mid = str(row.get("market_id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(row)
    return out


def score_row(row: dict) -> Tuple[float, float, float]:
    net_day = as_float(row.get("net_yield_per_day"), 0.0) or 0.0
    vol24h = as_float(row.get("volume_24h"), 0.0) or 0.0
    liq = as_float(row.get("liquidity_num"), 0.0) or 0.0
    return float(net_day), float(vol24h), float(liq)


def parse_state(raw: dict) -> dict:
    state = raw if isinstance(raw, dict) else {}
    positions = state.get("positions") if isinstance(state.get("positions"), list) else []
    state["positions"] = [p for p in positions if isinstance(p, dict)]
    state["day"] = str(state.get("day") or "")
    state["daily_notional_usd"] = float(as_float(state.get("daily_notional_usd"), 0.0) or 0.0)
    return state


def refresh_resolved_positions(
    positions: List[dict],
    timeout_sec: float,
    win_threshold: float,
    lose_threshold: float,
) -> int:
    resolved_now = 0
    for pos in positions:
        if str(pos.get("status") or "") != "open":
            continue
        mid = str(pos.get("market_id") or "").strip()
        if not mid:
            continue
        market = fetch_market_by_id(mid, timeout_sec=timeout_sec)
        if not isinstance(market, dict):
            continue
        yn = extract_yes_no_prices(market)
        if yn is None:
            continue
        yes_price, no_price = yn
        no_wins = no_price >= win_threshold and yes_price <= lose_threshold
        yes_wins = yes_price >= win_threshold and no_price <= lose_threshold
        if not no_wins and not yes_wins:
            continue
        pos["status"] = "resolved"
        pos["resolved_utc"] = iso_now()
        pos["resolution"] = "NO_WIN" if no_wins else "NO_LOSE"
        pos["resolved_yes_price"] = float(yes_price)
        pos["resolved_no_price"] = float(no_price)
        resolved_now += 1
    return resolved_now


def extract_order_id(payload) -> str:
    if isinstance(payload, dict):
        for k in ("orderID", "order_id", "id"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in payload.values():
            oid = extract_order_id(v)
            if oid:
                return oid
    elif isinstance(payload, list):
        for x in payload:
            oid = extract_order_id(x)
            if oid:
                return oid
    return ""


def response_has_error(payload) -> bool:
    if isinstance(payload, dict):
        for k in ("error", "errorMsg", "message"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return True
        for v in payload.values():
            if response_has_error(v):
                return True
    elif isinstance(payload, list):
        for x in payload:
            if response_has_error(x):
                return True
    return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Execute no-longshot NO entries from screen CSV (observe-first).")
    p.add_argument("--screen-csv", default="logs/no_longshot_fast_screen_lowyes_latest.csv")
    p.add_argument("--state-file", default="logs/no_longshot_live_state.json")
    p.add_argument("--exec-log-file", default="logs/no_longshot_live_executions.jsonl")
    p.add_argument("--log-file", default="logs/no_longshot_live.log")
    p.add_argument("--max-new-orders", type=int, default=1)
    p.add_argument("--order-size-shares", type=float, default=5.0)
    p.add_argument("--max-daily-notional-usd", type=float, default=10.0)
    p.add_argument("--max-open-positions", type=int, default=10)
    p.add_argument("--max-entry-no-price", type=float, default=0.84)
    p.add_argument("--price-buffer-cents", type=float, default=0.2)
    p.add_argument("--min-liquidity", type=float, default=0.0)
    p.add_argument("--min-volume-24h", type=float, default=0.0)
    p.add_argument("--api-timeout-sec", type=float, default=20.0)
    p.add_argument("--win-threshold", type=float, default=0.99)
    p.add_argument("--lose-threshold", type=float, default=0.01)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--confirm-live", default="")
    p.add_argument("--clob-host", default="https://clob.polymarket.com")
    p.add_argument("--chain-id", type=int, default=137)
    p.add_argument("--pretty", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.execute and str(args.confirm_live or "") != "YES":
        print('Refusing live mode: pass --confirm-live YES with --execute')
        return 2
    if int(args.max_new_orders) < 0:
        print("max-new-orders must be >= 0")
        return 2
    if float(args.order_size_shares) <= 0:
        print("order-size-shares must be > 0")
        return 2
    if float(args.max_daily_notional_usd) < 0:
        print("max-daily-notional-usd must be >= 0")
        return 2
    if int(args.max_open_positions) < 1:
        print("max-open-positions must be >= 1")
        return 2
    if float(args.max_entry_no_price) <= 0 or float(args.max_entry_no_price) >= 1:
        print("max-entry-no-price must be in (0,1)")
        return 2

    screen_csv = resolve_path(str(args.screen_csv), "no_longshot_fast_screen_lowyes_latest.csv")
    state_file = resolve_path(str(args.state_file), "no_longshot_live_state.json")
    exec_log_file = resolve_path(str(args.exec_log_file), "no_longshot_live_executions.jsonl")
    log_file = resolve_path(str(args.log_file), "no_longshot_live.log")
    logger = Logger(log_file)

    state = parse_state(read_json(state_file, {}))
    today = day_key_utc()
    if state.get("day") != today:
        state["day"] = today
        state["daily_notional_usd"] = 0.0
    positions = state.get("positions") if isinstance(state.get("positions"), list) else []

    resolved_now = refresh_resolved_positions(
        positions=positions,
        timeout_sec=max(1.0, float(args.api_timeout_sec)),
        win_threshold=max(0.5, min(1.0, float(args.win_threshold))),
        lose_threshold=max(0.0, min(0.5, float(args.lose_threshold))),
    )

    open_positions = [p for p in positions if str(p.get("status") or "") == "open"]
    open_market_ids = {str(p.get("market_id") or "").strip() for p in open_positions}
    open_count = len(open_positions)
    daily_notional = float(as_float(state.get("daily_notional_usd"), 0.0) or 0.0)

    rows = unique_rows_by_market(load_screen_rows(screen_csv))
    rows.sort(key=score_row, reverse=True)
    logger.info(
        f"start mode={'LIVE' if args.execute else 'observe-preview'} rows={len(rows)} "
        f"open_positions={open_count} daily_notional={daily_notional:.4f}"
    )

    client = None
    if args.execute:
        try:
            client = build_clob_client_from_env(
                clob_host=str(args.clob_host),
                chain_id=int(args.chain_id),
                missing_env_message="Missing env for CLOB auth. Need PM_PRIVATE_KEY(_FILE/_DPAPI_FILE) and PM_FUNDER.",
                invalid_key_message="Invalid PM_PRIVATE_KEY format. Expected 64 hex chars (optionally prefixed with 0x).",
            )
        except Exception as e:
            logger.info(f"fatal: could not init clob client: {e}")
            return 2

    attempted = 0
    submitted = 0
    skipped = 0
    errors = 0

    for row in rows:
        if attempted >= int(args.max_new_orders):
            break
        if open_count >= int(args.max_open_positions):
            break

        market_id = str(row.get("market_id") or "").strip()
        if not market_id:
            continue
        if market_id in open_market_ids:
            continue

        no_price_screen = as_float(row.get("no_price"), None)
        liq = as_float(row.get("liquidity_num"), 0.0) or 0.0
        vol = as_float(row.get("volume_24h"), 0.0) or 0.0
        if no_price_screen is None:
            continue
        if no_price_screen <= 0.0 or no_price_screen > float(args.max_entry_no_price):
            continue
        if liq < float(args.min_liquidity):
            continue
        if vol < float(args.min_volume_24h):
            continue

        market = fetch_market_by_id(market_id, timeout_sec=max(1.0, float(args.api_timeout_sec)))
        if not isinstance(market, dict):
            skipped += 1
            append_jsonl(
                exec_log_file,
                {
                    "ts_utc": iso_now(),
                    "mode": "LIVE" if args.execute else "observe",
                    "market_id": market_id,
                    "status": "skip",
                    "reason": "market_fetch_failed",
                },
            )
            continue

        no_token_id = extract_no_token_id(market)
        yn = extract_yes_no_prices(market)
        no_price_live = yn[1] if yn is not None else no_price_screen
        if not no_token_id:
            skipped += 1
            append_jsonl(
                exec_log_file,
                {
                    "ts_utc": iso_now(),
                    "mode": "LIVE" if args.execute else "observe",
                    "market_id": market_id,
                    "status": "skip",
                    "reason": "no_token_missing",
                },
            )
            continue

        limit_price = q_cent_up(
            min(
                float(args.max_entry_no_price),
                max(float(no_price_screen), float(no_price_live)) + (float(args.price_buffer_cents) / 100.0),
            )
        )
        if limit_price <= 0 or limit_price >= 1:
            skipped += 1
            continue

        size = round(float(args.order_size_shares), 2)
        notional = float(limit_price) * float(size)
        if float(args.max_daily_notional_usd) > 0 and (daily_notional + notional) > float(args.max_daily_notional_usd):
            logger.info(
                f"skip market={market_id}: daily_notional cap "
                f"{daily_notional:.4f}+{notional:.4f}>{float(args.max_daily_notional_usd):.4f}"
            )
            skipped += 1
            continue

        attempted += 1
        event = {
            "ts_utc": iso_now(),
            "mode": "LIVE" if args.execute else "observe",
            "market_id": market_id,
            "question": str(row.get("question") or ""),
            "token_id_no": no_token_id,
            "screen_no_price": float(no_price_screen),
            "live_no_price": float(no_price_live),
            "limit_price": float(limit_price),
            "size_shares": float(size),
            "notional_usd": float(notional),
        }

        if not args.execute:
            event["status"] = "observe_preview"
            append_jsonl(exec_log_file, event)
            logger.info(
                f"preview market={market_id} no={limit_price:.3f} size={size:.2f} "
                f"notional={notional:.4f}"
            )
            continue

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            order = client.create_order(
                OrderArgs(
                    token_id=str(no_token_id),
                    price=float(limit_price),
                    size=float(size),
                    side="BUY",
                )
            )
            resp = client.post_order(order, orderType=OrderType.FOK, post_only=False)
            order_id = extract_order_id(resp)
            has_error = response_has_error(resp)
            if has_error and not order_id:
                event["status"] = "error"
                event["reason"] = "order_rejected"
                event["response"] = str(resp)[:500]
                append_jsonl(exec_log_file, event)
                logger.info(f"order rejected market={market_id}")
                errors += 1
                continue

            entry = {
                "position_id": f"m{market_id}",
                "market_id": market_id,
                "question": str(row.get("question") or ""),
                "token_id_no": str(no_token_id),
                "entry_utc": iso_now(),
                "entry_day": today,
                "entry_no_price": float(limit_price),
                "size_shares": float(size),
                "notional_usd": float(notional),
                "order_id": str(order_id),
                "status": "open",
                "resolution": None,
                "resolved_utc": None,
                "resolved_yes_price": None,
                "resolved_no_price": None,
            }
            positions.append(entry)
            open_market_ids.add(market_id)
            open_count += 1
            daily_notional += float(notional)
            submitted += 1

            event["status"] = "submitted"
            event["order_id"] = str(order_id)
            append_jsonl(exec_log_file, event)
            logger.info(
                f"submitted market={market_id} order_id={str(order_id)[:16]} "
                f"no={limit_price:.3f} size={size:.2f} notional={notional:.4f}"
            )
        except Exception as e:
            event["status"] = "error"
            event["reason"] = f"{type(e).__name__}: {e}"
            append_jsonl(exec_log_file, event)
            logger.info(f"order exception market={market_id}: {type(e).__name__}: {e}")
            errors += 1

    state["positions"] = positions
    state["daily_notional_usd"] = float(daily_notional)
    state["generated_utc"] = iso_now()
    state["last_run"] = {
        "mode": "LIVE" if args.execute else "observe",
        "screen_csv": str(screen_csv),
        "attempted": int(attempted),
        "submitted": int(submitted),
        "skipped": int(skipped),
        "errors": int(errors),
        "resolved_now": int(resolved_now),
        "open_positions": int(sum(1 for p in positions if str(p.get("status") or "") == "open")),
        "daily_notional_usd": float(daily_notional),
    }
    write_json(state_file, state, pretty=bool(args.pretty))

    logger.info(
        f"done attempted={attempted} submitted={submitted} skipped={skipped} "
        f"errors={errors} resolved_now={resolved_now} open_positions={state['last_run']['open_positions']} "
        f"daily_notional={daily_notional:.4f}"
    )
    logger.info(f"state={state_file}")
    logger.info(f"exec_log={exec_log_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
