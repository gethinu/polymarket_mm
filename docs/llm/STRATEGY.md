# Strategy Canon (polymarket_mm)

This file is the canonical strategy register for concurrent chat/workstream coordination.
For operational source-of-truth workflow, follow `docs/llm/CANON.md`.

## Adoption Rule

- `ADOPTED`: usable in current operations.
- `REJECTED`: remove from active operation.
- `PENDING`: not enough evidence yet.

## Current KPI

Do not hand-edit KPI numbers in this file.

Always read the latest values from:

- `logs/strategy_register_latest.json`

Primary keys:

- `no_longshot_status.monthly_return_now_text`
- `no_longshot_status.monthly_return_now_source`
- `no_longshot_status.monthly_return_now_new_condition_text`
- `no_longshot_status.monthly_return_now_all_text`
- `no_longshot_status.rolling_30d_monthly_return_text`
- `no_longshot_status.rolling_30d_monthly_return_new_condition_text`
- `no_longshot_status.rolling_30d_monthly_return_all_text`
- `realized_30d_gate.decision`

## Bankroll Policy

- Initial bankroll (temporary default): `$100`.
- Strategy allocation ratio (default): equal-weight allocation across currently `ADOPTED` strategies.
- Live start max risk: cap daily risk at `5%` of bankroll (`$5/day` when bankroll is `$100`).
- For analytics/report scripts using `--assumed-bankroll-usd` / `-AssumedBankrollUsd`, use this policy bankroll by default unless an explicit override is required.
- Diversification memo (observe-only diagnostic, 2026-02-27):
  - Source: `logs/uncorrelated_portfolio_proxy_analysis_latest.json`, `docs/memo_uncorrelated_portfolio_latest.txt`
  - Explicit 5-strategy study (includes `gamma_eventpair_exec_edge_filter_observe`) estimated `portfolio_risk_proxy.risk_reduction_vs_avg_std=+12.1407%` with overlap `3` days and low confidence.
  - Same snapshot estimated `portfolio_monthly_proxy.improvement_vs_no_longshot_monthly_proxy=-5.8266%` (equal-weight pair proxy underperformed current no-longshot monthly proxy).
  - Daily morning uncorrelated diagnostics use the same fixed 5-strategy cohort by default (`scripts/check_morning_status.py` `--uncorrelated-strategy-ids` default).
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

