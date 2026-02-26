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
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Set

import websockets
from lib.clob_arb_cli import parse_clob_arb_args
from lib.clob_auth import build_clob_client_from_env
from lib.clob_arb_eval import (
    _estimate_exec_cost_clob,
    apply_observe_exec_edge_and_log_metrics,
    compute_candidate,
    extract_book_items,
    format_candidate,
    format_candidate_brief,
    make_signature,
    update_book_from_snapshot,
)
from lib.clob_arb_models import LocalBook, RunStats, RuntimeState
from lib.clob_arb_execution import (
    maybe_execute_candidate_live,
)
from lib.clob_arb_sports import DEFAULT_ESPN_SCOREBOARD_PATHS
from lib.clob_arb_runtime import (
    append_jsonl,
    build_monitor_loop_tuning,
    fetch_simmer_positions,
    initialize_execution_backend,
    load_state,
    log_monitor_startup,
    maybe_apply_daily_loss_guard as _maybe_apply_daily_loss_guard_runtime,
    save_state,
    sdk_request,
)
from lib.clob_arb_universe import (
    apply_subscription_token_cap,
    build_subscription_maps,
    build_universe_baskets,
)
from lib.runtime_common import (
    day_key_local,
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
    if not args.log_file:
        # Default to the same file used by the Windows task runner.
        args.log_file = str(script_dir.parent / "logs" / "clob-arb-monitor.log")
    if not args.state_file:
        args.state_file = str(script_dir.parent / "logs" / "clob_arb_state.json")

    logger = Logger(args.log_file)
    state_file = Path(args.state_file)
    state = load_state(state_file)
    save_state(state_file, state)

    metrics_file_raw = str(getattr(args, "metrics_file", "") or "").strip()
    if not metrics_file_raw:
        metrics_file_raw = str(script_dir.parent / "logs" / "clob-arb-monitor-metrics.jsonl")
    if metrics_file_raw.lower() in {"off", "none", "null", "disable", "disabled", "0"}:
        metrics_file: Optional[Path] = None
        args.metrics_file = ""
    else:
        metrics_file = Path(metrics_file_raw)
        metrics_file.parent.mkdir(parents=True, exist_ok=True)
        args.metrics_file = str(metrics_file)

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

    universe, baskets, empty_notify = build_universe_baskets(
        args=args,
        logger=logger,
        include_re=gamma_include_re,
        exclude_re=gamma_exclude_re,
    )
    if not baskets:
        if empty_notify:
            maybe_notify_discord(logger, empty_notify)
        return 1

    baskets, selection_empty_notify = apply_subscription_token_cap(
        baskets=baskets,
        universe=universe,
        args=args,
        logger=logger,
    )
    if not baskets:
        if selection_empty_notify:
            maybe_notify_discord(logger, selection_empty_notify)
        return 1

    max_tokens = int(getattr(args, "max_subscribe_tokens", 0) or 0)
    token_to_events, event_map, token_ids = build_subscription_maps(baskets)

    log_monitor_startup(
        logger=logger,
        args=args,
        universe=universe,
        baskets_count=len(baskets),
        token_ids_count=len(token_ids),
        max_tokens=max_tokens,
        metrics_file=metrics_file,
    )
    maybe_notify_discord(
        logger,
        (
            f"CLOBBOT started ({'LIVE' if args.execute else 'observe'}) | "
            f"universe={universe} min_edge={args.min_edge_cents:.2f}c strategy={args.strategy}"
        ),
    )

    exec_init = initialize_execution_backend(
        args=args,
        logger=logger,
        baskets=baskets,
        build_clob_client_func=build_clob_client,
    )
    if not bool(exec_init.get("ok", False)):
        return int(exec_init.get("exit_code", 1) or 0)

    exec_backend = str(exec_init.get("exec_backend", "none") or "none")
    client = exec_init.get("client")
    simmer_api_key = str(exec_init.get("simmer_api_key", "") or "")
    baskets = exec_init.get("baskets", baskets)
    if bool(exec_init.get("baskets_changed", False)):
        token_to_events, event_map, token_ids = build_subscription_maps(baskets)

    logger.info(f"Runtime baskets: {len(baskets)}")
    logger.info(f"Runtime subscribed token IDs: {len(token_ids)}")

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

                _metrics_row, observe_exec_filtered = apply_observe_exec_edge_and_log_metrics(
                    candidate=c,
                    basket=basket,
                    books=books,
                    args=args,
                    metrics_file=metrics_file,
                    observe_exec_edge_filter=observe_exec_edge_filter,
                    observe_exec_edge_min_usd=observe_exec_edge_min_usd,
                    observe_exec_edge_strike_limit=observe_exec_edge_strike_limit,
                    observe_exec_edge_cooldown_sec=observe_exec_edge_cooldown_sec,
                    observe_exec_edge_filter_strategies=observe_exec_edge_filter_strategies,
                    append_jsonl_func=append_jsonl,
                    logger=logger,
                )

                if c.net_edge < (args.min_edge_cents / 100.0):
                    continue

                if observe_exec_filtered:
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
                    await maybe_execute_candidate_live(
                        candidate=c,
                        basket=basket,
                        state=state,
                        args=args,
                        logger=logger,
                        books=books,
                        exec_backend=exec_backend,
                        client=client,
                        simmer_api_key=simmer_api_key,
                        now_ts_value=now,
                        notify_func=maybe_notify_discord,
                        save_state_func=save_state,
                        state_file=state_file,
                        sdk_request_func=sdk_request,
                        fetch_simmer_positions_func=fetch_simmer_positions,
                        estimate_exec_cost_func=_estimate_exec_cost_clob,
                    )

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

