# polymarket_mm

Prediction-market automation experiments (Windows-first).

This repo is intentionally **separate** from `c:\\Repos\\quant_trading_system` (US equities systems).

## What’s Here

- Polymarket CLOB:
  - `scripts/bot_supervisor.py` + `configs/bot_supervisor.observe.json` (parallel bot supervisor with restart/status/stop)
  - `scripts/run_bot_supervisor.ps1` (background-first runner for supervisor profile)
  - `scripts/polymarket_clob_mm.py` (maker-only market making, inventory-aware)
  - `scripts/polymarket_clob_arb_realtime.py` (observe-only arb monitor; can be enabled to execute)
  - `scripts/polymarket_btc5m_lag_observe.py` (observe-only BTC 5m lag signal + paper PnL monitor)
  - `scripts/polymarket_btc5m_panic_observe.py` (observe-only BTC 5m/15m panic-fade signal + paper PnL monitor)
  - `scripts/polymarket_btc5m_lmsr_observe.py` (observe-only BTC 5m LMSR/Bayes + fractional Kelly signal monitor)
  - `scripts/polymarket_event_driven_observe.py` (observe-only event-driven mispricing monitor with Bayesian prior/market blend + fractional Kelly sizing)
  - `scripts/polymarket_clob_fade_observe.py` (observe-only multi-bot fade monitor with consensus entry simulation)
  - `scripts/fade_monitor_dashboard.py` (realtime monitoring web dashboard for fade observe logs)
  - `scripts/report_clob_mm_observation.py` (24h observation report)
  - `scripts/report_clob_observation.py` (arb observation report)
  - `scripts/report_btc5m_lmsr_observation.py` (BTC 5m LMSR observe metrics report)
  - `scripts/report_clob_fade_observation.py` (fade-monitor observation report)
- Simmer ($SIM) demo trading:
  - `scripts/simmer_pingpong_mm.py` (inventory ping-pong using Simmer SDK on `venue=simmer`)
  - `scripts/report_simmer_observation.py` (24h observation report)
  - `scripts/optimize_simmer_pingpong_params.py` (grid/random/hybrid search + walk-forward ranking from observe metrics)
  - `scripts/simmer_status.py` (portfolio/positions quick check)
- Crypto MM observe simulator:
  - `scripts/bitflyer_mm_observe.py` (bitFlyer public board based maker simulation, observe-only)
  - `scripts/report_bitflyer_mm_observation.py` (window report from observe metrics/log/state)
  - `scripts/optimize_bitflyer_mm_params.py` (grid search by replaying observe metrics)

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

## Documentation Destination

- Use `docs/` as the single destination for new notes, memos, and runbooks.
- Treat `doc/` as legacy and do not add new files there.
- See `docs/README.md` for placement rules.
