# State

This repo intentionally keeps runtime outputs under `logs/` (gitignored).

## PowerShell Task Runtime

- PowerShell task scripts run in background by default.
- Background launch mode does not introduce extra long-lived state files by itself.
- Existing task outputs continue to use documented `logs/` files (for example `logs/clob-arb-monitor.log`, `logs/no_longshot_daily_run.log`, `logs/simmer-ab-daily-report.log`).

## Files

Bot supervisor:
- Log: `logs/bot-supervisor.log`
- State: `logs/bot_supervisor_state.json`

Polymarket CLOB MM:
- Log: `logs/clob-mm.log`
- State: `logs/clob_mm_state.json`
- Metrics: `logs/clob-mm-metrics.jsonl`

Polymarket CLOB arb monitor:
- Log: `logs/clob-arb-monitor.log`
- State: `logs/clob_arb_state.json`

Polymarket CLOB fade monitor (observe-only):
- Log: `logs/clob-fade-observe.log`
- State: `logs/clob_fade_observe_state.json`
- Metrics: `logs/clob-fade-observe-metrics.jsonl`
- Optional runtime control (hot reload): `logs/clob_fade_runtime_control.json`
- Optional side router log: `logs/fade-side-router.log`

Polymarket BTC 5m lag monitor (observe-only):
- Log: `logs/btc5m-lag-observe.log`
- State: `logs/btc5m_lag_observe_state.json`
- Metrics: `logs/btc5m-lag-observe-metrics.jsonl`

Polymarket BTC short-window panic-fade monitor (observe-only):
- 5m default:
  - Log: `logs/btc5m-panic-observe.log`
  - State: `logs/btc5m_panic_observe_state.json`
  - Metrics: `logs/btc5m-panic-observe-metrics.jsonl`
- 15m default (when `--window-minutes 15` and paths are default):
  - Log: `logs/btc15m-panic-observe.log`
  - State: `logs/btc15m_panic_observe_state.json`
  - Metrics: `logs/btc15m-panic-observe-metrics.jsonl`

Simmer ping-pong:
- Log: `logs/simmer-pingpong.log`
- State: `logs/simmer_pingpong_state.json`
- Instance lock: `logs/simmer_pingpong_state.json.lock`
- Metrics: `logs/simmer-pingpong-metrics.jsonl`
- A/B helper outputs:
  - `logs/simmer-ab-daily-report.log`
  - `logs/simmer-ab-daily-compare-latest.txt`
  - `logs/simmer-ab-daily-compare-history.jsonl`
- A/B state locks:
  - `logs/simmer_ab_baseline_state.json.lock`
  - `logs/simmer_ab_candidate_state.json.lock`
- State details:
  - `market_states[*].buy_target` / `sell_target` are persisted target bands.
  - `market_states[*].last_fill_ts` tracks holding age for time-based exit controls.
  - Targets are refreshed by `quote_refresh_sec` while flat; when inventory is held, targets can stay anchored to the last execution band to avoid chase-recentering.
  - On universe changes, previous market states are retained as inactive (not deleted) to preserve PnL continuity.

bitFlyer MM simulator (observe-only):
- Log: `logs/bitflyer-mm-observe.log`
- State: `logs/bitflyer_mm_observe_state.json`
- Metrics: `logs/bitflyer-mm-observe-metrics.jsonl`

Polymarket trader cohort autopsy toolkit (observe-only):
- Report JSON: `logs/trader_cohort_*.json` (default output)
- Optional report JSON: 任意の `--out` パス（relativeはrepo基準、単純ファイル名は `logs/` 配下）

Polymarket weather mimic profile builder (observe-only):
- Profile JSON: `logs/weather_mimic_profile_*.json` (default output)
- Optional supervisor config JSON: `logs/bot_supervisor.weather_mimic.observe.json`
- Optional generated supervisor runtime files:
  - `logs/<profile_name>_supervisor.log`
  - `logs/<profile_name>_supervisor_state.json`
- Optional periodic scanner snapshots (when generated supervisor config is executed):
  - `logs/<profile_name>_no_longshot_latest.csv`
  - `logs/<profile_name>_no_longshot_latest.json`
  - `logs/<profile_name>_lateprob_latest.csv`
  - `logs/<profile_name>_lateprob_latest.json`

## Secrets

Secrets are not stored in this repo. Prefer:
- User environment variables
- DPAPI-protected files outside the repo (Windows)
