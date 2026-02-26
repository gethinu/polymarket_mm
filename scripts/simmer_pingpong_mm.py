#!/usr/bin/env python3
"""
Simmer ($SIM) inventory "ping-pong" bot (demo trading).

This is NOT CLOB market making. Simmer's "SIMMER (LMSR)" markets behave like an AMM.
We implement a simple ping-pong:
- Maintain a buy target and sell target around a reference price (ref).
- If price <= buy_target and inventory < max -> buy
- If price >= sell_target and inventory > 0 -> sell

Safety:
- Observe-only by default (no trades) unless --execute AND --confirm-live YES.
- Daily loss guard (realized + unrealized mark-to-last) halts and requires manual unhalt.

Discord:
- Uses CLOBBOT_DISCORD_WEBHOOK_URL / DISCORD_WEBHOOK_URL
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen


SIMMER_API_BASE = "https://api.simmer.markets"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_FILE = str(DEFAULT_REPO_ROOT / "logs" / "simmer-pingpong.log")
DEFAULT_STATE_FILE = str(DEFAULT_REPO_ROOT / "logs" / "simmer_pingpong_state.json")
DEFAULT_METRICS_FILE = str(DEFAULT_REPO_ROOT / "logs" / "simmer-pingpong-metrics.jsonl")


def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_ts() -> float:
    return time.time()


def local_day_key() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _env_str(name: str) -> str:
    return str(os.environ.get(name, "") or "").strip()


def _resolve_repo_path(raw_path: str) -> Path:
    p = Path(str(raw_path or "").strip())
    if not p:
        return DEFAULT_REPO_ROOT
    if p.is_absolute():
        return p
    return (DEFAULT_REPO_ROOT / p).resolve()


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


def _post_json(url: str, payload: dict, timeout_sec: float = 7.0) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "simmer-pingpong-bot/1.0",
        },
        method="POST",
    )
    with urlopen(req, timeout=timeout_sec) as _:
        return


def maybe_notify_discord(logger: "Logger", message: str) -> None:
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
            _post_json(url, {"content": content}, timeout_sec=7.0)
        except Exception as e:
            code = getattr(e, "code", None)
            if isinstance(code, int):
                logger.info(f"[{iso_now()}] notify(discord) failed: HTTP {code}")
            else:
                logger.info(f"[{iso_now()}] notify(discord) failed: {type(e).__name__}")

    threading.Thread(target=_send, daemon=True).start()


class Logger:
    def __init__(self, log_file: str):
        self.log_file = Path(log_file)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, msg: str) -> None:
        with self.log_file.open("a", encoding="utf-8", newline="\n") as f:
            f.write(msg + "\n")

    def info(self, msg: str) -> None:
        # Avoid crashing on Windows consoles with legacy encodings (cp932, etc.).
        try:
            print(msg)
        except UnicodeEncodeError:
            try:
                enc = getattr(sys.stdout, "encoding", None) or "utf-8"
                print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))
            except Exception:
                pass
        self._append(msg)


def acquire_instance_lock(lock_file: Path, logger: Logger):
    """Best-effort single-instance lock per state file."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fp = lock_file.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt  # type: ignore

            fp.seek(0)
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # type: ignore

            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            fp.close()
        except Exception:
            pass
        logger.info(f"[{iso_now()}] skipped: another instance is already running (lock={lock_file})")
        return None

    try:
        fp.seek(0)
        fp.truncate(0)
        fp.write(str(os.getpid()))
        fp.flush()
    except Exception:
        pass
    return fp


