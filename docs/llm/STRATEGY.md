# Strategy Canon (polymarket_mm)

This file is the canonical strategy register for concurrent chat/workstream coordination.
For operational source-of-truth workflow, follow `docs/llm/CANON.md`.

## Adoption Rule

- `ADOPTED`: usable in current operations.
- `REJECTED`: remove from active operation.
- `PENDING`: not enough evidence yet (includes `REVIEW`-equivalent hold state).

## Current KPI

Do not hand-edit KPI numbers in this file.

Always read the latest values from:

- `logs/strategy_register_latest.json`

Primary keys:

- `kpi_core.daily_realized_pnl_usd`
- `kpi_core.monthly_return_now_text`
- `kpi_core.max_drawdown_30d_text`

Secondary keys (diagnostics/compatibility):

- `no_longshot_status.monthly_return_now_text`
- `no_longshot_status.monthly_return_now_source`
- `no_longshot_status.monthly_return_now_new_condition_text`
- `no_longshot_status.monthly_return_now_all_text`
- `no_longshot_status.rolling_30d_monthly_return_text`
- `realized_30d_gate.decision`

## Bankroll Policy

- Initial bankroll (temporary default): `$60`.
- Strategy allocation ratio (default): equal-weight allocation across currently `ADOPTED` strategies.
- Live start max risk: cap daily risk at `5%` of bankroll (`$3/day` when bankroll is `$60`).
- For analytics/report scripts using `--assumed-bankroll-usd` / `-AssumedBankrollUsd`, use this policy bankroll by default unless an explicit override is required.
- Diversification memo (observe-only diagnostic, 2026-02-27):
  - Source: `logs/uncorrelated_portfolio_proxy_analysis_latest.json`, `docs/memo_uncorrelated_portfolio_latest.txt`
  - Explicit 5-strategy study (includes `gamma_eventpair_exec_edge_filter_observe`) estimated `portfolio_risk_proxy.risk_reduction_vs_avg_std=+12.1407%` with overlap `3` days and low confidence.
  - Same snapshot estimated `portfolio_monthly_proxy.improvement_vs_no_longshot_monthly_proxy=-5.8266%` (equal-weight pair proxy underperformed current no-longshot monthly proxy).
  - Daily morning uncorrelated diagnostics use fixed 4-strategy cohort by default (`scripts/check_morning_status.py` `--uncorrelated-strategy-ids` default).
  - This does not override active allocation policy; operational allocation remains equal-weight across currently `ADOPTED` strategies only.

## Active Strategies

1. `weather_clob_arb_buckets_observe`

- Status: `ADOPTED` (as of 2026-02-23, revalidated 2026-02-25).
- Scope: Polymarket weather basket opportunities, observe-only.
- Runtime:
  - `python scripts/polymarket_clob_arb_realtime.py --universe weather --strategy buckets`
  - Wrapper: `scripts/run_weather_arb_observe.ps1` (default strategy `buckets`)
- Evidence snapshot (2026-02-23 report):
  - Source log: `logs/clob-arb-weather-observe-24h.log`
  - `python scripts/report_clob_observation.py --log-file logs/clob-arb-weather-observe-24h.log --hours 24`
  - Opportunities: `>= $0.0000` was `259/1225 (21.1%)`, `>= $0.0500` was `204/1225 (16.7%)`
- Evidence snapshot (2026-02-25 profit-window report):
  - Source log: `logs/clob-arb-weather-observe-24h.log`
  - `python scripts/report_weather_arb_profit_window.py --log-file logs/clob-arb-weather-observe-24h.log --hours 2000 --assumed-bankroll-usd 100 --target-monthly-return-pct 15 --pretty`
  - Base scenario monthly projection: `+13.13%` (capture `35%`, threshold `>=4.00c`, span `13.11h`)
  - Gate result: `NO_GO` against stretch target `+15%` (used as tuning checkpoint, not observe adoption blocker)
