# State

This repo intentionally keeps runtime outputs under `logs/` (gitignored).

## PowerShell Task Runtime

- PowerShell task scripts run in background by default.
- Scheduled Task actions run hidden with `-NoBackground` so task instance state stays observable and `MultipleInstances=IgnoreNew` can prevent overlap.
- Background launch mode does not introduce extra long-lived state files by itself.
- `scripts/disable_repo_tasks.ps1` is an operational stop helper and does not create new long-lived state files.
- `scripts/set_no_longshot_daily_mode.ps1` is an operational mode switch helper and does not create new long-lived state files.
- Existing task outputs continue to use documented `logs/` files (for example `logs/clob-arb-monitor.log`, `logs/no_longshot_daily_run.log`, `logs/simmer-ab-daily-report.log`).
- `run_no_longshot_daily_report.ps1` uses `logs/no_longshot_daily_run.lock` for single-instance enforcement (stale lock auto-cleanup).

## Files

Bot supervisor:
- Log: `logs/bot-supervisor.log`
- State: `logs/bot_supervisor_state.json`
- State JSON writes use temp-file atomic replace with retry on transient Windows sharing conflicts.

No-longshot daily daemon (observe-first, live optional):
- Log: `logs/no_longshot_daily_daemon.log`
- State: `logs/no_longshot_daily_daemon_state.json`
- Instance lock: `logs/no_longshot_daily_daemon.lock`
- 既存の日次レポート出力（`logs/no_longshot_daily_*.txt/.json/.csv`）は `run_no_longshot_daily_report.ps1` 側の仕様を継承。
- `no_longshot_daily_daemon.py` の `--realized-refresh-sec` が有効な場合、同 daemon が下記 realized tracker artifacts を日中にも更新する（observe-only）。
- `run_no_longshot_daily_report.ps1` は `-LiveExecute -LiveConfirm YES` 明示時のみ `scripts/execute_no_longshot_live.py` を起動し、small-size live entry を実行する（既定は未実行）。
- Forward realized tracker state/artifacts:
  - Ledger state JSON: `logs/no_longshot_forward_positions.json`
  - Realized daily series JSONL: `logs/no_longshot_realized_daily.jsonl`
  - Latest realized snapshot JSON: `logs/no_longshot_realized_latest.json`
  - Latest rolling-30d monthly return text: `logs/no_longshot_monthly_return_latest.txt`
- Optional strict-lite experiment artifacts (rejected on 2026-02-27; kept for forensics, not active operation):
  - Watcher script/log: `logs/run_no_longshot_strict_lite_watch.ps1`, `logs/no_longshot_strict_lite_watch.log`
  - Screen artifacts: `logs/no_longshot_strict_lite_screen_latest.csv`, `logs/no_longshot_strict_lite_screen_latest.json`
  - Ledger + realized artifacts: `logs/no_longshot_strict_lite_forward_positions.json`, `logs/no_longshot_strict_lite_realized_daily.jsonl`, `logs/no_longshot_strict_lite_realized_latest.json`, `logs/no_longshot_strict_lite_monthly_return_latest.txt`
  - Comparison/range snapshots: `logs/no_longshot_strict_lite_vs_baseline_latest.json`, `logs/no_longshot_strict_lite_status_latest.txt`, `logs/no_longshot_strict_lite_outcome_range_latest.json`, `logs/no_longshot_strict_lite_outcome_range_latest.txt`
- Optional no-longshot live helper runtime files:
  - Log: `logs/no_longshot_live.log`
  - State: `logs/no_longshot_live_state.json`
  - Execution log JSONL: `logs/no_longshot_live_executions.jsonl`

Polymarket event-driven mispricing monitor (observe-only):
- Log: `logs/event-driven-observe.log`
- Signals JSONL: `logs/event-driven-observe-signals.jsonl`
- Metrics JSONL: `logs/event-driven-observe-metrics.jsonl`
- Optional signal dedupe state JSON: `logs/event-driven-observe-signal-state.json`
- Profit-window summary JSON: `logs/event_driven_profit_window_latest.json`
- Profit-window summary TXT: `logs/event_driven_profit_window_latest.txt`
- Optional guarded micro-live state JSON: `logs/event_driven_live_state.json`
  - tracks open positions, daily notional, and recent preview/fill keys for repeat cooldown
  - open position rows store actual filled `size_shares` / `notional_usd`; partial-fill audit fields may include `requested_size_shares`, `requested_notional_usd`, and `order_status`
