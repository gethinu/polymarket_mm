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

## Secrets

Secrets are not stored in this repo. Prefer:
- User environment variables
- DPAPI-protected files outside the repo (Windows)

