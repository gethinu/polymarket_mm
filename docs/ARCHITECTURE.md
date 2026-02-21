# Architecture (polymarket_mm)

This repository contains small, Windows-first scripts for prediction-market automation.

PowerShell task runners under `scripts/*.ps1` are designed to launch detached background processes by default (with foreground opt-out for debugging).

## Components

Polymarket CLOB:
- `scripts/bot_supervisor.py` (+ `configs/bot_supervisor.observe.json`)
  - Launches and supervises multiple bots in parallel from one profile
  - Tracks runtime state and supports `status`/`stop` control commands
- `scripts/polymarket_clob_arb_realtime.py`
  - Realtime scanner/monitor for CLOB opportunities (observe-only by default)
  - Optional execution backend support (danger)
- `scripts/polymarket_btc5m_lag_observe.py`
  - Observe-only BTC 5m lag signal monitor using external spot feeds
  - Includes paper-entry / settlement simulation per 5-minute window
- `scripts/polymarket_btc5m_panic_observe.py`
  - Observe-only BTC 5m/15m panic-fade monitor using CLOB extreme-price conditions
  - Includes paper-entry / settlement simulation per window
- `scripts/polymarket_clob_mm.py`
  - Maker-only quoting (post-only) for a small set of tokens
  - Inventory-aware "ping-pong" quoting
  - Quiet-by-default logs + Discord notifications
- `scripts/fetch_trades.py`
  - Observe-only Data API fetcher for wallet/profile trade history
  - Accepts wallet, `@handle`, and Polymarket profile URLs
- `scripts/analyze_trader_cohort.py`
  - Observe-only multi-account autopsy and cohort strategy extraction
  - Emits JSON report with mimic template hints under `logs/`
- `scripts/build_weather_mimic_profile.py`
  - Observe-only bridge from cohort autopsy JSON to actionable scanner profiles
  - Generates replayable command set and optional supervisor config under `logs/`

Simmer (virtual funds):
- `scripts/simmer_pingpong_mm.py`
  - Ping-pong inventory strategy using Simmer SDK
  - Default `venue=simmer` for demo trading
- `scripts/optimize_simmer_pingpong_params.py`
  - Observe-metrics replay optimizer for large parameter sweeps
  - Supports variable risk scaling (`inverse_vol`) and walk-forward robustness ranking

## Notifications

All bots can post to Discord via webhook:
- `CLOBBOT_DISCORD_WEBHOOK_URL` (secret)
- `CLOBBOT_DISCORD_MENTION` (optional)

Notification policy:
- Event-driven only: startup, stop, fills, loss-guard halt, error halt, and periodic summaries
- No quote-by-quote spam

## Safety Model

- Default mode is "observe-only" wherever possible.
- Live modes require explicit confirmation flags.
- Daily loss guards halt and do not auto-resume.
