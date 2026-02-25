# Interfaces

## CLI Entrypoints

PowerShell task scripts (background by default):
- Default behavior:
  - Launches a detached background PowerShell process and returns immediately.
  - Use `-NoBackground` to run in the current console for debugging.
- Common control flags:
  - `-NoBackground` (run foreground)
  - `-Background` (internal child-mode flag; usually not passed manually)
- Scheduled Task registration policy:
  - Task actions use `powershell.exe -WindowStyle Hidden ... -NoBackground` so Task Scheduler keeps instance tracking (`MultipleInstances=IgnoreNew`) effective.
  - Detached child spawning from task actions is avoided to prevent process fan-out.
- Entrypoints:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_clob_arb_monitor.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_arb_observe.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_arb_profit_window.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_arb_observe_task.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/set_weather_24h_alarm.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/cancel_weather_24h_alarm.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_24h_postcheck.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_24h_alarm_task.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_bot_supervisor.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_event_driven_daily_report.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_wallet_autopsy_daily_report.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_mimic_pipeline_daily.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_morning_status_daily.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_simmer_ab_daily_report.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_clob_arb_monitor_task.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_no_longshot_daily_task.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_event_driven_daily_task.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_wallet_autopsy_daily_task.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_mimic_pipeline_daily_task.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_morning_status_daily_task.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_simmer_pingpong_task.ps1`
    - If new task creation is denied, installer falls back to reusing `PolymarketClobMM`.
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup_clob_backend.ps1`
    - `setup_clob_backend.ps1` is interactive and relaunches in a separate PowerShell window.

Bot supervisor (parallel manager for multiple bots):
- Python entrypoint:
  - `python scripts/bot_supervisor.py run --config configs/bot_supervisor.observe.json`
  - `python scripts/bot_supervisor.py status --state-file logs/bot_supervisor_state.json`
  - `python scripts/bot_supervisor.py stop --state-file logs/bot_supervisor_state.json`
- PowerShell runner (background by default):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_bot_supervisor.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_bot_supervisor.ps1 -NoBackground`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_bot_supervisor.ps1 -ConfigFile configs/bot_supervisor.observe.json`
- Key flags:
  - `run`: `--config`, `--log-file`, `--state-file`, `--poll-sec`, `--write-state-sec`, `--run-seconds`, `--no-restart`
  - `run`: `--halt-on-job-failure`, `--halt-when-all-stopped`
  - `status/stop`: `--state-file`
  - env overrides: `BOTSUP_CONFIG`, `BOTSUP_RUN_SECONDS`, `BOTSUP_POLL_SEC`, `BOTSUP_WRITE_STATE_SEC`
- Default `configs/bot_supervisor.observe.json` jobs:
  - enabled: `btc5m_panic`, `event_driven`, `no_longshot_daily_daemon`, `fade_both`, `fade_long_canary`, `fade_short_canary`, `fade_router`
  - disabled by default: `btc5m_lag`, `clob_fade`
  - `no_longshot_daily_daemon` を有効化する場合、重複実行防止のため Scheduled Task `NoLongshotDailyReport` は停止/無効化して運用する

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
  - `python scripts/polymarket_clob_arb_realtime.py --universe btc-updown --strategy yes-no --btc-updown-window-minutes 5,15 --btc-5m-windows-back 1 --btc-5m-windows-forward 1 --min-edge-cents 1.0`
  - `python scripts/polymarket_clob_arb_realtime.py --universe gamma-active --strategy event-pair --gamma-limit 300 --max-subscribe-tokens 40`
  - `python scripts/polymarket_clob_arb_realtime.py --universe gamma-active --strategy event-pair --max-subscribe-tokens 40 --observe-exec-edge-filter --observe-exec-edge-min-usd 0 --observe-exec-edge-strike-limit 2 --observe-exec-edge-cooldown-sec 90 --observe-exec-edge-filter-strategies event-yes`
  - `python scripts/polymarket_clob_arb_realtime.py --universe gamma-active --strategy yes-no --sports-live-only --sports-live-prestart-min 10 --sports-live-postend-min 30`
  - `python scripts/polymarket_clob_arb_realtime.py --universe gamma-active --strategy yes-no --sports-live-only --sports-market-types draw,moneyline,total --sports-require-matchup --sports-feed-provider espn --sports-feed-strict`
  - `python scripts/polymarket_clob_arb_realtime.py --universe gamma-active --strategy yes-no --sports-live-only --notify-observe-signals`
  - `python scripts/polymarket_clob_arb_realtime.py --universe gamma-active --strategy all --max-subscribe-tokens 80 --wallet-signal-enable --wallet-signal-weight 0.25 --metrics-log-all-candidates`
- Key flags:
  - `--universe` (`weather` / `gamma-active` / `btc-5m` / `btc-updown`)
  - `--strategy` (`buckets` / `yes-no` / `event-pair` / `both` / `all`)
  - `--btc-updown-window-minutes`（`btc-5m`/`btc-updown` で監視する短期窓。`5` または `15`、複数は `5,15`）
  - `--btc-5m-windows-back`, `--btc-5m-windows-forward`（`btc-5m`/`btc-updown` で前後何窓まで同時監視するか）
  - `--sports-live-only`, `--sports-live-prestart-min`, `--sports-live-postend-min`（`gamma-active + yes-no` でスポーツ市場の試合中/前後のみを監視）
  - `--sports-market-types`, `--sports-market-types-exclude`（`gamma-active + yes-no` のスポーツ市場タイプ絞り込み。`moneyline,spread,total,draw,btts,other`）
  - `--sports-require-matchup`（`--sports-live-only` と併用時に `A vs B` / `A @ B` 形式の対戦テキストを必須化）
  - `--sports-feed-provider`, `--sports-feed-strict`, `--sports-feed-timeout-sec`, `--sports-feed-live-buffer-sec`, `--sports-feed-espn-paths`（外部スポーツフィード連携。`provider=espn` でライブ窓判定を補強）
  - `--notify-observe-signals`, `--observe-notify-min-interval-sec`（observe-only でも閾値シグナル検知時に Discord 通知。間隔で連投抑制）
  - `--observe-exec-edge-filter`, `--observe-exec-edge-min-usd`, `--observe-exec-edge-strike-limit`, `--observe-exec-edge-cooldown-sec`, `--observe-exec-edge-filter-strategies`（observe-only で exec推定エッジが連続で弱いイベントを一時ミュート）
  - `--metrics-file`, `--metrics-log-all-candidates`（候補評価メトリクスJSONL。既定は `logs/clob-arb-monitor-metrics.jsonl`）
  - `--wallet-signal-enable`, `--wallet-signal-weight`, `--wallet-signal-max-baskets`, `--wallet-signal-holders-limit`, `--wallet-signal-top-wallets`, `--wallet-signal-min-trades`, `--wallet-signal-max-trades`, `--wallet-signal-page-size`（Gamma選別にホルダー行動スコアを合成）
  - wallet signal の順位反映は `--max-subscribe-tokens > 0` の scored selection 時のみ有効。
  - `event-pair` は binary negRisk イベントの `YES+YES` と `NO+NO` ペアを監視（observe-only 既定）
- Observation report:
  - `python scripts/report_clob_observation.py --hours 24`
  - `python scripts/report_clob_observation.py --hours 24 --discord`

Polymarket CLOB arb Kelly replay (observe-only):
- Replay:
  - `python scripts/replay_clob_arb_kelly.py`
  - `python scripts/replay_clob_arb_kelly.py --hours 72 --edge-mode exec --fill-ratio-mode min --miss-penalty 0.002 --scales 0.25,0.50,1.00 --pretty`
  - `python scripts/replay_clob_arb_kelly.py --metrics-glob "logs/clob-arb-monitor*.jsonl" --bootstrap-iters 5000 --out-json logs/clob-arb-kelly-replay-summary.json`
- Key flags:
  - Inputs/window: `--metrics-file`, `--metrics-glob`, `--hours`, `--max-samples`, `--min-gap-ms-per-event`, `--require-threshold-pass`
  - Return proxy: `--edge-mode`, `--fill-ratio-mode`, `--miss-penalty`, `--min-fill-ratio`, `--stale-grace-sec`, `--stale-penalty-per-sec`, `--max-worst-stale-sec`, `--min-edge-usd`
  - Kelly/MC: `--max-full-kelly`, `--scales`, `--bootstrap-iters`, `--bootstrap-sample-size`, `--seed`
  - Output: `--out-json`, `--pretty`

Polymarket CLOB arb sports window analyzer (observe-only):
- Analyze:
  - `python scripts/analyze_clob_arb_sports_windows.py --metrics-file logs/clob-arb-monitor-metrics.jsonl --hours 6`
  - `python scripts/analyze_clob_arb_sports_windows.py --metrics-file logs/clob-arb-metrics-sports-step1-20260223_113431.jsonl --near-zero-floor -0.01 --out-json logs/clob-arb-sports-window-analysis.json`
- Key flags:
  - `--metrics-file`, `--hours`, `--since`, `--until`（分析対象ウィンドウ）
  - `--near-zero-floor`, `--bucket-minutes`, `--min-bucket-samples`（near-zero/時間帯抽出の閾値）
  - `--recommend-top-n`, `--out-json`（推奨市場タイプ件数とJSON出力）

