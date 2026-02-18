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
- Observation report:
  - `python scripts/report_simmer_observation.py --hours 24`
  - `python scripts/report_simmer_observation.py --hours 24 --discord`
- Status:
  - `python scripts/simmer_status.py`

bitFlyer BTC/JPY MM simulator (observe-only):
- Observe:
  - `python scripts/bitflyer_mm_observe.py`
- Tuning example:
  - `python scripts/bitflyer_mm_observe.py --quote-half-spread-yen 350 --order-size-btc 0.001 --max-inventory-btc 0.01 --run-seconds 3600`
- Observation report:
  - `python scripts/report_bitflyer_mm_observation.py --hours 24`
  - `python scripts/report_bitflyer_mm_observation.py --hours 24 --discord`
- Parameter optimization (metrics replay):
  - `python scripts/optimize_bitflyer_mm_params.py --hours 24`
  - `python scripts/optimize_bitflyer_mm_params.py --hours 24 --half-spreads-yen 150,250,350,500 --order-sizes-btc 0.0005,0.001 --top-n 8`

## Secrets (Environment)

- `SIMMER_API_KEY` (required for Simmer SDK)
- `PM_PRIVATE_KEY_DPAPI_FILE` + `PM_FUNDER` (required for Polymarket CLOB)
- `CLOBBOT_DISCORD_WEBHOOK_URL` (optional, secret)
