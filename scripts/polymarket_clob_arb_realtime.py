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

import asyncio
import json
import sys
import traceback
from pathlib import Path
from typing import Dict, Optional

import websockets
from lib.clob_arb_cli import parse_clob_arb_args
from lib.clob_auth import build_clob_client_from_env
from lib.clob_arb_eval import (
    _estimate_exec_cost_clob,
    collect_impacted_events_from_payload,
    format_candidate_brief,
    process_impacted_event,
)
from lib.clob_arb_models import LocalBook, RunStats, RuntimeState
from lib.clob_arb_execution import (
    maybe_execute_candidate_live,
)
from lib.clob_arb_sports import DEFAULT_ESPN_SCOREBOARD_PATHS
from lib.clob_arb_runtime import (
    append_jsonl,
    build_monitor_loop_tuning,
    compile_gamma_market_regexes,
    compute_recv_timeout_seconds,
    fetch_simmer_positions,
    initialize_execution_backend,
    load_state,
    log_monitor_startup,
    maybe_emit_periodic_summary,
    maybe_emit_run_summary,
    maybe_rollover_daily_state,
    maybe_apply_daily_loss_guard as _maybe_apply_daily_loss_guard_runtime,
    prepare_monitor_runtime,
    resolve_monitor_paths,
    run_timeout_reached,
    save_state,
    sdk_request,
    should_skip_halted_execute_run,
    should_log_idle_heartbeat,
)
from lib.clob_arb_universe import (
    apply_subscription_token_cap,
    build_subscription_maps,
    build_universe_baskets,
)
from lib.runtime_common import (
    iso_now,
    maybe_notify_discord as _maybe_notify_discord,
    now_ts,
)

WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def maybe_notify_discord(logger: "Logger", message: str) -> None:
    _maybe_notify_discord(
        logger,
        message,
        timeout_sec=5.0,
        user_agent="clob-arb-monitor/1.0",
    )


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



def maybe_apply_daily_loss_guard(state: RuntimeState, args, logger: Logger) -> bool:
    return _maybe_apply_daily_loss_guard_runtime(
        state,
        args,
        logger,
        notify_func=maybe_notify_discord,
    )

def build_clob_client(args):
    return build_clob_client_from_env(
        clob_host=args.clob_host,
        chain_id=args.chain_id,
        missing_env_message="Missing env for live execution. Set PM_PRIVATE_KEY and PM_FUNDER (or PM_PROXY_ADDRESS).",
        invalid_key_message="Invalid PM_PRIVATE_KEY format. Expected 64 hex chars (optionally prefixed with 0x).",
    )



