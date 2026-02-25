# Link Note 01

- source_url: `https://x.com/SynthdataCo/status/2021658564109234501`
- source_type: `x_status`
- confidence: `high`
- capture_gaps:
  - image figures not OCR-transcribed

## captured_claims

- A Synth-powered bot can compare model probabilities vs Polymarket contract prices and execute quickly.
- Short-end hourly up/down contracts around `85c` may still be mispriced under model estimates.
- Claimed edge examples exceed `10%` in some observations.

## actionable_rules

- Define explicit edge threshold `model_prob - market_price`.
- Gate by time-to-expiry to avoid uncontrolled horizon drift.
- Keep claims in observe-only mode until net edge survives cost assumptions.

## required_data

- Synth forecast probability snapshots (timestamped).
- Polymarket price/liquidity snapshots (timestamped).
- Settlement outcomes and fee/slippage assumptions.

## entry_exit_logic

- Candidate entry when model edge exceeds threshold near expiry.
- Sizing via fractional Kelly or fixed stake ceiling.
- Settlement-based realized PnL accounting.

## risk_notes

- Forecast/model drift risk and potential non-stationarity.
- Short-window costs can dominate gross edge.
- Source is promotional and should not be treated as validated performance evidence.

## observe_only_test_plan

- Capture model-vs-market deltas in an offline log.
- Run closed-market high-probability calibration:
  - `python scripts/report_hourly_updown_highprob_calibration.py --assets bitcoin,ethereum --hours 72 --tte-minutes 20 --price-min 0.80 --price-max 0.95 --max-trades-per-market 3000 --pretty`
- Replay fee-adjusted expectancy by edge bucket.
- Require stability over multi-day sample before any operational promotion.

## open_questions

- Exact model update cadence and API schema were not provided.
- No reproducible trade ledger was linked in the post.

Evidence artifacts:
- `logs/link_intake_20260224_link5_retry.json`
- `logs/link_intake_raw_20260224_135704/01.txt`
