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
- `no_longshot_status.rolling_30d_monthly_return_text`
- `realized_30d_gate.decision`

## Bankroll Policy

- Initial bankroll (temporary default): `$100`.
- Strategy allocation ratio (default): equal-weight allocation across currently `ADOPTED` strategies.
- Live start max risk: cap daily risk at `5%` of bankroll (`$5/day` when bankroll is `$100`).
- For analytics/report scripts using `--assumed-bankroll-usd` / `-AssumedBankrollUsd`, use this policy bankroll by default unless an explicit override is required.

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
- Status: `ADOPTED` (as of 2026-02-25, observe-only).
- Scope: Polymarket no-longshot daily monitor + logical-gap scan + forward realized tracker, observe-only.
- Runtime:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -NoBackground`
  - `python scripts/no_longshot_daily_daemon.py --run-at-hhmm 00:05 --skip-refresh --realized-refresh-sec 900 --realized-entry-top-n 0`
- Evidence snapshot (2026-02-25 daily summary):
  - Source summary: `logs/no_longshot_daily_summary.txt`
  - Read latest keys: `monthly_return_now`, `rolling_30d_monthly_return`, `monthly_return_now_source`
- Evidence snapshot (latest strategy register):
  - Source JSON: `logs/strategy_register_latest.json`
  - Read latest keys: `no_longshot_status.monthly_return_now_text`, `no_longshot_status.monthly_return_now_source`, `no_longshot_status.rolling_30d_monthly_return_text`, `realized_30d_gate.decision`
- Evidence snapshot (2026-02-25 realized refresh):
  - Source JSON: `logs/no_longshot_realized_latest.json`
  - Read latest keys: `metrics.resolved_positions`, `metrics.open_positions`, `metrics.observed_days`, `metrics.rolling_30d.return_pct`
- Evidence snapshot (2026-02-25 guarded OOS):
  - Source JSON: `logs/no_longshot_daily_oos_guarded.json`
  - `walkforward_oos.capital_return`: `+9.3882%` (`n=36`, span `49.6d`, annualized `+93.59%`, `LOW_CONF span<90d`)
- Decision note: keep this strategy in observe-only; monthly-return claims must always be read from latest realized artifacts.
- Operational gate: treat `logs/no_longshot_monthly_return_latest.txt` / `logs/no_longshot_realized_latest.json` as authority for monthly return; keep quality review active until resolved sample size is non-trivial.

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

4. `gamma_eventpair_exec_edge_filter_observe`
- Status: `ADOPTED` (as of 2026-02-25, observe-only).
- Scope: Polymarket gamma-active event-pair strategy with observe-only exec-edge suppression (`event-yes` filter).
- Runtime:
  - `python scripts/polymarket_clob_arb_realtime.py --universe gamma-active --strategy event-pair --observe-exec-edge-filter --observe-exec-edge-min-usd 0 --observe-exec-edge-strike-limit 2 --observe-exec-edge-cooldown-sec 90 --observe-exec-edge-filter-strategies event-yes`
  - `python scripts/polymarket_clob_arb_realtime.py --universe gamma-active --strategy event-pair --gamma-limit 500 --gamma-min-liquidity 1000 --gamma-min-volume24hr 100 --gamma-scan-max-markets 20000 --gamma-max-days-to-end 60 --gamma-score-halflife-days 14 --max-markets-per-event 5 --max-subscribe-tokens 400 --metrics-log-all-candidates --observe-exec-edge-filter --observe-exec-edge-min-usd 0 --observe-exec-edge-strike-limit 2 --observe-exec-edge-cooldown-sec 90 --observe-exec-edge-filter-strategies event-yes`
  - `python scripts/polymarket_clob_arb_realtime.py --universe gamma-active --strategy event-pair --gamma-limit 1500 --gamma-min-liquidity 0 --gamma-min-volume24hr 0 --gamma-scan-max-markets 40000 --gamma-max-days-to-end 0 --gamma-score-halflife-days 14 --max-markets-per-event 5 --max-subscribe-tokens 400 --metrics-log-all-candidates --observe-exec-edge-filter --observe-exec-edge-min-usd 0 --observe-exec-edge-strike-limit 2 --observe-exec-edge-cooldown-sec 90 --observe-exec-edge-filter-strategies event-yes`
  - `python scripts/replay_clob_arb_kelly.py --metrics-file logs/clob-arb-monitor-metrics-eventpair-session.jsonl --require-threshold-pass --fill-ratio-mode min --miss-penalty 0.005 --stale-grace-sec 2 --stale-penalty-per-sec 0.001 --max-worst-stale-sec 10 --scales 0.1,0.25,0.5,0.75,1.0 --bootstrap-iters 3000 --pretty --out-json logs/clob-arb-kelly-replay-eventpair-session-conservative-v2.json`
- Evidence snapshot:
  - Source metrics: `logs/clob-arb-monitor-metrics-eventpair-session.jsonl`
  - AB quality summary: `logs/clob-arb-ab3-comparison-summary.json` (`filter_yes.pass_unblocked_exec_positive_rate=98.3%`)
  - Monthly estimate (conservative): `logs/clob-arb-eventpair-monthly-estimate-conservative-20260225.json` (`weighted_monthly_return=+3.72%`)
- Evidence snapshot (adopted observe refresh 2026-02-25):
  - Source metrics: `logs/clob-arb-monitor-metrics-eventpair-adopt-20260225_182309.jsonl` (`rows=134`, `rows_blocked=44`)
  - Replay summaries: `logs/clob-arb-kelly-replay-eventpair-adopt-20260225_182309-base.json`, `logs/clob-arb-kelly-replay-eventpair-adopt-20260225_182309-gap5s.json`
  - Monthly estimate (outlier-trim recommendation): `logs/clob-arb-eventpair-monthly-estimate-adopt-20260225_182309.json` (`recommended_monthly_return=+3.46%`, rule: `mean_edge_pct_raw <= 25%`)
- Evidence snapshot (extended observe refresh 2026-02-25):
  - Source metrics: `logs/clob-arb-monitor-metrics-eventpair-adopt-extended-20260225_205027.jsonl` (`rows=443`, `rows_blocked=180`)
  - Replay summaries: `logs/clob-arb-kelly-replay-eventpair-adopt-extended-20260225_205027-base.json`, `logs/clob-arb-kelly-replay-eventpair-adopt-extended-20260225_205027-gap5s.json`
  - Monthly estimate (outlier-trim recommendation): `logs/clob-arb-eventpair-monthly-estimate-adopt-extended-20260225_205027.json` (`recommended_monthly_return=+2.60%`, rule: `mean_edge_pct_raw <= 25%`)
- Evidence snapshot (30m combined refresh 2026-02-25):
  - Source metrics: `logs/clob-arb-monitor-metrics-eventpair-adopt-extended2a-20260225_211108.jsonl`, `logs/clob-arb-monitor-metrics-eventpair-adopt-extended2b-20260225_212630.jsonl` (`rows_total=863`, `rows_blocked=381`)
  - Replay summaries: `logs/clob-arb-kelly-replay-eventpair-adopt-extended2ab-20260225-base.json`, `logs/clob-arb-kelly-replay-eventpair-adopt-extended2ab-20260225-gap5s.json`
  - Monthly estimate (outlier-trim recommendation): `logs/clob-arb-eventpair-monthly-estimate-adopt-extended2ab-20260225.json` (`recommended_monthly_return=+2.87%`, rule: `mean_edge_pct_raw <= 25%`)
- Evidence snapshot (coverage refresh 2026-02-25):
  - Source coverage (historical): `logs/clob-arb-eventpair-coverage-tuning-latest.json` (`runs[adopt_extended2ab_20260225]`)
  - `distinct_events_all=4` (`event_gate_target=20`, not met at that time)
- Evidence snapshot (coverage probe refresh 2026-02-25, 90s observe):
  - Source metrics: `logs/clob-arb-monitor-metrics-eventpair-coverage-probe-baseline.jsonl`, `logs/clob-arb-monitor-metrics-eventpair-coverage-probe-expanded.jsonl`
  - Baseline probe (`gamma-limit=500`, `gamma-scan-max-markets=20000`, `max-subscribe-tokens=400`): `distinct_events_all=7`, `distinct_events_threshold=4`
  - Expanded probe (`gamma-limit=1000`, `gamma-scan-max-markets=40000`, `max-subscribe-tokens=800`): `distinct_events_all=7`, `distinct_events_threshold=4`
  - Coverage note: this runtime uses `--gamma-scan-max-markets` for scan depth; `--gamma-pages` / `--gamma-page-size` are not supported in `polymarket_clob_arb_realtime.py`.
- Evidence snapshot (coverage expansion refresh 2026-02-26, observe-only):
  - Source comparison: `logs/clob-arb-eventpair-coverage-tuning-latest.json`
  - Relaxed profile (`gamma-limit=1500`, `gamma-min-liquidity=0`, `gamma-min-volume24hr=0`, `gamma-scan-max-markets=40000`, `max-subscribe-tokens=400`, `run-seconds=120`): `distinct_events_all=100`, `distinct_events_threshold=23`
  - High-load profile (`max-subscribe-tokens=1200`, `run-seconds=180`): `distinct_events_all=219`, `distinct_events_threshold=28`
  - Load note: coverage拡張は候補・購読トークン数を増やすため、ローカル負荷を抑える場合は `max-subscribe-tokens=400` を優先。
- Evidence snapshot (coverage latest refresh 2026-02-26, observe-only):
  - Source metrics: `logs/clob-arb-monitor-metrics-eventpair-adopt-coverage-refresh-20260226.jsonl` (`rows_total=7674`, `rows_blocked=2808`)
  - Source coverage: `logs/clob-arb-eventpair-metrics-coverage-latest.json`, `logs/clob-arb-eventpair-metrics-coverage-20260226.json`
  - `distinct_events_all=100`, `distinct_events_threshold=24`, `event_gate_target=20` (met)
- Evidence snapshot (regime compression check 2026-02-26, observe-only):
  - Source long-run metrics: `logs/clob-arb-metrics-eventpair-long-20260225_220258.jsonl` (`rows_total=5068`, `reason=candidate` only)
  - Threshold sensitivity: `logs/clob-arb-eventpair-threshold-sensitivity-20260226_201054.json`
  - Current threshold (`3c`, stale `<=10s`, gap `5s`) produced `sample_count=0` (no executable candidates under adopted gate)
  - What-if threshold (`0.5c`, stale `<=10s`, gap `5s`) produced `weighted_monthly_trim_edgepct_le_25=-7.01%`
- Evidence snapshot (strict exec-gate probe 2026-02-26, observe-only):
  - Source metrics: `logs/clob-arb-monitor-metrics-eventpair-tuned-20260226-20260226_204500.jsonl` (`rows=1461`, `threshold=86`, `blocked=1375`)
  - Tuning comparisons: `logs/clob-arb-eventpair-threshold-tuning-coverage-refresh-20260226.json`, `logs/clob-arb-eventpair-execgate-tuning-extended2ab-20260226.json`
  - Strict profile estimate: `logs/clob-arb-eventpair-monthly-estimate-tuned-20260226_204500.json` (`weighted_monthly_trim_edgepct_le_25=+5.62%`, `sample_count=21`, `distinct_events=3`)
- Decision note: promoted to active observe operations by operator decision on 2026-02-25; keep claims low-confidence and regime-aware until event coverage and signal quality stabilize.
- Operational gate: keep this strategy observe-only and mark `REVIEW` when (`distinct_events_threshold < 20` and `observed_realized_days < 30`) or when latest `3c` threshold check yields `sample_count=0`; monitor `distinct_events_all` with `--metrics-log-all-candidates` as secondary coverage signal.

5. `hourly_updown_highprob_calibration_observe`
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