- Decision note: positive edge incidence supports observe adoption; profit-window base projection was `+13.13%` on 2026-02-25 (stretch target `+15%` was not met).
- Operational gate:
  - `scripts/run_weather_24h_postcheck.ps1` default decision gate:
    - `samples >= 300`
    - `positive_0c_pct >= 30.0`
    - `positive_5c_pct >= 10.0`
  - Any gate miss is `REVIEW`.

2. `no_longshot_daily_observe`

- Status: `ADOPTED` (as of 2026-02-25, revalidated 2026-02-27, observe-first + live optional).
- Scope: Polymarket no-longshot daily monitor + logical-gap scan + forward realized tracker, with explicit-flag small-size live NO entry.
- Runtime:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -NoBackground -StrictRealizedBandOnly -RealizedFastYesMin 0.16 -RealizedFastYesMax 0.20 -RealizedFastMaxHoursToEnd 72 -RealizedFastMaxPages 120`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -NoBackground -StrictRealizedBandOnly -LiveExecute -LiveConfirm YES -LiveMaxOrders 1 -LiveOrderSizeShares 5 -LiveMaxDailyNotionalUsd 10 -LiveMaxOpenPositions 10 -LiveMaxEntryNoPrice 0.84 -LivePriceBufferCents 0.2`
  - `python scripts/no_longshot_daily_daemon.py --run-at-hhmm 00:05 --skip-refresh --realized-refresh-sec 900 --realized-entry-top-n 0 --runner-realized-fast-yes-min 0.16 --runner-realized-fast-yes-max 0.20 --runner-realized-fast-max-hours-to-end 72 --runner-realized-fast-max-pages 120 --runner-strict-realized-band-only`
  - `python scripts/no_longshot_daily_daemon.py --run-at-hhmm 00:05 --skip-refresh --runner-realized-fast-yes-min 0.16 --runner-realized-fast-yes-max 0.20 --runner-realized-fast-max-hours-to-end 72 --runner-realized-fast-max-pages 120 --runner-strict-realized-band-only --runner-live-execute --runner-live-confirm-live YES --runner-live-max-new-orders 1 --runner-live-order-size-shares 5 --runner-live-max-daily-notional-usd 10 --runner-live-max-open-positions 10 --runner-live-max-entry-no-price 0.84`
- Evidence snapshot (2026-02-25 daily summary):
  - Source summary: `logs/no_longshot_daily_summary.txt`
  - Read latest keys: `monthly_return_now`, `rolling_30d_monthly_return`, `monthly_return_now_source`
- Evidence snapshot (2026-02-26 fast-band check):
  - Source summary: `logs/no_longshot_daily_summary.txt`
  - `fast screen yes range=[0.16,0.2]`, `realized_entry_candidates=94`
- Evidence snapshot (2026-02-26 setting sensitivity):
  - Source JSON: `logs/no_longshot_setting_sensitivity_latest.json`
  - Baseline (`all`): `return_pct=-0.189566` (`trades=38`)
  - Cap profile (`cap_no_le_0.84`): `return_pct=+0.122439` (`trades=13`)
- Evidence snapshot (latest strategy register):
  - Source JSON: `logs/strategy_register_latest.json`
  - Read latest keys: `no_longshot_status.monthly_return_now_text`, `no_longshot_status.monthly_return_now_source`, `no_longshot_status.monthly_return_now_new_condition_text`, `no_longshot_status.monthly_return_now_all_text`, `no_longshot_status.rolling_30d_monthly_return_text`, `realized_30d_gate.decision`
- Evidence snapshot (2026-02-25 realized refresh):
  - Source JSON: `logs/no_longshot_realized_latest.json`
  - Read latest keys: `metrics.resolved_positions`, `metrics.open_positions`, `metrics.observed_days`, `metrics.rolling_30d.return_pct`
- Evidence snapshot (2026-02-25 guarded OOS):
  - Source JSON: `logs/no_longshot_daily_oos_guarded.json`
  - `walkforward_oos.capital_return`: `+9.3882%` (`n=36`, span `49.6d`, annualized `+93.59%`, `LOW_CONF span<90d`)
