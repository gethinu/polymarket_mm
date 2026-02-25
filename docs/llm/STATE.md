# State

This repo intentionally keeps runtime outputs under `logs/` (gitignored).

## PowerShell Task Runtime

- PowerShell task scripts run in background by default.
- Scheduled Task actions run hidden with `-NoBackground` so task instance state stays observable and `MultipleInstances=IgnoreNew` can prevent overlap.
- Background launch mode does not introduce extra long-lived state files by itself.
- Existing task outputs continue to use documented `logs/` files (for example `logs/clob-arb-monitor.log`, `logs/no_longshot_daily_run.log`, `logs/simmer-ab-daily-report.log`).

## Files

Bot supervisor:
- Log: `logs/bot-supervisor.log`
- State: `logs/bot_supervisor_state.json`
- State JSON writes use temp-file atomic replace with retry on transient Windows sharing conflicts.

No-longshot daily daemon (observe-only):
- Log: `logs/no_longshot_daily_daemon.log`
- State: `logs/no_longshot_daily_daemon_state.json`
- 既存の日次レポート出力（`logs/no_longshot_daily_*.txt/.json/.csv`）は `run_no_longshot_daily_report.ps1` 側の仕様を継承。
- `no_longshot_daily_daemon.py` の `--realized-refresh-sec` が有効な場合、同 daemon が下記 realized tracker artifacts を日中にも更新する（observe-only）。
- Forward realized tracker state/artifacts:
  - Ledger state JSON: `logs/no_longshot_forward_positions.json`
  - Realized daily series JSONL: `logs/no_longshot_realized_daily.jsonl`
  - Latest realized snapshot JSON: `logs/no_longshot_realized_latest.json`
  - Latest rolling-30d monthly return text: `logs/no_longshot_monthly_return_latest.txt`

Polymarket CLOB MM:
- Log: `logs/clob-mm.log`
- State: `logs/clob_mm_state.json`
- Metrics: `logs/clob-mm-metrics.jsonl`

Polymarket CLOB arb monitor:
- Log: `logs/clob-arb-monitor.log`
- State: `logs/clob_arb_state.json`
- Metrics: `logs/clob-arb-monitor-metrics.jsonl`

Polymarket CLOB arb Kelly replay (observe-only):
- Summary JSON: `logs/clob-arb-kelly-replay-summary.json`

Polymarket CLOB fade monitor (observe-only):
- Log: `logs/clob-fade-observe.log`
- State: `logs/clob_fade_observe_state.json`
- Instance lock: `<state-file>.lock` (example: `logs/clob_fade_observe_state.json.lock`)
- Metrics: `logs/clob-fade-observe-metrics.jsonl`
- Optional runtime control (hot reload): `logs/clob_fade_runtime_control.json`
- Optional side router log: `logs/fade-side-router.log`
- State/control JSON writes use temp-file atomic replace with retry on transient Windows sharing conflicts.
- Exit management is applied to open positions even when their token is no longer in the current active universe; max-hold timeout can close these inactive positions to avoid long-lived `open_inactive` residue.
- Optional fade-suite supervisor config: `logs/bot_supervisor.fade.observe.json`
- Optional fade-suite supervisor runtime files:
  - `logs/fade_observe_supervisor.log`
  - `logs/fade_observe_supervisor_state.json`

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

Polymarket BTC short-window panic claim validator (observe-only):
- Summary JSON: `logs/btc5m-panic-claims-latest.json` (default)
- Per-market CSV: `logs/btc5m-panic-claims-markets-latest.csv` (default)
- For `--window-minutes 15`, defaults switch to:
  - `logs/btc15m-panic-claims-latest.json`
  - `logs/btc15m-panic-claims-markets-latest.csv`

Polymarket social profit-claim validator (observe-only):
- Summary JSON: `logs/social_profit_claims_latest.json` (default)
- Summary Markdown: `logs/social_profit_claims_latest.md` (default)

Polymarket hourly up/down high-probability calibration (observe-only):
- Summary JSON: `logs/hourly_updown_highprob_calibration_latest.json` (default)
- Per-sample CSV: `logs/hourly_updown_highprob_calibration_samples_latest.csv` (default)

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

Polymarket weather 24h completion alarm:
- Alarm log: `logs/alarm_weather24h.log` (append-only alert history)
- Alarm marker: `logs/alarm_weather24h.marker` (latest alert snapshot)
- Waiter state: `logs/weather24h_alarm_waiter_state.json` (local detached alarm process metadata)
- Postcheck report: `logs/weather24h_postcheck_latest.txt`
- Postcheck summary: `logs/weather24h_postcheck_latest.json`

Polymarket weather arb monthly-return window estimator (observe-only):
- Observe log (runner default): `logs/clob-arb-weather-profit-observe.log`
- Observe state (runner default): `logs/clob_arb_weather_profit_state.json`
- Profit-window summary JSON: `logs/weather_arb_profit_window_latest.json`
- Profit-window summary TXT: `logs/weather_arb_profit_window_latest.txt`

Polymarket trader cohort autopsy toolkit (observe-only):
- Report JSON: `logs/trader_cohort_*.json` (default output)
- Optional report JSON: 任意の `--out` パス（relativeはrepo基準、単純ファイル名は `logs/` 配下）