- Optional guarded micro-live execution log JSONL: `logs/event_driven_live_executions.jsonl`
- Optional guarded micro-live log: `logs/event_driven_live.log`
- Optional local dashboard (read-only web): `scripts/event_driven_monitor_dashboard.py` (default `http://127.0.0.1:8788`)

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
  - Historical profile was long-only (`fade_long_canary` + `fade_dashboard`).
  - As of `2026-03-01`, the local long-only profile uses balanced entry/exit gates:
    - `--consensus-min-score 0.86`, `--consensus-min-agree 2`, `--consensus-min-score-agree1 0.86`, `--consensus-min-score-agree2 0.86`
    - `--take-profit-cents 0.16`, `--stop-loss-cents 0.20`
    - `--expected-move-cost-ratio 1.50`, `--min-expected-edge-cents 0.12`
  - As of `2026-03-01` staged judge `FINAL_500 / NO_GO`, legacy `fade_long_canary` is disabled.
  - As of `2026-03-01`, local profile is regime/side redesign shadow-run (`fade_regime_both_core`, `fade_regime_long_strict`, `fade_regime_short_strict`, `fade_dashboard`).
  - Regime redesign artifacts:
    - both core: `logs/clob-fade-observe-profit-regime-both.log`, `logs/clob_fade_observe_profit_regime_both_state.json`, `logs/clob-fade-observe-profit-regime-both-metrics.jsonl`
    - long strict: `logs/clob-fade-observe-profit-regime-long.log`, `logs/clob_fade_observe_profit_regime_long_state.json`, `logs/clob-fade-observe-profit-regime-long-metrics.jsonl`
    - short strict: `logs/clob-fade-observe-profit-regime-short.log`, `logs/clob_fade_observe_profit_regime_short_state.json`, `logs/clob-fade-observe-profit-regime-short-metrics.jsonl`
- Optional fade-suite supervisor runtime files:
  - `logs/fade_observe_supervisor.log`
  - `logs/fade_observe_supervisor_state.json`
- Optional fade watchdog runtime file:
  - `logs/fade_observe_watchdog.log`
- Fade long-only checkpoint artifacts (observe-only):
  - `logs/fade_longonly_24h_eval_current_latest.json`
  - `logs/fade_longonly_24h_eval_current_latest.txt`
  - JSON は `since_baseline`（原則 `logs/fade_longonly_24h_baseline_latest.json.started_at_local` 起点）と `since_supervisor_start` を併記する。
- Fade long-only staged decision artifacts (observe-only):
  - `logs/fade_longonly_checkpoint_decision_latest.json`
  - `logs/fade_longonly_checkpoint_decision_latest.txt`
  - 判定script既定の `metric_scope` は `since_baseline`（restart跨ぎで closed trades を累積評価）。
- Fade regime/side baseline artifacts (observe-only):
  - `logs/fade_regime_both_baseline_latest.json`
  - `logs/fade_regime_long_baseline_latest.json`
  - `logs/fade_regime_short_baseline_latest.json`
- Fade regime/side staged batch artifacts (observe-only):
  - per-arm checkpoint: `logs/fade_regime_both_eval_latest.json`, `logs/fade_regime_long_eval_latest.json`, `logs/fade_regime_short_eval_latest.json`
  - per-arm decision: `logs/fade_regime_both_decision_latest.json`, `logs/fade_regime_long_decision_latest.json`, `logs/fade_regime_short_decision_latest.json`
  - aggregate decision: `logs/fade_regime_staged_decision_latest.json`, `logs/fade_regime_staged_decision_latest.txt`
- Fade regime/side staged runner log (observe-only):
  - `logs/fade_regime_staged_checks_run.log`

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
- Discord summary dedupe state: `logs/simmer_discord_summary_dedupe_state.json`
- Discord summary dedupe lock: `logs/simmer_discord_summary_dedupe_state.json.lock`
- Optional dedicated supervisor config: `configs/bot_supervisor.simmer_main.observe.json`
- Optional dedicated supervisor runtime files:
  - `logs/simmer_main_supervisor.log`
  - `logs/simmer_main_supervisor_state.json`