- Evidence snapshot (2026-02-27 strict-band checkpoint):
  - Source summary: `logs/no_longshot_daily_summary.txt`
  - `strict_realized_band_only=True`, `realized_entry_source=fast_72h_lowyes`
  - `rolling_30d_monthly_return=+9.89%`, `rolling_30d_resolved_trades=21`（new-condition）
- Decision note: maintain fast realized band `YES 0.16-0.20` (`entry_no_price<=0.84` equivalent) as the live/observe共通の entry policy. Live は明示 `LiveExecute + LiveConfirm YES` でのみ許可し、既定は observe-only を維持する。Latest canonical monthly return snapshot (`logs/strategy_register_latest.json`, refreshed on 2026-02-27): new-condition `+9.89%` (`no_longshot_status.monthly_return_now_new_condition_text`) vs all-pop comparator `-14.82%` (`no_longshot_status.monthly_return_now_all_text`).
- Operational gate: authority は `logs/strategy_register_latest.json` の `kpi_core`（`daily_realized_pnl_usd`, `monthly_return_now_text`, `max_drawdown_30d_text`）を最優先とし、補助として `logs/no_longshot_monthly_return_latest.txt` / `logs/no_longshot_realized_latest.json` を参照する。latest summary の fast band が `[0.16,0.2]` から逸脱した場合は `REVIEW`。
- Capital gate checkpoint (fixed on 2026-02-27):
  - Core threshold: `no_longshot_status.rolling_30d_resolved_trades >= 30`（new-condition basis）
  - Current: `21`（need `9` more）
  - Recent pace reference (new-condition resolved): `2026-02-25=9`, `2026-02-26=11`（`10/day`）
  - Fixed practical judgment date: `2026-03-02`（conservative half-speed assumption `5/day` + 1-day buffer）
  - If threshold is still unmet on `2026-03-02`, keep live disabled and slide judgment date by `+3` calendar days.

3. `link_intake_walletseed_cohort_observe`

- Status: `ADOPTED` (as of 2026-02-25, observe-only).
- Scope: profile/wallet hints from social links are converted to reproducible cohort autopsy inputs, observe-only.
- Runtime:
  - `python scripts/run_link_intake_cohort.py logs/link_intake_20260224_7links.json --profile-name linkseed_7links --min-confidence medium --max-trades 2500 --pretty`
- Evidence snapshot (2026-02-25 run):
  - Source summary: `logs/linkseed_7links_link_intake_summary_latest.json`
  - `stats.extracted_user_count=1`, `stats.resolved_user_count=1`, `stats.cohort_ok=true`, `stats.failed_user_count=0`
- Evidence snapshot:
  - Source claim links: `https://x.com/kunst13r/status/2022707250956243402`, `https://polymarket.com/@k9Q2mX4L8A7ZP3R`
  - Intake evidence: `logs/link_intake_20260224_7links.json`
  - Per-link notes:
    - `docs/knowledge/link-intake/sessions/2026-02-24_polymarket-7links/01_kunstler-on-x-trader-profile-t-co-scg6gt.md`
    - `docs/knowledge/link-intake/sessions/2026-02-24_polymarket-7links/02_k9q2mx4l8a7zp3r-on-polymarket.md`
- Decision note: wallet/profile extraction and cohort analysis are coupled in one reproducible run; current gate conditions were satisfied on 2026-02-25.
- Operational gate: keep this strategy in `REVIEW` for any run where `stats.resolved_user_count < 1` or `stats.cohort_ok != true`.

4. `hourly_updown_highprob_calibration_observe`

