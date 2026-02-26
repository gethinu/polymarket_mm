from __future__ import annotations

import asyncio
import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Callable, Dict, List, Optional, Set, Tuple

from lib.clob_arb_models import Candidate, LocalBook, RuntimeState
from lib.runtime_common import iso_now, now_ts, parse_iso_or_epoch_to_ms
from polymarket_clob_arb_scanner import as_float, order_cost_for_shares


def estimate_simmer_total_amount(candidate: Candidate, args) -> float:
    slip = max(0.0, float(args.exec_slippage_bps or 0.0)) / 10000.0
    min_amt = max(0.0, float(args.simmer_min_amount or 0.0))
    total = 0.0
    for _, observed_cost in candidate.leg_costs:
        amt = float(observed_cost) * (1.0 + slip)
        if amt < min_amt:
            amt = min_amt
        total += amt
    return total


def _q_cents_up(x: float) -> float:
    if x <= 0:
        return 0.0
    return float((Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_CEILING)))


def _q_cents_down(x: float) -> float:
    if x <= 0:
        return 0.0
    return float((Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)))


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
        px = max(0.001, _q_cents_up(px))
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


def unwind_partial_clob(client, candidate: Candidate, fills: Dict[str, float], books: Dict[str, LocalBook], args, logger) -> dict:
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

    return {"attempted": attempted, "succeeded": attempted, "response": resp}


def execute_candidate_batch_simmer(api_key: str, candidate: Candidate, args, sdk_request_func) -> dict:
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
    resp = sdk_request_func(api_key, "POST", "/api/sdk/trades/batch", {"trades": trades})
    if isinstance(resp, dict):
        resp["_submitted_trades"] = trades
    return resp


