# Interfaces

## CLI Entrypoints

Polymarket CLOB market making:
- Observe:
  - `python scripts/polymarket_clob_mm.py`
- Live (danger):
  - `python scripts/polymarket_clob_mm.py --execute --confirm-live YES`
- Observation report:
  - `python scripts/report_clob_mm_observation.py --hours 24`
  - `python scripts/report_clob_mm_observation.py --hours 24 --discord`

Polymarket CLOB arb monitor:
- Observe:
  - `python scripts/polymarket_clob_arb_realtime.py`
- Observation report:
  - `python scripts/report_clob_observation.py --hours 24`
  - `python scripts/report_clob_observation.py --hours 24 --discord`

Simmer ($SIM) ping-pong demo:
- Observe:
  - `python scripts/simmer_pingpong_mm.py`
- Live (demo trades on `venue=simmer`):
  - `python scripts/simmer_pingpong_mm.py --execute --confirm-live YES`
- Status:
  - `python scripts/simmer_status.py`

## Secrets (Environment)

- `SIMMER_API_KEY` (required for Simmer SDK)
- `PM_PRIVATE_KEY_DPAPI_FILE` + `PM_FUNDER` (required for Polymarket CLOB)
- `CLOBBOT_DISCORD_WEBHOOK_URL` (optional, secret)