- Optional canary supervisor config: `configs/bot_supervisor.simmer_canary.observe.json`
- Optional canary supervisor runtime files:
  - `logs/simmer_canary_supervisor.log`
  - `logs/simmer_canary_supervisor_state.json`
- Optional A/B collector supervisor config: `configs/bot_supervisor.simmer_ab.observe.json`
- Optional A/B collector supervisor runtime files:
  - `logs/simmer_ab_supervisor.log`
  - `logs/simmer_ab_supervisor_state.json`
  - `logs/simmer_ab_observe_supervisor.lock`
- Optional A/B collector worker runtime files:
  - `logs/simmer-ab-baseline.log`
  - `logs/simmer-ab-candidate.log`
  - `logs/simmer_ab_baseline_state.json`
  - `logs/simmer_ab_candidate_state.json`
  - `logs/simmer-ab-baseline-metrics.jsonl`
  - `logs/simmer-ab-candidate-metrics.jsonl`
- A/B helper outputs:
  - `logs/simmer-ab-daily-report.log`
  - `logs/simmer-ab-daily-report.lock`
  - `logs/simmer-ab-daily-compare-latest.txt`
  - `logs/simmer-ab-daily-compare-history.jsonl`
  - `logs/simmer-ab-decision-latest.txt`
  - `logs/simmer-ab-decision-latest.json`
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
- Optional sample-shortfall recheck artifacts (when custom `-WaiterStateFile/-LogFile/-MarkerFile` are used):
  - `logs/weather24h_gate_recheck_waiter_state.json`
  - `logs/alarm_weather24h_gate_recheck.log`
  - `logs/alarm_weather24h_gate_recheck.marker`

Simmer hold-to-settlement followup (observe-only):
- Waiter state: `logs/simmer_settlement_followup_waiter_state.json` (detached watcher process metadata)
- Latest probe snapshot: `logs/simmer_settlement_followup_latest.json`
- Run log: `logs/simmer_settlement_followup_<utc_tag>.log`

Polymarket weather arb monthly-return window estimator (observe-only):
- Observe log (runner default): `logs/clob-arb-weather-profit-observe.log`
- Observe state (runner default): `logs/clob_arb_weather_profit_state.json`
- Profit-window summary JSON: `logs/weather_arb_profit_window_latest.json`
- Profit-window summary TXT: `logs/weather_arb_profit_window_latest.txt`
- GO transition state JSON: `logs/weather_arb_profit_window_transition_state.json`
- GO transition event log (JSONL): `logs/weather_arb_profit_window_transition.log`

Polymarket wallet autopsy toolkit (observe-only):
- Trade snapshot JSON: `logs/trades_<wallet_prefix>_<market_tag>_<utc_tag>.json` (default from `fetch_trades.py`)
- Single-wallet autopsy JSON: `logs/autopsy_<market_slug>_<wallet_prefix>_<utc_tag>.json` (default from `analyze_user.py`)
- Top-holder autopsy JSON: `logs/autopsy_<market_slug>_top<holders>_<utc_tag>.json` (default from `analyze_user.py --top-holders`)
- Candidate report JSON: `logs/wallet_autopsy_candidates*.json` (when simple output filename is used in `report_wallet_autopsy_candidates.py`)
- Entry timing report JSON: `logs/wallet_entry_timing_report.json` (default from `report_wallet_entry_timing.py`)
- Entry timing batch JSON: `logs/wallet_entry_timing_batch.json` (default from `report_wallet_entry_timing_batch.py`)

