#!/usr/bin/env python3
"""
Realtime Polymarket weather basket arbitrage monitor.

Default behavior is observe-only.
Optional live execution can be enabled with --execute and explicit confirmation.

Data flow:
1) Get weather markets from Simmer import index.
2) Resolve full event markets on Polymarket (Gamma API).
3) Subscribe to CLOB market channel over WebSocket for all YES token IDs.
4) Maintain local top-of-book / asks and compute basket cost in realtime.
5) Emit alerts when net edge exceeds threshold.
6) Optional: submit FOK batch BUY orders via py-clob-client.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import math
import os
import re
import string
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import websockets

from polymarket_clob_arb_scanner import (
    OutcomeLeg,
    as_float,
    buckets_look_exhaustive,
    build_markets_from_simmer_weather,
    event_key_for_market,
    event_title_for_market,
    fetch_active_markets,
    fetch_gamma_event_by_slug,
    extract_yes_no_token_ids,
    extract_yes_token_id,
    fetch_simmer_weather_markets,
    is_weather_bucket_market,
    order_cost_for_shares,
    parse_bucket_bounds,
    parse_json_string_field,
)

WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
SIMMER_API_BASE = "https://api.simmer.markets"


def _env_str(name: str) -> str:
    return str(os.environ.get(name, "") or "").strip()


def _user_env_from_registry(name: str) -> str:
    """Best-effort read of HKCU\\Environment for cases where process env isn't populated (Task Scheduler quirks)."""
    if not sys.platform.startswith("win"):
        return ""
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            v, _t = winreg.QueryValueEx(k, name)
            return str(v or "").strip()
    except Exception:
        return ""


def _env_int(name: str) -> Optional[int]:
    v = _env_str(name)
    if not v:
        return None
    try:
        return int(float(v))
    except Exception:
        return None


def _env_float(name: str) -> Optional[float]:
    v = _env_str(name)
    if not v:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _env_bool(name: str) -> Optional[bool]:
    v = _env_str(name).lower()
    if not v:
        return None
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _post_json(url: str, payload: dict, timeout_sec: float = 5.0) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "clob-arb-monitor/1.0",
        },
        method="POST",
    )
    with urlopen(req, timeout=timeout_sec) as _:
        # Discord webhook may return 204 No Content.
        return