- Status: `ADOPTED` (as of 2026-02-25, observe-only).
- Scope: short-horizon hourly crypto up/down high-probability pricing calibration, observe-only.
- Runtime:
  - `python scripts/report_hourly_updown_highprob_calibration.py --assets bitcoin,ethereum,solana,xrp --hours 168 --tte-minutes 45 --entry-max-age-minutes 90 --price-min 0.70 --price-max 0.95 --max-trades-per-market 3000 --pretty --out-json logs/hourly_updown_highprob_calibration_168h_tte45_70_95_btc_eth_sol_xrp.json --out-csv logs/hourly_updown_highprob_calibration_168h_tte45_70_95_btc_eth_sol_xrp_samples.csv`
- Evidence snapshot:
  - Source claim link: `https://x.com/SynthdataCo/status/2021658564109234501`
  - Intake evidence: `logs/link_intake_20260224_link5_retry.json`
  - Per-link note: `docs/knowledge/link-intake/sessions/2026-02-24_polymarket-link5-retry/01_synthdata-on-x-launch-a-polymarket-tradi.md`
- Evidence snapshot (2026-02-25 expanded run):
  - Source summary: `logs/hourly_updown_highprob_calibration_168h_tte45_70_95_btc_eth_sol_xrp.json`
  - Read latest keys: `summary.qualified_samples`, `summary.edge_empirical_minus_price`
- Decision note: expanded-asset calibration run met sample and edge gates; promote to active observe calibration monitoring.
- Operational gate: keep observe-only and revert to `REVIEW` when either `qualified_samples < 200` or `edge_empirical_minus_price <= 0` on the latest 7-day-equivalent calibration run.

5. `event_driven_mispricing_observe`

