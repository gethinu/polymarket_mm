# Strategy Integration Log (One-by-One)

Session:
- `2026-02-21_polymarket-5links-auto-v5`

## Link 01

Source:
- `https://x.com/AleiahLock/status/2024049808055431356`

Integrated target:
- `scripts/run_weather_mimic_pipeline.py`
- `scripts/build_weather_mimic_profile.py`
- `scripts/build_weather_consensus_watchlist.py`

Integrated rules (observe-only):
- Add winner token gating with `--winner-require-weather-token` and `--winner-min-weather-token-hits`.
- Add `--aleiah-weather-pack` preset:
  - defaults required winner tokens to `nyc,london` when unset
  - enables consensus overlap requirement
- Add consensus overlap gate:
  - pipeline/build-profile pass through `--consensus-require-overlap`
  - consensus builder enforces `--require-overlap` rows only

Operator examples:
- `python scripts/run_weather_mimic_pipeline.py --user-file logs/cohort_weather_7acct_inputs_20260222.txt --profile-name weather_aleiah --aleiah-weather-pack --no-run-scans`
- `python scripts/build_weather_consensus_watchlist.py --profile-name weather_focus_mimic --require-overlap --top-n 25`

## Link 02

Source:
- `https://x.com/browomo/status/2024075205245534532`

Integrated target:
- `scripts/run_weather_mimic_pipeline.py`
- `scripts/build_weather_mimic_profile.py`
- `scripts/build_weather_consensus_watchlist.py`

Integrated rules (observe-only):
- Add `--browomo-small-edge-pack` preset:
  - enables consensus overlap requirement
  - expands candidate breadth (`top_n>=50`, `scan_max_pages>=120`)
  - applies low-correlation diversification cap (`max_per_correlation_bucket=2` when unset)
  - prefers `score_mode=edge` when default balanced mode is used
- Add explicit correlation-bucket diversification controls:
  - pipeline/build-profile pass through `--consensus-max-per-correlation-bucket`
  - consensus builder enforces `--max-per-correlation-bucket`
  - output rows now include `correlation_bucket` for traceability

Operator examples:
- `python scripts/run_weather_mimic_pipeline.py --user-file logs/cohort_weather_7acct_inputs_20260222.txt --profile-name weather_browomo --browomo-small-edge-pack --no-run-scans`
- `python scripts/build_weather_consensus_watchlist.py --profile-name weather_focus_mimic --require-overlap --max-per-correlation-bucket 2 --top-n 50`

## Link 03

Source:
- `https://polymarket.com/@c0O0OLI0O0O0`

Integrated target:
- `scripts/run_weather_mimic_pipeline.py`
- `scripts/build_weather_mimic_profile.py`
- `scripts/build_weather_consensus_watchlist.py`

Integrated rules (observe-only):
- Add wallet-style winner filters:
  - `--winner-min-roundtrip-key-share-pct`
  - `--winner-min-close-leg-count`
  - `--winner-max-avg-interval-sec`
  - `--winner-min-sell-to-buy-notional-ratio`
- Add `--c0-micro-moves-pack` preset:
  - enforces wallet turnover filters above (if not explicitly set)
  - enables consensus overlap and correlation diversification (`max_per_correlation_bucket=2` when unset)
  - prefers `score_mode=edge`, raises candidate breadth (`top_n>=60`, `scan_max_pages>=140`)
  - raises liquidity floors (`min_liquidity>=700`, `min_volume_24h>=200`) for exit robustness
- Winner scoring output now includes wallet-style pass/fail fields for audit traceability.

Operator examples:
- `python scripts/run_weather_mimic_pipeline.py --user-file logs/cohort_weather_7acct_inputs_20260222.txt --profile-name weather_c0 --c0-micro-moves-pack --no-run-scans`
- `python scripts/run_weather_mimic_pipeline.py --user-file logs/cohort_weather_7acct_inputs_20260222.txt --profile-name weather_c0_custom --winner-min-roundtrip-key-share-pct 12 --winner-min-close-leg-count 80 --winner-max-avg-interval-sec 18000 --winner-min-sell-to-buy-notional-ratio 0.5 --consensus-require-overlap --consensus-max-per-correlation-bucket 2 --no-run-scans`

## Link 04

Source:
- `https://x.com/velonxbt/status/2024075034185142683`

Integrated target:
- `scripts/run_weather_mimic_pipeline.py`
- `scripts/build_weather_mimic_profile.py`
- `scripts/build_weather_consensus_watchlist.py`

Integrated rules (observe-only):
- Add consensus liquidity/turnover horizon controls:
  - `--min-turnover-ratio` on consensus builder (`volume_24h / liquidity_num` floor)
  - `--max-hours-to-end` on consensus builder (short-horizon cap)