Polymarket link-intake user extractor (observe-only):
- User seed file: `logs/link_intake_users_latest.txt` (default)
- Extraction summary JSON: `logs/link_intake_users_latest.json` (default)
- Optional output paths: `--out-user-file`, `--out-json`

Polymarket link-intake cohort runner (observe-only):
- User seed snapshots:
  - `logs/<profile_name>_link_intake_users_<utc_tag>.txt`
  - `logs/<profile_name>_link_intake_users_latest.txt`
- User extraction summaries:
  - `logs/<profile_name>_link_intake_users_<utc_tag>.json`
  - `logs/<profile_name>_link_intake_users_latest.json`
- Cohort outputs:
  - `logs/<profile_name>_link_intake_cohort_<utc_tag>.json`
  - `logs/<profile_name>_link_intake_cohort_latest.json`
- Runner summaries:
  - `logs/<profile_name>_link_intake_summary_<utc_tag>.json`
  - `logs/<profile_name>_link_intake_summary_latest.json`

Polymarket weather mimic profile builder (observe-only):
- Profile JSON: `logs/weather_mimic_profile_*.json` (default output)
  - `filters` には `consensus_score_mode` と、指定時のみ `consensus_weights_override`（overlap/net_yield/max_profit/liquidity/volume）を保持
- Optional supervisor config JSON: `logs/bot_supervisor.weather_mimic.observe.json`
- Optional generated supervisor runtime files:
  - `logs/<profile_name>_supervisor.log`
  - `logs/<profile_name>_supervisor_state.json`
- Optional periodic scanner snapshots (when generated supervisor config is executed):
  - `logs/<profile_name>_no_longshot_latest.csv`
  - `logs/<profile_name>_no_longshot_latest.json`
  - `logs/<profile_name>_lateprob_latest.csv`
  - `logs/<profile_name>_lateprob_latest.json`

Polymarket weather consensus watchlist builder (observe-only):
- Watchlist CSV: `logs/<profile_name>_consensus_watchlist_latest.csv`
- Watchlist JSON: `logs/<profile_name>_consensus_watchlist_latest.json`
- Snapshot HTML: `logs/<profile_name>_consensus_snapshot_latest.html`

Polymarket weather watchlist A/B dryrun comparator (observe-only):
- Report JSON:
  - `logs/<profile_name>_ab_vs_no_longshot_latest.json`
  - `logs/<profile_name>_ab_vs_lateprob_latest.json`
- Report Markdown:
  - `logs/<profile_name>_ab_vs_no_longshot_latest.md`
  - `logs/<profile_name>_ab_vs_lateprob_latest.md`

Polymarket weather Top30 readiness judge (observe-only):
- Decision snapshot JSON: `logs/<profile_name>_top30_readiness_<utc_tag>.json`
- Latest pointer JSON: `logs/<profile_name>_top30_readiness_latest.json`
- Execution plan JSON (operator artifact): `logs/<profile_name>_execution_plan_latest.json`
- Cross-profile readiness report JSON: `logs/weather_top30_readiness_report_latest.json`
- Cross-profile readiness report TXT: `logs/weather_top30_readiness_report_latest.txt`
- Daily runner log: `logs/weather_top30_readiness_daily_run.log`

Polymarket weather mimic pipeline (observe-only):
- Input snapshot: `logs/<profile_name>_inputs_<utc_tag>.txt`
- All-user cohort JSON: `logs/<profile_name>_cohort_all_<utc_tag>.json`
- Winner input snapshot: `logs/<profile_name>_winner_inputs_<utc_tag>.txt`
- Winner-only cohort JSON: `logs/<profile_name>_cohort_winners_<utc_tag>.json`
- Pipeline summary JSON: `logs/<profile_name>_pipeline_summary_<utc_tag>.json`
- Generated profile/supervisor/scanner outputs and A/B dryrun outputs follow existing weather mimic sections above.

Polymarket weather mimic pipeline daily runner (observe-only):
- Runner log: `logs/weather_mimic_pipeline_daily_run.log`
- Per-run artifacts are produced via `scripts/run_weather_mimic_pipeline.py` and follow the weather mimic pipeline section above.
- Daily run also refreshes `logs/<profile_name>_consensus_snapshot_latest.html` when consensus JSON is available.
- Daily run also refreshes `logs/<profile_name>_ab_vs_no_longshot_latest.json/.md` and `logs/<profile_name>_ab_vs_lateprob_latest.json/.md` when inputs are available.
- Daily run also refreshes `logs/<profile_name>_top30_readiness_latest.json` (Top30 readiness gate snapshot).

Polymarket CLOB realized PnL daily capture (observe-only):
- Daily series JSONL: `logs/clob_arb_realized_daily.jsonl`
- Latest snapshot JSON: `logs/clob_arb_realized_latest.json`

Polymarket strategy register snapshot (observe-only):
- Snapshot JSON: `logs/strategy_register_latest.json`
- Snapshot HTML: `logs/strategy_register_latest.html`

## Secrets

Secrets are not stored in this repo. Prefer:
- User environment variables
- DPAPI-protected files outside the repo (Windows)