- Status: `ADOPTED` (as of 2026-02-26, revalidated 2026-02-27, observe-only).
- Scope: event-driven Polymarket mispricing monitor (political/geopolitical/legal/regulatory/macropolicy classes), observe-only.
- Runtime:
  - `python scripts/polymarket_event_driven_observe.py --max-pages 12 --poll-sec 120 --min-edge-cents 0.8 --max-days-to-end 180 --top-n 20 --signal-cooldown-sec 7200 --signal-state-file logs/event-driven-observe-signal-state.json`
  - `python scripts/report_event_driven_profit_window.py --hours 24 --pretty`
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_event_driven_daily_report.ps1 -NoBackground -ProfitTargetMonthlyReturnPct 12`
- Evidence snapshot (2026-02-27 daily refresh / profit-window latest):
  - Source artifacts: `logs/event_driven_profit_window_latest.json`, `logs/event_driven_profit_window_latest.txt`
  - `decision=GO`, `projected_monthly_return=+936.35%` (base capture `35%`, threshold `>=5.00c`)
  - Quality gates met: `runs=436`, `episodes=114`, `unique_events=7`, `positive_ev_ratio=93.0%`
- Evidence snapshot (2026-02-27 class/diversity probe):
  - Source artifacts: `logs/event-driven-probe-postclass2-metrics.jsonl`, `logs/event-driven-probe-postclass2-signals.jsonl`
  - `event_count=42`, `candidate_count=14`, `top_written=14`
  - Signal classes: `election_politics`, `geopolitical`; emitted rows had `days_to_end` defined (`na_dte=0`)
- Decision note: promoted to active observe operations after class expansion (`election_politics`, `macro_policy`) and end-date guard (`allow_missing_end_date=false` default) increased actionable diversity while preserving quality gates.
- Operational gate:
  - Keep observe-only and mark `REVIEW` when latest `logs/event_driven_profit_window_latest.json` has any of:
    - `decision != GO`
    - `summary.episodes < 8`
    - `summary.unique_events < 4`
    - `summary.positive_ev_ratio < 0.60`
  - Keep default horizon hygiene (`allow_missing_end_date=false`; do not enable `--allow-missing-end-date` in production observe profile unless explicitly testing).

6. `gamma_eventpair_exec_edge_filter_observe`

- Status: `ADOPTED` (as of 2026-02-28, promoted from pending-release hold, observe-only).
- Scope: Polymarket gamma-active event-pair strategy with observe-only exec-edge suppression (`event-yes` filter).
- Runtime:
  - `python scripts/polymarket_clob_arb_realtime.py --universe gamma-active --strategy event-pair --gamma-limit 1500 --gamma-min-liquidity 0 --gamma-min-volume24hr 0 --gamma-scan-max-markets 40000 --gamma-max-days-to-end 60 --max-markets-per-event 5 --max-subscribe-tokens 400 --metrics-log-all-candidates --observe-exec-edge-filter --observe-exec-edge-min-usd 0.01 --observe-exec-edge-strike-limit 1 --observe-exec-edge-cooldown-sec 180 --observe-exec-edge-filter-strategies event-yes --min-edge-cents 10`
  - `python scripts/replay_clob_arb_kelly.py --metrics-file logs/clob-arb-monitor-metrics-eventpair-tuned-20260226-20260226_204500.jsonl,logs/clob-arb-monitor-metrics-eventpair-tuned30m-20260226-20260226_205340.jsonl,logs/clob-arb-monitor-metrics-eventpair-tuned3m-20260227_233912.jsonl --require-threshold-pass --fill-ratio-mode min --miss-penalty 0.005 --stale-grace-sec 2 --stale-penalty-per-sec 0.001 --max-worst-stale-sec 10 --min-gap-ms-per-event 5000 --scales 0.1,0.25,0.5,0.75,1.0 --bootstrap-iters 3000 --pretty --out-json logs/clob-arb-kelly-replay-eventpair-tuned38m-20260227-gap5s.json`
  - `python scripts/check_pending_release.py --strategy gamma_eventpair_exec_edge_filter_observe --conservative-costs --conservative-cost-cents 2 --pretty`
- Evidence snapshot (2026-02-27 strict revalidation):
  - Source metrics: `logs/clob-arb-monitor-metrics-eventpair-tuned3m-20260227_233912.jsonl` (`rows_total=1865`, `reason_threshold=515`, `distinct_events_threshold=7`)
  - Monthly estimate (3m strict): `logs/clob-arb-eventpair-monthly-estimate-tuned3m-20260227_233912.json` (`weighted_monthly_trim_edgepct_le_25=+4.07%`)
  - Monthly estimate (38m combined strict): `logs/clob-arb-eventpair-monthly-estimate-tuned38m-20260227.json` (`weighted_monthly_trim_edgepct_le_25=+5.47%`)
  - Kelly replay (38m strict combined): `logs/clob-arb-kelly-replay-eventpair-tuned38m-20260227-gap5s.json` (`full_fraction_estimate=0.293505`)
- Evidence snapshot (risk-transferability constraints):
  - Long-run NO_GO baseline: `logs/clob-arb-adoption-summary-20260226.json` (`decision=NO_GO`)
  - Prior long-run replay showed negative execution-edge regime (`full_kelly=0.0`): `logs/clob-arb-kelly-replay-eventpair-long2-exec.json`
- Adoption policy (`2026-02-28`, monthly-first + safety lock):
  - Principle:
    - monthly return is the primary screening signal
    - execution-edge + Kelly are mandatory safety locks for promotion to `ADOPTED`
  - Threshold policy:
    - keep `3m strict >= 0.7 x 38m combined strict` as-is (no immediate retune)
    - do not retune this ratio from a single refresh window
    - diagnostic recency bands (review aid, not standalone adoption gate): `>=0.85` strong, `[0.65,0.85)` watch, `<0.65` weak
  - MUST:
    - use outlier-trim monthly metric (`weighted_monthly_trim_edgepct_le_25`)
    - `3m strict > 0`
    - `38m combined strict > 0`
    - `3m strict >= 0.7 x 38m combined strict`
    - multi-event execution-edge remains positive in latest strict probe window
    - Kelly replay has `full_kelly > 0`
  - NICE TO HAVE:
    - `3m strict >= 38m combined strict`
    - no single-event concentration in trimmed monthly estimate
    - conservative cost/slippage assumptions keep execution-edge positive
- Decision rule:
  - IF all MUST conditions pass THEN `ADOPTED`
  - IF monthly conditions pass but any safety-lock condition fails THEN keep `PENDING` (`REVIEW`-equivalent hold)
  - IF monthly conditions pass and safety-lock is pending, keep observe-only and run periodic refresh (do not promote by monthly-only evidence)
  - IF either strict monthly metric is non-positive THEN `REJECTED`
- Decision note: promoted to `ADOPTED` on 2026-02-28 after strict replay (`logs/clob-arb-kelly-replay-eventpair-tuned38m-20260227-gap5s.json`) and conservative pending-release check both satisfied release locks (`execution_edge>0`, `full_kelly>0`, conservative edge positive).
- Operational gate: keep observe-only; immediately set `REVIEW` and stop promotion/live consideration if latest `check_pending_release.py --conservative-costs` returns `release_check=HOLD`.

## Rejected Strategies

1. `weather_clob_arb_yes_no_only`

- Status: `REJECTED`.
- Reason: low usefulness in this workspace run; switched to `buckets` as default.

2. `no_longshot_strict_lite_observe_experiment`

- Status: `REJECTED` (as of 2026-02-27, operator decision).
- Scope: strict-lite no-longshot side experiment (`non-crypto`, shorter horizon focus) tracked in isolated logs, observe-only.
- Runtime (experiment-only):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File logs/run_no_longshot_strict_lite_watch.ps1`
  - `python scripts/record_no_longshot_realized_daily.py --screen-csv logs/no_longshot_strict_lite_screen_latest.csv --positions-json logs/no_longshot_strict_lite_forward_positions.json --out-daily-jsonl logs/no_longshot_strict_lite_realized_daily.jsonl --out-latest-json logs/no_longshot_strict_lite_realized_latest.json --out-monthly-txt logs/no_longshot_strict_lite_monthly_return_latest.txt --entry-top-n 4 --per-trade-cost 0.002`
