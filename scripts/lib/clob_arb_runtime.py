from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.parse
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Pattern, Set, Tuple
from urllib.request import Request, urlopen

from lib.clob_arb_models import EventBasket, RuntimeState
from lib.clob_arb_sports import parse_sports_market_type_filter
from lib.clob_arb_universe import parse_btc_updown_window_minutes
from lib.runtime_common import day_key_local, iso_now, now_ts
from polymarket_clob_arb_scanner import as_float


SIMMER_API_BASE = "https://api.simmer.markets"


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
    state_file.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def sdk_request(api_key: str, method: str, endpoint: str, data: Optional[dict] = None) -> dict:
    if not api_key:
        return {"success": False, "error": "SIMMER_API_KEY missing"}

    url = SIMMER_API_BASE.rstrip("/") + endpoint
    body = None
    if method.upper() == "GET" and data:
        q = urllib.parse.urlencode({k: v for k, v in data.items() if v is not None})
        if q:
            url = url + ("?" if "?" not in url else "&") + q
    elif data is not None:
        body = json.dumps(data).encode("utf-8")

    req = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "clob-arb-monitor/1.0",
        },
        method=method.upper(),
    )

    try:
        with urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            if isinstance(payload, dict):
                return payload
            return {"success": True, "data": payload}
    except Exception as e:
        return {"success": False, "error": str(e)}


def fetch_simmer_portfolio(api_key: str) -> Optional[dict]:
    resp = sdk_request(api_key, "GET", "/api/sdk/portfolio")
    if not isinstance(resp, dict):
        return None

    if "pnl_total" in resp:
        return resp

    data = resp.get("data")
    if isinstance(data, dict):
        portfolio = data.get("portfolio")
        if isinstance(portfolio, dict):
            return portfolio

    portfolio = resp.get("portfolio")
    if isinstance(portfolio, dict):
        return portfolio
    return None


def fetch_simmer_settings(api_key: str) -> Optional[dict]:
    resp = sdk_request(api_key, "GET", "/api/sdk/settings")
    if isinstance(resp, dict) and isinstance(resp.get("settings"), dict):
        return resp.get("settings")
    if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
        return resp.get("data")
    return None


def fetch_simmer_positions(api_key: str) -> List[dict]:
    resp = sdk_request(api_key, "GET", "/api/sdk/portfolio")
    if not isinstance(resp, dict):
        return []
    positions = resp.get("positions")
    if isinstance(positions, list):
        return [p for p in positions if isinstance(p, dict)]
    data = resp.get("data")
    if isinstance(data, dict) and isinstance(data.get("positions"), list):
        return [p for p in data.get("positions") if isinstance(p, dict)]
    return []


def maybe_apply_daily_loss_guard(
    state: RuntimeState,
    args,
    logger,
    notify_func: Callable[[object, str], None],
) -> bool:
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

    pnl_total = as_float(portfolio.get("pnl_total"), float("nan"))
    if not isinstance(pnl_total, float) or pnl_total != pnl_total:
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
        notify_func(logger, f"CLOBBOT HALT: {state.halt_reason}")
        return False

    return True


