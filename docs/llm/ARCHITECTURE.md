# Architecture (polymarket_mm)

This repository contains small, Windows-first scripts for prediction-market automation.

PowerShell task runners under `scripts/*.ps1` are designed to launch detached background processes by default (with foreground opt-out for debugging).

## Canonical Docs

- Canonical operator/developer docs are centralized under `docs/llm/`.
- Active strategy decisions are centralized in `docs/llm/STRATEGY.md`.

## Components

Polymarket CLOB:
- `scripts/bot_supervisor.py` (+ `configs/bot_supervisor.observe.json`)
  - Launches and supervises multiple bots in parallel from one profile
  - Tracks runtime state and supports `status`/`stop` control commands
- `scripts/polymarket_clob_arb_realtime.py`
  - Realtime scanner/monitor for CLOB opportunities (observe-only by default)
  - Optional execution backend support (danger)
- `scripts/polymarket_btc5m_lag_observe.py`
  - Observe-only BTC 5m lag signal monitor using external spot feeds
  - Includes paper-entry / settlement simulation per 5-minute window
- `scripts/polymarket_btc5m_panic_observe.py`
  - Observe-only BTC 5m/15m panic-fade monitor using CLOB extreme-price conditions
  - Includes paper-entry / settlement simulation per window
- `scripts/report_btc5m_panic_claims.py`
  - Observe-only historical validator for panic-pricing frequency claims
  - Scans closed BTC up/down windows and summarizes winner-side low-price availability
- `scripts/report_social_profit_claims.py`
  - Observe-only validator for social/media profit claims using realized daily PnL artifacts
  - Converts headline claims to explicit measurable conditions (`daily`, `rolling-30d`, `hourly*hours`)
- `scripts/report_hourly_updown_highprob_calibration.py`
  - Observe-only calibration validator for hourly crypto up/down markets near expiry
  - Measures realized win-rate vs quoted high-probability entry prices at fixed time-to-end
- `scripts/polymarket_clob_mm.py`
  - Maker-only quoting (post-only) for a small set of tokens
  - Inventory-aware "ping-pong" quoting
  - Quiet-by-default logs + Discord notifications
- `scripts/fetch_trades.py`
  - Observe-only Data API fetcher for wallet/profile trade history
  - Accepts wallet, `@handle`, and Polymarket profile URLs
- `scripts/analyze_trades.py`
  - Observe-only single-wallet trade autopsy analyzer
  - Computes timeline profitability, hedge status, and behavior diagnostics from saved trade JSON
- `scripts/analyze_user.py`
  - Observe-only market-resolve + wallet autopsy wrapper
  - Supports one-wallet analysis and top-holder batch scans
- `scripts/report_wallet_autopsy_candidates.py`
  - Observe-only cross-market candidate extractor from top-holder autopsy logs
  - Supports hedge-edge and endgame timing filters
- `scripts/report_wallet_entry_timing.py`
  - Observe-only timing profiler for one autopsy JSON
  - Reports time-to-end distributions and minute-of-hour concentration
- `scripts/report_wallet_entry_timing_batch.py`
  - Observe-only batch timing profiler from candidate JSON
  - Produces comparable per-wallet timing rows for ranked review
- `scripts/run_wallet_autopsy_daily_report.ps1`
  - Background-by-default daily runner for candidate extraction + timing batch + summary artifacts
- `scripts/install_wallet_autopsy_daily_task.ps1`
  - Scheduled-task installer for recurring wallet autopsy daily reports
- `scripts/extract_link_intake_users.py`
  - Observe-only bridge from link-intake JSON to wallet/profile seed lists
  - Emits `--user-file` compatible outputs for cohort and mimic pipeline entrypoints
- `scripts/run_link_intake_cohort.py`
  - Observe-only orchestrator for `link_intake JSON -> user extraction -> cohort analysis`
  - Emits timestamped + latest artifacts for reproducible intake-to-cohort handoff
- `scripts/analyze_trader_cohort.py`
  - Observe-only multi-account autopsy and cohort strategy extraction
  - Emits JSON report with mimic template hints under `logs/`
- `scripts/build_weather_mimic_profile.py`
  - Observe-only bridge from cohort autopsy JSON to actionable scanner profiles
  - Generates replayable command set and optional supervisor config under `logs/`
- `scripts/build_weather_consensus_watchlist.py`
  - Observe-only merger for no-longshot/lateprob scanner outputs
  - Produces one ranked weather watchlist for operator review
- `scripts/render_weather_consensus_snapshot.py`
  - Observe-only visualizer that renders consensus watchlist JSON to HTML snapshot
- `scripts/judge_weather_top30_readiness.py`
  - Observe-only readiness gate evaluator for practical deployment decisions
  - Produces deterministic GO/NO_GO decision snapshots under `logs/`
- `scripts/report_weather_top30_readiness.py`
  - Observe-only aggregator for cross-profile readiness trend/status
  - Produces summary JSON/TXT under `logs/`