- Status: `ADOPTED` (as of 2026-02-25, revalidated 2026-02-26, observe-only).
- Scope: Polymarket no-longshot daily monitor + logical-gap scan + forward realized tracker, observe-only.
- Runtime:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -NoBackground -StrictRealizedBandOnly -RealizedFastYesMin 0.16 -RealizedFastYesMax 0.20 -RealizedFastMaxHoursToEnd 72 -RealizedFastMaxPages 120`
  - `python scripts/no_longshot_daily_daemon.py --run-at-hhmm 00:05 --skip-refresh --realized-refresh-sec 900 --realized-entry-top-n 0 --runner-realized-fast-yes-min 0.16 --runner-realized-fast-yes-max 0.20 --runner-realized-fast-max-hours-to-end 72 --runner-realized-fast-max-pages 120 --runner-strict-realized-band-only`
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
- Decision note: keep this strategy in observe-only; run with fast realized band `YES 0.16-0.20` (`entry_no_price<=0.84` equivalent). Latest canonical monthly return snapshot (`logs/strategy_register_latest.json`, refreshed on 2026-02-27): new-condition `+9.89%` (`no_longshot_status.monthly_return_now_new_condition_text`) vs all-pop comparator `-14.82%` (`no_longshot_status.monthly_return_now_all_text`).
- Operational gate: treat `logs/no_longshot_monthly_return_latest.txt` / `logs/no_longshot_realized_latest.json` as authority for monthly return; keep quality review active until resolved sample size is non-trivial, and mark `REVIEW` if the latest summary fast band drifts from `[0.16,0.2]`.
- Capital gate checkpoint (fixed on 2026-02-27):
  - Core threshold: `no_longshot_status.rolling_30d_resolved_trades >= 30`（new-condition basis）
  - Current: `21`（need `9` more）
  - Recent pace reference (new-condition resolved): `2026-02-25=9`, `2026-02-26=11`（`10/day`）
  - Fixed practical judgment date: `2026-03-02`（conservative half-speed assumption `5/day` + 1-day buffer）
  - If threshold is still unmet on `2026-03-02`, keep observe-only and slide judgment date by `+3` calendar days.

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

## Rejected Strategies

1. `weather_clob_arb_yes_no_only`

- Status: `REJECTED`.
- Reason: low usefulness in this workspace run; switched to `buckets` as default.

2. `gamma_eventpair_exec_edge_filter_observe`

- Status: `REJECTED` (as of 2026-02-26, observe-only).
- Scope: Polymarket gamma-active event-pair strategy with observe-only exec-edge suppression (`event-yes` filter).
- Runtime:
  - `python scripts/polymarket_clob_arb_realtime.py --universe gamma-active --strategy event-pair --gamma-limit 1500 --gamma-min-liquidity 0 --gamma-min-volume24hr 0 --gamma-scan-max-markets 40000 --gamma-max-days-to-end 0 --max-markets-per-event 5 --max-subscribe-tokens 400 --metrics-log-all-candidates --observe-exec-edge-filter --observe-exec-edge-min-usd 0 --observe-exec-edge-strike-limit 2 --observe-exec-edge-cooldown-sec 90 --observe-exec-edge-filter-strategies event-yes`
  - `python scripts/replay_clob_arb_kelly.py --metrics-file logs/clob-arb-metrics-eventpair-long2-20260226_091045.jsonl --require-threshold-pass --fill-ratio-mode min --miss-penalty 0.005 --stale-grace-sec 2 --stale-penalty-per-sec 0.001 --max-worst-stale-sec 10 --scales 0.1,0.25,0.5,0.75,1.0 --bootstrap-iters 3000 --pretty --out-json logs/clob-arb-kelly-replay-eventpair-long2-exec.json`
- Runtime note: revalidation only, not active operation.
- Evidence snapshot (2026-02-26 coverage gate refresh, observe-only):
  - Source metrics: `logs/clob-arb-monitor-metrics-eventpair-coverage-gate-refresh-20260226.jsonl`
  - Source coverage summaries: `logs/clob-arb-eventpair-metrics-coverage-latest.json`, `logs/clob-arb-eventpair-distinct-events-current-latest.json`, `logs/clob-arb-eventpair-coverage-tuning-latest.json`
  - `rows_total=5017`, `distinct_events_all=99`, `distinct_events_threshold_raw=25`, `distinct_events_threshold_exec=14` (raw coverage gate target 20 met)
- Evidence snapshot (2026-02-26 latest metrics-glob aggregate, observe-only):
  - Source summary: `logs/clob-arb-eventpair-distinct-events-current-latest.json` (latest run `logs/clob-arb-monitor-metrics-eventpair-coverage-gate-refresh-edge0p5-maxsub2000-20260226.jsonl`)
  - `rows_total=14306`, `distinct_events_all=338`, `distinct_events_threshold_raw=45`, `distinct_events_threshold_exec=19` (`exec>=20` is still short by 1)
- Evidence snapshot (2026-02-26 parameter checks, observe-only):
  - Source metrics (`max_subscribe_tokens` A/B on strict profile): `logs/clob-arb-monitor-metrics-eventpair-tuned-maxsub400-probe-20260226.jsonl`, `logs/clob-arb-monitor-metrics-eventpair-tuned-maxsub1200-probe-20260226.jsonl`
  - Both runs remained `Loaded baskets=40` / `Subscribed token IDs=80` and `distinct_events_threshold_raw=3`; raising `max_subscribe_tokens` alone did not expand strict-profile coverage.
  - Source metrics (strict high-scan probe): `logs/clob-arb-monitor-metrics-eventpair-strict-highscan-maxsub1200-20260226.jsonl`
  - Even with `gamma_limit=5000` / `gamma_scan_max_markets=100000`, run stayed `Loaded baskets=12` / `Subscribed token IDs=24`, with `distinct_events_threshold_raw=4`, `distinct_events_threshold_exec=3`.
  - Source metrics (`min_edge=0.5` with token-cap sweep): `logs/clob-arb-monitor-metrics-eventpair-coverage-gate-refresh-edge0p5-20260226.jsonl`, `logs/clob-arb-monitor-metrics-eventpair-coverage-gate-refresh-edge0p5-maxsub1200-20260226.jsonl`, `logs/clob-arb-monitor-metrics-eventpair-coverage-gate-refresh-edge0p5-maxsub2000-20260226.jsonl`
  - `distinct_events_threshold_raw` improved `29 -> 40 -> 45`, but `distinct_events_threshold_exec` stayed `19` across all token caps.
  - `polymarket_clob_arb_realtime.py` rejects `--gamma-pages` / `--gamma-page-size` as unrecognized arguments; scan-depth control is `--gamma-limit` + `--gamma-scan-max-markets`.
- Evidence snapshot (2026-02-26 long-run):
  - Source metrics: `logs/clob-arb-metrics-eventpair-long-20260225_220258.jsonl`, `logs/clob-arb-metrics-eventpair-long2-20260226_091045.jsonl`
  - `exec_mean_edge_usd` remained negative (`-0.245768`, `-0.245639`) and `exec_positive_count=0` in both runs.
- Evidence snapshot (2026-02-26 Kelly replay):
  - Source replay: `logs/clob-arb-kelly-replay-eventpair-long2-raw.json`, `logs/clob-arb-kelly-replay-eventpair-long2-exec.json`, `logs/clob-arb-kelly-replay-yesno-moneyline-raw.json`, `logs/clob-arb-kelly-replay-yesno-moneyline-exec.json`
  - Both event-pair and yes/no baselines reported negative mean edge with `full_kelly=0.0`.
- Evidence snapshot:
  - Consolidated decision: `logs/clob-arb-adoption-summary-20260226.json` (`decision=NO_GO`).
- Tuning proposal (coverage gate update, observe-only):
  - Keep coverage acceptance on `distinct_events_threshold_raw >= 20` (now met) and treat `distinct_events_threshold_exec >= 20` as a stretch KPI.
  - For coverage probes only, use `--min-edge-cents 0.5` and `--max-subscribe-tokens 2000`; keep adoption gate unchanged until execution-edge and Kelly conditions recover.
- Decision note: prior ADOPTED decision (2026-02-25) was superseded by extended observe evidence on 2026-02-26; keep this strategy out of active operations.
- Operational gate: only reconsider after multi-event evidence shows sustained positive execution edge and Kelly replay with `full_kelly > 0`.

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
- Evidence snapshot (2026-02-25 run):
  - Source summary: `logs/social_profit_claims_latest.json`
  - `summary.observed_days=2`, `meta.min_days=30`, all claim statuses were `INSUFFICIENT_DATA`
- Decision note: use measured realized PnL windows (`daily`, `rolling-30d`) to support/reject headline claims before considering strategy adoption.
- Operational gate: require at least 30 observed realized-PnL days (`--min-days 30`) before support/no-support judgment.