def initialize_execution_backend(
    args,
    logger,
    baskets: List[EventBasket],
    build_clob_client_func: Callable[[Any], Any],
) -> Dict[str, Any]:
    exec_backend = "none"
    client = None
    simmer_api_key = ""
    updated_baskets = list(baskets)

    if not bool(getattr(args, "execute", False)):
        return {
            "ok": True,
            "exit_code": 0,
            "exec_backend": exec_backend,
            "client": client,
            "simmer_api_key": simmer_api_key,
            "baskets": updated_baskets,
            "baskets_changed": False,
        }

    if str(getattr(args, "confirm_live", "") or "") != "YES":
        logger.info("Refusing live mode. Re-run with --confirm-live YES")
        return {
            "ok": False,
            "exit_code": 1,
            "exec_backend": exec_backend,
            "client": client,
            "simmer_api_key": simmer_api_key,
            "baskets": updated_baskets,
            "baskets_changed": False,
        }

    requested_backend = str(getattr(args, "exec_backend", "auto") or "auto").strip().lower()
    if requested_backend == "auto":
        if os.environ.get("PM_PRIVATE_KEY") and (os.environ.get("PM_FUNDER") or os.environ.get("PM_PROXY_ADDRESS")):
            exec_backend = "clob"
        elif os.environ.get("SIMMER_API_KEY"):
            exec_backend = "simmer"
        else:
            logger.info(
                "Live execution init failed: no backend credentials found "
                "(need PM_PRIVATE_KEY+PM_FUNDER for clob or SIMMER_API_KEY for simmer)."
            )
            return {
                "ok": False,
                "exit_code": 1,
                "exec_backend": exec_backend,
                "client": client,
                "simmer_api_key": simmer_api_key,
                "baskets": updated_baskets,
                "baskets_changed": False,
            }
    else:
        exec_backend = requested_backend

    if exec_backend == "clob":
        try:
            client = build_clob_client_func(args)
        except Exception as e:
            logger.info(f"Live execution init failed (clob): {e}")
            return {
                "ok": False,
                "exit_code": 1,
                "exec_backend": exec_backend,
                "client": client,
                "simmer_api_key": simmer_api_key,
                "baskets": updated_baskets,
                "baskets_changed": False,
            }
        logger.info("Live execution backend: clob")
        return {
            "ok": True,
            "exit_code": 0,
            "exec_backend": exec_backend,
            "client": client,
            "simmer_api_key": simmer_api_key,
            "baskets": updated_baskets,
            "baskets_changed": False,
        }

    if exec_backend == "simmer":
        simmer_api_key = str(os.environ.get("SIMMER_API_KEY", "") or "").strip()
        if not simmer_api_key:
            logger.info("Live execution init failed (simmer): SIMMER_API_KEY is missing")
            return {
                "ok": False,
                "exit_code": 1,
                "exec_backend": exec_backend,
                "client": client,
                "simmer_api_key": simmer_api_key,
                "baskets": updated_baskets,
                "baskets_changed": False,
            }

        logger.info(
            f"Live execution backend: simmer (venue={args.simmer_venue}, source={args.simmer_source})"
        )

        if str(getattr(args, "simmer_venue", "") or "").strip().lower() == "polymarket":
            settings = fetch_simmer_settings(simmer_api_key)
            if not settings:
                logger.info("SDK settings unavailable; continuing with venue=polymarket probe/execution path")
            else:
                if settings.get("trading_paused", False):
                    logger.info("Live execution paused: SDK settings show trading_paused=true")
                    return {
                        "ok": False,
                        "exit_code": 0,
                        "exec_backend": exec_backend,
                        "client": client,
                        "simmer_api_key": simmer_api_key,
                        "baskets": updated_baskets,
                        "baskets_changed": False,
                    }
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

        mapped = [basket for basket in updated_baskets if all(leg.simmer_market_id for leg in basket.legs)]
        if not mapped:
            logger.info("Live execution init failed (simmer): no fully-mapped event baskets available")
            return {
                "ok": False,
                "exit_code": 1,
                "exec_backend": exec_backend,
                "client": client,
                "simmer_api_key": simmer_api_key,
                "baskets": updated_baskets,
                "baskets_changed": False,
            }
        baskets_changed = len(mapped) != len(updated_baskets)
        if baskets_changed:
            logger.info(f"Filtered baskets for simmer mapping: {len(updated_baskets)} -> {len(mapped)}")
            updated_baskets = mapped

        return {
            "ok": True,
            "exit_code": 0,
            "exec_backend": exec_backend,
            "client": client,
            "simmer_api_key": simmer_api_key,
            "baskets": updated_baskets,
            "baskets_changed": baskets_changed,
        }

    logger.info(f"Live execution init failed: unsupported exec backend '{exec_backend}'")
    return {
        "ok": False,
        "exit_code": 1,
        "exec_backend": exec_backend,
        "client": client,
        "simmer_api_key": simmer_api_key,
        "baskets": updated_baskets,
        "baskets_changed": False,
    }