async def run(args) -> int:
    script_dir = Path(__file__).resolve().parent
    runtime_paths = resolve_monitor_paths(args=args, script_dir=script_dir)

    logger = Logger(args.log_file)
    state_file = runtime_paths["state_file"]
    state = load_state(state_file)
    save_state(state_file, state)
    metrics_file = runtime_paths["metrics_file"]

    gamma_include_re, gamma_exclude_re = compile_gamma_market_regexes(args=args, logger=logger)

    if should_skip_halted_execute_run(args=args, state=state, logger=logger):
        return 0

    setup = prepare_monitor_runtime(
        args=args,
        logger=logger,
        include_re=gamma_include_re,
        exclude_re=gamma_exclude_re,
        metrics_file=metrics_file,
        notify_func=maybe_notify_discord,
        build_universe_baskets_func=build_universe_baskets,
        apply_subscription_token_cap_func=apply_subscription_token_cap,
        build_subscription_maps_func=build_subscription_maps,
        initialize_execution_backend_func=initialize_execution_backend,
        build_clob_client_func=build_clob_client,
    )
    if not bool(setup.get("ok", False)):
        return int(setup.get("exit_code", 1) or 0)

    baskets = setup["baskets"]
    token_to_events = setup["token_to_events"]
    event_map = setup["event_map"]
    token_ids = setup["token_ids"]
    exec_backend = setup["exec_backend"]
    client = setup["client"]
    simmer_api_key = setup["simmer_api_key"]

    books: Dict[str, LocalBook] = {}
    start = now_ts()
    stats = RunStats()
    loop_tuning = build_monitor_loop_tuning(args)
    summary_every = float(loop_tuning["summary_every"])
    min_eval_interval = float(loop_tuning["min_eval_interval"])
    observe_notify_min_interval = float(loop_tuning["observe_notify_min_interval"])
    last_observe_notify_ts = 0.0
    observe_exec_edge_filter = bool(loop_tuning["observe_exec_edge_filter"])
    observe_exec_edge_min_usd = float(loop_tuning["observe_exec_edge_min_usd"])
    observe_exec_edge_strike_limit = int(loop_tuning["observe_exec_edge_strike_limit"])
    observe_exec_edge_cooldown_sec = float(loop_tuning["observe_exec_edge_cooldown_sec"])
    observe_exec_edge_filter_strategies = set(loop_tuning["observe_exec_edge_filter_strategies"])
    live_execution_ctx = {
        "execute_func": maybe_execute_candidate_live,
        "state": state,
        "exec_backend": exec_backend,
        "client": client,
        "simmer_api_key": simmer_api_key,
        "save_state_func": save_state,
        "state_file": state_file,
        "sdk_request_func": sdk_request,
        "fetch_simmer_positions_func": fetch_simmer_positions,
        "estimate_exec_cost_func": _estimate_exec_cost_clob,
    }

    async with websockets.connect(args.ws_url, ping_interval=20, ping_timeout=20, max_size=2**24) as ws:
        await ws.send(json.dumps({"type": "market", "assets_ids": token_ids}))
        logger.info(f"Connected: {args.ws_url}")

        while True:
            state = maybe_rollover_daily_state(
                state=state,
                state_file=state_file,
                logger=logger,
                save_state_func=save_state,
            )

            maybe_emit_periodic_summary(
                stats=stats,
                summary_every=summary_every,
                logger=logger,
                format_candidate_brief_func=format_candidate_brief,
            )

            if run_timeout_reached(run_started_at=start, run_seconds=float(args.run_seconds or 0.0)):
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
                timeout = compute_recv_timeout_seconds(
                    run_started_at=start,
                    run_seconds=float(args.run_seconds or 0.0),
                )
                if timeout is None:
                    logger.info("Run timeout reached. Exiting.")
                    break

                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                # If we're near the end of a timed run, avoid noisy heartbeats.
                if should_log_idle_heartbeat(
                    run_started_at=start,
                    run_seconds=float(args.run_seconds or 0.0),
                ):
                    logger.info(f"[{iso_now()}] heartbeat: no message in 30s")
                continue

            try:
                payload = json.loads(raw)
            except Exception:
                continue

            impacted_events = collect_impacted_events_from_payload(
                payload=payload,
                books=books,
                token_to_events=token_to_events,
            )
            if not impacted_events:
                continue
            live_execution_ctx["state"] = state

            for event_key in impacted_events:
                basket = event_map[event_key]
                last_observe_notify_ts = await process_impacted_event(
                    basket=basket,
                    books=books,
                    args=args,
                    stats=stats,
                    min_eval_interval=min_eval_interval,
                    metrics_file=metrics_file,
                    observe_exec_edge_filter=observe_exec_edge_filter,
                    observe_exec_edge_min_usd=observe_exec_edge_min_usd,
                    observe_exec_edge_strike_limit=observe_exec_edge_strike_limit,
                    observe_exec_edge_cooldown_sec=observe_exec_edge_cooldown_sec,
                    observe_exec_edge_filter_strategies=observe_exec_edge_filter_strategies,
                    observe_notify_min_interval=observe_notify_min_interval,
                    last_observe_notify_ts=last_observe_notify_ts,
                    logger=logger,
                    append_jsonl_func=append_jsonl,
                    notify_func=maybe_notify_discord,
                    live_execution_ctx=live_execution_ctx,
                )

    save_state(state_file, state)
    maybe_emit_run_summary(
        stats=stats,
        summary_every=summary_every,
        logger=logger,
        format_candidate_brief_func=format_candidate_brief,
    )
    maybe_notify_discord(logger, "CLOBBOT stopped")
    return 0



def parse_args():
    return parse_clob_arb_args(
        ws_url=WS_MARKET_URL,
        default_espn_paths=DEFAULT_ESPN_SCOREBOARD_PATHS,
    )

if __name__ == "__main__":
    args = parse_args()
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except Exception as e:
        # Ensure fatal errors are visible in the same runtime log file used by operators.
        try:
            logger = Logger(getattr(args, "log_file", None))
            logger.info(f"[{iso_now()}] fatal: {type(e).__name__}: {e}")
            for line in traceback.format_exc().rstrip().splitlines():
                logger.info(line)
        except Exception:
            pass
        raise SystemExit(1)

