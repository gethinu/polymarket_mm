# Restart Runbook — no-longshot small-live (post 2026-06-24 re-baseline)

Purpose: bring the `no_longshot_daily_observe` strategy back online after the
2026-06-24 re-baseline, accumulate a fresh rolling-30d gate, and (only after the
gate is freshly met) run tiny-size live entries.

Authoritative status/KPI source remains `logs/strategy_register_latest.json`.
Follow `docs/llm/CANON.md` for the full daily refresh sequence; this file is the
restart-specific quick path.

## Smoke-test status (verified 2026-06-25, Linux cloud env)

The pipeline was confirmed healthy with current dependencies:

- `polymarket_no_longshot_observe.py screen` reaches the live Polymarket gamma
  API through the proxy and returns real current markets.
- `execute_no_longshot_live.py` (no `--execute`) runs in `observe-preview` mode
  cleanly (`attempted=0 submitted=0 errors=0`) — no keys required for dry-run.
- Full suite: 344 passed, 7 skipped.

The cloud env is ephemeral and has no trading keys, so the persistent observe
host and all live execution must run on the operator's Windows machine.

## 0. One-time setup (Windows)

```powershell
python -m pip install -r requirements.txt          # pinned runtime deps
# Live auth (do NOT paste secrets into chat — set as user env / DPAPI file):
#   PM_PRIVATE_KEY_DPAPI_FILE  (recommended)
#   PM_FUNDER
```

## 1. Restart observe (rebuild the gate from zero)

Run the observe-only daily report so fresh realized data starts accumulating.
This places NO orders (no `-LiveExecute`).

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 `
  -NoBackground -StrictRealizedBandOnly `
  -RealizedFastYesMin 0.16 -RealizedFastYesMax 0.20 `
  -RealizedFastMaxHoursToEnd 72 -RealizedFastMaxPages 120 `
  -RealizedEntryTopN 2 -AllowRealizedEntryIngest
```

Schedule it daily (single mode only — task OR daemon, never both):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/set_no_longshot_daily_mode.ps1 -NoBackground -Mode task
```

## 2. Watch the capital gate

After each refresh, read the gate from the register snapshot:

```powershell
python scripts/render_strategy_register_snapshot.py --pretty
python scripts/check_strategy_gate_alarm.py --pretty
```

Gate to clear before any live escalation (reset on re-baseline, starts from 0):

- `no_longshot_status.rolling_30d_resolved_trades >= 30` (new-condition basis)
- latest fast band stays inside `[0.16, 0.20]`; if it drifts → treat as `REVIEW`

Do not reuse the old `21/30` progress or the expired `2026-03-02` date.

## 3. Tiny-size live (only after the gate is freshly met)

Same entry policy as observe, with explicit live flags and hard caps
(1 order/run, 5 shares, $10/day notional, NO price <= 0.84):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 `
  -NoBackground -StrictRealizedBandOnly -RealizedEntryTopN 2 -AllowRealizedEntryIngest `
  -LiveExecute -LiveConfirm YES -LiveMaxOrders 1 -LiveOrderSizeShares 5 `
  -LiveMaxDailyNotionalUsd 10 -LiveMaxOpenPositions 10 `
  -LiveMaxEntryNoPrice 0.84 -LivePriceBufferCents 0.2
```

Dry-run the executor first (no `--execute` ⇒ `observe-preview`, no keys needed):

```powershell
python scripts/execute_no_longshot_live.py --screen-csv logs/no_longshot_screen_latest.csv `
  --max-new-orders 1 --order-size-shares 5 --max-daily-notional-usd 10 `
  --max-entry-no-price 0.84 --price-buffer-cents 0.2 --pretty
```

Live requires BOTH `--execute --confirm-live YES`; default is always observe.

## 4. Reconcile / realized tracking

```powershell
python scripts/record_no_longshot_realized_daily.py --pretty
python scripts/report_no_longshot_monthly_return.py --pretty
```

Reality check on expectations ($60 bankroll, equal-weight across 4 ADOPTED ⇒
~$15 each): even the historical paper projection (+9.89%/mo) is ~$1.5/mo on
this size. Validate that the edge survives live fills/fees at micro size before
considering any bankroll increase.

## 5. Restart the other 3 ADOPTED observe runners

These are observe-only (no orders). Run each persistently (own terminal,
scheduled task, or the bot supervisor). They rebuild fresh evidence for the
weather / event-driven / gamma strategies in parallel with no-longshot.

```powershell
# weather_clob_arb_buckets_observe
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_arb_observe.ps1
#   (direct form: python scripts/polymarket_clob_arb_realtime.py --universe weather --strategy buckets)