def build_monitor_loop_tuning(args) -> Dict[str, Any]:
    observe_exec_edge_filter_strategies_raw = str(
        getattr(args, "observe_exec_edge_filter_strategies", "") or ""
    )
    observe_exec_edge_filter_strategies: Set[str] = {
        x.strip().lower() for x in observe_exec_edge_filter_strategies_raw.split(",") if x.strip()
    }
    return {
        "summary_every": max(0.0, float(getattr(args, "summary_every_sec", 0.0) or 0.0)),
        "min_eval_interval": max(0.0, float(getattr(args, "min_eval_interval_ms", 0) or 0)) / 1000.0,
        "observe_notify_min_interval": max(
            0.0, float(getattr(args, "observe_notify_min_interval_sec", 30.0) or 0.0)
        ),
        "observe_exec_edge_filter": (not bool(getattr(args, "execute", False)))
        and bool(getattr(args, "observe_exec_edge_filter", False)),
        "observe_exec_edge_min_usd": float(getattr(args, "observe_exec_edge_min_usd", 0.0) or 0.0),
        "observe_exec_edge_strike_limit": max(
            1, int(getattr(args, "observe_exec_edge_strike_limit", 3) or 1)
        ),
        "observe_exec_edge_cooldown_sec": max(
            1.0, float(getattr(args, "observe_exec_edge_cooldown_sec", 300.0) or 300.0)
        ),
        "observe_exec_edge_filter_strategies": observe_exec_edge_filter_strategies,
    }


def resolve_monitor_paths(args, script_dir: Path) -> Dict[str, Any]:
    if not getattr(args, "log_file", ""):
        args.log_file = str(script_dir.parent / "logs" / "clob-arb-monitor.log")
    if not getattr(args, "state_file", ""):
        args.state_file = str(script_dir.parent / "logs" / "clob_arb_state.json")

    metrics_file_raw = str(getattr(args, "metrics_file", "") or "").strip()
    if not metrics_file_raw:
        metrics_file_raw = str(script_dir.parent / "logs" / "clob-arb-monitor-metrics.jsonl")

    metrics_file: Optional[Path]
    if metrics_file_raw.lower() in {"off", "none", "null", "disable", "disabled", "0"}:
        metrics_file = None
        args.metrics_file = ""
    else:
        metrics_file = Path(metrics_file_raw)
        metrics_file.parent.mkdir(parents=True, exist_ok=True)
        args.metrics_file = str(metrics_file)

    return {
        "state_file": Path(str(args.state_file)),
        "metrics_file": metrics_file,
    }


def compile_gamma_market_regexes(args, logger) -> Tuple[Optional[Pattern[str]], Optional[Pattern[str]]]:
    include_re: Optional[Pattern[str]] = None
    exclude_re: Optional[Pattern[str]] = None

    include_raw = str(getattr(args, "gamma_include_regex", "") or "")
    if include_raw:
        try:
            include_re = re.compile(include_raw, re.IGNORECASE)
        except re.error as e:
            logger.info(f"[{iso_now()}] warning: invalid gamma_include_regex: {e} (ignoring)")

    exclude_raw = str(getattr(args, "gamma_exclude_regex", "") or "")
    if exclude_raw:
        try:
            exclude_re = re.compile(exclude_raw, re.IGNORECASE)
        except re.error as e:
            logger.info(f"[{iso_now()}] warning: invalid gamma_exclude_regex: {e} (ignoring)")

    return include_re, exclude_re


def should_skip_halted_execute_run(*, args, state: RuntimeState, logger) -> bool:
    if not bool(getattr(args, "execute", False)):
        return False
    if not bool(state.halted):
        return False
    logger.info(f"[{iso_now()}] state halted: {state.halt_reason}")
    logger.info(
        f"[{iso_now()}] run skipped while halted (will auto-reset next day or clear in state file)"
    )
    return True