def sdk_request(api_key: str, method: str, endpoint: str, data: dict | None = None, timeout_sec: float = 30.0) -> dict:
    url = f"{SIMMER_API_BASE}{endpoint}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        if method == "GET":
            req = Request(url, headers=headers)
        else:
            body = json.dumps(data or {}).encode("utf-8")
            req = Request(url, data=body, headers=headers, method=method)
        with urlopen(req, timeout=timeout_sec) as r:
            return json.loads(r.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        return {"error": f"HTTP {e.code}: {error_body}"}
    except Exception as e:
        return {"error": str(e)}


def fetch_public_markets(status: str, limit: int, tag: str) -> list[dict]:
    qs = f"status={status}&limit={int(limit)}"
    if tag:
        qs = f"tags={tag}&" + qs
    url = f"{SIMMER_API_BASE}/api/markets?{qs}"
    with urlopen(url, timeout=20) as r:
        data = json.loads(r.read().decode())
    return data.get("markets", []) if isinstance(data, dict) else []


def _parse_iso_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Example: 2026-02-15T23:59:59
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _as_float(x, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _asset_bucket_from_label(label: str) -> str:
    t = str(label or "").lower()
    if "bitcoin" in t or " btc " in f" {t} ":
        return "bitcoin"
    if "ethereum" in t or " eth " in f" {t} ":
        return "ethereum"
    if "solana" in t or " sol " in f" {t} ":
        return "solana"
    if "dogecoin" in t or "doge" in t:
        return "doge"
    if "xrp" in t or "ripple" in t:
        return "xrp"
    return "other"


def _parse_asset_quotas(raw: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    text = str(raw or "").strip()
    if not text:
        return out
    for part in text.split(","):
        item = str(part or "").strip()
        if not item or ":" not in item:
            continue
        name, value = item.split(":", 1)
        asset = str(name or "").strip().lower()
        if not asset:
            continue
        try:
            q = int(float(str(value or "").strip()))
        except Exception:
            continue
        if q > 0:
            out[asset] = q
    return out


def _str_to_bool(s: str) -> Optional[bool]:
    v = str(s or "").strip().lower()
    if not v:
        return None
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _choose_markets_auto(
    markets: list[dict],
    count: int,
    min_to_resolve_min: float,
    max_to_resolve_min: float,
    min_divergence: float,
    prob_min: float,
    prob_max: float,
    asset_quotas: Optional[Dict[str, int]] = None,
) -> list[tuple[str, str]]:
    now = datetime.now().astimezone()
    scored: list[tuple[float, str, str, str]] = []
    for m in markets or []:
        mid = str(m.get("id") or "").strip()
        q = str(m.get("question") or "").strip()
        if not mid or not q:
            continue

        p = _as_float(m.get("probability"), math.nan)
        if not math.isfinite(p) or p <= 0 or p >= 1:
            continue
        if p < prob_min or p > prob_max:
            continue

        resolves_at = _parse_iso_dt(str(m.get("resolves_at") or ""))
        if resolves_at:
            resolves_at = resolves_at.astimezone()
            mins = (resolves_at - now).total_seconds() / 60.0
            if mins < float(min_to_resolve_min or 0.0):
                continue
            if float(max_to_resolve_min or 0.0) > 0.0 and mins > float(max_to_resolve_min):
                continue
        elif float(max_to_resolve_min or 0.0) > 0.0:
            continue

        # Basic score: prefer probabilities near 0.5 (more room to move in both directions).
        score = 1.0 - abs(p - 0.5)

        # If divergence exists, prefer larger magnitude (might correlate with activity/mispricing).
        div_abs = abs(_as_float(m.get("divergence"), 0.0))
        if div_abs < float(min_divergence or 0.0):
            continue
        score += min(0.5, div_abs)

        scored.append((score, mid, q, _asset_bucket_from_label(q)))

    scored.sort(key=lambda x: x[0], reverse=True)

    max_count = max(0, int(count or 0))
    if max_count <= 0:
        return []

    quotas = dict(asset_quotas or {})
    if not quotas:
        out: list[tuple[str, str]] = []
        for _, mid, q, _asset in scored[:max_count]:
            out.append((mid, q))
        return out

    out: list[tuple[str, str]] = []
    used: set[str] = set()
    # First pass: satisfy asset minimum quotas.
    for asset, quota in quotas.items():
        need = min(max(0, int(quota or 0)), max_count - len(out))
        if need <= 0:
            continue
        picked = 0
        for _, mid, q, bucket in scored:
            if mid in used or bucket != asset:
                continue
            out.append((mid, q))
            used.add(mid)
            picked += 1
            if picked >= need:
                break
        if len(out) >= max_count:
            break

    # Second pass: fill remaining slots by global score.
    if len(out) < max_count:
        for _, mid, q, _bucket in scored:
            if mid in used:
                continue
            out.append((mid, q))
            used.add(mid)
            if len(out) >= max_count:
                break

    return out


@dataclass
class MarketState:
    market_id: str
    label: str
    active: bool = True
    inventory_yes_shares: float = 0.0
    avg_cost: float = 0.0
    realized_pnl: float = 0.0

    last_price_yes: float = 0.0
    last_quote_ts: float = 0.0
    buy_target: float = 0.0
    sell_target: float = 0.0
    last_fill_ts: float = 0.0

    buy_trades: int = 0
    sell_trades: int = 0


@dataclass
class RuntimeState:
    market_states: Dict[str, MarketState] = field(default_factory=dict)
    active_market_ids: List[str] = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""
    consecutive_errors: int = 0
    day_key: str = ""
    day_pnl_anchor: float = 0.0


def load_state(path: Path) -> RuntimeState:
    if not path.exists():
        return RuntimeState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        ms: Dict[str, MarketState] = {}
        for mid, s in (raw.get("market_states") or {}).items():
            ms[mid] = MarketState(**s)
        return RuntimeState(
            market_states=ms,
            active_market_ids=list(raw.get("active_market_ids") or []),
            halted=bool(raw.get("halted", False)),
            halt_reason=str(raw.get("halt_reason", "")),
            consecutive_errors=int(raw.get("consecutive_errors", 0)),
            day_key=str(raw.get("day_key") or ""),
            day_pnl_anchor=_as_float(raw.get("day_pnl_anchor"), 0.0),
        )
    except Exception:
        return RuntimeState()


def save_state(path: Path, state: RuntimeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        "market_states": {k: asdict(v) for k, v in state.market_states.items()},
        "active_market_ids": list(state.active_market_ids or []),
        "halted": state.halted,
        "halt_reason": state.halt_reason,
        "consecutive_errors": state.consecutive_errors,
        "day_key": state.day_key,
        "day_pnl_anchor": state.day_pnl_anchor,
    }
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def _update_inventory_buy(s: MarketState, shares_bought: float, cost_spent: float) -> None:
    if shares_bought <= 0 or cost_spent <= 0:
        return
    total_cost = s.avg_cost * s.inventory_yes_shares + (cost_spent / shares_bought) * shares_bought
    s.inventory_yes_shares += shares_bought
    s.avg_cost = (total_cost / s.inventory_yes_shares) if s.inventory_yes_shares > 0 else 0.0


def _update_inventory_sell(s: MarketState, shares_sold: float, price_yes: float) -> None:
    if shares_sold <= 0:
        return
    # Best-effort realized PnL vs avg_cost.
    pnl = (price_yes - s.avg_cost) * shares_sold
    s.realized_pnl += pnl
    remaining = max(0.0, s.inventory_yes_shares - min(shares_sold, s.inventory_yes_shares))
    # Guard against tiny floating-point dust that can re-trigger sell logic.
    if remaining < 1e-9:
        remaining = 0.0
    s.inventory_yes_shares = remaining
    if s.inventory_yes_shares <= 0:
        s.avg_cost = 0.0


def _extract_fill_metrics(result: dict) -> tuple[float, float]:
    # Mirrors simmer-weather skill robustness.
    share_fields = ("shares_bought", "shares_sold", "shares", "filled_shares")
    cost_fields = ("cost", "amount_spent", "spent", "filled_cost")
    shares = 0.0
    cost = 0.0
    for f in share_fields:
        shares = max(shares, _as_float(result.get(f), 0.0))
    for f in cost_fields:
        cost = max(cost, _as_float(result.get(f), 0.0))
    return shares, cost


def _extract_rate_limit_wait_sec(err: str) -> float:
    text = str(err or "")
    m = re.search(r"per\s+(\d+)\s*s", text, flags=re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return 0.0
    m = re.search(r"wait\s+(\d+)\s*s", text, flags=re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return 0.0
    return 0.0


def _compute_total_pnl(state: RuntimeState) -> float:
    total = 0.0
    for s in (state.market_states or {}).values():
        p = float(s.last_price_yes or 0.0)
        inv = float(s.inventory_yes_shares or 0.0)
        avg = float(s.avg_cost or 0.0)
        realized = float(s.realized_pnl or 0.0)
        unreal = (p - avg) * inv if (inv > 0 and p > 0 and avg > 0) else 0.0
        total += realized + unreal
    return float(total)


def _append_metrics(metrics_file: str, payload: dict) -> None:
    if not metrics_file:
        return
    p = Path(metrics_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _apply_env_overrides(args):
    # CLI args take precedence over env vars.
    cli_flags: set[str] = set()
    for tok in sys.argv[1:]:
        t = str(tok or "").strip()
        if not t.startswith("--"):
            continue
        key = t[2:].split("=", 1)[0].strip().lower()
        if key:
            cli_flags.add(key)

    prefix = "SIMMER_PONG_"
    for k, v in vars(args).items():
        # Live execution must be explicitly enabled via CLI args.
        if k in {"execute", "confirm_live"}:
            continue
        cli_name = k.replace("_", "-").lower()
        if cli_name in cli_flags:
            continue
        env_name = prefix + k.upper()
        if isinstance(v, bool):
            b = _env_bool(env_name)
            if b is not None:
                setattr(args, k, b)
        elif isinstance(v, int):
            x = _env_int(env_name)
            if x is not None:
                setattr(args, k, x)
        elif isinstance(v, float):
            x = _env_float(env_name)
            if x is not None:
                setattr(args, k, x)
        elif isinstance(v, str):
            s = _env_str(env_name)
            if s:
                setattr(args, k, s)
    return args


async def run(args) -> int:
    log_file = str(_resolve_repo_path(args.log_file or DEFAULT_LOG_FILE))
    state_file = _resolve_repo_path(args.state_file or DEFAULT_STATE_FILE)
    args.metrics_file = str(_resolve_repo_path(args.metrics_file or DEFAULT_METRICS_FILE))
    lock_file = Path(str(state_file) + ".lock")
    logger = Logger(log_file)
    _instance_lock = acquire_instance_lock(lock_file, logger)
    if _instance_lock is None:
        return 0

    env_exec = _str_to_bool(_env_str("SIMMER_PONG_EXECUTE"))
    reg_exec = _str_to_bool(_user_env_from_registry("SIMMER_PONG_EXECUTE"))
    if not args.execute and (env_exec is True or reg_exec is True):
        logger.info(f"[{iso_now()}] note: SIMMER_PONG_EXECUTE=1 is ignored. Live mode requires CLI --execute --confirm-live YES.")

    if args.execute and args.confirm_live != "YES":
        raise SystemExit('Refusing live mode: pass --confirm-live YES with --execute')
    paper_mode = bool(args.paper_trades and (not args.execute))
    if args.paper_trades and args.execute:
        logger.info(f"[{iso_now()}] note: --paper-trades is ignored in LIVE mode.")

    api_key = _env_str("SIMMER_API_KEY")
    api_key_source = "process-env" if api_key else ""
    if not api_key:
        api_key = _user_env_from_registry("SIMMER_API_KEY")
        api_key_source = "HKCU\\Environment" if api_key else ""
    if not api_key:
        logger.info(f"[{iso_now()}] fatal: SIMMER_API_KEY is not set (User env). Get it from simmer.markets/dashboard -> SDK.")
        return 2
    logger.info(f"[{iso_now()}] SIMMER_API_KEY: OK (source={api_key_source})")

    state = load_state(state_file)

    logger.info("Simmer Ping-Pong Bot")
    logger.info("=" * 56)
    logger.info(f"Mode: {'LIVE' if args.execute else 'observe-only'} | venue={args.venue}")
    logger.info(f"Paper trades: {'on' if paper_mode else 'off'}")
    logger.info(f"Universe: {'manual' if args.market_ids else 'auto'} | markets_target={args.auto_select_count if not args.market_ids else len(args.market_ids.split(','))}")
    logger.info(f"Spread: {args.spread_cents:.2f}c | trade_shares={args.trade_shares:.2f} | max_inventory_shares={args.max_inventory_shares:.2f}")
    logger.info(f"Poll: {args.poll_sec:.2f}s | refresh: {args.quote_refresh_sec:.1f}s | metrics_every={args.metrics_sample_sec:.0f}s")
    logger.info(
        "Exit controls: "
        f"max_hold_sec={float(args.max_hold_sec or 0.0):.0f} "
        f"sell_decay={float(args.sell_target_decay_cents_per_min or 0.0):.3f}c/min"
    )
    logger.info(
        "Universe filters: "
        f"resolve_min={float(args.min_time_to_resolve_min or 0.0):.0f}m "
        f"resolve_max={float(args.max_time_to_resolve_min or 0.0):.0f}m(0=disabled) "
        f"divergence_min={float(args.min_divergence or 0.0):.4f} "
        f"prob=[{float(args.prob_min):.2f},{float(args.prob_max):.2f}]"
    )
    logger.info(f"Loss guard: daily_limit=${args.daily_loss_limit_usd:.2f} (0=disabled)")
    logger.info(f"Log: {log_file}")
    logger.info(f"State: {state_file}")

    maybe_notify_discord(logger, f"SIMMER_PONG started ({'LIVE' if args.execute else 'observe'}) | spread={args.spread_cents:.2f}c shares={args.trade_shares:g} venue={args.venue}")

    # Select markets.
    asset_quotas = _parse_asset_quotas(str(args.asset_quotas or ""))
    if asset_quotas:
        logger.info("Asset quotas: " + ", ".join([f"{k}:{v}" for k, v in asset_quotas.items()]))

    selected: list[tuple[str, str]] = []
    if args.market_ids:
        for mid in [x.strip() for x in args.market_ids.split(",") if x.strip()]:
            selected.append((mid, f"market:{mid}"))
    else:
        markets = fetch_public_markets(status="active", limit=int(args.public_limit), tag=str(args.public_tag or "").strip())
        selected = _choose_markets_auto(
            markets=markets,
            count=int(args.auto_select_count or 0),
            min_to_resolve_min=float(args.min_time_to_resolve_min or 0.0),
            max_to_resolve_min=float(args.max_time_to_resolve_min or 0.0),
            min_divergence=float(args.min_divergence or 0.0),
            prob_min=float(args.prob_min),
            prob_max=float(args.prob_max),
            asset_quotas=asset_quotas,
        )

    if not selected:
        logger.info(f"[{iso_now()}] fatal: no markets selected (adjust filters or set --market-ids).")
        maybe_notify_discord(logger, "SIMMER_PONG HALT: no markets selected.")
        return 2

    active_ids = [mid for mid, _ in selected]
    active_set = set(active_ids)
    state.active_market_ids = list(active_ids)
    # Keep stale markets as inactive to preserve PnL continuity across universe changes.
    for mid in list(state.market_states.keys()):
        if mid not in active_set:
            state.market_states[mid].active = False

    logger.info("Selected markets:")
    for mid, label in selected:
        logger.info(f"  - {mid} | {label[:120]}")

    # Initialize market state.
    for mid, label in selected:
        if mid not in state.market_states:
            state.market_states[mid] = MarketState(market_id=mid, label=label)
        else:
            state.market_states[mid].label = label
        state.market_states[mid].active = True

    save_state(state_file, state)

    start_ts = now_ts()
    last_summary = 0.0
    last_metrics = 0.0
    trade_cooldowns: Dict[Tuple[str, str], float] = {}
    signal_log_cooldowns: Dict[Tuple[str, str], float] = {}
    signal_log_every_sec = max(15.0, min(120.0, float(args.quote_refresh_sec or 30.0)))

    while True:
        if int(args.run_seconds or 0) > 0 and (now_ts() - start_ts) >= int(args.run_seconds):
            logger.info(f"[{iso_now()}] run_seconds reached -> exiting")
            break
        if state.halted:
            await asyncio.sleep(2.0)
            continue

        try:
            # Daily anchor update.
            today = local_day_key()
            if not state.day_key:
                state.day_key = today
                state.day_pnl_anchor = _compute_total_pnl(state)
            elif state.day_key != today and not state.halted:
                state.day_key = today
                state.day_pnl_anchor = _compute_total_pnl(state)

            now = now_ts()
            half = float(args.spread_cents or 2.0) / 200.0

            for mid in list(state.market_states.keys()):
                s = state.market_states[mid]
                if not s.active:
                    continue

                # Fetch context (best-effort); fallback to public probability if needed.
                ctx = sdk_request(api_key, "GET", f"/api/sdk/context/{mid}")
                if isinstance(ctx, dict) and "error" not in ctx:
                    market = ctx.get("market") or {}
                    p = _as_float(market.get("probability") or market.get("price_yes") or market.get("current_price"), 0.0)
                else:
                    # Fallback: public endpoint might not support per-id fetch; keep last.
                    p = float(s.last_price_yes or 0.0)
                if p <= 0 or p >= 1:
                    continue

                s.last_price_yes = float(p)
                hold_sec = 0.0
                if float(s.inventory_yes_shares or 0.0) > 1e-9 and float(s.last_fill_ts or 0.0) > 0:
                    hold_sec = max(0.0, now - float(s.last_fill_ts or 0.0))

                # Quote refresh: avoid chase-recentering while carrying inventory.
                flat_inventory = float(s.inventory_yes_shares or 0.0) <= 0.0
                targets_unset = s.buy_target <= 0 and s.sell_target <= 0
                if targets_unset or (flat_inventory and (now - float(s.last_quote_ts or 0.0)) >= float(args.quote_refresh_sec or 30.0)):
                    s.buy_target = max(0.001, float(p) - half)
                    s.sell_target = min(0.999, float(p) + half)
                    s.last_quote_ts = now

                # Entry (buy).
                max_inventory = float(args.max_inventory_shares or 0.0)
                remaining_shares = (max_inventory - float(s.inventory_yes_shares or 0.0)) if max_inventory > 0 else float("inf")
                allow_buy = remaining_shares > 0.0
                buy_cd_until = float(trade_cooldowns.get((mid, "buy"), 0.0))
                seed_interval_sec = float(args.paper_seed_every_sec or 0.0)
                seed_trigger = False
                if paper_mode and seed_interval_sec > 0 and float(s.inventory_yes_shares or 0.0) <= 1e-9:
                    last_fill = float(s.last_fill_ts or 0.0)
                    seed_trigger = (last_fill <= 0.0) or ((now - last_fill) >= seed_interval_sec)
                buy_signal = float(p) <= float(s.buy_target or 0.0)
                if allow_buy and now >= buy_cd_until and (buy_signal or seed_trigger):
                    buy_filled = False
                    # Runtime entry-band guard: avoid buying tail probabilities after a market shifts/resolves.
                    if (not seed_trigger) and (float(p) < float(args.prob_min) or float(p) > float(args.prob_max)):
                        key = (mid, "skip_entry_band")
                        if now >= float(signal_log_cooldowns.get(key, 0.0)):
                            logger.info(
                                f"[{iso_now()}] BUY skipped market={mid}: p={p:.3f} outside entry band "
                                f"[{float(args.prob_min):.3f}, {float(args.prob_max):.3f}]"
                            )
                            signal_log_cooldowns[key] = now + signal_log_every_sec
                        s.last_quote_ts = now
                        continue

                    trade_shares = max(1.0, float(args.trade_shares or 1.0))
                    target_shares = min(trade_shares, max(0.0, remaining_shares)) if math.isfinite(remaining_shares) else trade_shares
                    if target_shares <= 0:
                        continue

                    # Simmer buy API accepts USD amount. Convert target shares -> amount and enforce caps.
                    min_amount = max(0.0, float(args.min_trade_amount or 0.0))
                    max_amount = max(0.0, float(args.max_trade_amount or 0.0))
                    amount = float(p) * target_shares
                    if max_amount > 0:
                        amount = min(amount, max_amount)

                    # Never violate share/inventory intent due min-amount floors at tiny probabilities.
                    if min_amount > 0 and amount + 1e-12 < min_amount:
                        implied_shares = min_amount / float(p) if float(p) > 0 else float("inf")
                        key = (mid, "skip_min_amount")
                        if now >= float(signal_log_cooldowns.get(key, 0.0)):
                            logger.info(
                                f"[{iso_now()}] BUY skipped market={mid}: min_trade_amount={min_amount:.2f} "
                                f"implies ~{implied_shares:.2f} shares at p={p:.3f} (target={target_shares:.2f}, "
                                f"inv={s.inventory_yes_shares:.2f}, cap={max_inventory:.2f})"
                            )
                            signal_log_cooldowns[key] = now + signal_log_every_sec
                        s.last_quote_ts = now
                        continue

                    amount = max(min_amount, amount)

                    if args.execute:
                        result = sdk_request(
                            api_key,
                            "POST",
                            "/api/sdk/trade",
                            {
                                "market_id": mid,
                                "side": "yes",
                                "amount": amount,
                                "venue": args.venue,
                                "source": args.trade_source,
                            },
                        )
                        if isinstance(result, dict) and result.get("success"):
                            shares, cost = _extract_fill_metrics(result)
                            if shares > 0 and cost > 0:
                                fill_px = cost / shares
                                _update_inventory_buy(s, shares_bought=shares, cost_spent=cost)
                                s.buy_trades += 1
                                logger.info(
                                    f"[{iso_now()}] FILL BUY {shares:.2f} shares (amt={amount:.2f}, fill={fill_px:.3f}, mark={p:.3f}) "
                                    f"| inv={s.inventory_yes_shares:.2f} avg={s.avg_cost:.3f}"
                                )
                                maybe_notify_discord(
                                    logger,
                                    f"SIMMER_PONG FILL BUY {shares:.2f}@{fill_px:.3f} mark={p:.3f} "
                                    f"inv={s.inventory_yes_shares:.2f} avg={s.avg_cost:.3f} | {s.label[:100]}",
                                )
                                anchor = fill_px if fill_px > 0 else float(p)
                                s.buy_target = max(0.001, anchor - half)
                                s.sell_target = min(0.999, anchor + half)
                                s.last_quote_ts = now
                                s.last_fill_ts = now
                                buy_filled = True
                        else:
                            err = (result or {}).get("error", "Unknown error") if isinstance(result, dict) else "Unknown error"
                            logger.info(f"[{iso_now()}] BUY failed market={mid}: {err}")
                            wait_sec = _extract_rate_limit_wait_sec(str(err))
                            if wait_sec > 0:
                                trade_cooldowns[(mid, "buy")] = now + wait_sec + 1.0
                    elif paper_mode:
                        fill_px = float(p)
                        shares = (amount / fill_px) if fill_px > 0 else 0.0
                        shares = min(shares, max(0.0, remaining_shares))
                        cost = shares * fill_px
                        if shares > 0 and cost > 0:
                            _update_inventory_buy(s, shares_bought=shares, cost_spent=cost)
                            s.buy_trades += 1
                            logger.info(
                                f"[{iso_now()}] FILL BUY {shares:.2f} shares (paper:{'seed' if seed_trigger else 'signal'}, amt={amount:.2f}, fill={fill_px:.3f}, mark={p:.3f}) "
                                f"| inv={s.inventory_yes_shares:.2f} avg={s.avg_cost:.3f}"
                            )
                            anchor = fill_px
                            s.buy_target = max(0.001, anchor - half)
                            s.sell_target = min(0.999, anchor + half)
                            s.last_quote_ts = now
                            s.last_fill_ts = now
                            buy_filled = True
                    else:
                        key = (mid, "would_buy")
                        if now >= float(signal_log_cooldowns.get(key, 0.0)):
                            logger.info(f"[{iso_now()}] would BUY amt={amount:.2f} @ p={p:.3f} target={s.buy_target:.3f} | {s.label[:80]}")
                            signal_log_cooldowns[key] = now + signal_log_every_sec

                    # Cool down before next re-evaluation. Filled trades anchor targets explicitly above.
                    if not buy_filled:
                        s.last_quote_ts = now

                # Exit (sell).
                allow_sell = s.inventory_yes_shares > 1e-9
                sell_cd_until = float(trade_cooldowns.get((mid, "sell"), 0.0))
                sell_target_eff = float(s.sell_target or 0.0)
                if allow_sell and hold_sec > 0 and float(args.sell_target_decay_cents_per_min or 0.0) > 0:
                    decay_prob = (float(args.sell_target_decay_cents_per_min) / 100.0) * (hold_sec / 60.0)
                    sell_target_eff = max(0.001, sell_target_eff - decay_prob)
                force_unwind = bool(float(args.max_hold_sec or 0.0) > 0 and hold_sec >= float(args.max_hold_sec or 0.0))
                if force_unwind:
                    key = (mid, "force_unwind")
                    if now >= float(signal_log_cooldowns.get(key, 0.0)):
                        logger.info(
                            f"[{iso_now()}] FORCE SELL market={mid}: hold={hold_sec:.0f}s >= max_hold_sec={float(args.max_hold_sec):.0f}s "
                            f"(p={p:.3f}, target={sell_target_eff:.3f})"
                        )
                        signal_log_cooldowns[key] = now + signal_log_every_sec

                if allow_sell and now >= sell_cd_until and (force_unwind or float(p) >= sell_target_eff):
                    sell_filled = False
                    sell_shares = min(float(args.trade_shares or 1.0), float(s.inventory_yes_shares))
                    if sell_shares <= 0:
                        continue
                    if args.execute:
                        result = sdk_request(
                            api_key,
                            "POST",
                            "/api/sdk/trade",
                            {
                                "market_id": mid,
                                "side": "yes",
                                "action": "sell",
                                "shares": sell_shares,
                                "venue": args.venue,
                                "source": args.trade_source,
                            },
                        )
                        if isinstance(result, dict) and result.get("success"):
                            shares, _ = _extract_fill_metrics(result)
                            filled = min(float(shares), float(sell_shares)) if shares > 0 else 0.0
                            if filled <= 0:
                                logger.info(f"[{iso_now()}] SELL success but 0 fill market={mid}; retry in 30s")
                                trade_cooldowns[(mid, "sell")] = now + 30.0
                            else:
                                _update_inventory_sell(s, shares_sold=filled, price_yes=float(p))
                                s.sell_trades += 1
                                logger.info(f"[{iso_now()}] FILL SELL {filled:.2f} shares @ p={p:.3f} | inv={s.inventory_yes_shares:.2f} pnl={s.realized_pnl:+.3f}")
                                maybe_notify_discord(logger, f"SIMMER_PONG FILL SELL {filled:.2f}@{p:.3f} inv={s.inventory_yes_shares:.2f} pnl={s.realized_pnl:+.3f} | {s.label[:100]}")
                                anchor = float(p)
                                s.buy_target = max(0.001, anchor - half)
                                s.sell_target = min(0.999, anchor + half)
                                s.last_quote_ts = now
                                s.last_fill_ts = now
                                sell_filled = True
                        else:
                            err = (result or {}).get("error", "Unknown error") if isinstance(result, dict) else "Unknown error"
                            logger.info(f"[{iso_now()}] SELL failed market={mid}: {err}")
                            wait_sec = _extract_rate_limit_wait_sec(str(err))
                            if wait_sec > 0:
                                trade_cooldowns[(mid, "sell")] = now + wait_sec + 1.0
                    elif paper_mode:
                        filled = min(float(sell_shares), float(s.inventory_yes_shares))
                        if filled > 0:
                            _update_inventory_sell(s, shares_sold=filled, price_yes=float(p))
                            s.sell_trades += 1
                            logger.info(
                                f"[{iso_now()}] FILL SELL {filled:.2f} shares @ p={p:.3f} (paper) "
                                f"| inv={s.inventory_yes_shares:.2f} pnl={s.realized_pnl:+.3f}"
                            )
                            anchor = float(p)
                            s.buy_target = max(0.001, anchor - half)
                            s.sell_target = min(0.999, anchor + half)
                            s.last_quote_ts = now
                            s.last_fill_ts = now
                            sell_filled = True
                    else:
                        key = (mid, "would_sell")
                        if now >= float(signal_log_cooldowns.get(key, 0.0)):
                            logger.info(
                                f"[{iso_now()}] would SELL {sell_shares:.2f} @ p={p:.3f} "
                                f"target={sell_target_eff:.3f} hold={hold_sec:.0f}s | {s.label[:80]}"
                            )
                            signal_log_cooldowns[key] = now + signal_log_every_sec

                    if not sell_filled:
                        s.last_quote_ts = now

            # Metrics sampling.
            if float(args.metrics_sample_sec or 0.0) > 0 and (now_ts() - last_metrics) >= float(args.metrics_sample_sec):
                last_metrics = now_ts()
                for mid, s in state.market_states.items():
                    if not s.active:
                        continue
                    _append_metrics(
                        args.metrics_file,
                        {
                            "ts": iso_now(),
                            "ts_ms": int(now_ts() * 1000.0),
                            "market_id": mid,
                            "label": s.label[:180],
                            "p_yes": float(s.last_price_yes or 0.0),
                            "buy_target": float(s.buy_target or 0.0),
                            "sell_target": float(s.sell_target or 0.0),
                            "inv": float(s.inventory_yes_shares or 0.0),
                        },
                    )

            # Periodic summary.
            if float(args.summary_every_sec or 0.0) > 0 and (now_ts() - last_summary) >= float(args.summary_every_sec):
                last_summary = now_ts()
                total = _compute_total_pnl(state)
                today_pnl = total - float(state.day_pnl_anchor or 0.0)
                parts = []
                for s in state.market_states.values():
                    if not s.active:
                        continue
                    parts.append(f"inv={s.inventory_yes_shares:.1f} pnl={s.realized_pnl:+.2f} {s.label[:32]}")
                msg = f"SIMMER_PONG summary: pnl_today={today_pnl:+.2f} total={total:+.2f} | " + " | ".join(parts)[:1600]
                logger.info(f"[{iso_now()}] {msg}")
                maybe_notify_discord(logger, msg)

            # Daily loss guard.
            if float(args.daily_loss_limit_usd or 0.0) > 0:
                total = _compute_total_pnl(state)
                today_pnl = total - float(state.day_pnl_anchor or 0.0)
                if today_pnl <= -float(args.daily_loss_limit_usd):
                    state.halted = True
                    state.halt_reason = f"Daily loss guard hit: pnl_today {today_pnl:+.2f} <= -{float(args.daily_loss_limit_usd):.2f}"
                    logger.info(f"[{iso_now()}] HALT: {state.halt_reason}")
                    maybe_notify_discord(logger, f"SIMMER_PONG HALT: {state.halt_reason}")
                    save_state(state_file, state)
                    await asyncio.sleep(2.0)
                    continue

            state.consecutive_errors = 0
            save_state(state_file, state)
            await asyncio.sleep(float(args.poll_sec or 2.0))

        except Exception as e:
            state.consecutive_errors += 1
            logger.info(f"[{iso_now()}] error: loop exception: {e}")
            if state.consecutive_errors >= int(args.max_consecutive_errors or 7):
                state.halted = True
                state.halt_reason = f"Consecutive errors cap reached ({state.consecutive_errors}/{args.max_consecutive_errors})"
                logger.info(f"[{iso_now()}] HALT: {state.halt_reason}")
                maybe_notify_discord(logger, f"SIMMER_PONG HALT: {state.halt_reason}")
            save_state(state_file, state)
            await asyncio.sleep(2.0)

    maybe_notify_discord(logger, "SIMMER_PONG stopped")
    save_state(state_file, state)
    return 0


def parse_args():
    p = argparse.ArgumentParser(description="Simmer inventory ping-pong (venue=simmer demo)")
    p.add_argument("--venue", default="simmer", help="venue: simmer|polymarket|kalshi")
    p.add_argument("--trade-source", default="pingpong-mm", help="source label attached to trades")

    p.add_argument("--market-ids", default="", help="Comma-separated market UUIDs to trade (manual universe)")
    p.add_argument("--auto-select-count", type=int, default=3, help="Auto-select N markets from public list")
    p.add_argument("--public-limit", type=int, default=200, help="Public markets fetch limit")
    p.add_argument("--public-tag", default="crypto", help="Public markets filter tag (empty=all)")
    p.add_argument(
        "--asset-quotas",
        default="",
        help="Optional minimum asset quotas for auto-universe (e.g. bitcoin:2,ethereum:1,solana:1)",
    )
    p.add_argument("--min-time-to-resolve-min", type=float, default=30.0, help="Filter: minimum minutes until resolves_at")
    p.add_argument("--max-time-to-resolve-min", type=float, default=0.0, help="Filter: maximum minutes until resolves_at (0=disabled)")
    p.add_argument("--min-divergence", type=float, default=0.0, help="Filter: minimum abs(divergence) for auto-selected markets")
    p.add_argument("--prob-min", type=float, default=0.05, help="Filter: min probability")
    p.add_argument("--prob-max", type=float, default=0.95, help="Filter: max probability")

    p.add_argument("--spread-cents", type=float, default=3.0, help="Total ping-pong band (cents)")
    p.add_argument("--trade-shares", type=float, default=5.0, help="Target shares per leg (buy amount is derived from this)")
    p.add_argument("--max-inventory-shares", type=float, default=10.0, help="Inventory cap (shares)")
    p.add_argument("--min-trade-amount", type=float, default=1.0, help="Buy amount floor (trade skipped if this implies oversize)")
    p.add_argument("--max-trade-amount", type=float, default=5.0, help="Buy amount cap")

    p.add_argument("--poll-sec", type=float, default=2.0, help="Polling interval")
    p.add_argument("--quote-refresh-sec", type=float, default=30.0, help="Re-center targets every N sec")
    p.add_argument("--max-hold-sec", type=float, default=0.0, help="Force inventory unwind after this holding time (0=disabled)")
    p.add_argument(
        "--sell-target-decay-cents-per-min",
        type=float,
        default=0.0,
        help="Lower sell target over time while inventory is open (0=disabled)",
    )
    p.add_argument("--summary-every-sec", type=float, default=3600.0, help="Discord summary interval (0=disabled)")
    p.add_argument("--metrics-file", default=DEFAULT_METRICS_FILE, help="Metrics JSONL path")
    p.add_argument("--metrics-sample-sec", type=float, default=60.0, help="Metrics sample interval (0=disabled)")

    p.add_argument("--daily-loss-limit-usd", type=float, default=5.0, help="Daily loss guard (0=disabled)")
    p.add_argument("--max-consecutive-errors", type=int, default=7, help="Halt after N consecutive errors")

    p.add_argument("--run-seconds", type=int, default=0, help="Auto-exit after N seconds (0=run forever)")
    p.add_argument("--paper-trades", action="store_true", help="Observe-only synthetic fills on signal (no API trades)")
    p.add_argument("--paper-seed-every-sec", type=float, default=0.0, help="Observe-only: force a synthetic entry when flat every N sec (0=disabled)")
    p.add_argument("--execute", action="store_true", help="Enable live trades")
    p.add_argument("--confirm-live", default="", help='Must be "YES" when --execute is enabled')

    p.add_argument("--log-file", default=DEFAULT_LOG_FILE, help="Log file path")
    p.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="State file path")

    args = p.parse_args()
    return _apply_env_overrides(args)


if __name__ == "__main__":
    args = parse_args()
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("\nStopped by user.")
