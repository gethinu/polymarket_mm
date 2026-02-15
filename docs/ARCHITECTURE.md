# Architecture (polymarket_mm)

This repository contains small, Windows-first scripts for prediction-market automation.

## Components

Polymarket CLOB:
- `scripts/polymarket_clob_arb_realtime.py`
  - Realtime scanner/monitor for CLOB opportunities (observe-only by default)
  - Optional execution backend support (danger)
- `scripts/polymarket_clob_mm.py`
  - Maker-only quoting (post-only) for a small set of tokens
  - Inventory-aware "ping-pong" quoting
  - Quiet-by-default logs + Discord notifications

Simmer (virtual funds):
- `scripts/simmer_pingpong_mm.py`
  - Ping-pong inventory strategy using Simmer SDK
  - Default `venue=simmer` for demo trading

## Notifications

All bots can post to Discord via webhook:
- `CLOBBOT_DISCORD_WEBHOOK_URL` (secret)
- `CLOBBOT_DISCORD_MENTION` (optional)

## Safety Model

- Default mode is "observe-only" wherever possible.
- Live modes require explicit confirmation flags.
- Daily loss guards halt and do not auto-resume.