def prepare_monitor_runtime(
    *,
    args,
    logger,
    include_re,
    exclude_re,
    metrics_file: Optional[Path],
    notify_func: Callable[[Any, str], None],
    build_universe_baskets_func: Callable[..., Tuple[str, List[EventBasket], str]],
    apply_subscription_token_cap_func: Callable[..., Tuple[List[EventBasket], str]],
    build_subscription_maps_func: Callable[[List[EventBasket]], Tuple[Dict[str, Set[str]], Dict[str, EventBasket], List[str]]],
    initialize_execution_backend_func: Callable[..., Dict[str, Any]],
    build_clob_client_func: Callable[[Any], Any],
) -> Dict[str, Any]:
    universe, baskets, empty_notify = build_universe_baskets_func(
        args=args,
        logger=logger,
        include_re=include_re,
        exclude_re=exclude_re,
    )
    if not baskets:
        if empty_notify:
            notify_func(logger, empty_notify)
        return {"ok": False, "exit_code": 1}

    baskets, selection_empty_notify = apply_subscription_token_cap_func(
        baskets=baskets,
        universe=universe,
        args=args,
        logger=logger,
    )
    if not baskets:
        if selection_empty_notify:
            notify_func(logger, selection_empty_notify)
        return {"ok": False, "exit_code": 1}

    max_tokens = int(getattr(args, "max_subscribe_tokens", 0) or 0)
    token_to_events, event_map, token_ids = build_subscription_maps_func(baskets)

    log_monitor_startup(
        logger=logger,
        args=args,
        universe=universe,
        baskets_count=len(baskets),
        token_ids_count=len(token_ids),
        max_tokens=max_tokens,
        metrics_file=metrics_file,
    )
    notify_func(
        logger,
        (
            f"CLOBBOT started ({'LIVE' if args.execute else 'observe'}) | "
            f"universe={universe} min_edge={args.min_edge_cents:.2f}c strategy={args.strategy}"
        ),
    )

    exec_init = initialize_execution_backend_func(
        args=args,
        logger=logger,
        baskets=baskets,
        build_clob_client_func=build_clob_client_func,
    )
    if not bool(exec_init.get("ok", False)):
        return {"ok": False, "exit_code": int(exec_init.get("exit_code", 1) or 0)}

    exec_backend = str(exec_init.get("exec_backend", "none") or "none")
    client = exec_init.get("client")
    simmer_api_key = str(exec_init.get("simmer_api_key", "") or "")
    baskets = exec_init.get("baskets", baskets)
    if bool(exec_init.get("baskets_changed", False)):
        token_to_events, event_map, token_ids = build_subscription_maps_func(baskets)

    logger.info(f"Runtime baskets: {len(baskets)}")
    logger.info(f"Runtime subscribed token IDs: {len(token_ids)}")

    return {
        "ok": True,
        "exit_code": 0,
        "universe": universe,
        "baskets": baskets,
        "token_to_events": token_to_events,
        "event_map": event_map,
        "token_ids": token_ids,
        "exec_backend": exec_backend,
        "client": client,
        "simmer_api_key": simmer_api_key,
    }


def build_live_execution_context(
    *,
    state: RuntimeState,
    exec_backend: str,
    client,
    simmer_api_key: str,
    save_state_func: Callable[[Path, RuntimeState], None],
    state_file: Path,
    sdk_request_func,
    fetch_simmer_positions_func,
    estimate_exec_cost_func,
    execute_func,
) -> Dict[str, Any]:
    return {
        "execute_func": execute_func,
        "state": state,
        "exec_backend": exec_backend,
        "client": client,
        "simmer_api_key": simmer_api_key,
        "save_state_func": save_state_func,
        "state_file": state_file,
        "sdk_request_func": sdk_request_func,
        "fetch_simmer_positions_func": fetch_simmer_positions_func,
        "estimate_exec_cost_func": estimate_exec_cost_func,
    }


async def run_connected_monitor_loop(
    *,
    ws,
    ws_url: str,
    token_ids: List[str],
    state: RuntimeState,
    state_file: Path,
    args,
    stats,
    start_ts: float,
    summary_every: float,
    logger,
    format_candidate_brief_func: Callable[[Any], str],
    maybe_apply_daily_loss_guard_func: Callable[[RuntimeState, Any, Any], bool],
    save_state_func: Callable[[Path, RuntimeState], None],
    process_ws_raw_message_func,
    process_ws_raw_message_kwargs: Dict[str, Any],
    initial_last_observe_notify_ts: float = 0.0,
) -> RuntimeState:
    await ws.send(json.dumps({"type": "market", "assets_ids": token_ids}))
    logger.info(f"Connected: {ws_url}")

    last_observe_notify_ts = float(initial_last_observe_notify_ts or 0.0)

    while True:
        state = maybe_rollover_daily_state(
            state=state,
            state_file=state_file,
            logger=logger,
            save_state_func=save_state_func,
        )

        maybe_emit_periodic_summary(
            stats=stats,
            summary_every=summary_every,
            logger=logger,
            format_candidate_brief_func=format_candidate_brief_func,
        )

        if run_timeout_reached(run_started_at=start_ts, run_seconds=float(args.run_seconds or 0.0)):
            logger.info("Run timeout reached. Exiting.")
            break

        if args.execute:
            if not maybe_apply_daily_loss_guard_func(state, args, logger):
                save_state_func(state_file, state)
                if state.halted:
                    logger.info(f"[{iso_now()}] run ending early due to halt state")
                    break
                await asyncio.sleep(2)
                continue

        try:
            timeout = compute_recv_timeout_seconds(
                run_started_at=start_ts,
                run_seconds=float(args.run_seconds or 0.0),
            )
            if timeout is None:
                logger.info("Run timeout reached. Exiting.")
                break

            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            if should_log_idle_heartbeat(
                run_started_at=start_ts,
                run_seconds=float(args.run_seconds or 0.0),
            ):
                logger.info(f"[{iso_now()}] heartbeat: no message in 30s")
            continue

        msg_kwargs = dict(process_ws_raw_message_kwargs)
        msg_kwargs["raw"] = raw
        msg_kwargs["state"] = state
        msg_kwargs["last_observe_notify_ts"] = last_observe_notify_ts
        last_observe_notify_ts = await process_ws_raw_message_func(**msg_kwargs)

    return state


