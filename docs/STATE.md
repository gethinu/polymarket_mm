# State

This repo intentionally keeps runtime outputs under `logs/` (gitignored).

## Files

Polymarket CLOB MM:
- Log: `logs/clob-mm.log`
- State: `logs/clob_mm_state.json`
- Metrics: `logs/clob-mm-metrics.jsonl`

Polymarket CLOB arb monitor:
- Log: `logs/clob-arb-monitor.log`
- State: `logs/clob_arb_state.json`

Simmer ping-pong:
- Log: `logs/simmer-pingpong.log`
- State: `logs/simmer_pingpong_state.json`
- Metrics: `logs/simmer-pingpong-metrics.jsonl`
- State details:
  - `market_states[*].buy_target` / `sell_target` are persisted target bands.
  - Targets are refreshed by `quote_refresh_sec` while flat; when inventory is held, targets can stay anchored to the last execution band to avoid chase-recentering.
  - On universe changes, previous market states are retained as inactive (not deleted) to preserve PnL continuity.

bitFlyer MM simulator (observe-only):
- Log: `logs/bitflyer-mm-observe.log`
- State: `logs/bitflyer_mm_observe_state.json`
- Metrics: `logs/bitflyer-mm-observe-metrics.jsonl`

## Secrets

Secrets are not stored in this repo. Prefer:
- User environment variables
- DPAPI-protected files outside the repo (Windows)
