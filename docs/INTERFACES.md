# Interfaces

## CLI Entrypoints

PowerShell task scripts (background by default):
- Default behavior:
  - Launches a detached background PowerShell process and returns immediately.
  - Use `-NoBackground` to run in the current console for debugging.
- Common control flags:
  - `-NoBackground` (run foreground)
  - `-Background` (internal child-mode flag; usually not passed manually)
- Entrypoints:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_clob_arb_monitor.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_bot_supervisor.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_simmer_ab_daily_report.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_clob_arb_monitor_task.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_no_longshot_daily_task.ps1`
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
  - `python scripts/polymarket_clob_arb_realtime.py --universe gamma-active --strategy event-pair --gamma-limit 300 --max-subscribe-tokens 40`
  - `python scripts/polymarket_clob_arb_realtime.py --universe gamma-active --strategy yes-no --sports-live-only --sports-live-prestart-min 10 --sports-live-postend-min 30`
  - `python scripts/polymarket_clob_arb_realtime.py --universe gamma-active --strategy yes-no --sports-live-only --notify-observe-signals`
- Key flags:
  - `--universe` (`weather` / `gamma-active` / `btc-5m`)
  - `--strategy` (`buckets` / `yes-no` / `event-pair` / `both` / `all`)
  - `--sports-live-only`, `--sports-live-prestart-min`, `--sports-live-postend-min`（`gamma-active + yes-no` でスポーツ市場の試合中/前後のみを監視）
  - `--notify-observe-signals`, `--observe-notify-min-interval-sec`（observe-only でも閾値シグナル検知時に Discord 通知。間隔で連投抑制）
  - `event-pair` は binary negRisk イベントの `YES+YES` と `NO+NO` ペアを監視（observe-only 既定）
- Observation report:
  - `python scripts/report_clob_observation.py --hours 24`
  - `python scripts/report_clob_observation.py --hours 24 --discord`

Polymarket wallet autopsy toolkit (observe-only):
- Fetch user trades (Data API):
  - `python scripts/fetch_trades.py 0xWallet`
  - `python scripts/fetch_trades.py 'c0O0OLI0O0O0'`
  - `python scripts/fetch_trades.py '@c0O0OLI0O0O0'`
  - `python scripts/fetch_trades.py 'https://polymarket.com/@c0O0OLI0O0O0'`
  - `python scripts/fetch_trades.py 0xfedc381bf3fb5d20433bb4a0216b15dbbc5c6398` (profile URL unstable時のwallet fallback)
  - `python scripts/fetch_trades.py 0xWallet --market 0xConditionId --max-trades 2000`
  - `python scripts/fetch_trades.py 0xWallet --market 0xConditionId --out my_trades.json --pretty`
- Analyze saved trades JSON:
  - `python scripts/analyze_trades.py logs/my_trades.json`
  - `python scripts/analyze_trades.py logs/my_trades.json --out logs/my_trades_summary.json --pretty`
- Resolve market by name and autopsy one wallet:
  - `python scripts/analyze_user.py "Bitcoin Up or Down February 6" 0xWallet`
  - `python scripts/analyze_user.py "Bitcoin Up or Down February 6" 0xWallet my_analysis.json --pretty`
- Scan top holders in a market and autopsy each:
  - `python scripts/analyze_user.py "Bitcoin Up or Down February 6" --top-holders 10`
  - `python scripts/analyze_user.py "Bitcoin Up or Down February 6" --top-holders 10 --holders-limit 30 --max-trades 1500`
- Key flags:
  - `fetch_trades.py`: `--market`, `--limit`, `--max-trades`, `--sleep-sec`, `--out`, `--pretty`
    - profile URL の `@0x...-<suffix>` 形式（例: `https://polymarket.com/@0xabc...-1762688003124`）も受け付け、先頭の wallet (`0x...`) を自動解決
    - live profile 解決が失敗した場合は `docs/knowledge/link-intake/profile_wallet_map.json` を fallback として参照
  - `analyze_trades.py`: `--wallet`, `--market-title`, `--out`, `--pretty`
  - `analyze_user.py`: `--top-holders`, `--holders-limit`, `--page-size`, `--max-trades`, `--pretty`