- Add pipeline/build-profile pass-through:
  - `--consensus-min-turnover-ratio`
  - `--consensus-max-hours-to-end`
- Add `--velon-micro-moves-pack` preset:
  - enforces overlap + diversification (`max_per_correlation_bucket=2` when unset)
  - uses faster monitor cadence (`scan_interval_sec<=120`)
  - raises breadth/liquidity defaults (`scan_max_pages>=160`, `top_n>=80`, `min_liquidity>=1000`, `min_volume_24h>=300`)
  - applies short-horizon + turnover defaults (`min_turnover_ratio=0.30`, `max_hours_to_end=48`)
  - tightens wallet-style winner filters for exit discipline:
    - `min_roundtrip_key_share_pct=10`
    - `min_close_leg_count=50`
    - `max_avg_interval_sec=22000`
    - `min_sell_to_buy_notional_ratio=0.60`
  - prefers `score_mode=edge` when default mode is balanced

Operator examples:
- `python scripts/run_weather_mimic_pipeline.py --user-file logs/cohort_weather_7acct_inputs_20260222.txt --profile-name weather_velon --velon-micro-moves-pack --no-run-scans`
- `python scripts/build_weather_consensus_watchlist.py --profile-name weather_focus_mimic --require-overlap --max-per-correlation-bucket 2 --min-turnover-ratio 0.30 --max-hours-to-end 48 --top-n 80`

## Link 05

Source:
- `https://x.com/RohOnChain/status/2023781142663754049`

Integrated target:
- `scripts/run_weather_mimic_pipeline.py`
- `scripts/build_weather_mimic_profile.py`

Integrated rules (observe-only):
- Add no_longshot/lateprob cost & robustness pass-through:
  - `--no-longshot-per-trade-cost`
  - `--no-longshot-min-net-yield-per-day`
  - `--lateprob-per-trade-cost`
  - `--lateprob-max-active-stale-hours`
- Add `--roan-roadmap-pack` preset:
  - applies overlap + stronger diversification (`max_per_correlation_bucket=1` when unset)
  - raises breadth/liquidity defaults (`scan_max_pages>=180`, `top_n>=100`, `min_liquidity>=1200`, `min_volume_24h>=400`)
  - shortens cadence (`scan_interval_sec<=90`) and horizon (`max_hours_to_end=36` if unset)
  - raises turnover floor (`min_turnover_ratio=0.50` if unset)
  - enforces cost-aware edge gate (`no_longshot_per_trade_cost=0.003`, `no_longshot_min_net_yield_per_day=0.02`, `lateprob_per_trade_cost=0.003`, `lateprob_max_active_stale_hours=4`)
  - applies stricter winner behavior defaults:
    - `min_roundtrip_key_share_pct=10`
    - `min_close_leg_count=70`
    - `max_avg_interval_sec=20000`
    - `min_sell_to_buy_notional_ratio=0.65`
  - prefers `score_mode=edge` when default mode is balanced

Operator examples:
- `python scripts/run_weather_mimic_pipeline.py --user-file logs/cohort_weather_7acct_inputs_20260222.txt --profile-name weather_roan --roan-roadmap-pack --no-run-scans`
- `python scripts/build_weather_mimic_profile.py logs/weather_roan_smoketest_cohort_winners_20260223_043641.json --profile-name weather_roan_custom --no-longshot-per-trade-cost 0.003 --no-longshot-min-net-yield-per-day 0.02 --lateprob-per-trade-cost 0.003 --lateprob-max-active-stale-hours 4 --pretty`

Status:
- Link 01 integrated.
- Link 02 integrated.
- Link 03 integrated.
- Link 04 integrated.
- Link 05 integrated.
- Next: Cross-link synthesis and live/paper execution checklist hardening.

Validation artifacts:
- `logs/weather_aleiah_smoketest_pipeline_summary_20260222_142356.json`
- `logs/weather_7acct_auto_consensus_watchlist_overlap_latest.json`
- `logs/weather_7acct_auto_consensus_snapshot_overlap_latest.html`
- `logs/weather_browomo_smoketest_pipeline_summary_20260222_143609.json`
- `logs/weather_7acct_auto_consensus_watchlist_overlap_div2_latest.json`
- `logs/weather_7acct_auto_consensus_snapshot_overlap_div2_latest.html`
- `logs/weather_c0_smoketest_pipeline_summary_20260223_032020.json`
- `logs/weather_velon_smoketest_pipeline_summary_20260223_042838.json`
- `logs/weather_7acct_auto_consensus_watchlist_velon_latest.json`
- `logs/weather_7acct_auto_consensus_watchlist_velon_packlike_latest.json`
- `logs/weather_7acct_auto_consensus_snapshot_velon_packlike_latest.html`
- `logs/weather_roan_smoketest_pipeline_summary_20260223_043641.json`