- `scripts/record_simmer_realized_daily.py`
  - Observe-only daily recorder for Simmer SDK realized-PnL snapshot
  - Produces cumulative daily JSONL and latest snapshot JSON under `logs/`
- `scripts/render_strategy_register_snapshot.py`
  - Observe-only registry aggregator for `docs/llm/STRATEGY.md` + readiness/runtime hints
  - Produces one JSON/HTML snapshot for strategy list and decision status under `logs/`
- `scripts/check_strategy_gate_alarm.py`
  - Observe-only gate transition checker for strategy register 3-stage decision
  - Emits one-shot transition alarms and persists last-seen stage under `logs/`
- `scripts/check_morning_status.py` (+ `scripts/check_morning_status.ps1`)
  - Observe-only one-command morning check for realized refresh + gate/readiness/health summary
  - Supports fail-fast guards for gate finalization and automation health decisions
- `scripts/run_morning_status_daily.ps1`
  - Background-by-default daily runner wrapper for morning status checks
- `scripts/install_morning_status_daily_task.ps1`
  - Scheduled-task installer for recurring morning status checks
- `scripts/run_weather_top30_readiness_daily.ps1`
  - Background-by-default daily runner for strict/quality readiness refresh and aggregation
- `scripts/install_weather_top30_readiness_daily_task.ps1`
  - Scheduled-task installer for recurring Top30 readiness daily runs
- `scripts/run_weather_mimic_pipeline.py`
  - Observe-only end-to-end orchestrator for profile URL intake, winner selection, mimic profile build, and scanner execution
  - Keeps intermediate artifacts under `logs/` for reproducibility
- `scripts/run_weather_mimic_pipeline_daily.ps1`
  - Background-by-default daily runner wrapper for weather mimic pipeline
- `scripts/install_weather_mimic_pipeline_daily_task.ps1`
  - Scheduled-task installer for recurring weather mimic pipeline runs
- `scripts/weather_daily_daemon.py`
  - Observe-only daemon for weather mimic/top30 daily jobs without relying on Task Scheduler
  - Keeps runtime artifacts under `logs/` with single-instance lock semantics
- `scripts/set_weather_daily_mode.ps1`
  - Observe-only mode switcher for weather daily execution path (`Task Scheduler` vs `weather_daily_daemon`)
  - Applies duplicate-run guard by toggling weather scheduled tasks alongside supervisor daemon enablement
- `scripts/set_no_longshot_daily_mode.ps1`
  - Observe-only mode switcher for no-longshot daily execution path (`Task Scheduler` vs `no_longshot_daily_daemon`)
  - Applies duplicate-run guard by toggling `NoLongshotDailyReport` task alongside supervisor daemon enablement
- `scripts/run_weather_24h_alarm_action.ps1`
  - One-shot alarm action (append log + marker + bounded notification)
- `scripts/run_weather_24h_postcheck.ps1`
  - Observe-only follow-up report and usefulness snapshot after weather 24h alarm
- `scripts/report_weather_arb_profit_window.py`
  - Observe-only estimator that converts weather arb monitor logs into thresholded monthly-return projections
  - Produces deterministic GO/NO_GO judgment snapshots under `logs/`
- `scripts/run_weather_arb_profit_window.ps1`
  - Background-by-default one-shot runner for weather observe + profit-window report generation
- `scripts/set_weather_24h_alarm.ps1`
  - Detached local waiter setter for one-shot alarm execution
- `scripts/cancel_weather_24h_alarm.ps1`
  - Local waiter canceller for one-shot alarm execution
- `scripts/install_weather_24h_alarm_task.ps1`
  - One-shot scheduled-task installer for weather observe completion alarms

Simmer (virtual funds):
- `scripts/simmer_pingpong_mm.py`
  - Ping-pong inventory strategy using Simmer SDK
  - Default `venue=simmer` for demo trading
- `scripts/optimize_simmer_pingpong_params.py`
  - Observe-metrics replay optimizer for large parameter sweeps
  - Supports variable risk scaling (`inverse_vol`) and walk-forward robustness ranking
- `scripts/run_simmer_ab_observe_supervisor.ps1`
  - Background-by-default runner for baseline/candidate A/B collectors via `bot_supervisor`
  - Uses dedicated profile `configs/bot_supervisor.simmer_ab.observe.json`
- `scripts/install_simmer_ab_observe_task.ps1`
  - Installer for recurring A/B collector watchdog launch (Scheduled Task / fallback reuse)
  - Supports fallback to startup-script handoff when task registration is blocked

## Notifications

All bots can post to Discord via webhook:
- `CLOBBOT_DISCORD_WEBHOOK_URL` (secret)
- `CLOBBOT_DISCORD_MENTION` (optional)

Notification policy:
- Event-driven only: startup, stop, fills, loss-guard halt, error halt, and periodic summaries
- No quote-by-quote spam

## Safety Model

- Default mode is "observe-only" wherever possible.
- Live modes require explicit confirmation flags.
- Daily loss guards halt and do not auto-resume.
