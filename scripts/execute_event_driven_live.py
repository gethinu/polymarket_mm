#!/usr/bin/env python3
"""
Event-driven micro-live entry helper (observe-first).

Default behavior is observe-preview (no order submission). Live execution is
enabled only when both --execute and --confirm-live YES are specified.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from lib.clob_auth import build_clob_client_from_env
from polymarket_clob_arb_scanner import parse_json_string_field


GAMMA_API_BASE = "https://gamma-api.polymarket.com"
USER_AGENT = "Mozilla/5.0 (compatible; event-driven-live/1.0)"


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
    if not math.isfinite(n):
        return default
    return float(n)


def q_cent_up(v: float) -> float:
    if not math.isfinite(v) or v <= 0.0:
        return 0.0
    return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def q_size_down(v: float) -> float:
    if not math.isfinite(v) or v <= 0.0:
        return 0.0
    return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


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


def extract_token_id_for_side(market: dict, side: str) -> Optional[str]:
    token_ids = [str(x) for x in parse_json_string_field(market.get("clobTokenIds"))]
    outcomes = [str(x).strip().lower() for x in parse_json_string_field(market.get("outcomes"))]
    want = str(side or "").strip().lower()
    if want not in {"yes", "no"} or len(token_ids) < 2:
        return None
    if outcomes and len(outcomes) == len(token_ids):
        for i, out in enumerate(outcomes):
            if out == want:
                return token_ids[i]
    return token_ids[0] if want == "yes" else token_ids[1]


def extract_min_order_size(market: dict) -> float:
    for key in ("orderMinSize", "minimumOrderSize", "minOrderSize"):
        n = as_float(market.get(key))
        if n is not None and n > 0.0:
            return float(n)
    return 0.0


def parse_signal_ts_utc(raw: str) -> Optional[dt.datetime]:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def parse_iso_ts_utc(raw: str) -> Optional[dt.datetime]:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        ts = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc)


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


def iter_signal_rows(lines: Iterable[str]) -> Iterable[dict]:
    for line in lines:
        raw = str(line or "").strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        ts = parse_signal_ts_utc(str(obj.get("ts") or ""))
        market_id = str(obj.get("market_id") or "").strip()
        side = str(obj.get("side") or "").strip().upper()
        selected_price = as_float(obj.get("selected_price"))
        edge_cents = as_float(obj.get("edge_cents"))
        confidence = as_float(obj.get("confidence"))
        if ts is None or not market_id or side not in {"YES", "NO"}:
            continue
        if selected_price is None or edge_cents is None or confidence is None:
            continue
        yield {
            "ts": ts,
            "market_id": market_id,
            "question": str(obj.get("question") or "").strip(),
            "event_class": str(obj.get("event_class") or "").strip(),
            "side": side,
            "selected_price": float(selected_price),
            "edge_cents": float(edge_cents),
            "confidence": float(confidence),
            "liquidity_num": float(as_float(obj.get("liquidity_num"), 0.0) or 0.0),
            "volume_24h": float(as_float(obj.get("volume_24h"), 0.0) or 0.0),
        }


def load_signal_rows(path: Path, max_age_min: float) -> List[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    rows = list(iter_signal_rows(lines))
    if max_age_min <= 0.0:
        return rows
    cutoff = now_utc() - dt.timedelta(minutes=float(max_age_min))
    return [row for row in rows if row["ts"] >= cutoff]


def unique_latest_rows(rows: List[dict]) -> List[dict]:
    best: Dict[str, dict] = {}
    for row in rows:
        key = f"{row['market_id']}:{row['side']}"
        prev = best.get(key)
        if prev is None or row["ts"] > prev["ts"]:
            best[key] = row
    out = list(best.values())
    out.sort(
        key=lambda row: (
            float(row.get("edge_cents") or 0.0),
            float(row.get("confidence") or 0.0),
            float(row["ts"].timestamp()),
        ),
        reverse=True,
    )
    return out


def parse_state(raw: dict) -> dict:
    state = raw if isinstance(raw, dict) else {}
    positions = state.get("positions") if isinstance(state.get("positions"), list) else []
    recent_actions = state.get("recent_actions") if isinstance(state.get("recent_actions"), list) else []
    state["positions"] = [p for p in positions if isinstance(p, dict)]
    state["recent_actions"] = [x for x in recent_actions if isinstance(x, dict)]
    state["day"] = str(state.get("day") or "")
    state["daily_notional_usd"] = float(as_float(state.get("daily_notional_usd"), 0.0) or 0.0)
    return state


def prune_recent_actions(actions: List[dict], cooldown_min: float, ref_ts: Optional[dt.datetime] = None) -> List[dict]:
    if float(cooldown_min) <= 0.0:
        return []
    ref = ref_ts or now_utc()
    cutoff = ref - dt.timedelta(minutes=float(cooldown_min))
    out: List[dict] = []
    for action in actions:
        key = str(action.get("position_key") or "").strip()
        ts = parse_iso_ts_utc(str(action.get("ts_utc") or ""))
        if not key or ts is None or ts < cutoff:
            continue
        out.append(
            {
                "position_key": key,
                "ts_utc": ts.isoformat(),
                "status": str(action.get("status") or "").strip(),
            }
        )
    out.sort(key=lambda row: str(row.get("ts_utc") or ""))
    return out


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
        market_id = str(pos.get("market_id") or "").strip()
        side = str(pos.get("side") or "").strip().upper()
        if not market_id or side not in {"YES", "NO"}:
            continue
        market = fetch_market_by_id(market_id, timeout_sec=timeout_sec)
        if not isinstance(market, dict):
            continue
        prices = extract_yes_no_prices(market)
        if prices is None:
            continue
        yes_price, no_price = prices
        if side == "YES":
            side_wins = yes_price >= win_threshold and no_price <= lose_threshold
            side_loses = no_price >= win_threshold and yes_price <= lose_threshold
        else:
            side_wins = no_price >= win_threshold and yes_price <= lose_threshold
            side_loses = yes_price >= win_threshold and no_price <= lose_threshold
        if not side_wins and not side_loses:
            continue
        pos["status"] = "resolved"
        pos["resolved_utc"] = iso_now()
        pos["resolution"] = f"{side}_{'WIN' if side_wins else 'LOSE'}"
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


def build_order_plan(
    screen_price: float,
    live_price: float,
    max_entry_price: float,
    price_buffer_cents: float,
    max_stake_usd: float,
    min_order_size: float,
) -> Tuple[Optional[dict], str]:
    limit_price = q_cent_up(
        min(
            float(max_entry_price),
            max(float(screen_price), float(live_price)) + (float(price_buffer_cents) / 100.0),
        )
    )
    if limit_price <= 0.0 or limit_price >= 1.0:
        return None, "limit_price_invalid"
    if float(max_stake_usd) <= 0.0:
        return None, "max_stake_non_positive"
    size_cap = q_size_down(float(max_stake_usd) / float(limit_price))
    if size_cap <= 0.0:
        return None, "stake_too_small"
    min_size = max(0.0, float(min_order_size))
    if min_size > 0.0 and (size_cap + 1e-9) < min_size:
        return None, "min_order_size_exceeds_cap"
    size = size_cap
    notional = float(size) * float(limit_price)
    if notional <= 0.0:
        return None, "notional_non_positive"
    return {
        "limit_price": float(limit_price),
        "size_shares": float(size),
        "notional_usd": float(notional),
    }, ""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Execute event-driven micro-live entries from recent observe signals (observe-first).")
    p.add_argument("--signals-file", default="logs/event-driven-observe-signals.jsonl")
    p.add_argument("--state-file", default="logs/event_driven_live_state.json")
    p.add_argument("--exec-log-file", default="logs/event_driven_live_executions.jsonl")
    p.add_argument("--log-file", default="logs/event_driven_live.log")
    p.add_argument("--max-new-orders", type=int, default=1)
    p.add_argument("--max-stake-usd", type=float, default=5.0)
    p.add_argument("--max-daily-notional-usd", type=float, default=5.0)
    p.add_argument("--max-open-positions", type=int, default=2)
    p.add_argument("--signal-max-age-min", type=float, default=30.0)
    p.add_argument("--min-edge-cents", type=float, default=5.0)
    p.add_argument("--min-confidence", type=float, default=0.80)
    p.add_argument("--max-entry-price", type=float, default=0.35)
    p.add_argument("--price-buffer-cents", type=float, default=0.2)
    p.add_argument("--min-liquidity", type=float, default=5000.0)
    p.add_argument("--min-volume-24h", type=float, default=250.0)
    p.add_argument("--repeat-cooldown-min", type=float, default=360.0)
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
    if float(args.max_stake_usd) <= 0.0:
        print("max-stake-usd must be > 0")
        return 2
    if float(args.max_daily_notional_usd) < 0.0:
        print("max-daily-notional-usd must be >= 0")
        return 2
    if float(args.repeat_cooldown_min) < 0.0:
        print("repeat-cooldown-min must be >= 0")
        return 2
    if int(args.max_open_positions) < 1:
        print("max-open-positions must be >= 1")
        return 2
    if float(args.max_entry_price) <= 0.0 or float(args.max_entry_price) >= 1.0:
        print("max-entry-price must be in (0,1)")
        return 2

    signals_file = resolve_path(str(args.signals_file), "event-driven-observe-signals.jsonl")
    state_file = resolve_path(str(args.state_file), "event_driven_live_state.json")
    exec_log_file = resolve_path(str(args.exec_log_file), "event_driven_live_executions.jsonl")
    log_file = resolve_path(str(args.log_file), "event_driven_live.log")
    logger = Logger(log_file)

    state = parse_state(read_json(state_file, {}))
    today = day_key_utc()
    if state.get("day") != today:
        state["day"] = today
        state["daily_notional_usd"] = 0.0
    positions = state.get("positions") if isinstance(state.get("positions"), list) else []
    recent_actions = prune_recent_actions(
        state.get("recent_actions") if isinstance(state.get("recent_actions"), list) else [],
        cooldown_min=max(0.0, float(args.repeat_cooldown_min)),
    )
    recent_keys = {str(row.get("position_key") or "").strip() for row in recent_actions if str(row.get("position_key") or "").strip()}

    resolved_now = refresh_resolved_positions(
        positions=positions,
        timeout_sec=max(1.0, float(args.api_timeout_sec)),
        win_threshold=max(0.5, min(1.0, float(args.win_threshold))),
        lose_threshold=max(0.0, min(0.5, float(args.lose_threshold))),
    )

    open_positions = [p for p in positions if str(p.get("status") or "") == "open"]
    open_keys = {f"{str(p.get('market_id') or '').strip()}:{str(p.get('side') or '').strip().upper()}" for p in open_positions}
    open_count = len(open_positions)
    daily_notional = float(as_float(state.get("daily_notional_usd"), 0.0) or 0.0)

    rows = unique_latest_rows(load_signal_rows(signals_file, max_age_min=max(0.0, float(args.signal_max_age_min))))
    logger.info(
        f"start mode={'LIVE' if args.execute else 'observe-preview'} signals={len(rows)} "
        f"open_positions={open_count} daily_notional={daily_notional:.4f} "
        f"recent_keys={len(recent_keys)}"
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
        side = str(row.get("side") or "").strip().upper()
        side_key = f"{market_id}:{side}"
        if not market_id or side not in {"YES", "NO"}:
            continue
        if side_key in open_keys:
            continue
        if side_key in recent_keys:
            logger.info(f"skip market={market_id} side={side}: repeat_cooldown active")
            skipped += 1
            continue
        if float(row.get("edge_cents") or 0.0) < float(args.min_edge_cents):
            continue
        if float(row.get("confidence") or 0.0) < float(args.min_confidence):
            continue
        if float(row.get("selected_price") or 0.0) <= 0.0 or float(row.get("selected_price") or 0.0) > float(args.max_entry_price):
            continue
        if float(row.get("liquidity_num") or 0.0) < float(args.min_liquidity):
            continue
        if float(row.get("volume_24h") or 0.0) < float(args.min_volume_24h):
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
                    "side": side,
                    "status": "skip",
                    "reason": "market_fetch_failed",
                },
            )
            continue

        token_id = extract_token_id_for_side(market, side)
        prices = extract_yes_no_prices(market)
        if not token_id or prices is None:
            skipped += 1
            append_jsonl(
                exec_log_file,
                {
                    "ts_utc": iso_now(),
                    "mode": "LIVE" if args.execute else "observe",
                    "market_id": market_id,
                    "side": side,
                    "status": "skip",
                    "reason": "token_or_price_missing",
                },
            )
            continue

        yes_price, no_price = prices
        live_selected_price = yes_price if side == "YES" else no_price
        plan, reason = build_order_plan(
            screen_price=float(row.get("selected_price") or 0.0),
            live_price=float(live_selected_price),
            max_entry_price=float(args.max_entry_price),
            price_buffer_cents=float(args.price_buffer_cents),
            max_stake_usd=float(args.max_stake_usd),
            min_order_size=extract_min_order_size(market),
        )
        if plan is None:
            skipped += 1
            append_jsonl(
                exec_log_file,
                {
                    "ts_utc": iso_now(),
                    "mode": "LIVE" if args.execute else "observe",
                    "market_id": market_id,
                    "side": side,
                    "status": "skip",
                    "reason": reason,
                },
            )
            continue

        notional = float(plan["notional_usd"])
        if float(args.max_daily_notional_usd) > 0.0 and (daily_notional + notional) > float(args.max_daily_notional_usd):
            logger.info(
                f"skip market={market_id} side={side}: daily_notional cap "
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
            "event_class": str(row.get("event_class") or ""),
            "side": side,
            "token_id": token_id,
            "signal_ts_utc": row["ts"].isoformat(),
            "signal_edge_cents": float(row.get("edge_cents") or 0.0),
            "signal_confidence": float(row.get("confidence") or 0.0),
            "screen_selected_price": float(row.get("selected_price") or 0.0),
            "live_selected_price": float(live_selected_price),
            "limit_price": float(plan["limit_price"]),
            "size_shares": float(plan["size_shares"]),
            "max_stake_usd": float(args.max_stake_usd),
            "notional_usd": float(notional),
        }

        if not args.execute:
            event["status"] = "observe_preview"
            append_jsonl(exec_log_file, event)
            recent_actions.append({"position_key": side_key, "ts_utc": event["ts_utc"], "status": "observe_preview"})
            recent_keys.add(side_key)
            logger.info(
                f"preview market={market_id} side={side} px={float(plan['limit_price']):.3f} "
                f"size={float(plan['size_shares']):.2f} notional={notional:.4f}"
            )
            continue

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            order = client.create_order(
                OrderArgs(
                    token_id=str(token_id),
                    price=float(plan["limit_price"]),
                    size=float(plan["size_shares"]),
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
                logger.info(f"order rejected market={market_id} side={side}")
                errors += 1
                continue

            entry = {
                "position_id": f"{market_id}:{side}",
                "market_id": market_id,
                "side": side,
                "question": str(row.get("question") or ""),
                "event_class": str(row.get("event_class") or ""),
                "token_id": str(token_id),
                "entry_utc": iso_now(),
                "entry_day": today,
                "entry_price": float(plan["limit_price"]),
                "size_shares": float(plan["size_shares"]),
                "max_stake_usd": float(args.max_stake_usd),
                "notional_usd": float(notional),
                "edge_cents": float(row.get("edge_cents") or 0.0),
                "confidence": float(row.get("confidence") or 0.0),
                "order_id": str(order_id),
                "status": "open",
                "resolution": None,
                "resolved_utc": None,
                "resolved_yes_price": None,
                "resolved_no_price": None,
            }
            positions.append(entry)
            open_keys.add(side_key)
            recent_actions.append({"position_key": side_key, "ts_utc": event["ts_utc"], "status": "submitted"})
            recent_keys.add(side_key)
            open_count += 1
            daily_notional += float(notional)
            submitted += 1

            event["status"] = "submitted"
            event["order_id"] = str(order_id)
            append_jsonl(exec_log_file, event)
            logger.info(
                f"submitted market={market_id} side={side} order_id={str(order_id)[:16]} "
                f"px={float(plan['limit_price']):.3f} size={float(plan['size_shares']):.2f} "
                f"notional={notional:.4f}"
            )
        except Exception as e:
            event["status"] = "error"
            event["reason"] = f"{type(e).__name__}: {e}"
            append_jsonl(exec_log_file, event)
            logger.info(f"order exception market={market_id} side={side}: {type(e).__name__}: {e}")
            errors += 1

    state["positions"] = positions
    state["recent_actions"] = prune_recent_actions(recent_actions, cooldown_min=max(0.0, float(args.repeat_cooldown_min)))
    state["daily_notional_usd"] = float(daily_notional)
    state["generated_utc"] = iso_now()
    state["last_run"] = {
        "mode": "LIVE" if args.execute else "observe",
        "signals_file": str(signals_file),
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