def unwind_partial_simmer(api_key: str, batch_response: dict, args, logger, sdk_request_func, fetch_simmer_positions_func) -> dict:
    results = batch_response.get("results", [])
    submitted = batch_response.get("_submitted_trades", [])
    if not isinstance(results, list):
        return {"attempted": 0, "succeeded": 0}

    positions = fetch_simmer_positions_func(api_key)
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
        resp = sdk_request_func(
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


def unwind_candidate_positions_simmer(api_key: str, candidate: Candidate, args, logger, sdk_request_func, fetch_simmer_positions_func) -> dict:
    positions = fetch_simmer_positions_func(api_key)
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
        resp = sdk_request_func(
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
    from py_clob_client.clob_types import TradeParams

    trades: List[dict] = []
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


async def reconcile_execution(client, candidate: Candidate, submit_ts_ms: int, args, logger) -> dict:
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
    logger,
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

        filtered = []
        for a in book.asks:
            ap = as_float(a.get("price"), math.inf)
            if math.isfinite(ap) and ap <= px:
                filtered.append(a)
        if order_cost_for_shares(filtered, shares) is None:
            return False, f"insufficient ask depth <=${px:.2f} token_id={leg.token_id}"

    return True, ""


async def execute_with_retries_clob(client, candidate: Candidate, books: Dict[str, LocalBook], args, logger) -> Tuple[bool, dict]:
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


async def execute_with_retries_simmer(
    api_key: str,
    candidate: Candidate,
    args,
    logger,
    sdk_request_func,
    fetch_simmer_positions_func,
) -> Tuple[bool, dict]:
    last_result: dict = {}

    for attempt in range(1, args.exec_max_attempts + 1):
        response = execute_candidate_batch_simmer(api_key, candidate, args, sdk_request_func=sdk_request_func)
        failed_count = int(as_float(response.get("failed_count"), 0.0))
        success = bool(response.get("success")) and failed_count == 0
        last_result = {
            "attempt": attempt,
            "response": response,
            "all_filled": success,
        }

        if success:
            return True, last_result

        if args.simmer_unwind_partial:
            unwind = unwind_partial_simmer(
                api_key,
                response,
                args,
                logger,
                sdk_request_func=sdk_request_func,
                fetch_simmer_positions_func=fetch_simmer_positions_func,
            )
            last_result["unwind_from_response"] = unwind
            if unwind.get("attempted", 0) > 0:
                logger.info(
                    f"[{iso_now()}] live: partial batch unwind(from_response) attempted={unwind.get('attempted')} "
                    f"succeeded={unwind.get('succeeded')}"
                )

            flatten = unwind_candidate_positions_simmer(
                api_key,
                candidate,
                args,
                logger,
                sdk_request_func=sdk_request_func,
                fetch_simmer_positions_func=fetch_simmer_positions_func,
            )
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


async def maybe_execute_candidate_live(
    *,
    candidate: Candidate,
    basket,
    state: RuntimeState,
    args,
    logger,
    books: Dict[str, LocalBook],
    exec_backend: str,
    client,
    simmer_api_key: str,
    now_ts_value: float,
    notify_func: Callable[[object, str], None],
    save_state_func: Callable[[object, RuntimeState], None],
    state_file,
    sdk_request_func,
    fetch_simmer_positions_func,
    estimate_exec_cost_func: Callable[[Candidate, float], float],
) -> None:
    if (now_ts_value - float(getattr(basket, "last_exec_ts", 0.0) or 0.0)) < float(args.exec_cooldown_sec):
        logger.info("  live: skipped (event execution cooldown)")
        return

    allowed, reason = can_execute_candidate(
        state=state,
        candidate=candidate,
        args=args,
        logger=logger,
        exec_backend=exec_backend,
        client=client,
    )
    if not allowed:
        logger.info(f"  live: skipped ({reason})")
        save_state_func(state_file, state)
        return

    exec_cost = None
    exec_edge = None
    exec_edge_pct = None
    if exec_backend == "clob":
        exec_cost = estimate_exec_cost_func(candidate, float(args.exec_slippage_bps))
        exec_edge = candidate.payout_after_fee - float(exec_cost) - candidate.fixed_cost
        exec_edge_pct = (float(exec_edge) / candidate.payout_after_fee) if candidate.payout_after_fee > 0 else 0.0
        if exec_edge < (float(args.min_edge_cents) / 100.0):
            logger.info(
                f"  live: skipped (edge after cents/slippage ${exec_edge:.4f} < "
                f"threshold ${(float(args.min_edge_cents)/100.0):.4f})"
            )
            return

    if exec_backend == "clob":
        ok, why = precheck_clob_books(candidate, books, args)
        if not ok:
            logger.info(f"  live: skipped (book precheck: {why})")
            return

    if exec_backend == "clob" and exec_edge is not None and exec_cost is not None:
        notify_func(
            logger,
            (
                f"ENTRY ({exec_backend}) {candidate.title} | "
                f"est edge {exec_edge_pct:.2%} (${exec_edge:.4f}) | "
                f"est cost ${exec_cost:.4f} | legs={len(candidate.leg_costs)}"
            ),
        )
    else:
        notify_func(
            logger,
            (
                f"ENTRY ({exec_backend}) {candidate.title} | "
                f"edge {candidate.edge_pct:.2%} (${candidate.net_edge:.4f}) | legs={len(candidate.leg_costs)}"
            ),
        )

    if exec_backend == "clob":
        ok, result = await execute_with_retries_clob(client, candidate, books, args, logger)
    else:
        ok, result = await execute_with_retries_simmer(
            simmer_api_key,
            candidate,
            args,
            logger,
            sdk_request_func=sdk_request_func,
            fetch_simmer_positions_func=fetch_simmer_positions_func,
        )
    basket.last_exec_ts = now_ts_value

    if ok:
        state.executions_today += 1
        notional_inc = candidate.basket_cost
        state.consecutive_failures = 0
        if exec_backend == "clob":
            if exec_cost is not None and math.isfinite(float(exec_cost)) and float(exec_cost) > 0:
                notional_inc = float(exec_cost)
            state.notional_today += notional_inc
            logger.info(
                f"  live: filled (attempt={result.get('attempt')}, "
                f"fills={result.get('fills')})"
            )
            notify_func(
                logger,
                (
                    f"Filled ({exec_backend}) {candidate.title} | "
                    f"edge {candidate.edge_pct:.2%} (${candidate.net_edge:.4f}) | "
                    f"cost ${notional_inc:.4f} | legs={len(candidate.leg_costs)}"
                ),
            )
        else:
            total_cost = as_float(result.get("response", {}).get("total_cost"), math.nan)
            if math.isfinite(total_cost) and total_cost > 0:
                notional_inc = total_cost
            else:
                notional_inc = estimate_simmer_total_amount(candidate, args)
            state.notional_today += notional_inc
            logger.info(
                f"  live: batch executed (attempt={result.get('attempt')}, "
                f"total_cost=${notional_inc:.4f})"
            )
            notify_func(
                logger,
                (
                    f"Executed ({exec_backend}) {candidate.title} | "
                    f"edge {candidate.edge_pct:.2%} (${candidate.net_edge:.4f}) | "
                    f"total_cost ${notional_inc:.4f} | legs={len(candidate.leg_costs)}"
                ),
            )
    else:
        state.consecutive_failures += 1
        logger.info(
            f"  live: not filled (attempt={result.get('attempt')}, "
            f"fails={state.consecutive_failures}, detail={result})"
        )
        notify_func(
            logger,
            (
                f"NO FILL ({exec_backend}) {candidate.title} | "
                f"edge {candidate.edge_pct:.2%} (${candidate.net_edge:.4f}) | "
                f"reason: {summarize_exec_failure(result)}"
            ),
        )
        if args.max_consecutive_failures > 0 and state.consecutive_failures >= args.max_consecutive_failures:
            state.halted = True
            state.halt_reason = (
                f"Consecutive failure cap reached ({state.consecutive_failures}/"
                f"{args.max_consecutive_failures})"
            )
            logger.info(f"[{iso_now()}] guard: HALT {state.halt_reason}")
            notify_func(logger, f"CLOBBOT HALT: {state.halt_reason}")

    save_state_func(state_file, state)