Polymarket wallet autopsy daily runner (observe-only):
- Runner log: `logs/wallet_autopsy_daily_run.log`
- Candidate snapshot JSON: `logs/wallet_autopsy_daily_candidates_<utc_tag>.json`
- Timing batch JSON: `logs/wallet_autopsy_daily_timing_<utc_tag>.json`
- Summary TXT: `logs/wallet_autopsy_daily_summary_<utc_tag>.txt`
- Summary JSON: `logs/wallet_autopsy_daily_summary_<utc_tag>.json`

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
- Cross-profile overview HTML: `logs/weather_consensus_overview_latest.html`

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
- Daily runner also refreshes `logs/strategy_realized_pnl_daily.jsonl` and `logs/strategy_realized_latest.json`.
- Daily runner also refreshes `logs/strategy_gate_alarm.log` and `logs/strategy_gate_alarm_state.json`.
- Daily runner also refreshes `logs/automation_health_latest.json` and `logs/automation_health_latest.txt`.
- Daily runner also refreshes `logs/weather_consensus_overview_latest.html`.

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
- Daily run also refreshes `logs/weather_consensus_overview_latest.html`.
- Daily run also refreshes `logs/<profile_name>_ab_vs_no_longshot_latest.json/.md` and `logs/<profile_name>_ab_vs_lateprob_latest.json/.md` when inputs are available.
- Daily run also refreshes `logs/<profile_name>_top30_readiness_latest.json` (Top30 readiness gate snapshot).
- Daily run also refreshes `logs/strategy_realized_pnl_daily.jsonl` and `logs/strategy_realized_latest.json`.
- Daily run also refreshes `logs/strategy_gate_alarm.log` and `logs/strategy_gate_alarm_state.json`.
- Daily run also refreshes `logs/automation_health_latest.json` and `logs/automation_health_latest.txt`.

Polymarket weather daily daemon (observe-only):
- Daemon log: `logs/weather_daily_daemon.log`
- Daemon state: `logs/weather_daily_daemon_state.json`
- Daemon lock: `logs/weather_daily_daemon.lock`

Morning status daily runner (observe-only):
- Runner log: `logs/morning_status_daily_run.log`
- 実行時に `scripts/check_morning_status.py` を呼び出し、必要に応じて以下既存 artifact を更新:
  - `logs/strategy_register_latest.json`
  - `logs/uncorrelated_portfolio_proxy_analysis_latest.json`
  - `logs/strategy_gate_alarm.log`, `logs/strategy_gate_alarm_state.json`
  - `logs/automation_health_latest.json`, `logs/automation_health_latest.txt`
  - `docs/llm/IMPLEMENTATION_LEDGER.md`（`--skip-implementation-ledger` 指定時は更新しない）
- no-longshot practical gate 進捗（判定日/残日数/threshold到達）も `logs/strategy_gate_alarm_state.json` から参照する。

Polymarket CLOB realized PnL daily capture (observe-only):
- Daily series JSONL: `logs/clob_arb_realized_daily.jsonl`
- Latest snapshot JSON: `logs/clob_arb_realized_latest.json`
- `clob_arb_realized_daily.jsonl` は累積 realized snapshot 系列（Simmer SDK由来）。日次損益評価では day-over-day 差分を使う。

Polymarket strategy-scoped realized PnL materializer (observe-only):
- Daily series JSONL: `logs/strategy_realized_pnl_daily.jsonl`
- Latest snapshot JSON: `logs/strategy_realized_latest.json`

Polymarket strategy register snapshot (observe-only):
- Snapshot JSON: `logs/strategy_register_latest.json`
- Snapshot HTML: `logs/strategy_register_latest.html`
- Snapshot JSON には `bankroll_policy`、`realized_30d_gate`、`realized_monthly_return` を含む（`realized_30d_gate.decision` は30日最終判定の互換フィールド）。
- Snapshot JSON には `kpi_core`（`daily_realized_pnl_usd`, `daily_realized_pnl_usd_text`, `daily_realized_pnl_day`, `monthly_return_now_text`, `monthly_return_now_source`, `max_drawdown_30d_ratio`, `max_drawdown_30d_text`, `source`）を含む。`monthly_return_now_source` が `realized_rolling_30d_new_condition` のときは、日次PnL/30d最大DDも同じ新条件系列を優先する。
- Snapshot JSON の `no_longshot_status` には月利系に加えて `daily_realized_pnl_usd_latest`, `daily_realized_pnl_day`, `rolling_30d_max_drawdown_ratio`, `rolling_30d_max_drawdown_text` と各 `*_source` を含む。
- `bankroll_policy` は `docs/llm/STRATEGY.md` の `## Bankroll Policy` から抽出され、`initial_bankroll_usd`、`allocation_mode`、`live_max_daily_risk_ratio`、`live_max_daily_risk_usd`、`default_adopted_allocations` を保持する。
- `realized_30d_gate` は `decision_3stage`（7日暫定 / 14日中間 / 30日確定）、`decision_3stage_label_ja`、`stage_label_ja`、`stages`（`label_ja` 含む）、`next_stage`（`label_ja` 含む）を含む。
- `realized_monthly_return` は `strategy_realized_pnl_daily.jsonl` を優先し、未存在時は累積 snapshot の差分系列をフォールバック利用して計算される。