Polymarket weather arb monitor helper (observe-only):
- PowerShell runner (background by default):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_arb_observe.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_arb_observe.ps1 -NoBackground -RunSeconds 300`
- Key flags:
  - `-RunSeconds`, `-MinEdgeCents`, `-Shares`, `-Strategy` (`buckets`/`yes-no`/`both`), `-SummaryEverySec`, `-MaxSubscribeTokens`
  - `-LogFile`, `-StateFile`, `-NoBackground`
  - Default strategy is `buckets` for weather basket monitoring.
  - This helper always forces observe-only (`CLOBBOT_EXECUTE=0` in process scope).
- Scheduled task installer:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_arb_observe_task.ps1 -NoBackground`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_arb_observe_task.ps1 -NoBackground -RunNow`
  - Key flags: `-TaskName`, `-IntervalMinutes`, `-DurationDays`, `-RunSeconds`, `-MinEdgeCents`, `-Shares`, `-Strategy`, `-SummaryEverySec`, `-MaxSubscribeTokens`

Polymarket weather 24h completion alarm:
- Preferred alarm setter (detached local waiter, background by default):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/set_weather_24h_alarm.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/set_weather_24h_alarm.ps1 -NoBackground -AlarmAt "2026-02-24 09:00:00"`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/cancel_weather_24h_alarm.ps1`
- Legacy alarm installer (one-shot scheduled task):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_24h_alarm_task.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_24h_alarm_task.ps1 -NoBackground -AlarmAt "2026-02-24 09:00:00"`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_24h_alarm_task.ps1 -NoBackground -AlarmAt "2026-02-24 09:00:00" -RunNow`
- Alarm action (task-internal one-shot command):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_24h_alarm_action.ps1`
- Postcheck runner (observe-only, alarm follow-up):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_24h_postcheck.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_24h_postcheck.ps1 -NoBackground -Hours 24`
- Key flags:
  - setter: `-AlarmAt`, `-Message`, `-LogFile`, `-MarkerFile`, `-WaiterStateFile`, `-MsgTimeoutSec`, `-NoBackground`
  - cancel: `-WaiterStateFile`
  - postcheck: `-ObserveLogFile`, `-Hours`, `-ThresholdsCents`, `-MinUsefulPct`, `-ReportFile`, `-SummaryFile`, `-AlarmLogFile`, `-NoBackground`
  - installer: `-TaskName`, `-AlarmAt`, `-Message`, `-LogFile`, `-MarkerFile`, `-MsgTimeoutSec`, `-RunNow`, `-NoBackground`
  - action: `-Message`, `-LogFile`, `-MarkerFile`, `-MsgTimeoutSec`, `-DisableMsg`
- Behavior:
  - Writes append log, latest marker, and local waiter state under `logs/`.
  - Uses bounded `msg.exe` notification (forced timeout) to avoid task hang.
  - Runs `run_weather_24h_postcheck.ps1` after alarm action to produce latest usefulness snapshot.

Polymarket weather arb monthly-return window estimator (observe-only):
- Report tool:
  - `python scripts/report_weather_arb_profit_window.py --log-file logs/clob-arb-weather-observe.log --hours 24`
  - `python scripts/report_weather_arb_profit_window.py --log-file logs/clob-arb-weather-observe.log --hours 24 --thresholds-cents "1,1.5,2,3,4" --capture-ratios "0.25,0.35,0.50" --assumed-bankroll-usd 100 --target-monthly-return-pct 15 --pretty`
- One-shot runner (observe + report, background by default):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_arb_profit_window.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_arb_profit_window.ps1 -NoBackground -ObserveRunSeconds 1800 -AssumedBankrollUsd 100 -TargetMonthlyReturnPct 15`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_arb_profit_window.ps1 -NoBackground -SkipObserve -ReportHours 24 -FailOnNoGo`
- Key flags:
  - report: `--thresholds-cents`, `--capture-ratios`, `--base-capture-ratio`, `--assumed-bankroll-usd`, `--target-monthly-return-pct`, `--min-opportunities-per-day`, `--min-unique-events`, `--min-positive-rows-pct`, `--out-json`, `--out-txt`
  - runner: `-SkipObserve`, `-ObserveRunSeconds`, `-ObserveMinEdgeCents`, `-ObserveStrategy`, `-ObserveLogFile`, `-ObserveStateFile`, `-ReportHours`, `-AssumedBankrollUsd`, `-TargetMonthlyReturnPct`, `-FailOnNoGo`
  - runner always uses `scripts/run_weather_arb_observe.ps1` internally, so execution remains observe-only (`CLOBBOT_EXECUTE=0` enforced in process scope).

Polymarket wallet autopsy toolkit (observe-only):
- Fetch user trades (Data API):
  - `python scripts/fetch_trades.py 0xWallet`
  - `python scripts/fetch_trades.py 'c0O0OLI0O0O0'`
  - `python scripts/fetch_trades.py '@c0O0OLI0O0O0'`
  - `python scripts/fetch_trades.py 'https://polymarket.com/@c0O0OLI0O0O0'`
  - `python scripts/fetch_trades.py 0xfedc381bf3fb5d20433bb4a0216b15dbbc5c6398`
  - `python scripts/fetch_trades.py 0xWallet --market 0xConditionId --max-trades 2000`
  - `python scripts/fetch_trades.py 0xWallet --market 0xConditionId --out my_trades.json --pretty`
- Analyze saved trades JSON:
  - `python scripts/analyze_trades.py logs/my_trades.json`
  - `python scripts/analyze_trades.py logs/my_trades.json --out logs/my_trades_summary.json --pretty`
- Delayed copy simulation from wallet history:
  - `python scripts/simulate_wallet_copy_latency.py 0xWallet`
  - `python scripts/simulate_wallet_copy_latency.py @0x8dxd --max-trades 3000 --latency-sec-buckets 0,1,2,5,10`
  - `python scripts/simulate_wallet_copy_latency.py logs/my_trades.json --entry-slippage-bps 5 --exit-slippage-bps 5 --per-sec-slippage-bps 0.6 --out logs/wallet_copy_latency.json --pretty`
- Resolve market by name and autopsy one wallet:
  - `python scripts/analyze_user.py "Bitcoin Up or Down February 6" 0xWallet`
  - `python scripts/analyze_user.py "Bitcoin Up or Down February 6" 0xWallet my_analysis.json --pretty`
- Scan top holders in a market and autopsy each:
  - `python scripts/analyze_user.py "Bitcoin Up or Down February 6" --top-holders 10`
  - `python scripts/analyze_user.py "Bitcoin Up or Down February 6" --top-holders 10 --holders-limit 30 --max-trades 1500`
- Key flags:
  - `fetch_trades.py`: `--market`, `--limit`, `--max-trades`, `--sleep-sec`, `--out`, `--pretty`
    - profile URL の `@0x...-<suffix>` 形式（例: `https://polymarket.com/@0xabc...-1762688003124`）も受け付け、先頭の wallet (`0x...`) を自動解決
  - `analyze_trades.py`: `--wallet`, `--market-title`, `--out`, `--pretty`
  - `analyze_user.py`: `--top-holders`, `--holders-limit`, `--page-size`, `--max-trades`, `--pretty`
  - `simulate_wallet_copy_latency.py`: `--market`, `--max-trades`, `--latency-sec-buckets`, `--entry-slippage-bps`, `--exit-slippage-bps`, `--per-sec-slippage-bps`, `--taker-fee-rate`, `--min-hold-sec`, `--out`, `--pretty`
- Cross-market candidate report from autopsy logs:
  - `python scripts/report_wallet_autopsy_candidates.py`
  - `python scripts/report_wallet_autopsy_candidates.py --latest-per-market --min-trades 10 --min-profitable-pct 70 --out wallet_autopsy_candidates.json`
  - `python scripts/report_wallet_autopsy_candidates.py --latest-per-market --statuses ARBITRAGE_CANDIDATE --min-trades 10 --min-profitable-pct 70 --min-hedge-edge-pct 1.0 --out wallet_autopsy_candidates_arb_strict.json`
  - `python scripts/report_wallet_autopsy_candidates.py --latest-per-market --statuses ARBITRAGE_CANDIDATE --min-trades 10 --min-profitable-pct 70 --min-hedge-edge-pct 1.0 --max-time-to-end-p50-sec 3600 --timing-sides BUY --out wallet_autopsy_candidates_arb_endgame.json`
  - `python scripts/report_wallet_autopsy_candidates.py --latest-per-market --statuses ARBITRAGE_CANDIDATE --min-trades 10 --min-profitable-pct 70 --min-hedge-edge-pct 1.0 --max-time-to-end-p10-sec 1200 --max-time-to-end-p50-sec 3600 --max-time-to-end-p90-sec 5400 --timing-sides BUY --out wallet_autopsy_candidates_arb_endgame_tight.json`
  - `python scripts/report_wallet_autopsy_candidates.py --latest-per-market --statuses ARBITRAGE_CANDIDATE --min-trades 10 --min-profitable-pct 70 --min-hedge-edge-pct 1.0 --max-time-to-end-p50-sec 3600 --max-time-to-end-p90-sec 5400 --timing-sides BUY --out wallet_autopsy_candidates_arb_endgame_consistent.json`
  - `python scripts/report_wallet_autopsy_candidates.py --latest-per-market --statuses ARBITRAGE_CANDIDATE --timing-profile endgame_consistent --min-timing-trade-count 50 --timing-sides BUY --out wallet_autopsy_candidates_arb_profile_consistent.json`
  - `python scripts/report_wallet_autopsy_candidates.py --statuses ARBITRAGE_CANDIDATE --top 30`
  - `python scripts/report_wallet_autopsy_candidates.py --glob "autopsy_*_top20_*.json" --statuses ARBITRAGE_CANDIDATE,PARTIAL_HEDGE --min-trades 5`
- Entry timing report from single-wallet autopsy JSON:
  - `python scripts/report_wallet_entry_timing.py logs/autopsy_<market>_<wallet>.json`
  - `python scripts/report_wallet_entry_timing.py logs/autopsy_<market>_<wallet>.json --sides BUY --sec-buckets 30,60,120,300,600 --out wallet_entry_timing.json`
  - `python scripts/report_wallet_entry_timing.py logs/autopsy_<market>_<wallet>.json --sides BUY,SELL --top-minutes 15`
- Entry timing batch report from candidate JSON:
  - `python scripts/report_wallet_entry_timing_batch.py logs/wallet_autopsy_candidates_arb_endgame_3600.json`
  - `python scripts/report_wallet_entry_timing_batch.py logs/wallet_autopsy_candidates_arb_all_v2.json --top 10 --sides BUY --max-trades 1500 --out wallet_entry_timing_batch.json`
  - `python scripts/report_wallet_entry_timing_batch.py logs/wallet_autopsy_candidates_arb_all_v2.json --timing-profile endgame_consistent --top 10 --out wallet_entry_timing_batch_profiled.json`
  - `python scripts/report_wallet_entry_timing_batch.py logs/wallet_autopsy_candidates_arb_all_v2.json --sides BUY,SELL --sec-buckets 60,300,900,1800,3600 --top-minutes 20`
- Daily runner (PowerShell, background by default):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_wallet_autopsy_daily_report.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_wallet_autopsy_daily_report.ps1 -NoBackground -LatestPerMarket -TimingProfile endgame_consistent -MinTimingTradeCount 30`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_wallet_autopsy_daily_report.ps1 -NoBackground -Statuses ARBITRAGE_CANDIDATE,PARTIAL_HEDGE -Top 80 -BatchTop 30`
  - 日次実行で `logs/wallet_autopsy_daily_summary_*.txt` と `logs/wallet_autopsy_daily_summary_*.json` を出力
- Daily task installer (PowerShell):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_wallet_autopsy_daily_task.ps1 -NoBackground -StartTime 00:10 -LatestPerMarket`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_wallet_autopsy_daily_task.ps1 -NoBackground -StartTime 00:10 -TimingProfile endgame_consistent -MinTimingTradeCount 30 -RunNow`

Polymarket link-intake user extractor (observe-only):
- Extract wallet/profile seeds from link-intake JSON:
  - `python scripts/extract_link_intake_users.py logs/link_intake_20260224_7links.json`
  - `python scripts/extract_link_intake_users.py logs/link_intake_20260224_7links.json --min-confidence medium --out-user-file logs/link_intake_users_7links.txt --out-json logs/link_intake_users_7links.json --pretty`
  - `python scripts/extract_link_intake_users.py logs/link_intake_20260224_7links.json --resolve-profiles`
- Intended handoff:
  - `python scripts/analyze_trader_cohort.py --user-file logs/link_intake_users_latest.txt --pretty`
  - `python scripts/run_weather_mimic_pipeline.py --user-file logs/link_intake_users_latest.txt --profile-name weather_linkseed`