def maybe_rollover_daily_state(
    *,
    state: RuntimeState,
    state_file: Path,
    logger,
    save_state_func: Callable[[Path, RuntimeState], None],
) -> RuntimeState:
    today = day_key_local()
    if state.day == today:
        return state

    next_state = RuntimeState(day=today)
    save_state_func(state_file, next_state)
    logger.info(f"[{iso_now()}] state: daily counters reset")
    return next_state


def maybe_emit_periodic_summary(
    *,
    stats,
    summary_every: float,
    logger,
    format_candidate_brief_func: Callable[[Any], str],
) -> None:
    if summary_every <= 0:
        return

    now = now_ts()
    if (now - stats.last_summary_ts) < summary_every:
        return

    window_sec = max(1, int(now - stats.window_started_at))
    if stats.best_window:
        logger.info(
            f"[{iso_now()}] summary({window_sec}s): "
            f"candidates={stats.candidates_window} | {format_candidate_brief_func(stats.best_window)}"
        )
    else:
        logger.info(f"[{iso_now()}] summary({window_sec}s): candidates={stats.candidates_window} | none")

    stats.candidates_window = 0
    stats.best_window = None
    stats.window_started_at = now
    stats.last_summary_ts = now


def run_timeout_reached(*, run_started_at: float, run_seconds: float) -> bool:
    if not run_seconds:
        return False
    return (now_ts() - run_started_at) >= run_seconds


def compute_recv_timeout_seconds(
    *,
    run_started_at: float,
    run_seconds: float,
    default_timeout_sec: float = 30.0,
) -> Optional[float]:
    timeout = float(default_timeout_sec)
    if not run_seconds:
        return timeout

    remaining = run_seconds - (now_ts() - run_started_at)
    if remaining <= 0:
        return None
    return min(timeout, max(0.2, float(remaining)))


def should_log_idle_heartbeat(
    *,
    run_started_at: float,
    run_seconds: float,
    suppress_tail_window_sec: float = 2.0,
) -> bool:
    if not run_seconds:
        return True
    remaining = run_seconds - (now_ts() - run_started_at)
    return remaining > suppress_tail_window_sec


def maybe_emit_run_summary(
    *,
    stats,
    summary_every: float,
    logger,
    format_candidate_brief_func: Callable[[Any], str],
) -> None:
    if summary_every <= 0:
        return

    if stats.best_all:
        logger.info(
            f"[{iso_now()}] run summary: candidates={stats.candidates_total} | "
            f"{format_candidate_brief_func(stats.best_all)}"
        )
    else:
        logger.info(f"[{iso_now()}] run summary: candidates={stats.candidates_total} | none")