Polymarket strategy uncorrelated-portfolio reporter (observe-only):
- Analysis snapshot JSON: `logs/uncorrelated_portfolio_proxy_analysis_<yyyymmdd>.json`
- Analysis latest JSON (morning check default): `logs/uncorrelated_portfolio_proxy_analysis_latest.json`
- Output memo (docs artifact): `docs/memo_uncorrelated_portfolio_<yyyymmdd>.txt`
- Output latest memo (docs artifact): `docs/memo_uncorrelated_portfolio_latest.txt`
- 相関は strategy-level realized daily return を優先し、未整備戦略は observe proxy 日次系列をフォールバック利用する。
- 戦略ごとの不足メトリクス（missing daily return / overlap不足 / proxy依存）を JSON/memo に明記する。
- JSON `meta.strategy_scope_mode` は分析cohortの決定方法（`explicit_strategy_ids` / `adopted_from_strategy_register`）を示す。

Polymarket strategy gate stage alarm (observe-only):
- Alarm log: `logs/strategy_gate_alarm.log`
- Alarm state JSON: `logs/strategy_gate_alarm_state.json`
- State/log records include:
  - `decision_3stage` transition info (strategy gate)
  - `capital_gate_core` transition info (`HOLD` / `ELIGIBLE_REVIEW`)
  - `rolling_30d_resolved_trades` and `capital_min_resolved_trades`
  - no-longshot practical gate info:
    - `no_longshot_practical_status`
    - `no_longshot_practical_active_decision_date`
    - `no_longshot_practical_remaining_days`
    - `no_longshot_practical_threshold_met`
    - `no_longshot_practical_rollover_count`
    - `no_longshot_practical_last_rollover_on`

Polymarket pending-release transition alarm (observe-only):
- Alarm log JSONL: `logs/pending_release_alarm.log`
- Alarm state JSON: `logs/pending_release_alarm_state.json`
- Instance lock file: `logs/pending_release_alarm.lock`
- `scripts/check_pending_release_alarm.py` uses checker output (`release_check`, `release_ready`, `reason_codes`) and emits transition alarms only when state changes.
- state JSON writes use temp-file atomic replace and keep per-context slots (strategy + conservative settings + override inputs) to avoid cross-profile overwrite.

Polymarket pending-release batch runner (observe-only):
- Latest batch summary JSON: `logs/pending_release_batch_latest.json`
- `scripts/run_pending_release_alarm_batch.py` runs one or more pending-release alarm checks and aggregates scheduler-facing exit/decision.

Automation health report (observe-only):
- Latest health JSON: `logs/automation_health_latest.json`
- Latest health TXT: `logs/automation_health_latest.txt`
- Freshness checks include runner logs such as:
  - `logs/weather_top30_readiness_daily_run.log`
  - `logs/weather_mimic_pipeline_daily_run.log`
  - `logs/no_longshot_daily_run.log`
  - `logs/morning_status_daily_run.log`
- Optional freshness checks include:
  - `logs/event_driven_daily_run.log`
  - `logs/event_driven_daily_summary.txt`
  - `logs/event_driven_profit_window_latest.json`
  - `logs/wallet_autopsy_daily_run.log`
  - `logs/simmer-ab-daily-report.log`
  - `logs/simmer-ab-decision-latest.json`
  - `logs/bot_supervisor_state.json`
  - `logs/fade_regime_staged_checks_run.log`
  - `logs/fade_regime_staged_decision_latest.json`

## Secrets

Secrets are not stored in this repo. Prefer:
- User environment variables
- DPAPI-protected files outside the repo (Windows)