- Key flags:
  - `intake_json` (`link_intake_*.json` input)
  - `--min-confidence` (`none` / `low` / `medium` / `high`)
  - `--resolve-profiles`（抽出した profile URL / `@handle` を observe-only で wallet 解決）
  - `--out-user-file`, `--out-json`, `--pretty`

Polymarket link-intake cohort runner (observe-only):
- Run `link_intake JSON -> user extraction -> cohort autopsy` in one command:
  - `python scripts/run_link_intake_cohort.py logs/link_intake_20260224_7links.json`
  - `python scripts/run_link_intake_cohort.py logs/link_intake_20260224_7links.json --profile-name linkseed_7links --min-confidence medium --max-trades 2500 --pretty`
  - `python scripts/run_link_intake_cohort.py logs/link_intake_20260224_7links.json --profile-name linkseed_7links --no-resolve-profiles --weather-keywords "weather,temperature,forecast"`
- Key flags:
  - `intake_json` (`link_intake_*.json` input)
  - `--profile-name`（出力ファイルの接頭辞）
  - `--min-confidence` (`none` / `low` / `medium` / `high`)
  - `--resolve-profiles` / `--no-resolve-profiles`
  - Pass-through to cohort analysis: `--limit`, `--max-trades`, `--sleep-sec`, `--weather-keywords`, `--min-low-price`, `--max-high-price`, `--top-markets`
  - `--pretty`

Polymarket trader cohort autopsy toolkit (observe-only):
- Analyze multiple wallets/profiles and extract mimic hints:
  - `python scripts/analyze_trader_cohort.py --user https://polymarket.com/@meropi --user https://polymarket.com/@1pixel`
  - `python scripts/analyze_trader_cohort.py --user-file logs/target_users.txt --max-trades 4000 --pretty`
  - `python scripts/analyze_trader_cohort.py --user @securebet --user @automatedAItradingbot --out logs/weather_cohort.json --pretty`
- Key flags:
  - `--user` (repeatable wallet / `@handle` / profile URL)
  - `--user-file` (newline-separated user identifiers)
  - `--max-trades`, `--limit`, `--sleep-sec` (Data API fetch controls)
  - `--top-markets` (walletごとに保持する上位市場件数)
  - `--weather-keywords` (custom weather判定キーワード)
  - `--min-low-price`, `--max-high-price` (低価格・高価格帯の判定しきい値)
  - `--out`, `--pretty`

Polymarket weather mimic profile builder (observe-only):
- Build scanner settings/commands from cohort autopsy report:
  - `python scripts/build_weather_mimic_profile.py logs/cohort_weather_watchlist_20260221.json`
  - `python scripts/build_weather_mimic_profile.py logs/cohort_weather_focus_winners_20260221.json --profile-name weather_focus --pretty`
  - `python scripts/build_weather_mimic_profile.py logs/cohort_weather_watchlist_20260221.json --out-json logs/weather_mimic_profile_latest.json --out-supervisor-config logs/bot_supervisor.weather_mimic.observe.json`
  - `python scripts/bot_supervisor.py run --config logs/bot_supervisor.weather_mimic.observe.json`
  - 生成される supervisor config には `no_longshot` / `lateprob` / `consensus_watchlist` の3ジョブが含まれる
- Key flags:
  - `cohort_json` (`scripts/analyze_trader_cohort.py` の出力JSON)
  - `--profile-name` (出力ログ名/ジョブ名の接頭辞)
  - `--out-json`, `--out-supervisor-config`, `--pretty`
  - `--scan-max-pages`, `--scan-page-size`, `--scan-interval-sec` (生成されるobserveジョブの周期/負荷設定。weather探索では `--scan-max-pages` の既定値は深めの `80`)
  - `--min-liquidity`, `--min-volume-24h`, `--top-n` (生成されるフィルタ基準。既定は `--min-liquidity 500 --min-volume-24h 100`)
  - `--lateprob-disable-weather-filter`（生成される lateprob コマンドに `--include-regex ""` を入れて weather 既定フィルタを無効化）
  - `--consensus-score-mode`（`balanced` / `liquidity` / `edge`。生成される consensus watchlist スコア重みを切替）
  - `--consensus-weight-overlap`, `--consensus-weight-net-yield`, `--consensus-weight-max-profit`, `--consensus-weight-liquidity`, `--consensus-weight-volume`（consensus重みの手動上書き）
  - `--consensus-require-overlap`（consensus watchlist を no_longshot/lateprob 共通銘柄に限定）
  - `--consensus-max-per-correlation-bucket`（推定相関バケットごとの候補上限）
  - `--consensus-min-turnover-ratio`, `--consensus-max-hours-to-end`（consensus候補の回転率/残時間フィルタ）
  - `--no-longshot-per-trade-cost`, `--no-longshot-min-net-yield-per-day`（生成される no_longshot コマンドにコスト/最低期待値を付与）
  - `--lateprob-per-trade-cost`, `--lateprob-max-active-stale-hours`（生成される lateprob コマンドにコスト/stale上限を付与）
  - 生成される `include-regex` は weather core を優先（`weather|temperature|precipitation|forecast|\\brain\\b|\\bsnow\\b|\\bwind\\b|humidity`）

Polymarket weather consensus watchlist builder (observe-only):
- Merge weather mimic scanner outputs into one ranked list:
  - `python scripts/build_weather_consensus_watchlist.py`
  - `python scripts/build_weather_consensus_watchlist.py --no-longshot-csv logs/weather_focus_mimic_no_longshot_latest.csv --lateprob-csv logs/weather_focus_mimic_lateprob_latest.csv --profile-name weather_focus_mimic`
  - `python scripts/build_weather_consensus_watchlist.py --top-n 30 --min-liquidity 800 --min-volume-24h 200 --pretty`
  - `python scripts/build_weather_consensus_watchlist.py --profile-name weather_focus_mimic --require-overlap --top-n 25`
  - `python scripts/build_weather_consensus_watchlist.py --profile-name weather_focus_mimic --require-overlap --max-per-correlation-bucket 2 --top-n 50`
- Key flags:
  - `--no-longshot-csv`, `--lateprob-csv`（入力CSV。既定は weather mimic 生成先）
  - `--profile-name`（既定出力名の接頭辞）
  - `--top-n`, `--min-liquidity`, `--min-volume-24h`（最終watchlistの品質しきい値）
  - `--score-mode`（`balanced` / `liquidity` / `edge`）
  - `--weight-overlap`, `--weight-net-yield`, `--weight-max-profit`, `--weight-liquidity`, `--weight-volume`（必要時の手動重み上書き）
  - `--require-overlap`（両スキャナ共通候補のみ残す）
  - `--max-per-correlation-bucket`（推定相関バケットごとの候補上限）
  - `--out-csv`, `--out-json`, `--pretty`

Polymarket weather watchlist A/B dryrun comparator (observe-only):
- Compare consensus watchlist with one baseline watchlist:
  - `python scripts/compare_weather_watchlists.py --consensus-json logs/weather_7acct_auto_consensus_watchlist_latest.json --baseline-json logs/weather_7acct_auto_no_longshot_latest.json --baseline-name no_longshot --out-json logs/weather_7acct_auto_ab_vs_no_longshot_latest.json --out-md logs/weather_7acct_auto_ab_vs_no_longshot_latest.md --pretty`
  - `python scripts/compare_weather_watchlists.py --consensus-json logs/weather_7acct_auto_consensus_watchlist_latest.json --baseline-json logs/weather_7acct_auto_lateprob_latest.json --baseline-name lateprob --out-json logs/weather_7acct_auto_ab_vs_lateprob_latest.json --out-md logs/weather_7acct_auto_ab_vs_lateprob_latest.md --pretty`
- Key flags:
  - `--consensus-json`, `--baseline-json`（watchlist JSON入力。`top` または `rows` を解釈）
  - `--consensus-name`, `--baseline-name`（レポート上のラベル）
  - `--top-n`（比較対象の上位件数）
  - `--out-json`, `--out-md`, `--pretty`

Polymarket weather consensus snapshot renderer (observe-only):
- Render watchlist JSON into a visual HTML snapshot:
  - `python scripts/render_weather_consensus_snapshot.py --consensus-json logs/weather_7acct_auto_consensus_watchlist_latest.json --profile-name weather_7acct_auto`
  - `python scripts/render_weather_consensus_snapshot.py --consensus-json logs/weather_daily_fulltest_consensus_watchlist_latest.json --out-html logs/weather_daily_fulltest_consensus_snapshot_latest.html`
- Key flags:
  - `--consensus-json`（`build_weather_consensus_watchlist.py` 出力JSON）
  - `--profile-name`（既定出力HTML名の接頭辞）
  - `--top-n`（可視化に表示する上位件数）
  - `--out-html`（出力HTML。simple filenameは `logs/` 配下）

Polymarket weather consensus overview renderer (observe-only):
- Render cross-profile comparison overview from multiple consensus watchlists:
  - `python scripts/render_weather_consensus_overview.py`
  - `python scripts/render_weather_consensus_overview.py --profile weather_7acct_auto --profile weather_visual_test --top-n 30`
  - `python scripts/render_weather_consensus_overview.py --profile weather_focus_mimic --out-html logs/weather_focus_overview_latest.html`
- Key flags:
  - `--profile`（repeatable。比較対象 profile 名。未指定時は `weather_7acct_auto` と `weather_visual_test`）
  - `--top-n`（profileごとに比較対象とする上位件数）
  - `--out-html`（出力HTML。simple filenameは `logs/` 配下）

Polymarket weather Top30 readiness judge (observe-only):
- Judge practical deployment readiness from consensus watchlist:
  - `python scripts/judge_weather_top30_readiness.py --consensus-json logs/weather_7acct_auto_consensus_watchlist_latest.json --supervisor-config logs/bot_supervisor.weather_7acct_auto.observe.json --pretty`
  - `python scripts/judge_weather_top30_readiness.py --consensus-json logs/weather_visual_test_consensus_watchlist_latest.json --execution-plan-file logs/weather_visual_test_execution_plan_latest.json --pretty`
  - `python scripts/judge_weather_top30_readiness.py --consensus-json logs/weather_visual_test_consensus_watchlist_latest.json --no-require-execution-plan`
- Key flags:
  - `--consensus-json`（`build_weather_consensus_watchlist.py` 出力JSON）
  - `--supervisor-config`（execution readiness 判定対象の supervisor 設定JSON）
  - `--execution-plan-file`（`ready_for_deploy=true` かつ guard/checklist 条件を満たす実行計画JSON。既定は `logs/<profile_name>_execution_plan_latest.json`）
  - `--min-row-count`, `--min-both-ratio`, `--min-median-net-yield-per-day`, `--min-top10-avg-max-profit`
  - `--min-median-liquidity`, `--min-median-volume-24h`, `--max-median-hours-to-end`（Hard Gate閾値）
  - `--require-execution-plan` / `--no-require-execution-plan`
  - `--out-json`, `--out-latest-json`, `--pretty`

