# Polymarket CLOB Market Maker (Local)

This folder is **separate** from `c:\\Repos\\quant_trading_system` docs/specs.

## What This Is

- Script: `C:\\Repos\\polymarket_mm\\scripts\\polymarket_clob_mm.py`
- Purpose: maker-only quoting (market making) on **a small set of tokens** (default: auto-select 3 from Gamma active markets)
- Default mode: **observe-only** (no orders)
- Live mode: requires `--execute` + `--confirm-live YES` (or env overrides)
- Default behavior: **quiet logs** (no quote spam; events + periodic summaries only)

## Where To Look

Recommended (repo-local, gitignored):

- Logs: `C:\\Repos\\polymarket_mm\\logs\\clob-mm.log`
- State: `C:\\Repos\\polymarket_mm\\logs\\clob_mm_state.json`
- Metrics (JSONL): `C:\\Repos\\polymarket_mm\\logs\\clob-mm-metrics.jsonl`

Note: the script also supports custom paths via `--log-file/--state-file/--metrics-file`
or env overrides (`CLOBMM_LOG_FILE`, `CLOBMM_STATE_FILE`, `CLOBMM_METRICS_FILE`).

## Windows Autostart

- Scheduled Task: `PolymarketClobMM`
- Runs hidden via `pythonw.exe` on logon.

Check:

```powershell
Get-ScheduledTask -TaskName PolymarketClobMM | Select-Object TaskName,State
Get-Content C:\Repos\polymarket_mm\logs\clob-mm.log -Tail 80
```

Stop:

```powershell
Stop-ScheduledTask -TaskName PolymarketClobMM
```

## Discord Notifications

The script posts to Discord if you set one of:

- `CLOBBOT_DISCORD_WEBHOOK_URL`
- `DISCORD_WEBHOOK_URL`

Optional:

- `CLOBBOT_DISCORD_MENTION` (e.g. `<@123...>` or `@here`)

## Configuration (Env Overrides)

Every CLI flag can be overridden via `CLOBMM_<FLAGNAME_IN_UPPERCASE>`.

Examples:

```powershell
[Environment]::SetEnvironmentVariable('CLOBMM_SPREAD_CENTS','3','User')
[Environment]::SetEnvironmentVariable('CLOBMM_ORDER_SIZE_SHARES','5','User')
```

Important keys:

- `CLOBMM_TOKEN_IDS` : comma-separated token_ids (manual universe)
- `CLOBMM_AUTO_SELECT_COUNT` : auto-select N tokens when `TOKEN_IDS` is empty
- `CLOBMM_GAMMA_MIN_LIQUIDITY`, `CLOBMM_GAMMA_MIN_VOLUME24HR`, `CLOBMM_GAMMA_MIN_SPREAD_CENTS`
- `CLOBMM_SPREAD_CENTS` : target total spread
- `CLOBMM_ORDER_SIZE_SHARES` : quoting size (shares)
- `CLOBMM_MAX_INVENTORY_SHARES` : stop bidding above this inventory
- `CLOBMM_POLL_SEC` : order book poll interval
- `CLOBMM_TRADE_POLL_SEC` : fills polling interval
- `CLOBMM_QUOTE_REFRESH_SEC` : cancel/replace maximum cadence
- `CLOBMM_DAILY_LOSS_LIMIT_USD` : halt if daily PnL <= -limit (realized + unrealized, mark-to-mid)
- `CLOBMM_ORDER_RECONCILE_SEC` : (live) reconcile open order IDs every N sec
- `CLOBMM_METRICS_FILE`, `CLOBMM_METRICS_SAMPLE_SEC` : metrics JSONL output
- `CLOBMM_LOG_QUOTES` : set to `1` to enable quote logs (debug)
- `CLOBMM_QUOTE_LOG_MIN_CHANGE_TICKS` : quote log throttle when `LOG_QUOTES=1`

## Observation Report (24h)

Summarize the last 24h of mid/spread samples (metrics JSONL) + fills/halts:

```powershell
python C:\Repos\polymarket_mm\scripts\report_clob_mm_observation.py --hours 24
python C:\Repos\polymarket_mm\scripts\report_clob_mm_observation.py --hours 24 --discord
```

## Enabling Live Mode (Danger)

Market making is **not** arbitrage. It can lose money.

To enable live mode, set:

```powershell
[Environment]::SetEnvironmentVariable('CLOBMM_EXECUTE','1','User')
[Environment]::SetEnvironmentVariable('CLOBMM_CONFIRM_LIVE','YES','User')
Restart-ScheduledTask -TaskName PolymarketClobMM
```

To go back to observe-only:

```powershell
[Environment]::SetEnvironmentVariable('CLOBMM_EXECUTE','0','User')
Restart-ScheduledTask -TaskName PolymarketClobMM
```