def log_monitor_startup(
    logger,
    args,
    universe: str,
    baskets_count: int,
    token_ids_count: int,
    max_tokens: int,
    metrics_file: Optional[Path],
) -> None:
    logger.info(f"Loaded baskets: {baskets_count}")
    logger.info(f"Subscribed token IDs: {token_ids_count}")
    logger.info(f"Mode: {'LIVE EXECUTION' if args.execute else 'observe-only'}")
    logger.info(f"Universe: {universe}")
    logger.info(
        f"Threshold: net edge >= {args.min_edge_cents:.2f}c | shares/leg={args.shares:.2f} | "
        f"winner_fee={args.winner_fee_rate:.2%} | fixed_cost=${args.fixed_cost:.4f}"
    )
    logger.info(f"Strategy: {args.strategy}")

    if universe in {"btc-5m", "btc-updown"}:
        mins = parse_btc_updown_window_minutes(str(getattr(args, "btc_updown_window_minutes", "5") or "5"))
        mins_txt = ",".join(str(x) for x in mins)
        logger.info(
            f"BTC up/down windows: minutes={mins_txt} "
            f"back={max(0, int(getattr(args, 'btc_5m_windows_back', 1)))} "
            f"forward={max(0, int(getattr(args, 'btc_5m_windows_forward', 1)))}"
        )

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
            allow_types = parse_sports_market_type_filter(str(getattr(args, "sports_market_types", "") or ""))
            deny_types = parse_sports_market_type_filter(str(getattr(args, "sports_market_types_exclude", "") or ""))
            allow_s = ",".join(sorted(allow_types)) if allow_types else "all"
            deny_s = ",".join(sorted(deny_types)) if deny_types else "none"
            logger.info(
                "Sports live filter (gamma yes-no): "
                f"enabled prestart={float(getattr(args, 'sports_live_prestart_min', 10.0)):.1f}m "
                f"postend={float(getattr(args, 'sports_live_postend_min', 30.0)):.1f}m "
                f"types={allow_s} exclude={deny_s} require_matchup={bool(getattr(args, 'sports_require_matchup', False))}"
            )
            sf_provider = str(getattr(args, "sports_feed_provider", "none") or "none").strip().lower()
            if sf_provider != "none":
                path_count = len(
                    [x.strip() for x in str(getattr(args, "sports_feed_espn_paths", "") or "").split(",") if x.strip()]
                )
                logger.info(
                    "Sports feed: "
                    f"provider={sf_provider} strict={bool(getattr(args, 'sports_feed_strict', False))} "
                    f"paths={path_count} timeout={float(getattr(args, 'sports_feed_timeout_sec', 5.0) or 5.0):.1f}s "
                    f"buffer={float(getattr(args, 'sports_feed_live_buffer_sec', 90.0) or 90.0):.0f}s"
                )
        if bool(getattr(args, "wallet_signal_enable", False)):
            logger.info(
                "Wallet signal ranking: "
                f"enabled weight={float(getattr(args, 'wallet_signal_weight', 0.25) or 0.0):.3f} "
                f"max_baskets={int(getattr(args, 'wallet_signal_max_baskets', 80) or 80)} "
                f"holders_limit={int(getattr(args, 'wallet_signal_holders_limit', 16) or 16)} "
                f"top_wallets={int(getattr(args, 'wallet_signal_top_wallets', 8) or 8)}"
            )
            if max_tokens <= 0:
                logger.info("Wallet signal note: ranking impact requires --max-subscribe-tokens > 0.")

    if max_tokens > 0:
        logger.info(f"Max subscribe tokens: {max_tokens}")
    if args.summary_every_sec:
        logger.info(f"Summary: every {float(args.summary_every_sec):.0f}s")
    if metrics_file:
        logger.info(
            f"Metrics: {args.metrics_file} | log_all_candidates={bool(getattr(args, 'metrics_log_all_candidates', False))}"
        )
    else:
        logger.info("Metrics: disabled")
    if (not args.execute) and bool(getattr(args, "observe_exec_edge_filter", False)):
        _s = str(getattr(args, "observe_exec_edge_filter_strategies", "") or "")
        _set = {x.strip().lower() for x in _s.split(",") if x.strip()}
        sfx = ",".join(sorted(_set)) if _set else "all"
        logger.info(
            "Observe exec-edge filter: "
            f"enabled min_usd={float(getattr(args, 'observe_exec_edge_min_usd', 0.0) or 0.0):.4f} "
            f"strikes={int(getattr(args, 'observe_exec_edge_strike_limit', 3) or 3)} "
            f"cooldown={float(getattr(args, 'observe_exec_edge_cooldown_sec', 300.0) or 300.0):.0f}s "
            f"strategies={sfx}"
        )
    if (not args.execute) and bool(getattr(args, "notify_observe_signals", False)):
        logger.info("Observe signal Discord notify: enabled")
