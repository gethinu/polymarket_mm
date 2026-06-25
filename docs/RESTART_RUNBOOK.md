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