# event_driven_mispricing_observe
python scripts/polymarket_event_driven_observe.py --max-pages 12 --poll-sec 120 `
  --min-edge-cents 0.8 --max-days-to-end 180 --top-n 20 `
  --signal-cooldown-sec 7200 --signal-state-file logs/event-driven-observe-signal-state.json
# daily profit-window report:
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_event_driven_daily_report.ps1 `
  -NoBackground -ProfitAssumedBankrollUsd 60 -ProfitMaxStakeUsd 5 -ProfitMaxDteDays 7 -ProfitMinUniqueEvents 2

# gamma_eventpair_exec_edge_filter_observe (observe-only exec-edge suppression)
python scripts/polymarket_clob_arb_realtime.py --universe gamma-active --strategy event-pair `
  --gamma-limit 1500 --gamma-min-liquidity 0 --gamma-min-volume24hr 0 --gamma-scan-max-markets 40000 `
  --gamma-max-days-to-end 60 --max-markets-per-event 5 --max-subscribe-tokens 400 `
  --metrics-log-all-candidates --observe-exec-edge-filter --observe-exec-edge-min-usd 0.01 `
  --observe-exec-edge-strike-limit 1 --observe-exec-edge-cooldown-sec 180 `
  --observe-exec-edge-filter-strategies event-yes --min-edge-cents 10
```

The bot supervisor (`configs/bot_supervisor.observe.json` via
`scripts/run_bot_supervisor.ps1`) can run several of these together with
auto-restart. Note it currently only has `event_driven` enabled and the FROZEN
strategies disabled; enable the no-longshot/weather jobs there only if you want
supervisor-managed restarts (some jobs carry machine-specific python paths —
review before enabling).

## 6. Gate auto-check + notification

The gate alarm reads the freshly-rendered register snapshot and emits a
transition alert (optionally to Discord) when the capital gate or the
no-longshot practical-judgment status changes.

```powershell
# After the daily refresh (section 2). Add --discord to push transitions.
python scripts/check_strategy_gate_alarm.py --pretty
python scripts/check_strategy_gate_alarm.py --discord --discord-webhook-env CLOBBOT_DISCORD_WEBHOOK_URL
```

IMPORTANT — make the re-baseline reset real on your machine:

- The practical-judgment date is no longer a hardcoded calendar date. With no
  `--no-longshot-practical-decision-date` flag it now anchors a fresh window at
  `today + 35 days` (code default), so a clean machine starts correctly.
- BUT the alarm persists the active date in `logs/strategy_gate_alarm_state.json`.
  If that file survives from the old runs it still holds the rotted
  `2026-03-xx` date and will read `OVERDUE`. To truly restart the gate, delete
  the stale state once before the first post-re-baseline run:

  ```powershell
  Remove-Item logs/strategy_gate_alarm_state.json -ErrorAction SilentlyContinue
  # also clear the old no-longshot realized tracker so rolling-30d restarts clean:
  Remove-Item logs/no_longshot_realized_daily.jsonl, logs/no_longshot_forward_positions.json -ErrorAction SilentlyContinue
  ```

After that, the gate should report `PENDING` / `remaining_days≈35` /
`rolling_30d_resolved_trades=0`, counting up from fresh data.

## 7. Capital → return projection (paper, pre-cost)

These are projections from Feb–Mar 2026 observe runs, NOT realized live results.
Use them only to size expectations, not as a promise. Per-strategy paper monthly
return (the credible three; event-driven's +183% is a diagnostic upper bound and
is excluded here):

| strategy | paper monthly return |
|---|---|
| no_longshot_daily | +9.89% (n=21, low confidence) |
| weather_buckets | +13.13% (missed its own +15% target) |
| gamma_eventpair | +5.47% (weakest; NO_GO on long-run baseline) |

Equal-weight blend across the 4 ADOPTED (event-driven counted as ~0 to stay
honest) ⇒ planning band of roughly **+5% to +10% / month gross paper**. Net of
fees, slippage and partial fills, assume materially less.

| bankroll | @5%/mo | @8%/mo | @10%/mo |
|---:|---:|---:|---:|
| $60 (current policy) | $3 | $4.8 | $6 |
| $600 | $30 | $48 | $60 |
| $6,000 | $300 | $480 | $600 |
| $60,000 | $3,000* | $4,800* | $6,000* |

`*` Capacity-limited and likely unrealistic. The edges here (longshot NO entries
at YES 0.16–0.20, weather basket mispricing, event-pair dislocations) live in
thin books; past a few hundred to low-thousands of dollars per strategy the edge
decays and the projected % will not hold. Treat large-bankroll rows as
arithmetic, not a plan — prove the % survives live at micro size first, then
scale only as far as observed fill quality allows.
