# Simmer ($SIM) Ping-Pong Bot

Goal: run a **demo** auto-trader using Simmer's virtual venue (`venue=simmer`), without touching real Polymarket funds.

## What This Is (and Is Not)

- This is **not** CLOB market making (no post-only limit orders).
- Simmer's "SIMMER (LMSR)" behaves like an AMM.
- The bot implements a simple inventory ping-pong:
  - maintain `buy_target` and `sell_target` around a reference price
  - buy when `p_yes <= buy_target` (if inventory below cap)
  - sell when `p_yes >= sell_target` (if inventory > 0)

### Buy Sizing Guardrails

- Simmer buy API uses USD `amount` (not direct `shares`).
- The bot derives buy amount from `trade_shares * p_yes`, then applies min/max amount caps.
- To prevent accidental oversize buys in very low-probability markets, the bot **skips** a buy when `min_trade_amount` would imply exceeding target/inventory shares.
- Runtime buys are also blocked when `p_yes` is outside `[prob_min, prob_max]` even if the market was selected earlier.
- While inventory is open, target bands stay anchored (no chase-recentering) to make exits less likely to be missed.
- If Simmer returns a per-market trade rate-limit error, the bot backs off and retries after the reported wait window.

## Files

- Script: `C:\\Repos\\polymarket_mm\\scripts\\simmer_pingpong_mm.py`
- Log: `C:\\Repos\\polymarket_mm\\logs\\simmer-pingpong.log`
- State: `C:\\Repos\\polymarket_mm\\logs\\simmer_pingpong_state.json`
- Metrics: `C:\\Repos\\polymarket_mm\\logs\\simmer-pingpong-metrics.jsonl`

## Prereqs

- `SIMMER_API_KEY` must be set (User env). Get it from `simmer.markets/dashboard` -> SDK.
- If you set it via `[Environment]::SetEnvironmentVariable(...,'User')`, restart your PowerShell (or set `$env:SIMMER_API_KEY` in the current session) so `python` can see it.
  - The bot also attempts a fallback read from `HKCU\\Environment` on Windows if the process environment is missing it.
- Optional Discord notifications:
  - `CLOBBOT_DISCORD_WEBHOOK_URL`
  - `CLOBBOT_DISCORD_MENTION`
  - Event-driven only (startup / stop / fills / halt / periodic summary). No quote-level spam.

## Observe-Only (Default)

```powershell
python C:\Repos\polymarket_mm\scripts\simmer_pingpong_mm.py
```

It will print/log `would BUY` / `would SELL` when thresholds are crossed.

## Live (Demo Trades on $SIM)

This will place trades on Simmer with virtual funds:

```powershell
python C:\Repos\polymarket_mm\scripts\simmer_pingpong_mm.py --execute --confirm-live YES
```

## Autostart (Optional)

This repo includes an installer script:

`C:\Repos\polymarket_mm\scripts\install_simmer_pingpong_task.ps1`

In this environment it may require running PowerShell as Administrator (Task Scheduler permissions).

### Fallback: Reuse An Existing Scheduled Task

If you cannot create a new task (Access denied), you can temporarily repurpose an existing one.

Example: swap `PolymarketClobMM` to run Simmer ping-pong:

```powershell
$task = 'PolymarketClobMM'
$py = (Get-ScheduledTask -TaskName $task).Actions.Execute
$newAction = New-ScheduledTaskAction -Execute $py -Argument '\"C:\Repos\polymarket_mm\scripts\simmer_pingpong_mm.py\"'
Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
Set-ScheduledTask -TaskName $task -Action $newAction | Out-Null
Start-ScheduledTask -TaskName $task
Get-ScheduledTask -TaskName $task | Select TaskName,State
```

Revert back to the Polymarket MM script:

```powershell
$task = 'PolymarketClobMM'
$py = (Get-ScheduledTask -TaskName $task).Actions.Execute
$newAction = New-ScheduledTaskAction -Execute $py -Argument '\"C:\Repos\polymarket_mm\scripts\polymarket_clob_mm.py\"'
Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
Set-ScheduledTask -TaskName $task -Action $newAction | Out-Null
Start-ScheduledTask -TaskName $task
Get-ScheduledTask -TaskName $task | Select TaskName,State
```

## Recommended Settings (Start Small)

```powershell
[Environment]::SetEnvironmentVariable('SIMMER_PONG_PUBLIC_TAG','crypto','User')
[Environment]::SetEnvironmentVariable('SIMMER_PONG_AUTO_SELECT_COUNT','3','User')
[Environment]::SetEnvironmentVariable('SIMMER_PONG_SPREAD_CENTS','3','User')
[Environment]::SetEnvironmentVariable('SIMMER_PONG_TRADE_SHARES','5','User')
[Environment]::SetEnvironmentVariable('SIMMER_PONG_MAX_INVENTORY_SHARES','10','User')
[Environment]::SetEnvironmentVariable('SIMMER_PONG_MIN_TRADE_AMOUNT','1','User')
[Environment]::SetEnvironmentVariable('SIMMER_PONG_MAX_TRADE_AMOUNT','5','User')
[Environment]::SetEnvironmentVariable('SIMMER_PONG_POLL_SEC','2','User')
[Environment]::SetEnvironmentVariable('SIMMER_PONG_QUOTE_REFRESH_SEC','30','User')
[Environment]::SetEnvironmentVariable('SIMMER_PONG_DAILY_LOSS_LIMIT_USD','5','User')
```

Enable 1h Discord summary:

```powershell
[Environment]::SetEnvironmentVariable('SIMMER_PONG_SUMMARY_EVERY_SEC','3600','User')
```

## Safety Notes

- This is **not arbitrage** and can lose money (even on $SIM).
- The bot has a daily loss guard. When it halts, it does **not** auto-resume.
- If you want to resume after a halt, you must clear `halted` in the state file or delete the state file.
