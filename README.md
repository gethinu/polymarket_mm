# polymarket_mm

Prediction-market automation experiments (Windows-first).

This repo is intentionally **separate** from `c:\\Repos\\quant_trading_system` (US equities systems).

## What’s Here

- Polymarket CLOB:
  - `scripts/polymarket_clob_mm.py` (maker-only market making, inventory-aware)
  - `scripts/polymarket_clob_arb_realtime.py` (observe-only arb monitor; can be enabled to execute)
  - `scripts/report_clob_mm_observation.py` (24h observation report)
  - `scripts/report_clob_observation.py` (arb observation report)
- Simmer ($SIM) demo trading:
  - `scripts/simmer_pingpong_mm.py` (inventory ping-pong using Simmer SDK on `venue=simmer`)
  - `scripts/simmer_status.py` (portfolio/positions quick check)

## Install

```powershell
python -m pip install -r requirements.txt
```

## Environment Variables (Secrets)

Do **not** commit secrets.

- Polymarket CLOB auth:
  - `PM_PRIVATE_KEY_DPAPI_FILE` (recommended on Windows)
  - `PM_FUNDER`
  - Optional: `PM_API_KEY`, `PM_API_SECRET`, `PM_API_PASSPHRASE`
- Simmer SDK:
  - `SIMMER_API_KEY`
- Discord notifications:
  - `CLOBBOT_DISCORD_WEBHOOK_URL`
  - Optional: `CLOBBOT_DISCORD_MENTION`

## Runbooks

- `docs/CLOB_MM.md`
- `docs/SIMMER_PINGPONG.md`
- `docs/SECURITY.md`