Polymarket weather Top30 readiness report aggregator (observe-only):
- Aggregate latest readiness decisions across profiles:
  - `python scripts/report_weather_top30_readiness.py --pretty`
  - `python scripts/report_weather_top30_readiness.py --glob "logs/*_top30_readiness_*latest.json" --mode strict`
  - `python scripts/report_weather_top30_readiness.py --profile weather_7acct_auto --profile weather_visual_test`
- Key flags:
  - `--glob`（入力 readiness JSON のglob）
  - `--mode`（`all` / `strict` / `quality` / `unknown`）
  - `--profile`（対象プロファイルの絞り込み）
  - `--out-json`, `--out-txt`, `--pretty`

Polymarket weather Top30 readiness daily runner (observe-only):
- PowerShell runner (background by default):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_top30_readiness_daily.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_top30_readiness_daily.ps1 -NoBackground -Profiles weather_7acct_auto,weather_visual_test -FailOnNoGo`
- Scheduled task installer:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_top30_readiness_daily_task.ps1 -NoBackground`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_top30_readiness_daily_task.ps1 -NoBackground -StartTime 00:40 -RunNow`
- Key flags:
  - runner: `-Profiles`, `-FailOnNoGo`, `-Discord`, `-NoBackground`
  - runner は各 profile に対して strict/quality readiness を再計算し、最後に集計レポートを更新
  - runner は実行後に `scripts/render_weather_consensus_overview.py` を呼び出し、`logs/weather_consensus_overview_latest.html` を更新
  - runner は実行後に `scripts/record_simmer_realized_daily.py` を呼び出し、`logs/clob_arb_realized_daily.jsonl` を更新
  - runner は実行後に `scripts/materialize_strategy_realized_daily.py` を呼び出し、`logs/strategy_realized_pnl_daily.jsonl` を更新
  - runner は実行後に `scripts/render_strategy_register_snapshot.py` を呼び出し、`logs/strategy_register_latest.json/.html` を更新
  - runner は実行後に `scripts/check_strategy_gate_alarm.py` を呼び出し、3段階ゲート遷移アラームを更新
  - runner は実行後に `scripts/report_automation_health.py` を呼び出し、`logs/automation_health_latest.json/.txt` を更新
  - `WEATHER_TOP30_READINESS_DAILY_DISCORD=1` でも Discord 送信を有効化
  - installer: `-TaskName`, `-StartTime`, `-Profiles`, `-FailOnNoGo`, `-Discord`, `-RunNow`
  - installer の `-RunNow` は Scheduled Task の即時起動ではなく、runner を `-NoBackground` で直接1回実行する（ヘッドレス環境での `LastTaskResult=0xC000013A` 汚染回避）。

Polymarket weather mimic pipeline (observe-only):
- Run end-to-end intake->winner-selection->mimic build->scan in one command:
  - `python scripts/run_weather_mimic_pipeline.py --user https://polymarket.com/@meropi --user https://polymarket.com/@securebet --profile-name weather_3w_mimic`
  - `python scripts/run_weather_mimic_pipeline.py --user-file logs/cohort_weather_7acct_inputs_20260222.txt --profile-name weather_7acct --consensus-score-mode edge`
  - `python scripts/run_weather_mimic_pipeline.py --user-file logs/cohort_weather_7acct_inputs_20260222.txt --profile-name weather_7acct --consensus-weight-overlap 0.25 --consensus-weight-net-yield 0.40 --consensus-weight-max-profit 0.20 --consensus-weight-liquidity 0.10 --consensus-weight-volume 0.05`
  - `python scripts/run_weather_mimic_pipeline.py --user-file logs/cohort_weather_7acct_inputs_20260222.txt --profile-name weather_aleiah --aleiah-weather-pack --no-run-scans`
  - `python scripts/run_weather_mimic_pipeline.py --user-file logs/cohort_weather_7acct_inputs_20260222.txt --profile-name weather_browomo --browomo-small-edge-pack --no-run-scans`
  - `python scripts/run_weather_mimic_pipeline.py --user-file logs/cohort_weather_7acct_inputs_20260222.txt --profile-name weather_c0 --c0-micro-moves-pack --no-run-scans`
  - `python scripts/run_weather_mimic_pipeline.py --user-file logs/cohort_weather_7acct_inputs_20260222.txt --profile-name weather_velon --velon-micro-moves-pack --no-run-scans`
  - `python scripts/run_weather_mimic_pipeline.py --user-file logs/cohort_weather_7acct_inputs_20260222.txt --profile-name weather_roan --roan-roadmap-pack --no-run-scans`
- Key flags:
  - `--user`（repeatable wallet / `@handle` / profile URL）
  - `--user-file`（newline-separated identifiers）
  - `--profile-name`（出力ファイル接頭辞）
  - `--min-weather-share-pct`, `--min-trades`, `--min-realized-pnl`（勝者抽出フィルタ）
  - `--winner-require-weather-token`（repeatable。勝者の `top_weather_tokens` に必要な語）
  - `--winner-min-weather-token-hits`（`winner-require-weather-token` の最小一致数）
  - `--winner-min-roundtrip-key-share-pct`（勝者の `roundtrip_key_share_pct` 下限）
  - `--winner-min-close-leg-count`（勝者の `close_leg_count` 下限）
  - `--winner-max-avg-interval-sec`（勝者の `activity.avg_interval_sec` 上限）
  - `--winner-min-sell-to-buy-notional-ratio`（勝者の `sell_notional / buy_notional` 下限）
  - `--aleiah-weather-pack`（Link01/Aleiah準拠の既定: `nyc`/`london` トークン重視 + consensus overlap 必須）
  - `--browomo-small-edge-pack`（Link02/browomo準拠の既定: 候補数増加 + 相関バケット分散 + consensus overlap）
  - `--c0-micro-moves-pack`（Link03/c0準拠の既定: wallet高回転フィルタ + overlap/diversification + 流動性下限強化）
  - `--velon-micro-moves-pack`（Link04/velon準拠の既定: exit前提の高回転フィルタ + 高流動性/短ホライゾン + 監視頻度強化）
  - `--roan-roadmap-pack`（Link05/Roan準拠の既定: コスト込みエッジ下限 + 短中ホライゾン/高流動性 + 分散上限1）
  - `--max-trades`, `--limit`, `--sleep-sec`（cohort取得パラメータ）
  - `--scan-max-pages`, `--scan-page-size`, `--min-liquidity`, `--min-volume-24h`, `--top-n`（scanner品質/負荷）
  - `--lateprob-disable-weather-filter`（lateprob 生成コマンドの weather 既定フィルタを無効化）
  - `--consensus-score-mode`, `--consensus-weight-overlap`, `--consensus-weight-net-yield`, `--consensus-weight-max-profit`, `--consensus-weight-liquidity`, `--consensus-weight-volume`
  - `--consensus-require-overlap`（consensus watchlist を no_longshot/lateprob 共通候補に限定）
  - `--consensus-max-per-correlation-bucket`（推定相関バケットごとの候補上限）
  - `--consensus-min-turnover-ratio`（consensus候補の `volume_24h / liquidity_num` 下限）
  - `--consensus-max-hours-to-end`（consensus候補の `hours_to_end` 上限）
  - `--no-longshot-per-trade-cost`, `--no-longshot-min-net-yield-per-day`（no_longshot 生成コマンドのコスト/最低期待値）
  - `--lateprob-per-trade-cost`, `--lateprob-max-active-stale-hours`（lateprob 生成コマンドのコスト/active stale 制約）
  - `--no-run-scans`（profile生成のみ、no_longshot/lateprob/consensus の即時実行を抑止）
  - `--no-run-scans` を指定しない通常実行時は、consensus を `no_longshot` / `lateprob` と比較するA/B dryrun (`scripts/compare_weather_watchlists.py`) を自動実行