- Evidence snapshot (latest before stop):
  - `logs/no_longshot_strict_lite_vs_baseline_latest.json`: strict-lite `-4.3520%` (`resolved=8`, `open=7`) vs baseline `-14.8190%` (`resolved=46`).
  - `logs/no_longshot_strict_lite_outcome_range_latest.json`: all-open resolution range `worst=-47.9612%`, `best=+4.0776%`, `breakeven_wins_on_avg=7` (all wins required).
  - `logs/no_longshot_strict_lite_watch.log`: guard entered `reason=freeze_negative_realized` with `entry_top_n=0`.
- Decision note: operator rejected this experiment on 2026-02-27 (`ボツ`) due weak expected upside versus downside risk.
- Operational gate: keep strict-lite watcher stopped and do not add new strict-lite entries; only reconsider with a materially different rule set and new dryrun evidence.

## Pending Strategies

1. `social_profit_claim_validation_observe`

- Status: `PENDING` (as of 2026-02-25).
- Scope: social/X performance claims around Polymarket bot profitability, observe-only.
- Runtime:
  - `python scripts/report_social_profit_claims.py`
  - `python scripts/report_social_profit_claims.py --input-glob "logs/*realized*daily*.jsonl" --min-days 30 --pretty`
- Evidence snapshot:
  - Source claim link: `https://x.com/frostikkkk/status/2015154001797390637`
  - Intake evidence: `logs/link_intake_20260224_7links.json`
  - Per-link note: `docs/knowledge/link-intake/sessions/2026-02-24_polymarket-7links/07_frostikk-on-x-how-claude-polymarket-will.md`
- Evidence snapshot (2026-02-28 run):
  - Source summary: `logs/social_profit_claims_latest.json`
  - `summary.observed_days=5`, `meta.min_days=30`, all claim statuses were `INSUFFICIENT_DATA`
- Decision note: use measured realized PnL windows (`daily`, `rolling-30d`) to support/reject headline claims before considering strategy adoption.
- Operational gate: require at least 30 observed realized-PnL days (`--min-days 30`) before support/no-support judgment.
