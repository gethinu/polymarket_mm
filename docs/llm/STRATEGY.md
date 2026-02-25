# Strategy Canon (polymarket_mm)

This file is the canonical strategy register for concurrent chat/workstream coordination.

## Adoption Rule

- `ADOPTED`: usable in current operations.
- `REJECTED`: remove from active operation.
- `PENDING`: not enough evidence yet.

## Current KPI

- `monthly_return_now`: `+5.14%` (as of 2026-02-25, source: `logs/no_longshot_daily_summary.txt`, field `monthly_return_now`)
- `rolling_30d_monthly_return`: `n/a` (source: `logs/no_longshot_monthly_return_latest.txt`)

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
  - `scripts/run_weather_24h_postcheck.ps1` decision rule uses `positive_0c_pct >= 20.0` as `ADOPT`, else `REVIEW`.

2. `no_longshot_daily_observe`
- Status: `ADOPTED` (as of 2026-02-25, observe-only).
- Scope: Polymarket no-longshot daily monitor + logical-gap scan + forward realized tracker, observe-only.
- Runtime:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -NoBackground`
  - `python scripts/no_longshot_daily_daemon.py --run-at-hhmm 00:05 --skip-refresh`
- Evidence snapshot (2026-02-25 daily summary):
  - Source summary: `logs/no_longshot_daily_summary.txt`
  - `monthly_return_now`: `+5.14%` (`monthly_return_now_source=backtest_oos_ann_to_monthly`)
  - `rolling_30d_monthly_return`: `n/a` (`rolling_30d_resolved_trades=0`)
- Evidence snapshot (2026-02-25 guarded OOS):
  - Source JSON: `logs/no_longshot_daily_oos_guarded.json`
  - `walkforward_oos.capital_return`: `+20.4538%` (`n=81`, span `112.9d`, annualized `+82.49%`)
- Decision note: added to active observe operations because current monthly proxy exceeds `+5%`; keep return claims anchored to realized tracker outputs.
- Operational gate: treat `logs/no_longshot_monthly_return_latest.txt` / `logs/no_longshot_realized_latest.json` as authority for realized monthly return; keep quality review active until rolling-30d realized is no longer `n/a`.

## Rejected Strategies

1. `weather_clob_arb_yes_no_only`
- Status: `REJECTED`.
- Reason: low usefulness in this workspace run; switched to `buckets` as default.

## Pending Strategies

1. `social_profit_claim_validation_observe`
- Status: `PENDING` (as of 2026-02-24).
- Scope: social/X performance claims around Polymarket bot profitability, observe-only.
- Runtime:
  - `python scripts/report_social_profit_claims.py`
  - `python scripts/report_social_profit_claims.py --input-glob "logs/*realized*daily*.jsonl" --min-days 30 --pretty`
- Evidence snapshot:
  - Source claim link: `https://x.com/frostikkkk/status/2015154001797390637`
  - Intake evidence: `logs/link_intake_20260224_7links.json`
  - Per-link note: `docs/knowledge/link-intake/sessions/2026-02-24_polymarket-7links/07_frostikk-on-x-how-claude-polymarket-will.md`
- Decision note: use measured realized PnL windows (`daily`, `rolling-30d`) to support/reject headline claims before considering strategy adoption.
- Operational gate: require at least 30 observed realized-PnL days (`--min-days 30`) before support/no-support judgment.

2. `hourly_updown_highprob_calibration_observe`
- Status: `PENDING` (as of 2026-02-24).
- Scope: short-horizon hourly crypto up/down high-probability pricing calibration, observe-only.
- Runtime:
  - `python scripts/report_hourly_updown_highprob_calibration.py --assets bitcoin,ethereum --hours 72 --tte-minutes 20 --price-min 0.80 --price-max 0.95 --max-trades-per-market 3000 --pretty`
- Evidence snapshot:
  - Source claim link: `https://x.com/SynthdataCo/status/2021658564109234501`
  - Intake evidence: `logs/link_intake_20260224_link5_retry.json`
  - Per-link note: `docs/knowledge/link-intake/sessions/2026-02-24_polymarket-link5-retry/01_synthdata-on-x-launch-a-polymarket-tradi.md`
- Decision note: calibrate realized hit-rate at fixed time-to-end against quoted high-probability prices before any model-based edge claims are trusted.
- Operational gate: require non-trivial sample count (`qualified_samples >= 200`) and positive calibration edge (`empirical_win_rate - avg_entry_price > 0`) for review.

3. `link_intake_walletseed_cohort_observe`
- Status: `PENDING` (as of 2026-02-24).
- Scope: profile/wallet hints from social links are converted to reproducible cohort autopsy inputs, observe-only.
- Runtime:
  - `python scripts/run_link_intake_cohort.py logs/link_intake_20260224_7links.json --profile-name linkseed_7links --min-confidence medium --max-trades 2500 --pretty`
- Evidence snapshot:
  - Source claim links: `https://x.com/kunst13r/status/2022707250956243402`, `https://polymarket.com/@k9Q2mX4L8A7ZP3R`
  - Intake evidence: `logs/link_intake_20260224_7links.json`
  - Per-link notes:
    - `docs/knowledge/link-intake/sessions/2026-02-24_polymarket-7links/01_kunstler-on-x-trader-profile-t-co-scg6gt.md`
    - `docs/knowledge/link-intake/sessions/2026-02-24_polymarket-7links/02_k9q2mx4l8a7zp3r-on-polymarket.md`
- Decision note: keep wallet/profile extraction and cohort analysis coupled in one observable run to reduce manual copy errors.
- Operational gate: require at least one resolved wallet and successful cohort output (`cohort.ok=true`) before strategy interpretation.