Polymarket weather mimic pipeline daily runner (observe-only):
- PowerShell runner (background by default):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_mimic_pipeline_daily.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_mimic_pipeline_daily.ps1 -NoBackground -ProfileName weather_7acct_auto -NoRunScans`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_mimic_pipeline_daily.ps1 -NoBackground -ProfileName weather_7acct_auto -NoRunScans -LateprobDisableWeatherFilter`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_mimic_pipeline_daily.ps1 -NoBackground -ProfileName weather_7acct_auto -FailOnReadinessNoGo`
- Scheduled task installer:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_mimic_pipeline_daily_task.ps1 -NoBackground`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_mimic_pipeline_daily_task.ps1 -NoBackground -LateprobDisableWeatherFilter`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_mimic_pipeline_daily_task.ps1 -NoBackground -StartTime 00:20 -RunNow`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_weather_mimic_pipeline_daily_task.ps1 -NoBackground -StartTime 00:20 -FailOnReadinessNoGo`
- Key flags:
  - `-UserFile`, `-ProfileName`
  - `-UserFile` 既定値は `configs/weather_mimic_target_users.txt`
  - `-MinWeatherSharePct`, `-MinTrades`, `-MinRealizedPnl`
  - `-ConsensusScoreMode`, `-ConsensusWeightOverlap`, `-ConsensusWeightNetYield`, `-ConsensusWeightMaxProfit`, `-ConsensusWeightLiquidity`, `-ConsensusWeightVolume`
  - `-NoRunScans`, `-LateprobDisableWeatherFilter`, `-Discord`, `-FailOnReadinessNoGo`
  - 実行後に `consensus_json` から `logs/<profile_name>_consensus_snapshot_latest.html` を自動生成
  - 実行後に `scripts/render_weather_consensus_overview.py` を呼び出し、`logs/weather_consensus_overview_latest.html` を更新
  - 通常実行では `logs/<profile_name>_ab_vs_no_longshot_latest.json/.md` と `logs/<profile_name>_ab_vs_lateprob_latest.json/.md` も更新
  - 実行後に `scripts/judge_weather_top30_readiness.py` を呼び出し、`logs/<profile_name>_top30_readiness_latest.json` も更新（execution plan が未整備でも判定JSONは更新される）
  - 実行後に `scripts/record_simmer_realized_daily.py` を呼び出し、`logs/clob_arb_realized_daily.jsonl` を更新
  - 実行後に `scripts/materialize_strategy_realized_daily.py` を呼び出し、`logs/strategy_realized_pnl_daily.jsonl` を更新
  - 実行後に `scripts/render_strategy_register_snapshot.py` を呼び出し、`logs/strategy_register_latest.json/.html` を更新
  - 実行後に `scripts/check_strategy_gate_alarm.py` を呼び出し、3段階ゲート遷移アラームを更新
  - 実行後に `scripts/report_automation_health.py` を呼び出し、`logs/automation_health_latest.json/.txt` を更新
  - `-FailOnReadinessNoGo` を指定すると readiness 判定が `GO` 以外（または判定不能）の場合に非0終了する
  - installer の `-RunNow` は Scheduled Task の即時起動ではなく、runner を `-NoBackground` で直接1回実行する（ヘッドレス環境での `LastTaskResult=0xC000013A` 汚染回避）。

Polymarket CLOB realized PnL daily capture (observe-only):
- Capture/update one daily realized-PnL row from Simmer SDK:
  - `python scripts/record_simmer_realized_daily.py`
  - `python scripts/record_simmer_realized_daily.py --day 2026-02-24 --out-jsonl logs/clob_arb_realized_daily.jsonl --out-latest-json logs/clob_arb_realized_latest.json --pretty`
- Note:
  - `logs/clob_arb_realized_daily.jsonl` は Simmer SDK の累積 realized スナップショット系列。日次損益として使う際は day-over-day 差分に変換する。
- Key flags:
  - `--day`（`YYYY-MM-DD`。既定は当日UTC）
  - `--out-jsonl`（日次系列JSONL。既定は `logs/clob_arb_realized_daily.jsonl`）
  - `--out-latest-json`（最新スナップショットJSON。既定は `logs/clob_arb_realized_latest.json`）
  - `--api-timeout-sec`, `--pretty`

Polymarket strategy-scoped realized PnL materializer (observe-only):
- Convert account-level realized snapshots into strategy-scoped daily rows:
  - `python scripts/materialize_strategy_realized_daily.py`
  - `python scripts/materialize_strategy_realized_daily.py --strategy-id weather_clob_arb_buckets_observe --source-jsonl logs/clob_arb_realized_daily.jsonl --out-jsonl logs/strategy_realized_pnl_daily.jsonl --out-latest-json logs/strategy_realized_latest.json --pretty`
- Key flags:
  - `--strategy-id`（対象戦略ID。既定 `weather_clob_arb_buckets_observe`）
  - `--source-jsonl`（入力系列。既定 `logs/clob_arb_realized_daily.jsonl`）
  - `--allocation-ratio`（戦略配賦率 0..1。既定 `1.0`）
  - `--source-series-mode`（`auto` / `cumulative_snapshot` / `daily_realized`）
  - `--first-day-delta-zero` / `--no-first-day-delta-zero`（累積系列の初日差分扱い）
  - `--out-jsonl`, `--out-latest-json`, `--pretty`

Polymarket strategy register snapshot (observe-only):
- Aggregate strategy canon + readiness + runtime hints into one JSON/HTML snapshot:
  - `python scripts/render_strategy_register_snapshot.py`
  - `python scripts/render_strategy_register_snapshot.py --strategy-md docs/llm/STRATEGY.md --readiness-glob "logs/*_top30_readiness_*latest.json" --realized-strategy-id weather_clob_arb_buckets_observe --out-json logs/strategy_register_latest.json --out-html logs/strategy_register_latest.html --pretty`
- Snapshot payload includes:
  - `realized_30d_gate`（realized 判定ゲート。互換目的で `decision` は従来の 30日判定を維持）
  - `realized_30d_gate.decision_3stage`（`7日暫定 / 14日中間 / 30日確定` の段階判定）
  - `realized_30d_gate.decision_3stage_label_ja`（段階判定の日本語表示ラベル）
  - `realized_30d_gate.stage_label_ja`（現在段階ラベルの日本語表示）
  - `realized_30d_gate.stages`（段階ごとの `min_days` と到達フラグ）
  - `realized_30d_gate.stages[*].label_ja`（段階ラベルの日本語表示）
  - `realized_30d_gate.next_stage`（次段階と残り日数）
  - `realized_30d_gate.next_stage.label_ja`（次段階ラベルの日本語表示）
  - `realized_30d_gate.stage_thresholds_days`（段階閾値）
  - `realized_monthly_return`（projected monthly return / rolling_30d return / bankroll source）
  - `logs/strategy_realized_pnl_daily.jsonl` が存在する場合は優先利用し、未存在時は `clob_arb_realized_daily.jsonl` をフォールバック利用。
- Key flags:
  - `--strategy-md`（戦略レジストリの入力Markdown。既定は `docs/llm/STRATEGY.md`）
  - `--readiness-glob`（readiness 判定JSONのglob。既定は `logs/*_top30_readiness_*latest.json`）
  - `--clob-state-file`（CLOB arb runtime state JSON。既定は `logs/clob_arb_state.json`）
  - `--min-realized-days`（実現PnL最終判定に必要な最小日数。既定は `30`。3段階の最終ゲートとして利用）
  - `--realized-strategy-id`（実現PnL判定の対象戦略ID。既定 `weather_clob_arb_buckets_observe`）
  - `--out-json`, `--out-html`, `--pretty`
  - `--skip-process-scan`（実行中プロセス検出を省略）

Polymarket strategy gate stage alarm (observe-only):
- Detect 3-stage gate transitions from strategy snapshot and emit one alarm event:
  - `python scripts/check_strategy_gate_alarm.py`
  - `python scripts/check_strategy_gate_alarm.py --snapshot-json logs/strategy_register_latest.json --state-json logs/strategy_gate_alarm_state.json --log-file logs/strategy_gate_alarm.log --strategy-id weather_clob_arb_buckets_observe --discord`
- Key flags:
  - `--snapshot-json`（入力スナップショットJSON。既定は `logs/strategy_register_latest.json`）
  - `--state-json`（前回判定保持の状態JSON。既定は `logs/strategy_gate_alarm_state.json`）
  - `--log-file`（アラーム追記ログ。既定は `logs/strategy_gate_alarm.log`）
  - `--strategy-id`（通知対象戦略ID。既定 `weather_clob_arb_buckets_observe`）
  - `--discord`（Webhook設定時に遷移アラームをDiscord通知）
  - `--pretty`

Morning strategy gate check (observe-only):
- Run one command to refresh and print concise gate status:
  - `python scripts/check_morning_status.py`
  - `python scripts/check_morning_status.py --no-refresh`
  - `python scripts/check_morning_status.py --fail-on-gate-not-ready`
  - `python scripts/check_morning_status.py --fail-on-stage-not-final`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check_morning_status.ps1`
- Key flags:
  - `--no-refresh`（`record_simmer_realized_daily.py` / `materialize_strategy_realized_daily.py` / `render_strategy_register_snapshot.py` の更新を省略）
  - `--skip-health`（`report_automation_health.py` の更新/判定を省略）
  - `--skip-gate-alarm`（`check_strategy_gate_alarm.py` の更新/判定を省略）
  - `--strategy-id`（gate判定対象。既定 `weather_clob_arb_buckets_observe`）
  - `--min-realized-days`（最終判定日数。既定 `30`）
  - `--snapshot-json`（読み取る strategy register JSON。既定 `logs/strategy_register_latest.json`）
  - `--health-json`（読み取る automation health JSON。既定 `logs/automation_health_latest.json`）
  - `--gate-alarm-state-json`（gateアラーム状態JSON。既定 `logs/strategy_gate_alarm_state.json`）
  - `--gate-alarm-log-file`（gateアラーム追記ログ。既定 `logs/strategy_gate_alarm.log`）
  - `--discord-gate-alarm`（gate遷移検知時にDiscord通知）
  - `--fail-on-gate-not-ready`（`realized_30d_gate.decision != READY_FOR_JUDGMENT` で非0終了）
  - `--fail-on-stage-not-final`（`realized_30d_gate.decision_3stage != READY_FINAL` で非0終了）
  - `--fail-on-health-no-go`（`automation_health.decision != GO` で非0終了）
  - `--skip-process-scan`（refresh時の process scan を省略）

Morning status daily runner (observe-only):
- PowerShell runner (background by default):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_morning_status_daily.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_morning_status_daily.ps1 -NoBackground -SkipProcessScan`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_morning_status_daily.ps1 -NoBackground -FailOnStageNotFinal -FailOnHealthNoGo`
- Scheduled task installer:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_morning_status_daily_task.ps1 -NoBackground`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_morning_status_daily_task.ps1 -NoBackground -StartTime 08:05 -RunNow`
- Key flags:
  - runner: `-StrategyId`, `-MinRealizedDays`, `-SnapshotJson`, `-HealthJson`, `-GateAlarmStateJson`, `-GateAlarmLogFile`
  - runner: `-NoRefresh`, `-SkipHealth`, `-SkipGateAlarm`, `-SkipProcessScan`, `-DiscordGateAlarm`
  - runner: `-FailOnGateNotReady`, `-FailOnStageNotFinal`, `-FailOnHealthNoGo`, `-NoBackground`
  - runner は `scripts/check_morning_status.py` を実行し、結果を `logs/morning_status_daily_run.log` に追記する
  - installer: `-TaskName`, `-StartTime`, `-StrategyId`, `-MinRealizedDays`, `-NoRefresh`, `-SkipHealth`, `-SkipGateAlarm`, `-SkipProcessScan`, `-DiscordGateAlarm`, `-FailOnGateNotReady`, `-FailOnStageNotFinal`, `-FailOnHealthNoGo`, `-RunNow`
  - installer の `-RunNow` は Scheduled Task の即時起動ではなく、runner を `-NoBackground` で直接1回実行する（ヘッドレス環境での `LastTaskResult=0xC000013A` 汚染回避）。

Automation health report (observe-only):
- Validate scheduled task/runtime freshness:
  - `python scripts/report_automation_health.py`
  - `python scripts/report_automation_health.py --task WeatherTop30ReadinessDaily --task WeatherMimicPipelineDaily --artifact logs/strategy_register_latest.json:30 --artifact logs/strategy_realized_pnl_daily.jsonl:30 --pretty`
- Key flags:
  - `--task`（repeatable。監視対象Scheduled Task名）
  - `--artifact`（repeatable。`PATH[:MAX_AGE_HOURS]` 形式）
  - `--out-json`, `--out-txt`, `--pretty`
- Default artifact checks include:
  - `logs/strategy_register_latest.json`, `logs/clob_arb_realized_daily.jsonl`, `logs/strategy_realized_pnl_daily.jsonl`
  - `logs/weather_top30_readiness_report_latest.json`, `logs/weather_top30_readiness_daily_run.log`, `logs/weather_mimic_pipeline_daily_run.log`, `logs/no_longshot_daily_run.log`
- Soft-fail behavior:
  - `LastTaskResult=0xC000013A (3221225786)` でも、対応 runner log/artifact が fresh な場合は `SOFT_FAIL_INTERRUPTED` として `NO_GO` 判定から除外する。
  - 再登録直後などの no-run sentinel 時刻（例: `0001` / `1601` / `1999-11-30`）は `NO_RUN_YET` として扱い、失敗扱いしない。
  - `NoLongshotDailyReport` が `Disabled` でも、`configs/bot_supervisor.observe.json` で `no_longshot_daily_daemon.enabled=true` の場合は `SUPPRESSED_BY_SUPERVISOR` として `NO_GO` 判定から除外する。

Polymarket BTC 5m LMSR/Bayes monitor (observe-only):
- Observe:
  - `python scripts/polymarket_btc5m_lmsr_observe.py`
  - `python scripts/polymarket_btc5m_lmsr_observe.py --kelly-fraction 0.20 --bankroll-usd 500 --min-edge-cents 0.8`
- Key flags:
  - `--prior-prob`, `--prior-strength`, `--obs-strength` (Bayesian posterior tuning)
  - `--kelly-fraction`, `--bankroll-usd`, `--min-bet-usd`, `--max-bet-usd` (fractional Kelly sizing)
  - `--windows-back`, `--windows-forward`, `--min-seconds-to-end`, `--max-seconds-to-end` (5m universe window)
  - `--signal-cooldown-sec`, `--summary-every-sec`, `--metrics-file`, `--log-file` (runtime logging cadence/files)
- Observation report:
  - `python scripts/report_btc5m_lmsr_observation.py --hours 24`
  - `python scripts/report_btc5m_lmsr_observation.py --hours 24 --discord`

Polymarket BTC short-window lag monitor (observe-only):
- Observe:
  - `python scripts/polymarket_btc5m_lag_observe.py`
  - `python scripts/polymarket_btc5m_lag_observe.py --entry-edge-cents 1.5 --shares 25 --taker-fee-rate 0.002`
  - `python scripts/polymarket_btc5m_lag_observe.py --window-minutes 15 --entry-edge-cents 1.2 --shares 25`
  - `python scripts/polymarket_btc5m_lag_observe.py --window-minutes 5 --entry-price-min 0.03 --entry-price-max 0.07 --require-reversal --reversal-lookback-sec 30 --reversal-min-move-usd 8`
- Key flags:
  - `--window-minutes` (`5` or `15`; default `5`)
  - `--entry-edge-cents`, `--alert-edge-cents`, `--shares`, `--min-remaining-sec`, `--no-max-one-entry-per-window` (signal and paper-entry gating)
  - `--entry-price-min`, `--entry-price-max` (optional entry price band filter; useful for low-price panic/reversal simulation)
  - `--require-reversal`, `--reversal-lookback-sec`, `--reversal-min-move-usd` (optional 2-leg spot reversal filter)
  - `--vol-lookback-sec`, `--sigma-floor-per-sqrt-sec`, `--settle-epsilon-usd` (fair-probability / settlement model)
  - `--daily-loss-limit-usd`, `--max-consecutive-errors` (observe-side risk guardrails)
  - `--summary-every-sec`, `--metrics-sample-sec`, `--log-file`, `--state-file`, `--metrics-file` (runtime outputs)
  - default runtime files auto-separate by window: `logs/btc5m-*` or `logs/btc15m-*` when paths are not explicitly specified

Polymarket BTC short-window panic-fade monitor (observe-only):
- Observe:
  - `python scripts/polymarket_btc5m_panic_observe.py`
  - `python scripts/polymarket_btc5m_panic_observe.py --cheap-ask-max-cents 8 --expensive-ask-min-cents 92 --shares 25`
  - `python scripts/polymarket_btc5m_panic_observe.py --window-minutes 15 --cheap-ask-max-cents 10 --expensive-ask-min-cents 90`
- Key flags:
  - `--window-minutes` (`5` or `15`; default `5`)
  - `--cheap-ask-max-cents`, `--expensive-ask-min-cents`, `--shares` (extreme-price trigger + paper size)
  - `--min-remaining-sec`, `--max-remaining-sec`, `--no-max-one-entry-per-window` (entry time window + per-window gating)
  - `--settle-epsilon-usd`, `--taker-fee-rate` (settlement tolerance + cost assumption)
  - `--daily-loss-limit-usd`, `--max-consecutive-errors` (observe-side risk guardrails)
  - `--summary-every-sec`, `--metrics-sample-sec`, `--log-file`, `--state-file`, `--metrics-file` (runtime outputs)
  - default runtime files auto-separate by window: `logs/btc5m-*` or `logs/btc15m-*` when paths are not explicitly specified

Polymarket BTC short-window panic claim validator (observe-only):
- Validate historical frequency claims from closed BTC up/down windows:
  - `python scripts/report_btc5m_panic_claims.py`
  - `python scripts/report_btc5m_panic_claims.py --hours 72 --thresholds 0.05,0.10 --max-markets 864`
  - `python scripts/report_btc5m_panic_claims.py --window-minutes 15 --hours 168 --max-trades-per-market 3000`
- Key flags:
  - `--window-minutes` (`5` or `15`; default `5`)
  - `--hours`, `--since`, `--until`, `--max-markets` (scan horizon and market cap)
  - `--thresholds` (winner-side minimum traded-price thresholds; decimal probabilities, default `0.05,0.10,0.15`)
  - `--page-size`, `--max-trades-per-market`, `--sleep-sec` (Data API fetch controls)
  - `--out-json`, `--out-csv`, `--pretty` (report outputs; default under `logs/`)

Polymarket social profit-claim validator (observe-only):
- Validate social/media profit claims against realized daily PnL artifacts:
  - `python scripts/report_social_profit_claims.py`
  - `python scripts/report_social_profit_claims.py --input-glob "logs/*realized*daily*.jsonl" --min-days 30 --pretty`
  - `python scripts/report_social_profit_claims.py --hourly-usd 25 --active-hours-per-day 8 --out-json logs/social_profit_claims_custom.json`
- Key flags:
  - `--input-file` (repeatable JSONL input; default candidates under `logs/`)
  - `--input-glob` (additional JSONL glob pattern)
  - `--min-days` (minimum observed days for support/no-support judgment)
  - `--daily-range-min`, `--daily-range-max` (daily claim range, default `500..800`)
  - `--monthly-target-usd` (default `75000`)
  - `--growth-start-usd`, `--growth-end-usd`, `--growth-days` (default `20000 -> 215000` over `30` days)
  - `--hourly-usd`, `--active-hours-per-day` (hourly claim conversion; default `25` and `8`)
  - `--out-json`, `--out-md`, `--pretty`

Polymarket hourly up/down high-probability calibration (observe-only):
- Validate high-probability near-expiry pricing claims from closed hourly crypto up/down markets:
  - `python scripts/report_hourly_updown_highprob_calibration.py`
  - `python scripts/report_hourly_updown_highprob_calibration.py --assets bitcoin,ethereum --hours 72 --tte-minutes 20 --price-min 0.80 --price-max 0.95 --max-trades-per-market 3000 --pretty`
  - `python scripts/report_hourly_updown_highprob_calibration.py --assets bitcoin --hours 168 --entry-max-age-minutes 60 --out-json logs/hourly_updown_highprob_btc.json`
- Key flags:
  - `--assets` (comma-separated asset slug prefixes; default `bitcoin,ethereum`)
  - `--hours`, `--since`, `--until`, `--max-markets` (scan horizon and cap)
  - `--tte-minutes` (entry cutoff as minutes-to-expiry; default `20`)
  - `--entry-max-age-minutes` (max trade staleness before cutoff; default `45`)
  - `--price-min`, `--price-max` (high-probability entry band; default `0.80..0.95`)
  - `--page-size`, `--max-trades-per-market`, `--sleep-sec` (Data API fetch controls)
  - `--out-json`, `--out-csv`, `--pretty`

Polymarket CLOB fade monitor (observe-only, multi-bot consensus simulation):
- Observe:
  - `python scripts/polymarket_clob_fade_observe.py`
  - `python scripts/polymarket_clob_fade_observe.py --max-tokens 15 --poll-sec 2 --summary-every-sec 60`
- Key flags:
  - `--gamma-pages`, `--gamma-page-size`, `--include-regex`, `--exclude-regex`, `--min-days-to-end`, `--max-days-to-end` (監視ユニバースの精度向上)
  - `--consensus-min-score`, `--consensus-min-agree` (multi-bot合意しきい値)
  - `--consensus-min-score-agree1`, `--consensus-min-score-agree2` (合意数別の追加スコア閾値)
  - `--allowed-sides` (`both`/`long`/`short` で方向制御)
  - `--min-non-extreme-agree` (extreme単独シグナル除外)
  - `--take-profit-cents`, `--stop-loss-cents`, `--max-hold-sec` (疑似ポジション出口条件)
  - `--trail-arm-cents`, `--trail-drop-cents`, `--breakeven-arm-cents` (含み益ロック/建値保護の出口条件)
  - `--execution-mode`, `--maker-spread-capture` (約定コストモデル)
  - `--tp-cost-mult`, `--sl-cost-mult` (往復コスト連動TP/SL)
  - `--expected-move-cost-ratio`, `--min-expected-edge-cents` (期待値フィルタ)
  - `--min-volatility-cents`, `--max-volatility-cents` (低変動・高変動の除外)
  - `--token-loss-cut-usd`, `--token-loss-min-trades`, `--token-min-winrate`, `--token-disable-sec` (負け銘柄の自動停止)
  - `--token-churn-streak`, `--token-churn-disable-sec`, `--token-churn-max-pnl-usd` (`timeout/trail/breakeven` 連続時の銘柄停止)
  - `--side-loss-cut-usd`, `--side-loss-min-trades`, `--side-min-winrate`, `--side-disable-sec` (LONG/SHORT 方向の自動停止)
  - `--control-file`, `--control-reload-sec` (JSONで `allowed_sides` などを実行中にhot reload)
  - `--daily-loss-limit-usd`, `--max-open-positions`, `--cooldown-sec`, `--loss-cooldown-mult` (リスク管理)
- Observation report:
  - `python scripts/report_clob_fade_observation.py --hours 24`
  - `python scripts/report_clob_fade_observation.py --hours 24 --discord`
- Parameter optimization (metrics replay):
  - `python scripts/optimize_clob_fade_params.py --hours 6`
  - `python scripts/optimize_clob_fade_params.py --hours 72 --metrics-glob "logs/clob-fade-observe-profit*-metrics.jsonl" --top-n 8`
- Entry-filter optimization (event logs):
  - `python scripts/optimize_clob_fade_entry_filters.py --hours 72`
  - `python scripts/optimize_clob_fade_entry_filters.py --hours 72 --strict-min-trades --min-trades 20`
- Realtime dashboard (local web):
  - `python scripts/fade_monitor_dashboard.py`
  - `python scripts/fade_monitor_dashboard.py --host 127.0.0.1 --port 8787 --window-minutes 60 --max-tokens 12`
  - `python scripts/fade_monitor_dashboard.py --primary-label LONG --metrics-file logs/clob-fade-observe-profit-long-canary-metrics.jsonl --state-file logs/clob_fade_observe_profit_long_canary_state.json --log-file logs/clob-fade-observe-profit-long-canary.log --secondary-label SHORT --secondary-metrics-file logs/clob-fade-observe-profit-short-canary-metrics.jsonl --secondary-state-file logs/clob_fade_observe_profit_short_canary_state.json --secondary-log-file logs/clob-fade-observe-profit-short-canary.log`
  - `--secondary-metrics-file` を指定すると dual-view モードになり、LONG/SHORT を同時表示できる（詳細パネルは `detail` セレクタで切替）。
  - key flags: `--primary-label`, `--secondary-label`, `--secondary-metrics-file`, `--secondary-state-file`, `--secondary-log-file`, `--detail-default`
- Side router (canary成績で `allowed_sides` を自動切替):
  - `python scripts/fade_side_router.py --once`
  - `python scripts/fade_side_router.py --poll-sec 30 --hold-side-sec 900 --decision-metric per_exit --switch-margin-usd 0.0006 --both-keep-margin-usd 0.0009 --out-control logs/clob_fade_runtime_control.json`
  - `--decision-metric` (`per_exit`/`cumulative`/`total_per_exit`/`total_cumulative`/`day_per_exit`/`day_cumulative`) で比較軸を選択（既定は `per_exit`）
  - `day_*` は日次アンカー差分（`state.day_anchor_total_pnl` 基準）を比較するため、短期の地合い変化に追随しやすい。
  - メインbot側で `--control-file logs/clob_fade_runtime_control.json` を指定して連携
  - `out-control` 既存JSONの `overrides` は保持され、ルーターは `allowed_sides` のみ上書きする。`consensus_min_score` や `min_expected_edge_cents` を同居させて併用できる。

Polymarket NO-longshot toolkit (observe-only):
- Active screener:
  - `python scripts/polymarket_no_longshot_observe.py screen`
  - `python scripts/polymarket_no_longshot_observe.py screen --yes-min 0.005 --yes-max 0.02 --min-days-to-end 14 --top-n 30`
  - `python scripts/polymarket_no_longshot_observe.py screen --yes-min 0.005 --yes-max 0.02 --min-days-to-end 0 --max-hours-to-end 6 --per-trade-cost 0.002 --sort-by net_yield_per_day_desc --top-n 30`
- Logical-gap scanner (numeric subset/disjoint inconsistencies, observe-only):
  - `python scripts/polymarket_no_longshot_observe.py gap`
  - `python scripts/polymarket_no_longshot_observe.py gap --min-days-to-end 0 --max-hours-to-end 6 --min-gross-edge-cents 0.3 --per-leg-cost 0.002 --relation both --top-n 30`
- Walk-forward validation (closed markets):
  - `python scripts/polymarket_no_longshot_observe.py walkforward`
  - `python scripts/polymarket_no_longshot_observe.py walkforward --sampling-mode stratified --offset-step 5000 --max-offset 425000 --yes-min 0.005 --yes-max 0.02 --per-trade-cost 0.002`
  - `python scripts/polymarket_no_longshot_observe.py walkforward --input-csv logs/no_longshot_walkforward_samples.csv --out-summary-json logs/no_longshot_walkforward_summary.json`
- Forward realized tracker (observe-only paper ledger):
  - `python scripts/record_no_longshot_realized_daily.py`
  - `python scripts/record_no_longshot_realized_daily.py --screen-csv logs/no_longshot_daily_screen.csv --entry-top-n 10 --per-trade-cost 0.002 --pretty`
- Key flags:
  - `screen`: `--yes-min`, `--yes-max`, `--min-days-to-end`, `--max-days-to-end`, `--min-hours-to-end`, `--max-hours-to-end`, `--min-liquidity`, `--min-volume-24h`, `--per-trade-cost`, `--min-net-yield-per-day`, `--sort-by`, `--exclude-keywords`, `--include-regex`, `--exclude-regex`
  - `gap`: `--relation`, `--yes-min`, `--yes-max`, `--min-days-to-end`, `--max-days-to-end`, `--min-hours-to-end`, `--max-hours-to-end`, `--max-end-diff-hours`, `--require-same-signature`, `--min-liquidity`, `--min-volume-24h`, `--min-gross-edge-cents`, `--min-net-edge-cents`, `--per-leg-cost`, `--max-pairs-per-event`, `--exclude-keywords`, `--include-regex`, `--exclude-regex`
  - `walkforward`: `--sampling-mode`, `--date-min`, `--date-max`, `--min-duration-days`, `--min-liquidity`, `--min-volume-24h`, `--min-history-points`, `--max-stale-hours`, `--hours-before-end`, `--lookback-hours`, `--yes-min-grid`, `--yes-max-grid`, `--min-train-n`, `--min-test-n`, `--period-frequency`, `--per-trade-cost`, `--max-open-positions`, `--max-open-per-category`
  - `record_no_longshot_realized_daily.py`: `--screen-csv`, `--positions-json`, `--out-daily-jsonl`, `--out-latest-json`, `--out-monthly-txt`, `--entry-top-n`, `--per-trade-cost`, `--win-threshold`, `--lose-threshold`
  - Default `--yes-max-grid` is conservative (`0.01,0.015,0.02`) to reduce high-tail overfitting.
- Daily runner (PowerShell):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -SkipRefresh`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -YesMin 0.4 -YesMax 0.6 -MinHistoryPoints 71 -MaxStaleHours 1.0 -MaxOpenPositions 20 -MaxOpenPerCategory 4 -ScreenMinLiquidity 50000 -ScreenMinVolume24h 1000`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -GapMaxHoursToEnd 6 -GapMinGrossEdgeCents 0.3 -GapPerLegCost 0.002 -GapRelation both`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -GapMaxDaysToEnd 180 -GapFallbackMaxDaysToEnd 180 -GapMaxHoursToEnd 6 -GapFallbackMaxHoursToEnd 48 -GapMinLiquidity 1000 -GapMinVolume24h 0 -GapMinGrossEdgeCents 0.3 -GapPerLegCost 0.002`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -GapSummaryMinNetEdgeCents 0.5`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -GapSummaryMode auto -GapSummaryTargetUniqueEvents 3 -GapSummaryThresholdGrid "0.5,1.0,2.0"`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -GapSummaryMode fixed -GapSummaryMinNetEdgeCents 1.0`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -ScreenMaxPages 12 -GapMaxPages 12 -GapFallbackMaxPages 30`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -GapMaxHoursToEnd 6 -GapFallbackMaxHoursToEnd 48 -GapMinGrossEdgeCents 0.3 -GapPerLegCost 0.002`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -GapFallbackNoHourCap`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -Discord`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -GuardMaxOpenPositions 16 -GuardMaxOpenPerCategory 3`
  - `NO_LONGSHOT_DAILY_DISCORD=1` でも通知有効（Webhookは `CLOBBOT_DISCORD_WEBHOOK_URL` または `DISCORD_WEBHOOK_URL`）
  - 日次実行時に `record_no_longshot_realized_daily.py` を呼び、`rolling_30d_monthly_return`（実測）を summary に追記
  - 実測エントリー入力は `logs/no_longshot_daily_screen.csv` を基本にしつつ、`logs/no_longshot_fast_screen_lowyes_latest.csv`（YES 0.01-0.20 / 残り72h以内）に候補があれば自動で fast 側を優先
  - Summary に `monthly_return_now` を追記（実測が未確定の間は Guarded OOS の `annualized_return` を月次換算したフォールバック値を表示）
  - 実測アーティファクト: `logs/no_longshot_forward_positions.json`, `logs/no_longshot_realized_daily.jsonl`, `logs/no_longshot_realized_latest.json`, `logs/no_longshot_monthly_return_latest.txt`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_no_longshot_daily_task.ps1 -StartTime 00:05 -RunNow`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_no_longshot_daily_task.ps1 -StartTime 00:05 -Discord`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_no_longshot_daily_task.ps1 -NoBackground -?`
  - installer key flags: `-TaskName`, `-RepoRoot`, `-StartTime`, `-SkipRefresh`, `-Discord`, `-RunNow`, `-PowerShellExe`, `-NoBackground`
  - `install_no_longshot_daily_task.ps1` は登録タスクに `-NoBackground` を付与して起動（子プロセス多重化を回避）
  - installer は task principal を `S4U` -> `Interactive` -> default の順で登録を試行。
  - installer 実行後は `Enable-ScheduledTask` を呼び、無効化状態を自動解除。
  - `-RunNow` は Scheduled Task の即時起動ではなく、runner を `-NoBackground` で直接1回実行する（ヘッドレス環境での `LastTaskResult=0xC000013A` 汚染回避）。
  - All/Guarded の既定値は `-AllMinTrainN 20 -AllMinTestN 1 -GuardMinTrainN 20 -GuardMinTestN 20`（`span<90d` の過熱表示を抑えるため）
  - Guarded 側の同時保有制約既定値は `-GuardMaxOpenPositions 16 -GuardMaxOpenPerCategory 3`（Allfolds と役割分離）
  - Summary の `ann=` は `span<90d` のとき `[LOW_CONF ...]` 警告を付与。
  - gap満期フィルタ既定値は `-GapMaxDaysToEnd 180 -GapFallbackMaxDaysToEnd 180`（超長期市場ノイズ抑制）。
  - gap流動性フィルタ既定値は `-GapMinLiquidity 1000 -GapMinVolume24h 0`（薄板ノイズ抑制）。
  - Summary表示フィルタの基準値は `-GapSummaryMinNetEdgeCents 0.5`。
  - Summary表示の `net` しきい値は既定で自動調整（`-GapSummaryMode auto`）し、`-GapSummaryTargetUniqueEvents`（既定3）を満たす最大しきい値を `-GapSummaryThresholdGrid`（既定 `0.5,1.0,2.0`）から選択。
  - 自動調整を固定値にしたい場合は `-GapSummaryMode fixed` を指定。
  - gap候補が0件なら `GapFallbackMaxHoursToEnd` と `GapFallbackMaxPages` に自動拡張して再スキャン。
  - それでも `interval_markets=0` の場合は、`max-hours-to-end` 制約なしの再スキャンを自動実行（`gap scan stage: fallback_no_hour_cap_auto`）。
  - `-GapFallbackNoHourCap` を付けると、`interval_markets` の値に関係なく、候補0件時に `max-hours-to-end` 制約なし再スキャンを強制追加。
  - Summaryの `Logical gaps now` は `raw` / `filtered` / `unique_events` を併記し、表示上位はイベント単位で最良1件に圧縮。
  - Summaryの `Logical gaps threshold stats` で `net>=0.50c/1.00c/2.00c` ごとの件数を併記し、しきい値調整の判断材料を可視化。
  - gap artifacts: `logs/no_longshot_daily_gap.csv`, `logs/no_longshot_daily_gap.json`
- Daily daemon (supervisor-managed):
  - `python scripts/no_longshot_daily_daemon.py --run-at-hhmm 00:05 --skip-refresh`
  - `python scripts/no_longshot_daily_daemon.py --run-at-hhmm 00:05 --poll-sec 15 --retry-delay-sec 900 --max-run-seconds 1800 --run-on-start`
  - `python scripts/no_longshot_daily_daemon.py --run-at-hhmm 00:05 --realized-refresh-sec 900 --realized-entry-top-n 0 --skip-refresh`
  - key flags: `--run-at-hhmm`, `--poll-sec`, `--retry-delay-sec`, `--max-run-seconds`, `--max-consecutive-failures`, `--run-on-start`, `--skip-refresh/--no-skip-refresh`, `--discord`, `--log-file`, `--state-file`
  - realized refresh key flags: `--python-exe`, `--realized-refresh-sec`, `--realized-timeout-sec`, `--realized-tool-path`, `--realized-screen-csv`, `--realized-positions-json`, `--realized-out-daily-jsonl`, `--realized-out-latest-json`, `--realized-out-monthly-txt`, `--realized-entry-top-n`, `--realized-per-trade-cost`, `--realized-api-timeout-sec`
  - `--realized-refresh-sec > 0` で daemon が `record_no_longshot_realized_daily.py` を定期実行し、`--realized-entry-top-n 0` なら resolve-only（新規エントリー追加なし）で rolling-30d 実測を同日更新できる
  - daemon は内部で `run_no_longshot_daily_report.ps1 -NoBackground` を呼び出し、observe-only 日次運用を supervisor 配下で継続する

Polymarket event-driven mispricing monitor (observe-only):
- Observe:
  - `python scripts/polymarket_event_driven_observe.py`
  - `python scripts/polymarket_event_driven_observe.py --min-edge-cents 0.8 --min-liquidity 20000 --top-n 20`
  - `python scripts/polymarket_event_driven_observe.py --poll-sec 300 --include-regex "acquire|approve|court|lawsuit"`
- Key flags:
  - Universe/filter: `--max-pages`, `--page-size`, `--include-regex`, `--exclude-regex`, `--include-non-event`
  - Horizon/liquidity guardrails: `--min-days-to-end`, `--max-days-to-end`, `--min-liquidity`, `--min-volume-24h`
  - Event-pricing model: `--prior-strength`, `--obs-strength`, `--directional-bias-scale`, `--fragility-obs-discount`, `--ambiguity-obs-discount`
  - Signal quality: `--min-edge-cents`, `--min-leg-price`, `--max-leg-price`, `--min-confidence`, `--top-n`
  - Paper sizing: `--kelly-fraction`, `--max-kelly-fraction`, `--bankroll-usd`, `--min-bet-usd`, `--max-bet-usd`
  - Runtime outputs: `--log-file`, `--signals-file`, `--metrics-file` (default under `logs/`)
- Observation report:
  - `python scripts/report_event_driven_observation.py --hours 24`
  - `python scripts/report_event_driven_observation.py --hours 24 --discord`
- Daily runner (PowerShell, background by default):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_event_driven_daily_report.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_event_driven_daily_report.ps1 -NoBackground -MaxPages 12 -MinEdgeCents 0.8 -TopN 20`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_event_driven_daily_report.ps1 -NoBackground -IncludeRegex "acquire|approve|court|lawsuit" -Discord`
- Daily task installer (PowerShell):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_event_driven_daily_task.ps1 -NoBackground`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_event_driven_daily_task.ps1 -NoBackground -StartTime 00:15 -RunNow`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_event_driven_daily_task.ps1 -NoBackground -PrincipalMode default`
- Key flags (runner / installer):
  - runner: `-MaxPages`, `-PageSize`, `-MinLiquidity`, `-MinVolume24h`, `-MinEdgeCents`, `-TopN`, `-ReportHours`
  - runner: `-IncludeRegex`, `-ExcludeRegex`, `-IncludeNonEvent`, `-ThresholdsCents`, `-Discord`
  - installer: `-TaskName`, `-StartTime`, `-MaxPages`, `-MinLiquidity`, `-MinVolume24h`, `-MinEdgeCents`, `-TopN`, `-ReportHours`, `-PrincipalMode`, `-ActionMode`, `-RunNow`
  - installer principal mode: `auto` / `default` / `s4u` / `interactive`（`s4u` は環境により権限不足で失敗する場合あり）
  - installer action mode 既定値は `powershell`（`cmd` wrapper 経由にも切替可）
  - installer の `-RunNow` は Scheduled Task の即時起動ではなく、runner を `-NoBackground` で直接1回実行する（ヘッドレス環境での `LastTaskResult=0xC000013A` 汚染回避）。
  - `EVENT_DRIVEN_DAILY_DISCORD=1` でも Discord 投稿を有効化可能（Webhook は `CLOBBOT_DISCORD_WEBHOOK_URL` / `DISCORD_WEBHOOK_URL`）
  - artifacts: `logs/event_driven_daily_summary.txt`, `logs/event_driven_daily_run.log`

Polymarket late-resolution high-probability validator (observe-only):
- Active screener:
  - `python scripts/polymarket_lateprob_observe.py screen`
  - `python scripts/polymarket_lateprob_observe.py screen --max-hours-to-end 0.25 --side-mode yes-only --yes-high-min 0.92 --yes-high-max 0.99 --top-n 20`
  - `python scripts/polymarket_lateprob_observe.py screen --max-hours-to-end 0.5 --max-active-stale-hours 6`
  - `python scripts/polymarket_lateprob_observe.py screen --include-regex ""`（weather既定フィルタを無効化）
- Closed-market backtest:
  - `python scripts/polymarket_lateprob_observe.py backtest`
  - `python scripts/polymarket_lateprob_observe.py backtest --hours-before-end 0.25 --side-mode both --yes-high-min 0.9 --yes-high-max 0.99 --yes-low-min 0.01 --yes-low-max 0.1 --per-trade-cost 0.002`
- Key flags:
  - `screen`: `--min-hours-to-end`, `--max-hours-to-end`, `--max-active-stale-hours`（`active=true` でも `endDate` が古すぎる市場を除外。`-1`で無効化）, `--include-regex`, `--exclude-regex`, `--side-mode`, `--yes-high-min`, `--yes-high-max`, `--yes-low-min`, `--yes-low-max`, `--min-liquidity`, `--min-volume-24h`, `--per-trade-cost`
  - `screen` の `--include-regex` 既定値は weather core（`weather|temperature|precipitation|forecast|\brain\b|\bsnow\b|\bwind\b|humidity`）
  - `backtest`: `--sampling-mode`, `--date-min`, `--date-max`, `--hours-before-end`, `--lookback-hours`, `--max-stale-hours`, `--history-fidelity`, `--side-mode`, `--yes-high-min`, `--yes-high-max`, `--yes-low-min`, `--yes-low-max`, `--per-trade-cost`, `--max-open-positions`, `--max-open-per-category`
  - `backtest` summary JSON には timing quality（`timing_quality`, `timing_quality_by_side`, `by_quarter.*.timing_quality`）を含み、`hours-before-end` に対する実エントリー時刻の乖離を確認可能
  - Artifacts are written under `logs/` by default (`lateprob_backtest_samples_*.csv`, `lateprob_backtest_summary_*.json`).

Simmer ($SIM) ping-pong demo:
- Observe:
  - `python scripts/simmer_pingpong_mm.py`
- Live (demo trades on `venue=simmer`):
  - `python scripts/simmer_pingpong_mm.py --execute --confirm-live YES`
- Key flags:
  - `--paper-trades` (observe-onlyでも擬似約定で在庫回転を評価)
  - `--paper-seed-every-sec` (observe-onlyで一定間隔ごとに擬似エントリーを作る)
  - `--asset-quotas` (auto universe minimum mix, e.g. `bitcoin:2,ethereum:1,solana:1`)
  - `--max-hold-sec`, `--sell-target-decay-cents-per-min` (inventory回転・出口制御)
  - `--prob-min`, `--prob-max`, `--min-time-to-resolve-min` (極端確率/満期近接の除外)
  - Safety: `SIMMER_PONG_EXECUTE=1` だけではLIVE化されない。LIVEは必ず CLI の `--execute --confirm-live YES` が必要。
- Observation report:
  - `python scripts/report_simmer_observation.py --hours 24`
  - `python scripts/report_simmer_observation.py --hours 24 --discord`
- Parameter optimization (metrics replay, observe-only):
  - `python scripts/optimize_simmer_pingpong_params.py --hours 24`
  - `python scripts/optimize_simmer_pingpong_params.py --hours 72 --metrics-glob "logs/simmer-ab-*-metrics.jsonl" --top-n 20`
  - `python scripts/optimize_simmer_pingpong_params.py --hours 72 --risk-modes static,inverse_vol --target-volatilities 0.002,0.003 --vol-lookback-samples 30,90`
  - `python scripts/optimize_simmer_pingpong_params.py --hours 72 --search-mode hybrid --random-candidates 1200 --max-candidates 1500 --walkforward-splits 6 --rank-by robust --top-n 20`
- Key optimization flags:
  - `--spreads-cents`, `--quote-refresh-secs`, `--trade-shares`, `--max-inventory-shares`
  - `--max-hold-secs`, `--sell-decay-cpm` (inventory rotation / time-based exits)
  - `--risk-modes` (`static` / `inverse_vol`) and `--target-volatilities` (variable risk scaling)
  - `--search-mode` (`grid`/`random`/`hybrid`), `--random-candidates`, `--max-candidates`
  - `--walkforward-splits`, `--rank-by robust`, `--wf-std-penalty` (時系列ロバスト性評価)
  - `--sample-step`, `--max-samples` (長時間データ探索の高速化ダウンサンプリング)
  - `--entry-prob-min`, `--entry-prob-max`, `--seed-interval-sec`, `--per-share-fee`, `--slippage-cents` (実行コスト近似/低シグナル補助)
  - `--min-closed-cycles`, `--min-win-rate`, `--max-drawdown`, `--min-total-pnl` (候補フィルタ)
  - `--dd-penalty`, `--sharpe-weight`, `--expectancy-weight` (ranking score weights)
  - `--out-json`, `--out-commands` (summary / top candidate commands; defaultは`logs/`推奨)
- A/B compare: `python scripts/compare_simmer_ab_daily.py --hours 24`
  - `Decision: INSUFFICIENT` は、A/Bどちらかで `buy=0` / `sell=0` / `closed_cycles=0` がある状態（判定保留）。
- A/B trend: `python scripts/report_simmer_ab_trend.py --days 30 --last 14`
- A/B daily helper: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_simmer_ab_daily_report.ps1`
- Set `SIMMER_AB_DAILY_COMPARE_DISCORD=1` to enable Discord post on daily compare (requires webhook env).
- Status:
  - `python scripts/simmer_status.py`

bitFlyer BTC/JPY MM simulator (observe-only):
- Observe:
  - `python scripts/bitflyer_mm_observe.py`
- Tuning example:
  - `python scripts/bitflyer_mm_observe.py --quote-half-spread-yen 150 --quote-refresh-sec 60 --order-size-btc 0.001 --max-inventory-btc 0.01 --run-seconds 3600`
  - `python scripts/bitflyer_mm_observe.py --quote-half-spread-yen 1200 --quote-refresh-sec 10 --max-inventory-btc 0.005 --unwind-half-spread-yen 300`
- Observation report:
  - `python scripts/report_bitflyer_mm_observation.py --hours 24`
  - `python scripts/report_bitflyer_mm_observation.py --hours 24 --discord`
- Parameter optimization (metrics replay):
  - `python scripts/optimize_bitflyer_mm_params.py --hours 24`
  - `python scripts/optimize_bitflyer_mm_params.py --hours 24 --half-spreads-yen 80,120,150,250 --quote-refresh-secs 10,30,60,120 --order-sizes-btc 0.0005,0.001 --top-n 8`

## Secrets (Environment)

- `SIMMER_API_KEY` (required for Simmer SDK)
- `PM_PRIVATE_KEY_DPAPI_FILE` + `PM_FUNDER` (required for Polymarket CLOB)
- `CLOBBOT_DISCORD_WEBHOOK_URL` (optional, secret)