- Cross-market candidate report from autopsy logs:
  - `python scripts/report_wallet_autopsy_candidates.py`
  - `python scripts/report_wallet_autopsy_candidates.py --latest-per-market --min-trades 10 --min-profitable-pct 70 --out wallet_autopsy_candidates.json`
  - `python scripts/report_wallet_autopsy_candidates.py --statuses ARBITRAGE_CANDIDATE --top 30`
  - `python scripts/report_wallet_autopsy_candidates.py --glob "autopsy_*_top20_*.json" --statuses ARBITRAGE_CANDIDATE,PARTIAL_HEDGE --min-trades 5`

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
- Key flags:
  - `cohort_json` (`scripts/analyze_trader_cohort.py` の出力JSON)
  - `--profile-name` (出力ログ名/ジョブ名の接頭辞)
  - `--out-json`, `--out-supervisor-config`, `--pretty`
  - `--scan-max-pages`, `--scan-page-size`, `--scan-interval-sec` (生成されるobserveジョブの周期/負荷設定)
  - `--min-liquidity`, `--min-volume-24h`, `--top-n` (生成されるフィルタ基準)

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
- Key flags:
  - `--window-minutes` (`5` or `15`; default `5`)
  - `--entry-edge-cents`, `--alert-edge-cents`, `--shares`, `--min-remaining-sec`, `--no-max-one-entry-per-window` (signal and paper-entry gating)
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

Polymarket CLOB fade monitor (observe-only, multi-bot consensus simulation):
- Observe:
  - `python scripts/polymarket_clob_fade_observe.py`
  - `python scripts/polymarket_clob_fade_observe.py --max-tokens 15 --poll-sec 2 --summary-every-sec 60`
- Key flags:
  - `--gamma-pages`, `--gamma-page-size`, `--include-regex`, `--exclude-regex` (監視ユニバースの精度向上)
  - `--consensus-min-score`, `--consensus-min-agree` (multi-bot合意しきい値)
  - `--consensus-min-score-agree1`, `--consensus-min-score-agree2` (合意数別の追加スコア閾値)
  - `--allowed-sides` (`both`/`long`/`short` で方向制御)
  - `--min-non-extreme-agree` (extreme単独シグナル除外)
  - `--take-profit-cents`, `--stop-loss-cents`, `--max-hold-sec` (疑似ポジション出口条件)
  - `--execution-mode`, `--maker-spread-capture` (約定コストモデル)
  - `--tp-cost-mult`, `--sl-cost-mult` (往復コスト連動TP/SL)
  - `--expected-move-cost-ratio`, `--min-expected-edge-cents` (期待値フィルタ)
  - `--min-volatility-cents`, `--max-volatility-cents` (低変動・高変動の除外)
  - `--token-loss-cut-usd`, `--token-loss-min-trades`, `--token-min-winrate`, `--token-disable-sec` (負け銘柄の自動停止)
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
  - `--decision-metric` (`per_exit`/`cumulative`) で比較軸を選択（既定は `per_exit`）
  - メインbot側で `--control-file logs/clob_fade_runtime_control.json` を指定して連携

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
- Key flags:
  - `screen`: `--yes-min`, `--yes-max`, `--min-days-to-end`, `--max-days-to-end`, `--min-hours-to-end`, `--max-hours-to-end`, `--min-liquidity`, `--min-volume-24h`, `--per-trade-cost`, `--min-net-yield-per-day`, `--sort-by`, `--exclude-keywords`, `--include-regex`, `--exclude-regex`
  - `gap`: `--relation`, `--yes-min`, `--yes-max`, `--min-days-to-end`, `--max-days-to-end`, `--min-hours-to-end`, `--max-hours-to-end`, `--max-end-diff-hours`, `--require-same-signature`, `--min-liquidity`, `--min-volume-24h`, `--min-gross-edge-cents`, `--min-net-edge-cents`, `--per-leg-cost`, `--max-pairs-per-event`, `--exclude-keywords`, `--include-regex`, `--exclude-regex`
  - `walkforward`: `--sampling-mode`, `--date-min`, `--date-max`, `--min-duration-days`, `--min-liquidity`, `--min-volume-24h`, `--min-history-points`, `--max-stale-hours`, `--hours-before-end`, `--lookback-hours`, `--yes-min-grid`, `--yes-max-grid`, `--min-train-n`, `--min-test-n`, `--period-frequency`, `--per-trade-cost`, `--max-open-positions`, `--max-open-per-category`
  - Default `--yes-max-grid` is conservative (`0.01,0.015,0.02`) to reduce high-tail overfitting.