def maybe_notify_discord(logger: "Logger", message: str) -> None:
    """
    Optional notifications to a Discord webhook.

    Set one of:
      - CLOBBOT_DISCORD_WEBHOOK_URL
      - DISCORD_WEBHOOK_URL

    Optionally set:
      - CLOBBOT_DISCORD_MENTION (e.g. <@123...> or @here)
    """
    # Webhook URLs are secrets. Never print them (including indirectly via exception strings).
    url = (
        _env_str("CLOBBOT_DISCORD_WEBHOOK_URL")
        or _user_env_from_registry("CLOBBOT_DISCORD_WEBHOOK_URL")
        or _env_str("DISCORD_WEBHOOK_URL")
        or _user_env_from_registry("DISCORD_WEBHOOK_URL")
    )
    if not url:
        return

    mention = _env_str("CLOBBOT_DISCORD_MENTION")
    content = f"{mention} {message}".strip() if mention else message

    def _send():
        try:
            _post_json(url, {"content": content}, timeout_sec=5.0)
        except Exception as e:
            code = getattr(e, "code", None)
            if isinstance(code, int):
                logger.info(f"[{iso_now()}] notify(discord) failed: HTTP {code}")
            else:
                logger.info(f"[{iso_now()}] notify(discord) failed: {type(e).__name__}")

    # Never block the trading loop on external webhook IO.
    threading.Thread(target=_send, daemon=True).start()


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_unprotect(ciphertext: bytes) -> bytes:
    if not ciphertext:
        return b""
    if not sys.platform.startswith("win"):
        raise RuntimeError("DPAPI decrypt is only supported on Windows.")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    CryptUnprotectData = crypt32.CryptUnprotectData
    CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_DATA_BLOB),
    ]
    CryptUnprotectData.restype = ctypes.c_int

    LocalFree = kernel32.LocalFree
    LocalFree.argtypes = [ctypes.c_void_p]
    LocalFree.restype = ctypes.c_void_p

    buf = ctypes.create_string_buffer(ciphertext, len(ciphertext))
    in_blob = _DATA_BLOB(len(ciphertext), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DATA_BLOB()

    ok = CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            LocalFree(out_blob.pbData)


def _load_powershell_dpapi_securestring_file(path: str) -> str:
    """
    Decrypts a PowerShell `ConvertFrom-SecureString` output (DPAPI-backed) file.

    This avoids needing a plaintext `PM_PRIVATE_KEY` environment variable.
    """
    p = (path or "").strip()
    if not p:
        return ""
    try:
        raw = Path(p).read_text(encoding="ascii", errors="ignore").strip()
    except Exception:
        return ""

    # ConvertFrom-SecureString (DPAPI) emits a hex string like "01000000d08c9ddf...".
    hex_only = "".join(ch for ch in raw if ch in string.hexdigits)
    if len(hex_only) < 8 or (len(hex_only) % 2) != 0:
        return ""

    try:
        clear = _dpapi_unprotect(bytes.fromhex(hex_only))
    except Exception:
        return ""

    # PowerShell SecureString plaintext is UTF-16LE.
    try:
        return clear.decode("utf-16-le").rstrip("\x00").strip()
    except Exception:
        try:
            return clear.decode("utf-8", errors="ignore").strip()
        except Exception:
            return ""


def _load_plaintext_secret_file(path: str) -> str:
    """
    Load a plaintext secret from a file (first line), without ever printing it.

    Intended for Linux/VPS deployments where DPAPI isn't available.
    File should be chmod 600 and owned by the service user.
    """
    p = (path or "").strip()
    if not p:
        return ""
    try:
        raw = Path(p).read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""
    if not raw:
        return ""
    # Normalize / validate: accept 64-hex (with or without 0x prefix).
    s = raw.splitlines()[0].strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", s or ""):
        s = "0x" + s
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", s or ""):
        return ""
    return s


def _apply_env_overrides(args):
    # Only override when CLI left defaults intact; this keeps manual testing predictable.
    cli_tokens = list(sys.argv[1:])

    def _flag_explicit(attr: str) -> bool:
        flag = f"--{attr.replace('_', '-')}"
        for tok in cli_tokens:
            if tok == flag or tok.startswith(flag + "="):
                return True
        return False

    def maybe(attr: str, env: str, default, cast):
        raw = _env_str(env)
        if not raw:
            return
        if _flag_explicit(attr):
            return
        if getattr(args, attr) != default:
            return
        try:
            setattr(args, attr, cast(raw))
        except Exception:
            return

    maybe("run_seconds", "CLOBBOT_RUN_SECONDS", 0, lambda s: int(float(s)))
    maybe("summary_every_sec", "CLOBBOT_SUMMARY_EVERY_SEC", 0.0, float)
    maybe("universe", "CLOBBOT_UNIVERSE", "weather", str)
    maybe("shares", "CLOBBOT_SHARES", 5.0, float)
    maybe("min_edge_cents", "CLOBBOT_MIN_EDGE_CENTS", 1.0, float)
    # Polymarket fees vary by market; most markets have 0 taker fee and no additional settlement fee.
    # Keep this default at 0.0 and allow overriding via env/CLI if your venue configuration differs.
    maybe("winner_fee_rate", "CLOBBOT_WINNER_FEE_RATE", 0.0, float)
    maybe("fixed_cost", "CLOBBOT_FIXED_COST", 0.0, float)
    maybe("alert_cooldown_sec", "CLOBBOT_ALERT_COOLDOWN_SEC", 10.0, float)

    maybe("gamma_limit", "CLOBBOT_GAMMA_LIMIT", 500, lambda s: int(float(s)))
    maybe("gamma_offset", "CLOBBOT_GAMMA_OFFSET", 0, lambda s: int(float(s)))
    maybe("gamma_min_liquidity", "CLOBBOT_GAMMA_MIN_LIQUIDITY", 0.0, float)
    maybe("gamma_min_volume24hr", "CLOBBOT_GAMMA_MIN_VOLUME24HR", 0.0, float)
    maybe("gamma_scan_max_markets", "CLOBBOT_GAMMA_SCAN_MAX", 5000, lambda s: int(float(s)))
    maybe("gamma_max_days_to_end", "CLOBBOT_GAMMA_MAX_DAYS_TO_END", 0.0, float)
    maybe("gamma_score_halflife_days", "CLOBBOT_GAMMA_SCORE_HALFLIFE_DAYS", 30.0, float)
    maybe("gamma_include_regex", "CLOBBOT_GAMMA_INCLUDE_REGEX", "", str)
    maybe("gamma_exclude_regex", "CLOBBOT_GAMMA_EXCLUDE_REGEX", "", str)
    maybe("sports_live_prestart_min", "CLOBBOT_SPORTS_LIVE_PRESTART_MIN", 10.0, float)
    maybe("sports_live_postend_min", "CLOBBOT_SPORTS_LIVE_POSTEND_MIN", 30.0, float)

    maybe("btc_5m_windows_back", "CLOBBOT_BTC_5M_WINDOWS_BACK", 1, lambda s: int(float(s)))
    maybe("btc_5m_windows_forward", "CLOBBOT_BTC_5M_WINDOWS_FORWARD", 1, lambda s: int(float(s)))

    maybe("max_exec_per_day", "CLOBBOT_MAX_EXEC_PER_DAY", 20, lambda s: int(float(s)))
    maybe("max_notional_per_day", "CLOBBOT_MAX_NOTIONAL_PER_DAY", 200.0, float)
    maybe("max_open_orders", "CLOBBOT_MAX_OPEN_ORDERS", 0, lambda s: int(float(s)))
    maybe("max_consecutive_failures", "CLOBBOT_MAX_CONSEC_FAILURES", 3, lambda s: int(float(s)))
    maybe("daily_loss_limit_usd", "CLOBBOT_DAILY_LOSS_LIMIT_USD", 0.0, float)
    maybe("pnl_check_interval_sec", "CLOBBOT_PNL_CHECK_INTERVAL_SEC", 60.0, float)

    maybe("exec_slippage_bps", "CLOBBOT_EXEC_SLIPPAGE_BPS", 50.0, float)
    maybe("unwind_slippage_bps", "CLOBBOT_UNWIND_SLIPPAGE_BPS", 150.0, float)
    maybe("exec_cooldown_sec", "CLOBBOT_EXEC_COOLDOWN_SEC", 30.0, float)
    maybe("exec_max_attempts", "CLOBBOT_EXEC_MAX_ATTEMPTS", 2, lambda s: int(float(s)))
    maybe("exec_book_stale_sec", "CLOBBOT_EXEC_BOOK_STALE_SEC", 5.0, float)
    maybe("exec_backend", "CLOBBOT_EXEC_BACKEND", "auto", str)
    maybe("strategy", "CLOBBOT_STRATEGY", "both", str)
    maybe("max_legs", "CLOBBOT_MAX_LEGS", 0, lambda s: int(float(s)))

    maybe("simmer_venue", "CLOBBOT_SIMMER_VENUE", "polymarket", str)
    maybe("simmer_source", "CLOBBOT_SIMMER_SOURCE", "sdk:clob-arb", str)
    maybe("simmer_min_amount", "CLOBBOT_SIMMER_MIN_AMOUNT", 1.0, float)

    maybe("max_subscribe_tokens", "CLOBBOT_MAX_SUBSCRIBE_TOKENS", 0, lambda s: int(float(s)))
    maybe("min_eval_interval_ms", "CLOBBOT_MIN_EVAL_INTERVAL_MS", 0, lambda s: int(float(s)))
    maybe("max_markets_per_event", "CLOBBOT_MAX_MARKETS_PER_EVENT", 0, lambda s: int(float(s)))
    maybe("observe_notify_min_interval_sec", "CLOBBOT_OBSERVE_NOTIFY_MIN_INTERVAL_SEC", 30.0, float)

    maybe("log_file", "CLOBBOT_LOG_FILE", "", str)
    maybe("state_file", "CLOBBOT_STATE_FILE", "", str)

    execute = _env_bool("CLOBBOT_EXECUTE")
    if execute is True and not args.execute:
        args.execute = True
        # Match the existing PowerShell runner behavior (fully automated live mode).
        if not args.confirm_live:
            args.confirm_live = "YES"

    allow_best_only = _env_bool("CLOBBOT_ALLOW_BEST_ONLY")
    if allow_best_only is True and not getattr(args, "allow_best_only", False):
        args.allow_best_only = True

    sports_live_only = _env_bool("CLOBBOT_SPORTS_LIVE_ONLY")
    if sports_live_only is True and not getattr(args, "sports_live_only", False):
        args.sports_live_only = True

    notify_observe_signals = _env_bool("CLOBBOT_NOTIFY_OBSERVE_SIGNALS")
    if notify_observe_signals is True and not getattr(args, "notify_observe_signals", False):
        args.notify_observe_signals = True

    # Normalize a few option-like env vars (avoid crashing on bad values).
    if args.exec_backend not in {"auto", "clob", "simmer"}:
        args.exec_backend = "auto"
    if getattr(args, "universe", "weather") not in {"weather", "gamma-active", "btc-5m"}:
        args.universe = "weather"
    if args.strategy not in {"buckets", "yes-no", "event-pair", "both", "all"}:
        args.strategy = "both"

    return args


@dataclass
class Leg:
    market_id: str
    question: str
    label: str
    token_id: str
    simmer_market_id: str = ""
    side: str = "yes"


@dataclass
class EventBasket:
    key: str
    title: str
    legs: List[Leg]
    strategy: str = "buckets"
    # Metadata (primarily for gamma-active scoring/selection).
    market_id: str = ""
    event_id: str = ""
    event_slug: str = ""
    liquidity_num: float = 0.0
    volume24hr: float = 0.0
    spread: float = 0.0
    one_day_price_change: float = 0.0
    end_ms: Optional[int] = None
    # Market constraints (best-effort from Gamma metadata).
    min_order_size: float = 0.0
    price_tick_size: float = 0.0
    score: float = 0.0
    last_alert_ts: float = 0.0
    last_exec_ts: float = 0.0
    last_eval_ts: float = 0.0
    last_signature: str = ""


@dataclass
class LocalBook:
    asks: List[dict] = field(default_factory=list)
    bids: List[dict] = field(default_factory=list)
    best_ask: Optional[float] = None
    best_bid: Optional[float] = None
    # True when we had to synthesize a 1-level book from best_ask/best_bid updates.
    asks_synthetic: bool = False
    bids_synthetic: bool = False
    updated_at: float = 0.0


@dataclass
class Candidate:
    strategy: str
    event_key: str
    title: str
    shares_per_leg: float
    basket_cost: float
    payout_after_fee: float
    fixed_cost: float
    net_edge: float
    edge_pct: float
    leg_costs: List[Tuple[Leg, float]]


@dataclass
class RuntimeState:
    day: str
    executions_today: int = 0
    notional_today: float = 0.0
    consecutive_failures: int = 0
    halted: bool = False
    halt_reason: str = ""
    start_pnl_total: Optional[float] = None
    last_pnl_total: Optional[float] = None
    last_pnl_check_ts: float = 0.0


@dataclass
class RunStats:
    candidates_total: int = 0
    candidates_window: int = 0
    best_all: Optional[Candidate] = None
    best_window: Optional[Candidate] = None
    window_started_at: float = field(default_factory=lambda: time.time())
    last_summary_ts: float = field(default_factory=lambda: time.time())


def now_ts() -> float:
    return time.time()


def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def day_key_local() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def parse_iso_or_epoch_to_ms(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        x = float(value)
        if x <= 0:
            return None
        if x > 10_000_000_000:  # already ms
            return int(x)
        return int(x * 1000)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # numeric string
        try:
            return parse_iso_or_epoch_to_ms(float(s))
        except ValueError:
            pass
        # ISO format
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    return None


def gamma_first_event(m: dict) -> dict:
    events = m.get("events")
    if isinstance(events, list):
        for e in events:
            if isinstance(e, dict):
                return e
    return {}


def is_likely_sports_market(m: dict) -> bool:
    if m.get("sportsMarketType"):
        return True

    e0 = gamma_first_event(m)
    # Sports markets commonly expose these fields in Gamma event payloads.
    sports_keys = ("gameId", "startTime", "finishedTimestamp", "score", "period", "elapsed")
    if any(e0.get(k) not in (None, "") for k in sports_keys):
        return True

    slug = str(e0.get("slug") or m.get("slug") or "").lower()
    if slug.startswith(("nba-", "nfl-", "mlb-", "nhl-", "cbb-", "ncaa-", "epl-", "khl-", "soccer-")):
        return True

    q = str(m.get("question") or e0.get("title") or "").lower()
    return (" vs. " in q) or (" vs " in q)


def is_in_sports_live_window(m: dict, now_ms: int, prestart_min: float, postend_min: float) -> bool:
    if not is_likely_sports_market(m):
        return False

    e0 = gamma_first_event(m)
    if bool(e0.get("live")):
        return True
    if bool(e0.get("ended")):
        return False

    pre_ms = int(max(0.0, float(prestart_min or 0.0)) * 60_000)
    post_ms = int(max(0.0, float(postend_min or 0.0)) * 60_000)

    start_ms = parse_iso_or_epoch_to_ms(
        e0.get("startTime") or m.get("gameStartTime") or e0.get("startDate") or m.get("startDate")
    )
    finish_ms = parse_iso_or_epoch_to_ms(e0.get("finishedTimestamp") or m.get("closedTime"))
    end_ms = parse_iso_or_epoch_to_ms(e0.get("endDate") or m.get("endDate"))

    if start_ms is None:
        return False

    if finish_ms is not None:
        return (start_ms - pre_ms) <= now_ms <= (finish_ms + post_ms)
    if end_ms is not None:
        return (start_ms - pre_ms) <= now_ms <= (end_ms + post_ms)
    return (start_ms - pre_ms) <= now_ms <= (start_ms + post_ms)


class Logger:
    def __init__(self, log_file: Optional[str] = None):
        self.log_file = Path(log_file) if log_file else None
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, msg: str):
        if not self.log_file:
            return
        with self.log_file.open("a", encoding="utf-8", newline="\n") as f:
            f.write(msg + "\n")

    def info(self, msg: str):
        try:
            print(msg)
        except UnicodeEncodeError:
            enc = (getattr(sys.stdout, "encoding", None) or "utf-8")
            safe = str(msg).encode(enc, errors="replace").decode(enc, errors="replace")
            print(safe)
        self._append(msg)



def load_state(state_file: Path) -> RuntimeState:
    if state_file.exists():
        try:
            raw = json.loads(state_file.read_text(encoding="utf-8"))
            state = RuntimeState(**raw)
        except Exception:
            state = RuntimeState(day=day_key_local())
    else:
        state = RuntimeState(day=day_key_local())

    if state.day != day_key_local():
        state = RuntimeState(day=day_key_local())
    return state


def save_state(state_file: Path, state: RuntimeState):
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")


def sdk_request(api_key: str, method: str, endpoint: str, data: Optional[dict] = None) -> dict:
    url = f"{SIMMER_API_BASE}{endpoint}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        if method == "GET":
            req = Request(url, headers=headers, method="GET")
        else:
            body = json.dumps(data or {}).encode()
            req = Request(url, headers=headers, data=body, method=method)
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode() if e.fp else ""
        parsed = {}
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {}
        out = {
            "error": f"HTTP {e.code}: {body}",
            "_status": e.code,
        }
        if isinstance(parsed, dict):
            out["_body"] = parsed
        return out
    except URLError as e:
        return {"error": f"Connection error: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def fetch_simmer_portfolio(api_key: str) -> Optional[dict]:
    url = f"{SIMMER_API_BASE}/api/sdk/portfolio"
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
            if isinstance(data, dict):
                return data
            return None
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None


def fetch_simmer_settings(api_key: str) -> Optional[dict]:
    resp = sdk_request(api_key, "GET", "/api/sdk/settings")
    if isinstance(resp, dict) and not resp.get("error"):
        return resp
    return None


def fetch_simmer_positions(api_key: str) -> List[dict]:
    resp = sdk_request(api_key, "GET", "/api/sdk/positions")
    if not isinstance(resp, dict) or resp.get("error"):
        return []
    positions = resp.get("positions")
    if isinstance(positions, list):
        return [p for p in positions if isinstance(p, dict)]
    return []


def maybe_apply_daily_loss_guard(state: RuntimeState, args, logger: Logger) -> bool:
    """
    Returns True if run can continue, False if halted by drawdown guard.
    """
    if args.daily_loss_limit_usd <= 0:
        return True

    api_key = os.environ.get("SIMMER_API_KEY", "").strip()
    if not api_key:
        return True

    now = now_ts()
    if (now - state.last_pnl_check_ts) < args.pnl_check_interval_sec:
        return True

    state.last_pnl_check_ts = now
    portfolio = fetch_simmer_portfolio(api_key)
    if not portfolio:
        return True

    pnl_total = as_float(portfolio.get("pnl_total"), math.nan)
    if not math.isfinite(pnl_total):
        return True

    state.last_pnl_total = pnl_total
    if state.start_pnl_total is None:
        state.start_pnl_total = pnl_total
        logger.info(f"[{iso_now()}] guard: baseline pnl_total set to ${pnl_total:.2f}")
        return True

    drawdown = state.start_pnl_total - pnl_total
    if drawdown >= args.daily_loss_limit_usd:
        state.halted = True
        state.halt_reason = (
            f"Daily loss guard hit: drawdown ${drawdown:.2f} >= ${args.daily_loss_limit_usd:.2f}"
        )
        logger.info(f"[{iso_now()}] guard: HALT {state.halt_reason}")
        maybe_notify_discord(logger, f"CLOBBOT HALT: {state.halt_reason}")
        return False

    return True



def build_event_baskets(limit: int, min_outcomes: int, workers: int, strategy: str) -> List[EventBasket]:
    markets = build_markets_from_simmer_weather(limit=limit, workers=workers)
    weather = [m for m in markets if is_weather_bucket_market(m)]
    need_buckets = strategy in {"buckets", "both"}
    need_yes_no = strategy in {"yes-no", "both"}

    # Map Polymarket conditionId -> Simmer market UUID for SDK execution.
    simmer_rows = fetch_simmer_weather_markets(limit=max(limit, 500))
    condition_to_simmer_id: Dict[str, str] = {}
    for row in simmer_rows:
        cond = str(row.get("polymarket_id") or "").strip().lower()
        smid = str(row.get("id") or "").strip()
        if cond and smid:
            condition_to_simmer_id[cond] = smid

    baskets: List[EventBasket] = []

    if need_buckets:
        grouped: Dict[str, List[dict]] = {}
        for m in weather:
            k = event_key_for_market(m)
            grouped.setdefault(k, []).append(m)

        for k, ms in grouped.items():
            if len(ms) < min_outcomes:
                continue

            legs: List[Leg] = []
            shape_check_legs: List[OutcomeLeg] = []
            for m in ms:
                token_id = extract_yes_token_id(m)
                if not token_id:
                    continue
                label = str(m.get("groupItemTitle") or m.get("question") or m.get("slug") or "outcome")
                condition_id = str(m.get("conditionId") or "").strip().lower()
                leg = Leg(
                    market_id=str(m.get("id", "")),
                    question=str(m.get("question", "")),
                    label=label,
                    token_id=str(token_id),
                    simmer_market_id=condition_to_simmer_id.get(condition_id, ""),
                    side="yes",
                )
                legs.append(leg)
                shape_check_legs.append(
                    OutcomeLeg(
                        market_id=leg.market_id,
                        question=leg.question,
                        label=leg.label,
                        token_id=leg.token_id,
                        side=leg.side,
                        ask_cost=0.0,
                    )
                )

            if len(legs) < min_outcomes:
                continue
            if not buckets_look_exhaustive(shape_check_legs):
                continue

            baskets.append(
                EventBasket(
                    key=k,
                    title=event_title_for_market(ms[0]),
                    legs=legs,
                    strategy="buckets",
                )
            )

    if need_yes_no:
        for m in weather:
            yes_tid, no_tid = extract_yes_no_token_ids(m)
            if not yes_tid or not no_tid:
                continue

            market_id = str(m.get("id") or "").strip()
            if not market_id:
                continue

            question = str(m.get("question") or "weather market")
            condition_id = str(m.get("conditionId") or "").strip().lower()
            simmer_market_id = condition_to_simmer_id.get(condition_id, "")
            legs = [
                Leg(
                    market_id=market_id,
                    question=question,
                    label="YES",
                    token_id=str(yes_tid),
                    simmer_market_id=simmer_market_id,
                    side="yes",
                ),
                Leg(
                    market_id=market_id,
                    question=question,
                    label="NO",
                    token_id=str(no_tid),
                    simmer_market_id=simmer_market_id,
                    side="no",
                ),
            ]
            baskets.append(
                EventBasket(
                    key=f"yn:{market_id}",
                    title=f"{question} [YES+NO]",
                    legs=legs,
                    strategy="yes-no",
                )
            )

    return baskets


def build_gamma_yes_no_baskets(
    gamma_limit: int,
    gamma_offset: int,
    min_liquidity: float,
    min_volume24hr: float,
    scan_max_markets: int = 5000,
    max_days_to_end: float = 0.0,
    include_re: Optional[re.Pattern] = None,
    exclude_re: Optional[re.Pattern] = None,
    sports_live_only: bool = False,
    sports_live_prestart_min: float = 10.0,
    sports_live_postend_min: float = 30.0,
) -> List[EventBasket]:
    """
    Build a universe of generic YES+NO baskets from Polymarket Gamma active markets.

    This is NOT weather-specific and can greatly expand opportunities, but also increases
    competition and websocket message volume.
    """
    baskets: List[EventBasket] = []
    seen: Set[str] = set()
    now_ms = int(time.time() * 1000)
    max_end_ms: Optional[int] = None
    if float(max_days_to_end or 0.0) > 0:
        max_end_ms = now_ms + int(float(max_days_to_end) * 86400000.0)

    # Gamma markets are paginated by offset. We cap scanning to avoid slow startups
    # when filters are too strict.
    offset = max(0, int(gamma_offset or 0))
    want = max(0, int(gamma_limit or 0))
    scan_cap = max(0, int(scan_max_markets or 0))
    scanned = 0
    page_size = 500

    while len(baskets) < want and (scan_cap <= 0 or scanned < scan_cap):
        batch = page_size
        if scan_cap > 0:
            batch = min(batch, scan_cap - scanned)
            if batch <= 0:
                break

        markets = fetch_active_markets(limit=batch, offset=offset)
        if not markets:
            break
        offset += batch
        scanned += len(markets)

        for m in markets:
            if len(baskets) >= want:
                break

            # Do NOT skip "restricted" markets by default: Gamma often marks all active markets
            # restricted, and skipping would yield an empty universe. If you need to exclude them,
            # filter at the environment/config layer instead.
            if m.get("enableOrderBook") is False:
                continue
            # Fee-enabled markets (e.g., some short-dated crypto) require passing feeRateBps on orders.
            # For pure sum-to-one arbitrage we skip them by default.
            if m.get("feesEnabled") is True:
                continue
            if sports_live_only and not is_in_sports_live_window(
                m,
                now_ms=now_ms,
                prestart_min=sports_live_prestart_min,
                postend_min=sports_live_postend_min,
            ):
                continue

            liq = as_float(m.get("liquidityNum", m.get("liquidity", 0.0)), 0.0)
            vol24 = as_float(m.get("volume24hr", 0.0), 0.0)
            spread = as_float(m.get("spread", 0.0), 0.0)
            one_day_change = as_float(m.get("oneDayPriceChange", 0.0), 0.0)
            end_ms = parse_iso_or_epoch_to_ms(m.get("endDate") or m.get("endDateIso"))
            if max_end_ms is not None and end_ms is not None and end_ms > max_end_ms:
                continue
            if liq < float(min_liquidity or 0.0):
                continue
            if vol24 < float(min_volume24hr or 0.0):
                continue

            # For true sum-to-one arbitrage, require a binary market (exactly 2 outcomes).
            token_ids = m.get("clobTokenIds")
            outcomes = m.get("outcomes")
            token_list = parse_json_string_field(token_ids)
            outcomes_list = parse_json_string_field(outcomes)

            if len(token_list) != 2:
                continue
            if outcomes_list and len(outcomes_list) != 2:
                continue

            yes_tid, no_tid = token_list[0], token_list[1]
            if not yes_tid or not no_tid:
                continue

            market_id = str(m.get("id") or "").strip()
            if not market_id or market_id in seen:
                continue
            seen.add(market_id)

            question = str(m.get("question") or f"market {market_id}").strip()
            event_slug = str(gamma_first_event(m).get("slug") or "").strip()
            hay = f"{question}\n{event_slug}\n{market_id}"
            if include_re and not include_re.search(hay):
                continue
            if exclude_re and exclude_re.search(hay):
                continue
            label_a = "YES"
            label_b = "NO"
            if outcomes_list and len(outcomes_list) == 2:
                label_a = str(outcomes_list[0]).strip() or label_a
                label_b = str(outcomes_list[1]).strip() or label_b
            legs = [
                Leg(
                    market_id=market_id,
                    question=question,
                    label=label_a,
                    token_id=str(yes_tid),
                    side="yes",
                ),
                Leg(
                    market_id=market_id,
                    question=question,
                    label=label_b,
                    token_id=str(no_tid),
                    side="no",
                ),
            ]
            baskets.append(
                EventBasket(
                    key=f"yn:{market_id}",
                    title=f"{question} [YES+NO]",
                    legs=legs,
                    strategy="yes-no",
                    market_id=market_id,
                    event_id=str(gamma_first_event(m).get("id") or "").strip(),
                    event_slug=event_slug,
                    liquidity_num=float(liq),
                    volume24hr=float(vol24),
                    spread=float(spread),
                    one_day_price_change=float(one_day_change),
                    end_ms=end_ms,
                    min_order_size=as_float(m.get("orderMinSize"), 0.0),
                    price_tick_size=as_float(m.get("orderPriceMinTickSize"), 0.0),
                )
            )

    return baskets


def build_gamma_event_pair_baskets(
    gamma_limit: int,
    gamma_offset: int,
    min_liquidity: float,
    min_volume24hr: float,
    max_legs: int,
    scan_max_markets: int = 5000,
    max_days_to_end: float = 0.0,
    include_re: Optional[re.Pattern] = None,
    exclude_re: Optional[re.Pattern] = None,
) -> List[EventBasket]:
    """
    Build binary event pair baskets from Gamma active markets.

    Target structure:
      - one negRisk event has exactly two categorical outcomes (non-numeric labels)
      - each outcome is represented as its own YES/NO market

    For each such event we build:
      - YES+YES pair basket
      - NO+NO pair basket
    """
    grouped: Dict[str, List[dict]] = {}

    offset = max(0, int(gamma_offset or 0))
    want = max(0, int(gamma_limit or 0))
    scan_cap = max(0, int(scan_max_markets or 0))
    scanned = 0
    page_size = 500
    now_ms = int(time.time() * 1000)
    max_end_ms: Optional[int] = None
    if float(max_days_to_end or 0.0) > 0:
        max_end_ms = now_ms + int(float(max_days_to_end) * 86400000.0)

    while (scan_cap <= 0 or scanned < scan_cap):
        batch = page_size
        if scan_cap > 0:
            batch = min(batch, scan_cap - scanned)
            if batch <= 0:
                break

        markets = fetch_active_markets(limit=batch, offset=offset)
        if not markets:
            break
        offset += batch
        scanned += len(markets)

        for m in markets:
            if m.get("enableOrderBook") is False:
                continue
            if m.get("feesEnabled") is True:
                continue

            # Keep this strategy constrained to negRisk grouped markets.
            neg_risk_id = str(m.get("negRiskMarketID") or "").strip()
            if not neg_risk_id:
                continue

            token_list = parse_json_string_field(m.get("clobTokenIds"))
            outcomes_list = parse_json_string_field(m.get("outcomes"))
            if len(token_list) != 2:
                continue
            if outcomes_list and len(outcomes_list) != 2:
                continue

            label = str(m.get("groupItemTitle") or "").strip()
            if not label:
                continue
            label_lc = label.lower()
            # Keep this strategy on categorical binary outcomes; reject numeric/comparator-style labels.
            if re.search(r"\d", label_lc):
                continue
            if any(sym in label_lc for sym in ("<", ">", "%", "$", "°", "\"")):
                continue
            if re.search(r"\b(or more|or less|or below|or above|between|under|over|at least|at most)\b", label_lc):
                continue
            # Skip numeric bucket ladders (handled by the buckets strategy).
            if parse_bucket_bounds(label) is not None:
                continue

            yes_tid, no_tid = extract_yes_no_token_ids(m)
            if not yes_tid or not no_tid:
                continue

            grouped.setdefault(neg_risk_id, []).append(
                {
                    "market": m,
                    "label": label,
                    "yes_tid": str(yes_tid),
                    "no_tid": str(no_tid),
                }
            )

    baskets: List[EventBasket] = []
    for neg_risk_id, rows in grouped.items():
        if want > 0 and len(baskets) >= want:
            break

        by_label: Dict[str, dict] = {}
        for row in rows:
            label_key = str(row.get("label") or "").strip().lower()
            if not label_key:
                continue
            cur = by_label.get(label_key)
            if cur is None:
                by_label[label_key] = row
                continue
            # Prefer the row with deeper liquidity when duplicate labels appear.
            cur_liq = as_float((cur.get("market") or {}).get("liquidityNum", 0.0), 0.0)
            new_liq = as_float((row.get("market") or {}).get("liquidityNum", 0.0), 0.0)
            if new_liq > cur_liq:
                by_label[label_key] = row

        clean_rows = list(by_label.values())
        # This strategy is specifically for binary outcome pairs.
        if len(clean_rows) != 2:
            continue
        if max_legs > 0 and 2 > max_legs:
            continue

        clean_rows.sort(key=lambda x: str(x.get("label") or "").lower())

        first_market = (clean_rows[0].get("market") or {}) if clean_rows else {}
        e0 = ((first_market.get("events") or [{}])[0] or {}) if isinstance(first_market, dict) else {}
        event_slug = str(e0.get("slug") or "").strip()
        title = event_title_for_market(first_market) if isinstance(first_market, dict) else neg_risk_id
        labels_joined = " | ".join(str(r.get("label") or "") for r in clean_rows)
        hay = f"{title}\n{event_slug}\n{labels_joined}"
        if include_re and not include_re.search(hay):
            continue
        if exclude_re and exclude_re.search(hay):
            continue

        # Apply market-level viability filters after verifying full binary structure,
        # so we never accidentally treat a 3-way market as a 2-way pair.
        legs_meet_filters = True
        for row in clean_rows:
            m = row.get("market") or {}
            liq = as_float(m.get("liquidityNum", m.get("liquidity", 0.0)), 0.0)
            vol24 = as_float(m.get("volume24hr", 0.0), 0.0)
            if liq < float(min_liquidity or 0.0) or vol24 < float(min_volume24hr or 0.0):
                legs_meet_filters = False
                break
            end_ms = parse_iso_or_epoch_to_ms(m.get("endDate") or m.get("endDateIso"))
            # Require a valid non-stale end date for pair baskets.
            if end_ms is None:
                legs_meet_filters = False
                break
            if end_ms < (now_ms - 86400000):
                legs_meet_filters = False
                break
            if max_end_ms is not None and end_ms is not None and end_ms > max_end_ms:
                legs_meet_filters = False
                break
        if not legs_meet_filters:
            continue

        yes_legs: List[Leg] = []
        no_legs: List[Leg] = []
        liqs: List[float] = []
        vols: List[float] = []
        spreads: List[float] = []
        changes: List[float] = []
        min_sizes: List[float] = []
        tick_sizes: List[float] = []
        end_ms_vals: List[int] = []

        for row in clean_rows:
            m = row.get("market") or {}
            market_id = str(m.get("id") or "").strip()
            question = str(m.get("question") or "").strip()
            label = str(row.get("label") or "").strip()
            if not market_id or not label:
                continue

            yes_legs.append(
                Leg(
                    market_id=market_id,
                    question=question,
                    label=label,
                    token_id=str(row.get("yes_tid") or ""),
                    side="yes",
                )
            )
            no_legs.append(
                Leg(
                    market_id=market_id,
                    question=question,
                    label=label,
                    token_id=str(row.get("no_tid") or ""),
                    side="no",
                )
            )

            liqs.append(as_float(m.get("liquidityNum", m.get("liquidity", 0.0)), 0.0))
            vols.append(as_float(m.get("volume24hr", 0.0), 0.0))
            spreads.append(as_float(m.get("spread", 0.0), 0.0))
            changes.append(as_float(m.get("oneDayPriceChange", 0.0), 0.0))
            min_sizes.append(as_float(m.get("orderMinSize"), 0.0))
            tick_sizes.append(as_float(m.get("orderPriceMinTickSize"), 0.0))
            em = parse_iso_or_epoch_to_ms(m.get("endDate") or m.get("endDateIso"))
            if em:
                end_ms_vals.append(int(em))

        if len(yes_legs) != 2 or len(no_legs) != 2:
            continue

        event_id = str(e0.get("id") or "").strip()
        basket_end_ms = min(end_ms_vals) if end_ms_vals else None
        common_kwargs = {
            "market_id": neg_risk_id,
            "event_id": event_id,
            "event_slug": event_slug,
            "liquidity_num": float(min(liqs) if liqs else 0.0),
            "volume24hr": float(sum(vols) if vols else 0.0),
            "spread": float(max(spreads) if spreads else 0.0),
            "one_day_price_change": float(max((abs(x) for x in changes), default=0.0)),
            "end_ms": basket_end_ms,
            "min_order_size": float(max(min_sizes) if min_sizes else 0.0),
            "price_tick_size": float(max(tick_sizes) if tick_sizes else 0.0),
        }

        baskets.append(
            EventBasket(
                key=f"ey:{neg_risk_id}",
                title=f"{title} [YES+YES]",
                legs=yes_legs,
                strategy="event-yes",
                **common_kwargs,
            )
        )
        baskets.append(
            EventBasket(
                key=f"en:{neg_risk_id}",
                title=f"{title} [NO+NO]",
                legs=no_legs,
                strategy="event-no",
                **common_kwargs,
            )
        )

    return baskets


def build_gamma_bucket_baskets(
    gamma_limit: int,
    gamma_offset: int,
    min_liquidity: float,
    min_volume24hr: float,
    min_outcomes: int,
    max_legs: int,
    scan_max_markets: int = 5000,
    max_days_to_end: float = 0.0,
    include_re: Optional[re.Pattern] = None,
    exclude_re: Optional[re.Pattern] = None,
) -> List[EventBasket]:
    """
    Build a universe of bucket baskets (buy YES across all numeric buckets for one event)
    from Polymarket Gamma active markets.

    This targets "sum-to-one" arbitrage across mutually exclusive + (likely) exhaustive
    numeric ranges, e.g. "<250k ... 2m+". It skips categorical outcome sets.
    """
    baskets: List[EventBasket] = []

    offset = max(0, int(gamma_offset or 0))
    want = max(0, int(gamma_limit or 0))
    scan_cap = max(0, int(scan_max_markets or 0))
    scanned = 0
    page_size = 500
    now_ms = int(time.time() * 1000)
    max_end_ms: Optional[int] = None
    if float(max_days_to_end or 0.0) > 0:
        max_end_ms = now_ms + int(float(max_days_to_end) * 86400000.0)

    # Group markets by event key (questionID/negRiskMarketID/event id fallback).
    grouped: Dict[str, List[dict]] = {}

    while (scan_cap <= 0 or scanned < scan_cap):
        batch = page_size
        if scan_cap > 0:
            batch = min(batch, scan_cap - scanned)
            if batch <= 0:
                break

        markets = fetch_active_markets(limit=batch, offset=offset)
        if not markets:
            break
        offset += batch
        scanned += len(markets)

        for m in markets:
            if m.get("enableOrderBook") is False:
                continue
            if m.get("feesEnabled") is True:
                continue

            liq = as_float(m.get("liquidityNum", m.get("liquidity", 0.0)), 0.0)
            vol24 = as_float(m.get("volume24hr", 0.0), 0.0)
            if liq < float(min_liquidity or 0.0):
                continue
            if vol24 < float(min_volume24hr or 0.0):
                continue
            end_ms = parse_iso_or_epoch_to_ms(m.get("endDate") or m.get("endDateIso"))
            if max_end_ms is not None and end_ms is not None and end_ms > max_end_ms:
                continue

            question = str(m.get("question") or "").strip()
            event_slug = str(((m.get("events") or [{}])[0] or {}).get("slug") or "").strip()
            hay = f"{question}\n{event_slug}"
            if include_re and not include_re.search(hay):
                continue
            if exclude_re and exclude_re.search(hay):
                continue

            # Bucket legs are built from binary markets with a parseable groupItemTitle.
            token_list = parse_json_string_field(m.get("clobTokenIds"))
            outcomes_list = parse_json_string_field(m.get("outcomes"))
            if len(token_list) != 2:
                continue
            if outcomes_list and len(outcomes_list) != 2:
                continue

            label = str(m.get("groupItemTitle") or "").strip()
            if not label:
                continue
            if parse_bucket_bounds(label) is None:
                continue

            k = event_key_for_market(m)
            grouped.setdefault(k, []).append(m)

        # If the user asked for a bounded number of baskets, and we already have plenty of groups
        # that could qualify, stop scanning early to keep startup fast.
        if want > 0 and len(grouped) >= max(want * 4, want + 50):
            break

    # Build baskets from grouped markets.
    for k, ms in grouped.items():
        if want > 0 and len(baskets) >= want:
            break

        if len(ms) < int(min_outcomes or 0):
            continue

        # Cap legs at build-time to prevent huge subscriptions on local machines.
        if max_legs > 0 and len(ms) > max_legs:
            continue

        legs: List[Leg] = []
        shape_check_legs: List[OutcomeLeg] = []
        liqs: List[float] = []
        vols: List[float] = []
        spreads: List[float] = []
        changes: List[float] = []
        min_sizes: List[float] = []
        tick_sizes: List[float] = []
        end_ms: Optional[int] = None

        for m in ms:
            token_id = extract_yes_token_id(m)
            if not token_id:
                continue
            label = str(m.get("groupItemTitle") or "").strip()
            if not label or parse_bucket_bounds(label) is None:
                continue

            market_id = str(m.get("id") or "").strip()
            question = str(m.get("question") or "").strip()
            leg = Leg(
                market_id=market_id,
                question=question,
                label=label,
                token_id=str(token_id),
                side="yes",
            )
            legs.append(leg)
            shape_check_legs.append(
                OutcomeLeg(
                    market_id=market_id,
                    question=question,
                    label=label,
                    token_id=leg.token_id,
                    side=leg.side,
                    ask_cost=0.0,
                )
            )

            liqs.append(as_float(m.get("liquidityNum", m.get("liquidity", 0.0)), 0.0))
            vols.append(as_float(m.get("volume24hr", 0.0), 0.0))
            spreads.append(as_float(m.get("spread", 0.0), 0.0))
            changes.append(as_float(m.get("oneDayPriceChange", 0.0), 0.0))
            min_sizes.append(as_float(m.get("orderMinSize"), 0.0))
            tick_sizes.append(as_float(m.get("orderPriceMinTickSize"), 0.0))
            if end_ms is None:
                end_ms = parse_iso_or_epoch_to_ms(m.get("endDate") or m.get("endDateIso"))

        if len(legs) < int(min_outcomes or 0):
            continue
        if max_legs > 0 and len(legs) > max_legs:
            continue
        if not buckets_look_exhaustive(shape_check_legs):
            continue

        # Best-effort metadata: take from the first market's event payload.
        e0 = ((ms[0].get("events") or [{}])[0] or {}) if isinstance(ms[0], dict) else {}
        event_id = str(e0.get("id") or "").strip()
        event_slug = str(e0.get("slug") or "").strip()
        title = event_title_for_market(ms[0])

        baskets.append(
            EventBasket(
                key=k,
                title=title,
                legs=legs,
                strategy="buckets",
                market_id=event_id or str(ms[0].get("id") or "").strip(),
                event_id=event_id,
                event_slug=event_slug,
                liquidity_num=float(min(liqs) if liqs else 0.0),
                volume24hr=float(sum(vols) if vols else 0.0),
                spread=float(max(spreads) if spreads else 0.0),
                one_day_price_change=float(max((abs(x) for x in changes), default=0.0)),
                end_ms=end_ms,
                min_order_size=float(max(min_sizes) if min_sizes else 0.0),
                price_tick_size=float(max(tick_sizes) if tick_sizes else 0.0),
            )
        )

    return baskets


def build_btc_updown_5m_baskets(windows_back: int = 1, windows_forward: int = 1) -> List[EventBasket]:
    """
    Build a tiny rolling universe for Polymarket's BTC "Up or Down - 5 min" markets.

    This avoids scanning the entire Gamma /markets list (which may be large / reordered)
    by deterministically computing the event slugs (epoch start timestamps).

    Note: this is still *sum-to-one basket* logic. It will only trade if buying BOTH outcomes
    is cheaper than the fee-adjusted payout. It is not a momentum/scalping strategy.
    """
    back = max(0, int(windows_back or 0))
    fwd = max(0, int(windows_forward or 0))
    now_s = int(time.time())
    start_s = (now_s // 300) * 300

    baskets: List[EventBasket] = []
    seen: Set[str] = set()

    for i in range(-back, fwd + 1):
        slug = f"btc-updown-5m-{start_s + (i * 300)}"
        ev = fetch_gamma_event_by_slug(slug)
        if not isinstance(ev, dict):
            continue
        markets = ev.get("markets") or []
        if not isinstance(markets, list):
            continue

        for m in markets:
            if not isinstance(m, dict):
                continue
            if m.get("enableOrderBook") is False:
                continue

            token_list = parse_json_string_field(m.get("clobTokenIds"))
            outcomes_list = parse_json_string_field(m.get("outcomes"))
            if len(token_list) < 2:
                continue

            market_id = str(m.get("id") or "").strip()
            question = str(m.get("question") or ev.get("title") or slug).strip()
            title = str(ev.get("title") or question or slug).strip()
            end_ms = parse_iso_or_epoch_to_ms(m.get("endDate") or m.get("endDateIso") or ev.get("endDate"))

            legs: List[Leg] = []
            for idx, tid in enumerate(token_list):
                if not tid:
                    continue
                label = f"OUT{idx + 1}"
                if outcomes_list and idx < len(outcomes_list):
                    label = str(outcomes_list[idx]).strip() or label
                legs.append(
                    Leg(
                        market_id=market_id,
                        question=question,
                        label=label,
                        token_id=str(tid),
                        side=("yes" if idx == 0 else "no"),
                    )
                )

            if len(legs) < 2:
                continue

            key = f"slug:{slug}:{market_id}"
            if key in seen:
                continue
            seen.add(key)
            baskets.append(
                EventBasket(
                    key=key,
                    title=title,
                    legs=legs,
                    strategy="yes-no",
                    market_id=market_id,
                    event_id=str(ev.get("id") or "").strip(),
                    event_slug=str(ev.get("slug") or slug).strip(),
                    liquidity_num=as_float(m.get("liquidityNum", m.get("liquidity", 0.0)), 0.0),
                    volume24hr=as_float(m.get("volume24hr", 0.0), 0.0),
                    spread=as_float(m.get("spread", 0.0), 0.0),
                    one_day_price_change=abs(as_float(m.get("oneDayPriceChange", 0.0), 0.0)),
                    end_ms=end_ms,
                    min_order_size=as_float(m.get("orderMinSize"), 0.0),
                    price_tick_size=as_float(m.get("orderPriceMinTickSize"), 0.0),
                )
            )

    return baskets


def _q_cents_up(x: float) -> float:
    if not math.isfinite(x):
        return 0.0
    d = Decimal(str(x))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def _q_cents_down(x: float) -> float:
    if not math.isfinite(x):
        return 0.0
    d = Decimal(str(x))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_FLOOR))


def _estimate_exec_cost_clob(candidate: Candidate, slippage_bps: float) -> float:
    """
    Conservative cost estimate for live execution on the CLOB.

    Polymarket's "market buy" path rejects maker (USDC) amounts with >2 decimals. When we submit FOK
    buys, we therefore quantize *effective* cost to cents by rounding limit prices up to 2 decimals.
    """
    slip = max(0.0, float(slippage_bps or 0.0)) / 10000.0
    total = 0.0
    for _, observed_cost in candidate.leg_costs:
        est_price = float(observed_cost) / max(float(candidate.shares_per_leg), 1e-9)
        px = min(0.999, est_price * (1.0 + slip))
        px = max(0.001, _q_cents_up(px))
        total += px * float(candidate.shares_per_leg)
    return total


def _clamp01(x: float) -> float:
    if not math.isfinite(x):
        return 0.0
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    return float(x)


def score_gamma_basket(basket: EventBasket, now_ms: int, args) -> float:
    """
    Heuristic scoring for gamma-active selection.

    Goal: maximize "chance to matter" per subscribed token under local resource constraints:
      - prefer markets that resolve sooner (capital recycle, more repricing events)
      - prefer enough liquidity/volume to fill
      - prefer some spread/volatility (more dislocations)

    Score is relative; we only use it for ranking/selection.
    """
    # Time-to-end: exponential decay (short-dated markets are best for small capital).
    days_to_end = 365.0
    if basket.end_ms:
        raw_days = float(basket.end_ms - now_ms) / 86400000.0
        # Gamma occasionally returns "active" markets whose endDate is far in the past (stale/unresolved).
        # Don't rank these highly; they can lock capital indefinitely.
        if raw_days < -1.0:
            return -1.0
        days_to_end = max(0.0, raw_days)

    max_days = float(getattr(args, "gamma_max_days_to_end", 0.0) or 0.0)
    if max_days > 0 and days_to_end > max_days:
        return -1.0

    halflife = float(getattr(args, "gamma_score_halflife_days", 30.0) or 30.0)
    halflife = max(1.0, halflife)
    time_score = math.exp(-days_to_end / halflife)

    liq = max(0.0, float(basket.liquidity_num or 0.0))
    vol = max(0.0, float(basket.volume24hr or 0.0))
    spr = max(0.0, float(basket.spread or 0.0))
    volat = abs(float(basket.one_day_price_change or 0.0))

    # Soft-normalize signals to 0..1. Constants are empirical and safe defaults.
    liq_score = _clamp01(math.log1p(liq) / math.log1p(50_000.0))
    vol_score = _clamp01(math.log1p(vol) / math.log1p(50_000.0))
    spread_score = _clamp01(spr / 0.02)  # ~2c spread hits 1.0
    volat_score = _clamp01(volat / 0.02)  # ~2c daily move hits 1.0

    base = 0.40 * liq_score + 0.35 * vol_score + 0.15 * spread_score + 0.10 * volat_score
    return float(base * time_score)



def update_book_from_snapshot(item: dict, books: Dict[str, LocalBook]) -> Optional[str]:
    token_id = str(item.get("asset_id") or item.get("assetId") or "")
    if not token_id:
        return None

    book = books.setdefault(token_id, LocalBook())
    if isinstance(item.get("asks"), list):
        book.asks = item["asks"]
        book.asks_synthetic = False
        if book.asks:
            best_ask = min((as_float(x.get("price"), math.inf) for x in book.asks), default=math.inf)
            book.best_ask = best_ask if math.isfinite(best_ask) else None
    if isinstance(item.get("bids"), list):
        book.bids = item["bids"]
        book.bids_synthetic = False
        if book.bids:
            best_bid = max((as_float(x.get("price"), 0.0) for x in book.bids), default=0.0)
            book.best_bid = best_bid if best_bid > 0 else None

    if item.get("best_ask") is not None:
        b = as_float(item.get("best_ask"), math.inf)
        if math.isfinite(b):
            book.best_ask = b
            if not book.asks:
                book.asks = [{"price": b, "size": 1e9}]
                book.asks_synthetic = True
    if item.get("best_bid") is not None:
        b = as_float(item.get("best_bid"), 0.0)
        if b > 0:
            book.best_bid = b
            if not book.bids:
                book.bids = [{"price": b, "size": 1e9}]
                book.bids_synthetic = True

    book.updated_at = now_ts()
    return token_id



def extract_book_items(payload) -> List[dict]:
    out: List[dict] = []

    def add_if_book(d):
        if isinstance(d, dict) and (d.get("asset_id") or d.get("assetId")):
            if "asks" in d or "bids" in d or "best_ask" in d or "best_bid" in d:
                out.append(d)

    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            event_type = str(item.get("event_type", "")).lower()
            if event_type in {"book", "price_change", "tick_size_change"}:
                add_if_book(item)
                for key in ("changes", "price_changes", "items"):
                    nested = item.get(key)
                    if isinstance(nested, list):
                        for n in nested:
                            add_if_book(n)
            else:
                add_if_book(item)
    elif isinstance(payload, dict):
        add_if_book(payload)
        for key in ("changes", "price_changes", "items", "books"):
            nested = payload.get(key)
            if isinstance(nested, list):
                for n in nested:
                    add_if_book(n)

    return out



def compute_candidate(
    basket: EventBasket,
    books: Dict[str, LocalBook],
    shares_per_leg: float,
    winner_fee_rate: float,
    fixed_cost: float,
) -> Optional[Candidate]:
    leg_costs: List[Tuple[Leg, float]] = []

    if float(getattr(basket, "min_order_size", 0.0) or 0.0) > 0 and shares_per_leg < float(
        basket.min_order_size
    ):
        return None

    for leg in basket.legs:
        book = books.get(leg.token_id)
        if not book:
            return None

        asks = book.asks
        if not asks and book.best_ask:
            asks = [{"price": book.best_ask, "size": 1e9}]

        cost = order_cost_for_shares(asks, shares_per_leg)
        if cost is None:
            return None
        leg_costs.append((leg, cost))

    basket_cost = sum(c for _, c in leg_costs)
    payout = shares_per_leg * (1.0 - winner_fee_rate)
    net_edge = payout - basket_cost - fixed_cost
    edge_pct = (net_edge / payout) if payout > 0 else 0.0
    return Candidate(
        strategy=basket.strategy,
        event_key=basket.key,
        title=basket.title,
        shares_per_leg=shares_per_leg,
        basket_cost=basket_cost,
        payout_after_fee=payout,
        fixed_cost=fixed_cost,
        net_edge=net_edge,
        edge_pct=edge_pct,
        leg_costs=leg_costs,
    )



def format_candidate(candidate: Candidate) -> str:
    lines = []
    lines.append(
        f"[{iso_now()}] [{candidate.strategy}] EDGE ${candidate.net_edge:.4f} ({candidate.edge_pct:.2%}) | "
        f"cost ${candidate.basket_cost:.4f} | payout ${candidate.payout_after_fee:.4f} | "
        f"fixed ${candidate.fixed_cost:.4f}"
    )
    lines.append(f"  Event: {candidate.title} | legs={len(candidate.leg_costs)}")
    for leg, cost in sorted(candidate.leg_costs, key=lambda x: x[1])[:6]:
        lines.append(f"    - {leg.label}/{leg.side.upper()}: ${cost:.4f} (market_id={leg.market_id})")
    extra = len(candidate.leg_costs) - 6
    if extra > 0:
        lines.append(f"    - ... {extra} more")
    return "\n".join(lines)



def make_signature(candidate: Candidate) -> str:
    rounded = [f"{leg.side}:{c:.4f}" for leg, c in candidate.leg_costs]
    return f"{candidate.strategy}|{candidate.event_key}|{candidate.net_edge:.4f}|{','.join(rounded)}"


def estimate_simmer_total_amount(candidate: Candidate, args) -> float:
    """
    Simmer batch execution uses USD amounts (not shares). Estimate how much USD we will send.
    """
    slip = max(0.0, float(args.exec_slippage_bps or 0.0)) / 10000.0
    min_amt = max(0.0, float(args.simmer_min_amount or 0.0))
    total = 0.0
    for _, observed_cost in candidate.leg_costs:
        amt = float(observed_cost) * (1.0 + slip)
        if amt < min_amt:
            amt = min_amt
        total += amt
    return total


def format_candidate_brief(candidate: Candidate) -> str:
    return (
        f"EDGE ${candidate.net_edge:.4f} ({candidate.edge_pct:.2%}) | "
        f"cost ${candidate.basket_cost:.4f} | payout ${candidate.payout_after_fee:.4f} | "
        f"legs={len(candidate.leg_costs)} | {candidate.title}"
    )



def build_clob_client(args):
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError as e:
        raise RuntimeError("py-clob-client is not installed. Run: python -m pip install py-clob-client") from e

    private_key = (os.environ.get("PM_PRIVATE_KEY") or "").strip()
    if not private_key:
        private_key = _load_plaintext_secret_file(_env_str("PM_PRIVATE_KEY_FILE"))
    if not private_key:
        private_key = _load_powershell_dpapi_securestring_file(_env_str("PM_PRIVATE_KEY_DPAPI_FILE"))
    funder = os.environ.get("PM_FUNDER") or os.environ.get("PM_PROXY_ADDRESS")
    # EOA signature type is 0 (py_order_utils.model.EOA). Using 1 causes "invalid signature" on order posts
    # for normal wallets, so we default to 0 unless explicitly overridden.
    signature_type = int(os.environ.get("PM_SIGNATURE_TYPE", "0"))

    if private_key and not private_key.startswith("0x") and len(private_key) == 64:
        # Accept MetaMask-style hex without 0x prefix.
        private_key = "0x" + private_key

    if not private_key or not funder:
        raise RuntimeError(
            "Missing env for live execution. Set PM_PRIVATE_KEY and PM_FUNDER (or PM_PROXY_ADDRESS)."
        )

    if not (
        (private_key.startswith("0x") and len(private_key) == 66)
        or (len(private_key) == 64)
    ):
        raise RuntimeError(
            "Invalid PM_PRIVATE_KEY format. Expected 64 hex chars (optionally prefixed with 0x)."
        )

    client = ClobClient(
        host=args.clob_host,
        chain_id=args.chain_id,
        key=private_key,
        signature_type=signature_type,
        funder=funder,
    )

    k = os.environ.get("PM_API_KEY")
    s = os.environ.get("PM_API_SECRET")
    p = os.environ.get("PM_API_PASSPHRASE")
    if not s:
        s = _load_powershell_dpapi_securestring_file(_env_str("PM_API_SECRET_DPAPI_FILE"))
    if not p:
        p = _load_powershell_dpapi_securestring_file(_env_str("PM_API_PASSPHRASE_DPAPI_FILE"))
    if k and s and p:
        client.set_api_creds(ApiCreds(api_key=k, api_secret=s, api_passphrase=p))
    else:
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
    return client



def extract_order_ids(payload) -> List[str]:
    ids: Set[str] = set()

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                lk = str(k).lower()
                if lk in {"id", "orderid", "order_id"} and isinstance(x, str):
                    if x:
                        ids.add(x)
                walk(x)
        elif isinstance(v, list):
            for i in v:
                walk(i)

    walk(payload)
    return sorted(ids)


def summarize_exec_failure(result: dict) -> str:
    if not isinstance(result, dict):
        return str(result)

    if result.get("error"):
        return str(result.get("error"))

    # Common clob response shape: list of per-order dicts with errorMsg.
    resp = result.get("response")
    msgs: List[str] = []
    if isinstance(resp, list):
        for x in resp:
            if not isinstance(x, dict):
                continue
            m = x.get("errorMsg") or x.get("message") or x.get("error") or ""
            m = str(m).strip()
            if m and m not in msgs:
                msgs.append(m)
    if msgs:
        s = " | ".join(msgs)
        return s[:300]

    return "not filled"



def execute_candidate_batch(client, candidate: Candidate, slippage_bps: float):
    from py_clob_client.clob_types import OrderArgs, OrderType, PostOrdersArgs

    slip = max(0.0, float(slippage_bps or 0.0)) / 10000.0
    shares = float(candidate.shares_per_leg)
    posts = []
    for leg, observed_cost in candidate.leg_costs:
        est_price = float(observed_cost) / max(shares, 1e-9)
        px = min(0.999, est_price * (1.0 + slip))
        # Polymarket rejects "market buy" maker amounts with >2 decimals. Force cents-compatible limit prices.
        px = max(0.001, _q_cents_up(px))
        # Keep sizes stable and predictable (avoid float noise). We require integer-ish shares for safe execution.
        size = round(shares, 2)
        order = client.create_order(
            OrderArgs(
                token_id=leg.token_id,
                price=px,
                size=size,
                side="BUY",
            )
        )
        posts.append(PostOrdersArgs(order=order, orderType=OrderType.FOK, postOnly=False))

    return client.post_orders(posts)


def _floor_price_3dp(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return math.floor(value * 1000.0) / 1000.0


def unwind_partial_clob(client, candidate: Candidate, fills: Dict[str, float], books: Dict[str, LocalBook], args, logger: Logger) -> dict:
    """
    Best-effort flatten any filled legs when a multi-leg batch doesn't fully fill.
    """
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType, PostOrdersArgs
    except ImportError:
        return {"attempted": 0, "succeeded": 0, "error": "py-clob-client not installed"}

    slip = max(0.0, float(args.unwind_slippage_bps)) / 10000.0
    posts = []

    for leg, _ in candidate.leg_costs:
        filled = as_float(fills.get(leg.token_id), 0.0)
        if filled <= 0:
            continue

        book = books.get(leg.token_id)
        best_bid = None
        if book and book.bids:
            best_bid = max((as_float(x.get("price"), 0.0) for x in book.bids), default=0.0)
        if (not best_bid or best_bid <= 0) and book and book.best_bid:
            best_bid = as_float(book.best_bid, 0.0)
        if not best_bid or best_bid <= 0:
            logger.info(f"[{iso_now()}] live: unwind skipped (no bid) token_id={leg.token_id}")
            continue

        px = best_bid * (1.0 - slip)
        # Keep unwind orders cents-compatible as well.
        px = min(0.999, max(0.001, _q_cents_down(px)))
        size = round(float(filled), 2)
        if size <= 0:
            continue

        order = client.create_order(
            OrderArgs(
                token_id=leg.token_id,
                price=px,
                size=size,
                side="SELL",
            )
        )
        posts.append(PostOrdersArgs(order=order, orderType=OrderType.FOK, postOnly=False))

    if not posts:
        return {"attempted": 0, "succeeded": 0}

    attempted = len(posts)
    resp = None
    try:
        resp = client.post_orders(posts)
    except Exception as e:
        return {"attempted": attempted, "succeeded": 0, "error": str(e)}

    # Best-effort success detection. Response shapes vary; treat as "attempted" without guarantees.
    return {"attempted": attempted, "succeeded": attempted, "response": resp}


def execute_candidate_batch_simmer(api_key: str, candidate: Candidate, args) -> dict:
    trades = []
    slip = max(0.0, args.exec_slippage_bps) / 10000.0
    missing_mappings: List[str] = []
    for leg, observed_cost in candidate.leg_costs:
        if not leg.simmer_market_id:
            missing_mappings.append(leg.market_id)
            continue
        amount = observed_cost * (1.0 + slip)
        amount = max(amount, args.simmer_min_amount)
        trades.append(
            {
                "market_id": leg.simmer_market_id,
                "side": leg.side,
                "amount": round(amount, 4),
                "venue": args.simmer_venue,
                "source": args.simmer_source,
            }
        )
    if missing_mappings:
        return {
            "success": False,
            "error": f"missing simmer market mapping for {len(missing_mappings)} legs",
            "missing_market_ids": missing_mappings,
            "results": [],
            "failed_count": len(missing_mappings),
        }
    if not trades:
        return {
            "success": False,
            "error": "no executable trades after mapping",
            "results": [],
            "failed_count": 1,
        }
    resp = sdk_request(api_key, "POST", "/api/sdk/trades/batch", {"trades": trades})
    if isinstance(resp, dict):
        resp["_submitted_trades"] = trades
    return resp


def unwind_partial_simmer(api_key: str, batch_response: dict, args, logger: Logger) -> dict:
    results = batch_response.get("results", [])
    submitted = batch_response.get("_submitted_trades", [])
    if not isinstance(results, list):
        return {"attempted": 0, "succeeded": 0}

    # Some SDK responses omit shares even when trades were sent. Pull current positions as a fallback.
    positions = fetch_simmer_positions(api_key)
    pos_by_market: Dict[str, dict] = {}
    for p in positions:
        mid = str(p.get("market_id") or "")
        if mid:
            pos_by_market[mid] = p

    attempted = 0
    succeeded = 0
    for idx, row in enumerate(results):
        if not isinstance(row, dict):
            continue
        if not row.get("success"):
            continue

        market_id = str(row.get("market_id") or "")
        if not market_id:
            continue

        side = str(row.get("side") or "").strip().lower()
        if side not in {"yes", "no"} and isinstance(submitted, list) and idx < len(submitted):
            srow = submitted[idx]
            if isinstance(srow, dict):
                side = str(srow.get("side") or "").strip().lower()
        if side not in {"yes", "no"}:
            side = "yes"

        shares = as_float(row.get("shares"), 0.0)
        if shares <= 0:
            pos = pos_by_market.get(market_id, {})
            if str(pos.get("venue") or "").strip().lower() == str(args.simmer_venue).strip().lower():
                if side == "yes":
                    shares = as_float(pos.get("shares_yes"), 0.0)
                else:
                    shares = as_float(pos.get("shares_no"), 0.0)

        if shares <= 0:
            continue

        attempted += 1
        resp = sdk_request(
            api_key,
            "POST",
            "/api/sdk/trade",
            {
                "market_id": market_id,
                "side": side,
                "action": "sell",
                "shares": round(shares, 4),
                "venue": args.simmer_venue,
                "source": args.simmer_source,
                "reasoning": "Auto-unwind after partial batch fill",
            },
        )
        if not resp.get("error") and resp.get("success"):
            succeeded += 1
        else:
            logger.info(
                f"[{iso_now()}] live: unwind failed market_id={market_id} shares={shares:.4f} resp={resp}"
            )
    return {"attempted": attempted, "succeeded": succeeded}


def unwind_candidate_positions_simmer(api_key: str, candidate: Candidate, args, logger: Logger) -> dict:
    """
    Best-effort flattening when batch execution partially fails.

    We only unwind positions matching args.simmer_venue to avoid selling virtual ($SIM) positions
    when live venue is polymarket.
    """
    positions = fetch_simmer_positions(api_key)
    venue = str(args.simmer_venue).strip().lower()
    pos_by_market: Dict[str, dict] = {}
    for p in positions:
        if str(p.get("venue") or "").strip().lower() != venue:
            continue
        mid = str(p.get("market_id") or "")
        if mid:
            pos_by_market[mid] = p

    attempted = 0
    succeeded = 0
    for leg, _ in candidate.leg_costs:
        market_id = str(leg.simmer_market_id or "").strip()
        if not market_id:
            continue
        pos = pos_by_market.get(market_id)
        if not pos:
            continue

        side = str(leg.side or "yes").strip().lower()
        if side == "yes":
            shares = as_float(pos.get("shares_yes"), 0.0)
        else:
            shares = as_float(pos.get("shares_no"), 0.0)

        # Guard against accidentally selling unrelated larger inventory:
        # only flatten if position cost basis is within a small multiple of our daily notional cap.
        cap = float(args.max_notional_per_day or 0.0)
        if cap > 0:
            pos_cost = abs(as_float(pos.get("cost_basis"), 0.0))
            pos_value = abs(as_float(pos.get("current_value"), 0.0))
            if max(pos_cost, pos_value) > (cap * 2.0):
                logger.info(
                    f"[{iso_now()}] live: flatten skipped (position too large) market_id={market_id} "
                    f"side={side} cost_basis=${pos_cost:.2f} value=${pos_value:.2f} cap=${cap:.2f}"
                )
                continue
        if shares <= 0:
            continue

        attempted += 1
        resp = sdk_request(
            api_key,
            "POST",
            "/api/sdk/trade",
            {
                "market_id": market_id,
                "side": side,
                "action": "sell",
                "shares": round(shares, 6),
                "venue": args.simmer_venue,
                "source": args.simmer_source,
                "reasoning": "Flatten after partial batch failure",
            },
        )
        if not resp.get("error") and resp.get("success"):
            succeeded += 1
        else:
            logger.info(
                f"[{iso_now()}] live: flatten failed market_id={market_id} side={side} shares={shares:.6f} resp={resp}"
            )

    return {"attempted": attempted, "succeeded": succeeded}


def _trade_token_id(trade: dict) -> str:
    for k in ("asset_id", "assetId", "token_id", "tokenId"):
        v = trade.get(k)
        if v:
            return str(v)
    return ""



def _trade_size(trade: dict) -> float:
    for k in ("size", "amount", "filled_size", "matched_size", "maker_amount", "taker_amount"):
        v = as_float(trade.get(k), math.nan)
        if math.isfinite(v) and v > 0:
            return v
    return 0.0



def _trade_ts_ms(trade: dict) -> Optional[int]:
    for k in ("timestamp", "created_at", "createdAt", "matched_at", "time", "ts"):
        if k in trade:
            t = parse_iso_or_epoch_to_ms(trade.get(k))
            if t:
                return t
    return None



def get_recent_trades_for_token(client, token_id: str, submit_ts_ms: int) -> List[dict]:
    """
    Best-effort fetch of post-submit trades for this API key and token.
    """
    from py_clob_client.clob_types import TradeParams

    trades: List[dict] = []
    # Try millisecond-based filter first.
    for params in (
        TradeParams(asset_id=token_id, after=submit_ts_ms - 30_000),
        TradeParams(asset_id=token_id, after=int((submit_ts_ms - 30_000) / 1000)),
        TradeParams(asset_id=token_id),
    ):
        try:
            rows = client.get_trades(params)
            if isinstance(rows, list):
                trades = rows
                break
        except Exception:
            continue

    filtered = []
    for tr in trades:
        if not isinstance(tr, dict):
            continue
        tms = _trade_ts_ms(tr)
        if tms is not None and tms < (submit_ts_ms - 1000):
            continue
        filtered.append(tr)
    return filtered


async def reconcile_execution(client, candidate: Candidate, submit_ts_ms: int, args, logger: Logger) -> dict:
    token_ids = [leg.token_id for leg, _ in candidate.leg_costs]
    target = candidate.shares_per_leg * args.min_fill_ratio

    for _ in range(args.reconcile_polls):
        fills: Dict[str, float] = {tid: 0.0 for tid in token_ids}
        trade_ids: Set[str] = set()

        for tid in token_ids:
            rows = get_recent_trades_for_token(client, tid, submit_ts_ms)
            for tr in rows:
                trid = tr.get("id")
                if isinstance(trid, str) and trid in trade_ids:
                    continue
                if isinstance(trid, str):
                    trade_ids.add(trid)
                tok = _trade_token_id(tr) or tid
                if tok in fills:
                    fills[tok] += _trade_size(tr)

        all_filled = all(v >= target for v in fills.values())
        if all_filled:
            return {
                "all_filled": True,
                "fills": fills,
                "trade_ids": sorted(trade_ids),
            }

        await asyncio.sleep(args.reconcile_interval_sec)

    return {
        "all_filled": False,
        "fills": fills,
        "trade_ids": sorted(trade_ids),
    }



def can_execute_candidate(
    state: RuntimeState,
    candidate: Candidate,
    args,
    logger: Logger,
    exec_backend: str,
    client=None,
) -> Tuple[bool, str]:
    if state.halted:
        return False, f"halted: {state.halt_reason}"

    if args.max_legs > 0 and len(candidate.leg_costs) > args.max_legs:
        return False, f"legs cap exceeded ({len(candidate.leg_costs)}/{args.max_legs})"

    if exec_backend == "simmer":
        missing = [leg.market_id for leg, _ in candidate.leg_costs if not leg.simmer_market_id]
        if missing:
            return False, f"missing simmer mapping for {len(missing)} legs"

    if args.max_exec_per_day > 0 and state.executions_today >= args.max_exec_per_day:
        return False, f"daily exec cap reached ({state.executions_today}/{args.max_exec_per_day})"

    est_cost = candidate.basket_cost
    if exec_backend == "simmer":
        est_cost = estimate_simmer_total_amount(candidate, args)

    if args.max_notional_per_day > 0 and (state.notional_today + est_cost) > args.max_notional_per_day:
        return False, (
            f"daily notional cap reached (${state.notional_today:.2f} + ${est_cost:.2f} > "
            f"${args.max_notional_per_day:.2f})"
        )

    if args.max_consecutive_failures > 0 and state.consecutive_failures >= args.max_consecutive_failures:
        state.halted = True
        state.halt_reason = (
            f"Consecutive failure cap reached ({state.consecutive_failures}/{args.max_consecutive_failures})"
        )
        return False, f"halted: {state.halt_reason}"

    # Polymarket rejects maker/taker amount precision for "market buy" flows; in practice this makes
    # fractional share sizing fragile. Keep it simple and safe: require integer-ish share sizing.
    if exec_backend == "clob":
        shares = float(candidate.shares_per_leg)
        if abs(shares - round(shares)) > 1e-6:
            return False, f"shares_per_leg must be integer-like for clob execution (got {shares})"

    if exec_backend == "clob" and args.max_open_orders > 0 and client is not None:
        try:
            open_orders = client.get_orders()
            if isinstance(open_orders, list) and len(open_orders) >= args.max_open_orders:
                return False, f"open order cap reached ({len(open_orders)}/{args.max_open_orders})"
        except Exception as e:
            return False, f"could not check open orders: {e}"

    return True, ""


def precheck_clob_books(candidate: Candidate, books: Dict[str, LocalBook], args) -> Tuple[bool, str]:
    """
    Extra safety checks for live CLOB execution:
    - ensure books are fresh (avoid trading on stale data)
    - ensure explicit depth exists at/under our limit prices (avoid predictable no-fills)
    - optionally reject synthetic 1-level books derived from best_ask/best_bid-only updates

    This intentionally reduces trade count; it is designed for "small, reliable edges".
    """
    stale = max(0.0, float(getattr(args, "exec_book_stale_sec", 5.0) or 0.0))
    allow_best_only = bool(getattr(args, "allow_best_only", False))
    slip = max(0.0, float(args.exec_slippage_bps or 0.0)) / 10000.0
    shares = float(candidate.shares_per_leg)
    now = now_ts()

    for leg, observed_cost in candidate.leg_costs:
        book = books.get(leg.token_id)
        if not book:
            return False, f"missing book token_id={leg.token_id}"
        if stale > 0 and (now - float(book.updated_at or 0.0)) > stale:
            return False, f"stale book ({now - book.updated_at:.1f}s) token_id={leg.token_id}"
        if not allow_best_only and getattr(book, "asks_synthetic", False):
            return False, f"best_only book token_id={leg.token_id}"
        if not book.asks:
            return False, f"no asks token_id={leg.token_id}"

        est_price = float(observed_cost) / max(shares, 1e-9)
        px = min(0.999, est_price * (1.0 + slip))
        px = max(0.001, _q_cents_up(px))

        # Only count depth that is actually executable at our limit price.
        filtered = []
        for a in book.asks:
            ap = as_float(a.get("price"), math.inf)
            if math.isfinite(ap) and ap <= px:
                filtered.append(a)
        if order_cost_for_shares(filtered, shares) is None:
            return False, f"insufficient ask depth <=${px:.2f} token_id={leg.token_id}"

    return True, ""


async def execute_with_retries_clob(client, candidate: Candidate, books: Dict[str, LocalBook], args, logger: Logger) -> Tuple[bool, dict]:
    last_result: dict = {}

    for attempt in range(1, args.exec_max_attempts + 1):
        submit_ts_ms = int(now_ts() * 1000)
        try:
            response = execute_candidate_batch(client, candidate, args.exec_slippage_bps)
        except Exception as e:
            last_result = {
                "attempt": attempt,
                "error": str(e),
                "all_filled": False,
            }
            if attempt < args.exec_max_attempts:
                await asyncio.sleep(args.exec_retry_delay_sec)
                continue
            return False, last_result

        order_ids = extract_order_ids(response)
        recon = await reconcile_execution(client, candidate, submit_ts_ms, args, logger)
        recon["attempt"] = attempt
        recon["response"] = response
        recon["order_ids"] = order_ids
        last_result = recon

        if recon.get("all_filled"):
            return True, recon

        if args.cancel_unfilled_on_fail and order_ids:
            try:
                client.cancel_orders(order_ids)
                logger.info(f"[{iso_now()}] live: canceled unfilled order ids: {order_ids}")
            except Exception as e:
                logger.info(f"[{iso_now()}] live: cancel attempt failed: {e}")

        # Best-effort flatten any filled legs to avoid directional inventory.
        fills = recon.get("fills") if isinstance(recon, dict) else None
        if args.clob_unwind_partial and isinstance(fills, dict) and any(as_float(v, 0.0) > 0 for v in fills.values()):
            unwind = unwind_partial_clob(client, candidate, fills, books, args, logger)
            last_result["unwind"] = unwind
            if unwind.get("attempted", 0) > 0:
                logger.info(
                    f"[{iso_now()}] live: unwind(clob) attempted={unwind.get('attempted')} "
                    f"succeeded={unwind.get('succeeded')}"
                )

        if attempt < args.exec_max_attempts:
            await asyncio.sleep(args.exec_retry_delay_sec)

    return False, last_result


async def execute_with_retries_simmer(api_key: str, candidate: Candidate, args, logger: Logger) -> Tuple[bool, dict]:
    last_result: dict = {}

    for attempt in range(1, args.exec_max_attempts + 1):
        response = execute_candidate_batch_simmer(api_key, candidate, args)
        failed_count = int(as_float(response.get("failed_count"), 0.0))
        success = bool(response.get("success")) and failed_count == 0
        last_result = {
            "attempt": attempt,
            "response": response,
            "all_filled": success,
        }

        if success:
            return True, last_result

        # Best-effort flatten to avoid partial basket exposure.
        if args.simmer_unwind_partial:
            unwind = unwind_partial_simmer(api_key, response, args, logger)
            last_result["unwind_from_response"] = unwind
            if unwind.get("attempted", 0) > 0:
                logger.info(
                    f"[{iso_now()}] live: partial batch unwind(from_response) attempted={unwind.get('attempted')} "
                    f"succeeded={unwind.get('succeeded')}"
                )

            # Fallback: check current positions and flatten anything in the candidate legs.
            flatten = unwind_candidate_positions_simmer(api_key, candidate, args, logger)
            last_result["unwind_from_positions"] = flatten
            if flatten.get("attempted", 0) > 0:
                logger.info(
                    f"[{iso_now()}] live: flatten(from_positions) attempted={flatten.get('attempted')} "
                    f"succeeded={flatten.get('succeeded')}"
                )

        retry_delay = args.exec_retry_delay_sec
        status = response.get("_status")
        body = response.get("_body", {}) if isinstance(response, dict) else {}
        if status == 429 and isinstance(body, dict):
            retry_after = as_float(body.get("retry_after"), 0.0)
            if retry_after > retry_delay:
                retry_delay = retry_after

        if attempt < args.exec_max_attempts:
            await asyncio.sleep(retry_delay)

    return False, last_result


async def run(args) -> int:
    script_dir = Path(__file__).resolve().parent
    if not args.log_file:
        # Default to the same file used by the Windows task runner.
        args.log_file = str(script_dir.parent / "logs" / "clob-arb-monitor.log")
    if not args.state_file:
        args.state_file = str(script_dir.parent / "logs" / "clob_arb_state.json")

    logger = Logger(args.log_file)
    state_file = Path(args.state_file)
    state = load_state(state_file)
    save_state(state_file, state)

    gamma_include_re: Optional[re.Pattern] = None
    gamma_exclude_re: Optional[re.Pattern] = None
    if getattr(args, "gamma_include_regex", ""):
        try:
            gamma_include_re = re.compile(str(args.gamma_include_regex), re.IGNORECASE)
        except re.error as e:
            logger.info(f"[{iso_now()}] warning: invalid gamma_include_regex: {e} (ignoring)")
            gamma_include_re = None
    if getattr(args, "gamma_exclude_regex", ""):
        try:
            gamma_exclude_re = re.compile(str(args.gamma_exclude_regex), re.IGNORECASE)
        except re.error as e:
            logger.info(f"[{iso_now()}] warning: invalid gamma_exclude_regex: {e} (ignoring)")
            gamma_exclude_re = None

    if args.execute and state.halted:
        logger.info(f"[{iso_now()}] state halted: {state.halt_reason}")
        logger.info(
            f"[{iso_now()}] run skipped while halted (will auto-reset next day or clear in state file)"
        )
        return 0

    universe = str(getattr(args, "universe", "weather") or "weather").strip().lower()
    if universe not in {"weather", "gamma-active", "btc-5m"}:
        universe = "weather"

    if universe == "gamma-active":
        gamma_limit = getattr(args, "gamma_limit", 500)
        gamma_offset = getattr(args, "gamma_offset", 0)
        min_liq = getattr(args, "gamma_min_liquidity", 0.0)
        min_vol24 = getattr(args, "gamma_min_volume24hr", 0.0)
        scan_max = getattr(args, "gamma_scan_max_markets", 5000)
        max_days = float(getattr(args, "gamma_max_days_to_end", 0.0) or 0.0)
        sports_live_only = bool(getattr(args, "sports_live_only", False))
        sports_live_prestart_min = float(getattr(args, "sports_live_prestart_min", 10.0) or 10.0)
        sports_live_postend_min = float(getattr(args, "sports_live_postend_min", 30.0) or 30.0)
        build_max_legs = int(args.max_legs) if int(args.max_legs or 0) > 0 else 12

        if args.strategy == "yes-no":
            baskets = build_gamma_yes_no_baskets(
                gamma_limit=gamma_limit,
                gamma_offset=gamma_offset,
                min_liquidity=min_liq,
                min_volume24hr=min_vol24,
                scan_max_markets=scan_max,
                max_days_to_end=max_days,
                include_re=gamma_include_re,
                exclude_re=gamma_exclude_re,
                sports_live_only=sports_live_only,
                sports_live_prestart_min=sports_live_prestart_min,
                sports_live_postend_min=sports_live_postend_min,
            )
        elif args.strategy == "buckets":
            baskets = build_gamma_bucket_baskets(
                gamma_limit=gamma_limit,
                gamma_offset=gamma_offset,
                min_liquidity=min_liq,
                min_volume24hr=min_vol24,
                min_outcomes=args.min_outcomes,
                max_legs=build_max_legs,
                scan_max_markets=scan_max,
                max_days_to_end=max_days,
                include_re=gamma_include_re,
                exclude_re=gamma_exclude_re,
            )
        elif args.strategy == "event-pair":
            baskets = build_gamma_event_pair_baskets(
                gamma_limit=gamma_limit,
                gamma_offset=gamma_offset,
                min_liquidity=min_liq,
                min_volume24hr=min_vol24,
                max_legs=build_max_legs,
                scan_max_markets=scan_max,
                max_days_to_end=max_days,
                include_re=gamma_include_re,
                exclude_re=gamma_exclude_re,
            )
        else:
            # "both"/"all": merge; selection later will cap subscriptions.
            baskets = []
            baskets.extend(
                build_gamma_bucket_baskets(
                    gamma_limit=gamma_limit,
                    gamma_offset=gamma_offset,
                    min_liquidity=min_liq,
                    min_volume24hr=min_vol24,
                    min_outcomes=args.min_outcomes,
                    max_legs=build_max_legs,
                    scan_max_markets=scan_max,
                    max_days_to_end=max_days,
                    include_re=gamma_include_re,
                    exclude_re=gamma_exclude_re,
                )
            )
            baskets.extend(
                build_gamma_yes_no_baskets(
                    gamma_limit=gamma_limit,
                    gamma_offset=gamma_offset,
                    min_liquidity=min_liq,
                    min_volume24hr=min_vol24,
                    scan_max_markets=scan_max,
                    max_days_to_end=max_days,
                    include_re=gamma_include_re,
                    exclude_re=gamma_exclude_re,
                    sports_live_only=sports_live_only,
                    sports_live_prestart_min=sports_live_prestart_min,
                    sports_live_postend_min=sports_live_postend_min,
                )
            )
            if args.strategy == "all":
                baskets.extend(
                    build_gamma_event_pair_baskets(
                        gamma_limit=gamma_limit,
                        gamma_offset=gamma_offset,
                        min_liquidity=min_liq,
                        min_volume24hr=min_vol24,
                        max_legs=build_max_legs,
                        scan_max_markets=scan_max,
                        max_days_to_end=max_days,
                        include_re=gamma_include_re,
                        exclude_re=gamma_exclude_re,
                    )
                )
        if not baskets:
            logger.info("No valid Gamma baskets found.")
            maybe_notify_discord(logger, "CLOBBOT: gamma-active universe empty (no baskets). Check filters / Gamma API.")
            return 1
    elif universe == "btc-5m":
        baskets = build_btc_updown_5m_baskets(
            windows_back=int(getattr(args, "btc_5m_windows_back", 1) or 1),
            windows_forward=int(getattr(args, "btc_5m_windows_forward", 1) or 1),
        )
        if not baskets:
            logger.info("No valid BTC 5m baskets found (event slug fetch).")
            maybe_notify_discord(
                logger,
                "CLOBBOT: btc-5m universe empty (could not fetch current BTC 5m events).",
            )
            return 1
    else:
        baskets = build_event_baskets(
            limit=args.limit,
            min_outcomes=args.min_outcomes,
            workers=args.workers,
            strategy=args.strategy,
        )
        if not baskets:
            logger.info("No valid weather event baskets found.")
            maybe_notify_discord(logger, "CLOBBOT: weather universe empty (no baskets).")
            return 1

    max_tokens = int(getattr(args, "max_subscribe_tokens", 0) or 0)
    if max_tokens > 0:
        # Select a subset of baskets to keep WS + CPU manageable. For gamma-active we rank markets by
        # a heuristic score (time-to-end + activity + liquidity). For weather we keep the old greedy
        # "smallest baskets first" behavior.
        selected: List[EventBasket] = []
        tokens: Set[str] = set()

        if universe == "gamma-active":
            now_ms = int(time.time() * 1000)
            for b in baskets:
                b.score = score_gamma_basket(b, now_ms, args)

            ranked = [b for b in baskets if b.score >= 0]
            ranked.sort(key=lambda x: (x.score, x.volume24hr), reverse=True)

            max_per_event = int(getattr(args, "max_markets_per_event", 0) or 0)
            per_event: Dict[str, int] = {}

            for b in ranked:
                b_tokens = {leg.token_id for leg in b.legs if leg.token_id}
                if not b_tokens:
                    continue
                if len(tokens | b_tokens) > max_tokens:
                    continue

                if max_per_event > 0 and b.event_id:
                    used = per_event.get(b.event_id, 0)
                    if used >= max_per_event:
                        continue
                    per_event[b.event_id] = used + 1

                selected.append(b)
                tokens |= b_tokens

            logger.info(
                f"Applied scored selection (gamma-active) max_subscribe_tokens={max_tokens}: "
                f"baskets {len(baskets)} -> {len(selected)} | tokens={len(tokens)}"
            )
            if not ranked:
                logger.info(
                    "Note: no baskets passed scoring filters (likely stale endDate values, or gamma_max_days_to_end too strict)."
                )
            if selected:
                top = sorted(selected, key=lambda x: x.score, reverse=True)[:8]
                for t in top:
                    end_days = None
                    if t.end_ms:
                        end_days = max(0.0, float(t.end_ms - now_ms) / 86400000.0)
                    end_s = f"{end_days:.0f}d" if end_days is not None else "?d"
                    logger.info(
                        f"  picked score={t.score:.3f} end={end_s} vol24={t.volume24hr:.0f} liq={t.liquidity_num:.0f} "
                        f"spr={t.spread:.3f} id={t.market_id} q={t.title[:80]}"
                    )

            baskets = selected
            if not baskets:
                logger.info("No baskets selected after gamma-active scoring/caps.")
                maybe_notify_discord(
                    logger,
                    "CLOBBOT: gamma-active selection empty (all markets filtered out). "
                    "Try increasing CLOBBOT_GAMMA_SCAN_MAX, relaxing min_liquidity/min_volume, "
                    "or loosening CLOBBOT_GAMMA_MAX_DAYS_TO_END.",
                )
                return 1
        else:
            for b in sorted(baskets, key=lambda x: len({leg.token_id for leg in x.legs})):
                b_tokens = {leg.token_id for leg in b.legs if leg.token_id}
                if not b_tokens:
                    continue
                if len(tokens | b_tokens) > max_tokens:
                    continue
                selected.append(b)
                tokens |= b_tokens
            if selected and len(selected) != len(baskets):
                logger.info(
                    f"Applied max_subscribe_tokens={max_tokens}: baskets {len(baskets)} -> {len(selected)} | "
                    f"tokens={len(tokens)}"
                )
                baskets = selected

    token_to_events: Dict[str, Set[str]] = {}
    event_map: Dict[str, EventBasket] = {}
    token_ids: List[str] = []
    seen_tokens: Set[str] = set()
    for basket in baskets:
        event_map[basket.key] = basket
        for leg in basket.legs:
            token_to_events.setdefault(leg.token_id, set()).add(basket.key)
            if leg.token_id not in seen_tokens:
                token_ids.append(leg.token_id)
                seen_tokens.add(leg.token_id)

    logger.info(f"Loaded baskets: {len(baskets)}")
    logger.info(f"Subscribed token IDs: {len(token_ids)}")
    logger.info(f"Mode: {'LIVE EXECUTION' if args.execute else 'observe-only'}")
    logger.info(f"Universe: {universe}")
    logger.info(
        f"Threshold: net edge >= {args.min_edge_cents:.2f}c | shares/leg={args.shares:.2f} | "
        f"winner_fee={args.winner_fee_rate:.2%} | fixed_cost=${args.fixed_cost:.4f}"
    )
    logger.info(f"Strategy: {args.strategy}")
    if universe == "gamma-active":
        logger.info(
            "Gamma filters: "
            f"limit={getattr(args, 'gamma_limit', 500)} offset={getattr(args, 'gamma_offset', 0)} "
            f"min_liquidity={getattr(args, 'gamma_min_liquidity', 0.0)} "
            f"min_volume24hr={getattr(args, 'gamma_min_volume24hr', 0.0)} "
            f"scan_max={getattr(args, 'gamma_scan_max_markets', 5000)} "
            f"max_days_to_end={getattr(args, 'gamma_max_days_to_end', 0.0)} "
            f"score_halflife_days={getattr(args, 'gamma_score_halflife_days', 30.0)} "
            f"max_markets_per_event={getattr(args, 'max_markets_per_event', 0)}"
        )
        if bool(getattr(args, "sports_live_only", False)):
            logger.info(
                "Sports live filter (gamma yes-no): "
                f"enabled prestart={float(getattr(args, 'sports_live_prestart_min', 10.0)):.1f}m "
                f"postend={float(getattr(args, 'sports_live_postend_min', 30.0)):.1f}m"
            )
    if max_tokens > 0:
        logger.info(f"Max subscribe tokens: {max_tokens}")
    if args.summary_every_sec:
        logger.info(f"Summary: every {float(args.summary_every_sec):.0f}s")
    if (not args.execute) and bool(getattr(args, "notify_observe_signals", False)):
        logger.info("Observe signal Discord notify: enabled")
    maybe_notify_discord(
        logger,
        (
            f"CLOBBOT started ({'LIVE' if args.execute else 'observe'}) | "
            f"universe={universe} min_edge={args.min_edge_cents:.2f}c strategy={args.strategy}"
        ),
    )

    exec_backend = "none"
    client = None
    simmer_api_key = ""
    if args.execute:
        if args.confirm_live != "YES":
            logger.info("Refusing live mode. Re-run with --confirm-live YES")
            return 1

        if args.exec_backend == "auto":
            if os.environ.get("PM_PRIVATE_KEY") and (
                os.environ.get("PM_FUNDER") or os.environ.get("PM_PROXY_ADDRESS")
            ):
                exec_backend = "clob"
            elif os.environ.get("SIMMER_API_KEY"):
                exec_backend = "simmer"
            else:
                logger.info(
                    "Live execution init failed: no backend credentials found "
                    "(need PM_PRIVATE_KEY+PM_FUNDER for clob or SIMMER_API_KEY for simmer)."
                )
                return 1
        else:
            exec_backend = args.exec_backend

        if exec_backend == "clob":
            try:
                client = build_clob_client(args)
            except Exception as e:
                logger.info(f"Live execution init failed (clob): {e}")
                return 1
            logger.info("Live execution backend: clob")
        elif exec_backend == "simmer":
            simmer_api_key = os.environ.get("SIMMER_API_KEY", "").strip()
            if not simmer_api_key:
                logger.info("Live execution init failed (simmer): SIMMER_API_KEY is missing")
                return 1
            logger.info(
                f"Live execution backend: simmer (venue={args.simmer_venue}, source={args.simmer_source})"
            )
            if args.simmer_venue == "polymarket":
                settings = fetch_simmer_settings(simmer_api_key)
                if not settings:
                    logger.info("SDK settings unavailable; continuing with venue=polymarket probe/execution path")
                else:
                    if settings.get("trading_paused", False):
                        logger.info("Live execution paused: SDK settings show trading_paused=true")
                        return 0
                    if not settings.get("sdk_real_trading_enabled", False):
                        logger.info(
                            "Warning: sdk_real_trading_enabled=false in SDK settings; "
                            "continuing because direct trade endpoint may still execute."
                        )
                    usdc_balance = as_float(settings.get("polymarket_usdc_balance"), 0.0)
                    if usdc_balance <= 0:
                        logger.info(
                            "Warning: polymarket_usdc_balance<=0 in SDK settings; "
                            "trade attempts may fail until funding sync is fixed."
                        )
            mapped = [b for b in baskets if all(leg.simmer_market_id for leg in b.legs)]
            if not mapped:
                logger.info("Live execution init failed (simmer): no fully-mapped event baskets available")
                return 1
            if len(mapped) != len(baskets):
                logger.info(f"Filtered baskets for simmer mapping: {len(baskets)} -> {len(mapped)}")
                baskets = mapped

                token_to_events = {}
                event_map = {}
                token_ids = []
                seen_tokens = set()
                for basket in baskets:
                    event_map[basket.key] = basket
                    for leg in basket.legs:
                        token_to_events.setdefault(leg.token_id, set()).add(basket.key)
                        if leg.token_id not in seen_tokens:
                            token_ids.append(leg.token_id)
                            seen_tokens.add(leg.token_id)

    logger.info(f"Runtime baskets: {len(baskets)}")
    logger.info(f"Runtime subscribed token IDs: {len(token_ids)}")

    books: Dict[str, LocalBook] = {}
    start = now_ts()
    stats = RunStats()
    summary_every = max(0.0, float(getattr(args, "summary_every_sec", 0.0) or 0.0))
    min_eval_interval = max(0.0, float(getattr(args, "min_eval_interval_ms", 0) or 0)) / 1000.0
    observe_notify_min_interval = max(
        0.0, float(getattr(args, "observe_notify_min_interval_sec", 30.0) or 0.0)
    )
    last_observe_notify_ts = 0.0

    async with websockets.connect(args.ws_url, ping_interval=20, ping_timeout=20, max_size=2**24) as ws:
        await ws.send(json.dumps({"type": "market", "assets_ids": token_ids}))
        logger.info(f"Connected: {args.ws_url}")

        while True:
            # Reset daily counters automatically at date boundary.
            if state.day != day_key_local():
                state = RuntimeState(day=day_key_local())
                save_state(state_file, state)
                logger.info(f"[{iso_now()}] state: daily counters reset")

            if summary_every > 0 and (now_ts() - stats.last_summary_ts) >= summary_every:
                window_sec = max(1, int(now_ts() - stats.window_started_at))
                if stats.best_window:
                    logger.info(
                        f"[{iso_now()}] summary({window_sec}s): "
                        f"candidates={stats.candidates_window} | {format_candidate_brief(stats.best_window)}"
                    )
                else:
                    logger.info(
                        f"[{iso_now()}] summary({window_sec}s): candidates={stats.candidates_window} | none"
                    )
                stats.candidates_window = 0
                stats.best_window = None
                stats.window_started_at = now_ts()
                stats.last_summary_ts = now_ts()

            if args.run_seconds and (now_ts() - start) >= args.run_seconds:
                logger.info("Run timeout reached. Exiting.")
                break

            if args.execute:
                if not maybe_apply_daily_loss_guard(state, args, logger):
                    save_state(state_file, state)
                    if state.halted:
                        logger.info(f"[{iso_now()}] run ending early due to halt state")
                        break
                    await asyncio.sleep(2)
                    continue

            try:
                timeout = 30.0
                if args.run_seconds:
                    remaining = args.run_seconds - (now_ts() - start)
                    if remaining <= 0:
                        logger.info("Run timeout reached. Exiting.")
                        break
                    timeout = min(timeout, max(0.2, float(remaining)))

                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                # If we're near the end of a timed run, avoid noisy heartbeats.
                if not args.run_seconds or (args.run_seconds - (now_ts() - start)) > 2:
                    logger.info(f"[{iso_now()}] heartbeat: no message in 30s")
                continue

            try:
                payload = json.loads(raw)
            except Exception:
                continue

            changed_tokens: Set[str] = set()
            for item in extract_book_items(payload):
                token_id = update_book_from_snapshot(item, books)
                if token_id:
                    changed_tokens.add(token_id)

            if not changed_tokens:
                continue

            impacted_events: Set[str] = set()
            for token in changed_tokens:
                impacted_events.update(token_to_events.get(token, set()))

            for event_key in impacted_events:
                basket = event_map[event_key]
                if min_eval_interval > 0 and (now_ts() - basket.last_eval_ts) < min_eval_interval:
                    continue
                c = compute_candidate(
                    basket=basket,
                    books=books,
                    shares_per_leg=args.shares,
                    winner_fee_rate=args.winner_fee_rate,
                    fixed_cost=args.fixed_cost,
                )
                if not c:
                    continue
                basket.last_eval_ts = now_ts()

                stats.candidates_total += 1
                stats.candidates_window += 1
                if (stats.best_all is None) or (c.net_edge > stats.best_all.net_edge):
                    stats.best_all = c
                if (stats.best_window is None) or (c.net_edge > stats.best_window.net_edge):
                    stats.best_window = c

                if c.net_edge < (args.min_edge_cents / 100.0):
                    continue

                sig = make_signature(c)
                now = now_ts()
                if sig == basket.last_signature and (now - basket.last_alert_ts) < args.alert_cooldown_sec:
                    continue

                logger.info("")
                logger.info(format_candidate(c))
                basket.last_signature = sig
                basket.last_alert_ts = now

                if not args.execute and bool(getattr(args, "notify_observe_signals", False)):
                    if observe_notify_min_interval <= 0 or (now - last_observe_notify_ts) >= observe_notify_min_interval:
                        maybe_notify_discord(
                            logger,
                            (
                                f"OBSERVE SIGNAL {c.title} | "
                                f"edge {c.edge_pct:.2%} (${c.net_edge:.4f}) | "
                                f"cost ${c.basket_cost:.4f} | legs={len(c.leg_costs)}"
                            ),
                        )
                        last_observe_notify_ts = now

                if args.execute:
                    if (now - basket.last_exec_ts) < args.exec_cooldown_sec:
                        logger.info("  live: skipped (event execution cooldown)")
                        continue

                    allowed, reason = can_execute_candidate(
                        state=state,
                        candidate=c,
                        args=args,
                        logger=logger,
                        exec_backend=exec_backend,
                        client=client,
                    )
                    if not allowed:
                        logger.info(f"  live: skipped ({reason})")
                        save_state(state_file, state)
                        continue

                    exec_cost = None
                    exec_edge = None
                    exec_edge_pct = None
                    if exec_backend == "clob":
                        exec_cost = _estimate_exec_cost_clob(c, args.exec_slippage_bps)
                        exec_edge = c.payout_after_fee - float(exec_cost) - c.fixed_cost
                        exec_edge_pct = (float(exec_edge) / c.payout_after_fee) if c.payout_after_fee > 0 else 0.0
                        if exec_edge < (args.min_edge_cents / 100.0):
                            logger.info(
                                f"  live: skipped (edge after cents/slippage ${exec_edge:.4f} < "
                                f"threshold ${(args.min_edge_cents/100.0):.4f})"
                            )
                            continue

                    if exec_backend == "clob":
                        ok, why = precheck_clob_books(c, books, args)
                        if not ok:
                            logger.info(f"  live: skipped (book precheck: {why})")
                            continue

                    # Notify "entry" only when we are actually about to attempt execution.
                    if exec_backend == "clob" and exec_edge is not None and exec_cost is not None:
                        maybe_notify_discord(
                            logger,
                            (
                                f"ENTRY ({exec_backend}) {c.title} | "
                                f"est edge {exec_edge_pct:.2%} (${exec_edge:.4f}) | "
                                f"est cost ${exec_cost:.4f} | legs={len(c.leg_costs)}"
                            ),
                        )
                    else:
                        maybe_notify_discord(
                            logger,
                            (
                                f"ENTRY ({exec_backend}) {c.title} | "
                                f"edge {c.edge_pct:.2%} (${c.net_edge:.4f}) | legs={len(c.leg_costs)}"
                            ),
                        )

                    if exec_backend == "clob":
                        ok, result = await execute_with_retries_clob(client, c, books, args, logger)
                    else:
                        ok, result = await execute_with_retries_simmer(simmer_api_key, c, args, logger)
                    basket.last_exec_ts = now
                    if ok:
                        state.executions_today += 1
                        notional_inc = c.basket_cost
                        state.consecutive_failures = 0
                        if exec_backend == "clob":
                            if exec_cost is not None and math.isfinite(float(exec_cost)) and float(exec_cost) > 0:
                                notional_inc = float(exec_cost)
                            state.notional_today += notional_inc
                            logger.info(
                                f"  live: filled (attempt={result.get('attempt')}, "
                                f"fills={result.get('fills')})"
                            )
                            maybe_notify_discord(
                                logger,
                                (
                                    f"Filled ({exec_backend}) {c.title} | "
                                    f"edge {c.edge_pct:.2%} (${c.net_edge:.4f}) | "
                                    f"cost ${notional_inc:.4f} | legs={len(c.leg_costs)}"
                                ),
                            )
                        else:
                            total_cost = as_float(result.get("response", {}).get("total_cost"), math.nan)
                            if math.isfinite(total_cost) and total_cost > 0:
                                notional_inc = total_cost
                            else:
                                notional_inc = estimate_simmer_total_amount(c, args)
                            state.notional_today += notional_inc
                            logger.info(
                                f"  live: batch executed (attempt={result.get('attempt')}, "
                                f"total_cost=${notional_inc:.4f})"
                            )
                            maybe_notify_discord(
                                logger,
                                (
                                    f"Executed ({exec_backend}) {c.title} | "
                                    f"edge {c.edge_pct:.2%} (${c.net_edge:.4f}) | "
                                    f"total_cost ${notional_inc:.4f} | legs={len(c.leg_costs)}"
                                ),
                            )
                    else:
                        state.consecutive_failures += 1
                        logger.info(
                            f"  live: not filled (attempt={result.get('attempt')}, "
                            f"fails={state.consecutive_failures}, detail={result})"
                        )
                        maybe_notify_discord(
                            logger,
                            (
                                f"NO FILL ({exec_backend}) {c.title} | "
                                f"edge {c.edge_pct:.2%} (${c.net_edge:.4f}) | "
                                f"reason: {summarize_exec_failure(result)}"
                            ),
                        )
                        if (
                            args.max_consecutive_failures > 0
                            and state.consecutive_failures >= args.max_consecutive_failures
                        ):
                            state.halted = True
                            state.halt_reason = (
                                f"Consecutive failure cap reached ({state.consecutive_failures}/"
                                f"{args.max_consecutive_failures})"
                            )
                            logger.info(f"[{iso_now()}] guard: HALT {state.halt_reason}")
                            maybe_notify_discord(logger, f"CLOBBOT HALT: {state.halt_reason}")

                    save_state(state_file, state)

    save_state(state_file, state)
    if summary_every > 0:
        if stats.best_all:
            logger.info(
                f"[{iso_now()}] run summary: candidates={stats.candidates_total} | "
                f"{format_candidate_brief(stats.best_all)}"
            )
        else:
            logger.info(f"[{iso_now()}] run summary: candidates={stats.candidates_total} | none")
    maybe_notify_discord(logger, "CLOBBOT stopped")
    return 0



def parse_args():
    p = argparse.ArgumentParser(description="Realtime Polymarket CLOB basket arbitrage monitor")
    p.add_argument("--ws-url", default=WS_MARKET_URL, help="Market channel websocket URL")
    p.add_argument(
        "--universe",
        choices=("weather", "gamma-active", "btc-5m"),
        default="weather",
        help="Universe to monitor (weather uses Simmer weather index; gamma-active uses Gamma active markets; btc-5m fetches rolling BTC 5-min events by slug)",
    )
    p.add_argument("--limit", type=int, default=250, help="Max weather markets to build universe from")
    p.add_argument("--workers", type=int, default=24, help="Parallel workers for universe discovery")
    p.add_argument("--min-outcomes", type=int, default=4, help="Min bucket outcomes per event")
    p.add_argument("--gamma-limit", type=int, default=500, help="Max Gamma active markets to include (gamma-active)")
    p.add_argument("--gamma-offset", type=int, default=0, help="Gamma markets offset (pagination)")
    p.add_argument("--gamma-min-liquidity", type=float, default=0.0, help="Min Gamma liquidityNum filter (gamma-active)")
    p.add_argument(
        "--gamma-min-volume24hr", type=float, default=0.0, help="Min Gamma volume24hr filter (gamma-active)"
    )
    p.add_argument(
        "--gamma-scan-max-markets",
        type=int,
        default=5000,
        help="Max Gamma markets to scan for matches before giving up (gamma-active)",
    )
    p.add_argument(
        "--gamma-max-days-to-end",
        type=float,
        default=0.0,
        help="Hard filter: skip markets ending more than N days out (0=disabled, gamma-active)",
    )
    p.add_argument(
        "--gamma-score-halflife-days",
        type=float,
        default=30.0,
        help="Time decay half-life in days for gamma-active scoring (smaller => more short-dated markets)",
    )
    p.add_argument(
        "--gamma-include-regex",
        default="",
        help="Only include Gamma markets whose question/event slug matches this regex (case-insensitive)",
    )
    p.add_argument(
        "--gamma-exclude-regex",
        default="",
        help="Exclude Gamma markets whose question/event slug matches this regex (case-insensitive)",
    )
    p.add_argument(
        "--sports-live-only",
        action="store_true",
        help="For gamma yes-no baskets, only include likely sports markets that are live/near start-end window.",
    )
    p.add_argument(
        "--sports-live-prestart-min",
        type=float,
        default=10.0,
        help="With --sports-live-only: include markets this many minutes before scheduled start.",
    )
    p.add_argument(
        "--sports-live-postend-min",
        type=float,
        default=30.0,
        help="With --sports-live-only: keep markets this many minutes after finish/end timestamp.",
    )
    p.add_argument(
        "--btc-5m-windows-back",
        type=int,
        default=1,
        help="For universe=btc-5m: how many prior 5-min windows to include",
    )
    p.add_argument(
        "--btc-5m-windows-forward",
        type=int,
        default=1,
        help="For universe=btc-5m: how many future 5-min windows to include",
    )
    p.add_argument(
        "--strategy",
        choices=("buckets", "yes-no", "event-pair", "both", "all"),
        default="both",
        help="Arbitrage strategy to monitor (all=buckets+yes-no+event-pair, event-pair=YES+YES/NO+NO on binary negRisk events)",
    )

    p.add_argument("--shares", type=float, default=5.0, help="Shares per bucket leg")
    p.add_argument("--max-legs", type=int, default=0, help="Skip opportunities with more than N legs (0=unlimited)")
    p.add_argument("--min-edge-cents", type=float, default=1.0, help="Alert/execution threshold in cents")
    p.add_argument("--winner-fee-rate", type=float, default=0.0, help="Winner fee rate (default 0.0)")
    p.add_argument("--fixed-cost", type=float, default=0.0, help="Per-event fixed USD cost")
    p.add_argument("--alert-cooldown-sec", type=float, default=10.0, help="Suppress duplicate alerts")
    p.add_argument("--run-seconds", type=int, default=0, help="Auto-exit after N seconds (0=run forever)")
    p.add_argument("--summary-every-sec", type=float, default=0.0, help="Emit periodic summary line (0=disabled)")
    p.add_argument(
        "--notify-observe-signals",
        action="store_true",
        help="In observe-only mode, send Discord notification when threshold signal is detected.",
    )
    p.add_argument(
        "--observe-notify-min-interval-sec",
        type=float,
        default=30.0,
        help="With --notify-observe-signals: minimum seconds between observe signal notifications (global).",
    )
    p.add_argument(
        "--max-subscribe-tokens",
        type=int,
        default=0,
        help="Cap total subscribed token IDs (0=unlimited). Helps keep local PC stable.",
    )
    p.add_argument(
        "--min-eval-interval-ms",
        type=int,
        default=0,
        help="Debounce per-event evaluation (0=disabled). Helps reduce CPU on large universes.",
    )
    p.add_argument(
        "--max-markets-per-event",
        type=int,
        default=0,
        help="Limit subscribed markets per Gamma event id (0=unlimited). Helps diversify gamma-active universe.",
    )

    p.add_argument("--max-exec-per-day", type=int, default=20, help="Max successful executions per day")
    p.add_argument("--max-notional-per-day", type=float, default=200.0, help="Max basket cost deployed per day")
    p.add_argument("--max-open-orders", type=int, default=0, help="Max open orders allowed before blocking")
    p.add_argument("--max-consecutive-failures", type=int, default=3, help="Stop after N execution failures")
    p.add_argument("--daily-loss-limit-usd", type=float, default=0.0, help="Halt on daily pnl drawdown")
    p.add_argument("--pnl-check-interval-sec", type=float, default=60.0, help="PnL guard polling interval")
    p.add_argument(
        "--exec-book-stale-sec",
        type=float,
        default=5.0,
        help="Skip live execution if any leg book is older than N seconds (0=disabled).",
    )
    p.add_argument(
        "--allow-best-only",
        action="store_true",
        help="Allow execution using synthetic books derived from best_ask/best_bid-only updates (less reliable).",
    )

    p.add_argument("--execute", action="store_true", help="Enable live order submission")
    p.add_argument("--confirm-live", default="", help='Must be "YES" when --execute is enabled')
    p.add_argument(
        "--exec-backend",
        choices=("auto", "clob", "simmer"),
        default="auto",
        help="Live execution backend (auto detects credentials).",
    )

    p.add_argument("--simmer-venue", default="polymarket", help="Venue used for simmer batch execution")
    p.add_argument("--simmer-source", default="sdk:clob-arb", help="Source tag for simmer batch execution")
    p.add_argument("--simmer-min-amount", type=float, default=1.0, help="Min USD amount per leg in simmer mode")
    p.add_argument(
        "--no-simmer-unwind-partial",
        dest="simmer_unwind_partial",
        action="store_false",
        help="Do not auto-unwind successful legs after partial batch failures",
    )

    p.add_argument("--clob-host", default="https://clob.polymarket.com", help="CLOB host for execution")
    p.add_argument("--chain-id", type=int, default=137, help="EVM chain id for signing")
    p.add_argument("--exec-slippage-bps", type=float, default=50.0, help="Limit price cushion in bps")
    p.add_argument("--unwind-slippage-bps", type=float, default=150.0, help="Unwind price cushion in bps (clob)")
    p.add_argument("--exec-cooldown-sec", type=float, default=30.0, help="Per-event execution cooldown")
    p.add_argument("--exec-max-attempts", type=int, default=2, help="Max attempts for one candidate")
    p.add_argument("--exec-retry-delay-sec", type=float, default=2.0, help="Delay between retries")

    p.add_argument("--reconcile-polls", type=int, default=4, help="Number of fill reconciliation polls")
    p.add_argument("--reconcile-interval-sec", type=float, default=1.0, help="Seconds between reconciliation polls")
    p.add_argument("--min-fill-ratio", type=float, default=0.98, help="Required fill ratio per leg")
    p.add_argument(
        "--no-cancel-unfilled-on-fail",
        dest="cancel_unfilled_on_fail",
        action="store_false",
        help="Do not cancel lingering orders after failed reconciliation",
    )
    p.add_argument(
        "--no-clob-unwind-partial",
        dest="clob_unwind_partial",
        action="store_false",
        help="Do not attempt to unwind filled legs after partial clob fills",
    )
    p.set_defaults(cancel_unfilled_on_fail=True, clob_unwind_partial=True, simmer_unwind_partial=True)

    p.add_argument("--log-file", default="", help="Path to log file (optional)")
    p.add_argument("--state-file", default="", help="Path to runtime state json (optional)")

    args = p.parse_args()
    return _apply_env_overrides(args)


if __name__ == "__main__":
    args = parse_args()
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("\nStopped by user.")