- Daily runner (PowerShell):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -SkipRefresh`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -YesMin 0.4 -YesMax 0.6 -MinHistoryPoints 71 -MaxStaleHours 1.0 -MaxOpenPositions 20 -MaxOpenPerCategory 4 -ScreenMinLiquidity 50000 -ScreenMinVolume24h 1000`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -GapMaxHoursToEnd 6 -GapMinGrossEdgeCents 0.3 -GapPerLegCost 0.002 -GapRelation both`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -ScreenMaxPages 12 -GapMaxPages 12 -GapFallbackMaxPages 30`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -GapMaxHoursToEnd 6 -GapFallbackMaxHoursToEnd 48 -GapMinGrossEdgeCents 0.3 -GapPerLegCost 0.002`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -GapFallbackNoHourCap`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -Discord`
  - `NO_LONGSHOT_DAILY_DISCORD=1` でも通知有効（Webhookは `CLOBBOT_DISCORD_WEBHOOK_URL` または `DISCORD_WEBHOOK_URL`）
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_no_longshot_daily_task.ps1 -StartTime 00:05 -RunNow`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_no_longshot_daily_task.ps1 -StartTime 00:05 -Discord`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_no_longshot_daily_task.ps1 -NoBackground -?`
  - `install_no_longshot_daily_task.ps1` は登録タスクに `-NoBackground` を付与して起動（子プロセス多重化を回避）
  - Guarded 既定値は `-GuardMinTrainN 60 -GuardMinTestN 20`（`n=0` 回避のため）
  - Summary の `ann=` は `span<90d` のとき `[LOW_CONF ...]` 警告を付与。
  - gap候補が0件なら `GapFallbackMaxHoursToEnd` と `GapFallbackMaxPages` に自動拡張して再スキャン。
  - `-GapFallbackNoHourCap` を付けると、それでも0件のとき `max-hours-to-end` 制約を外した再スキャンを追加実行。
  - gap artifacts: `logs/no_longshot_daily_gap.csv`, `logs/no_longshot_daily_gap.json`

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

Polymarket late-resolution high-probability validator (observe-only):
- Active screener:
  - `python scripts/polymarket_lateprob_observe.py screen`
  - `python scripts/polymarket_lateprob_observe.py screen --max-hours-to-end 0.25 --side-mode yes-only --yes-high-min 0.92 --yes-high-max 0.99 --top-n 20`
- Closed-market backtest:
  - `python scripts/polymarket_lateprob_observe.py backtest`
  - `python scripts/polymarket_lateprob_observe.py backtest --hours-before-end 0.25 --side-mode both --yes-high-min 0.9 --yes-high-max 0.99 --yes-low-min 0.01 --yes-low-max 0.1 --per-trade-cost 0.002`
- Key flags:
  - `screen`: `--min-hours-to-end`, `--max-hours-to-end`, `--side-mode`, `--yes-high-min`, `--yes-high-max`, `--yes-low-min`, `--yes-low-max`, `--min-liquidity`, `--min-volume-24h`, `--per-trade-cost`
  - `backtest`: `--sampling-mode`, `--date-min`, `--date-max`, `--hours-before-end`, `--lookback-hours`, `--max-stale-hours`, `--history-fidelity`, `--side-mode`, `--yes-high-min`, `--yes-high-max`, `--yes-low-min`, `--yes-low-max`, `--per-trade-cost`, `--max-open-positions`, `--max-open-per-category`
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
