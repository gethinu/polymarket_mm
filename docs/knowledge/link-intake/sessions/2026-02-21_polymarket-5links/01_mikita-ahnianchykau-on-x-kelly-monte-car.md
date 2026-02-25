# Link 01

source_url:
- `https://x.com/mikita_crypto/status/2024492068647600546`

captured_claims:
- Kelly sizing should be adapted for binary prediction markets where payout is 1 and loss is full stake.
- Suggested sizing variants are full/half/quarter Kelly, plus Monte Carlo-capped Kelly variants.
- Portfolio process is EV-driven allocation with leftover cash held unallocated.
- Validation guidance is to invalidate strategies with randomized chunk testing, not one long backtest.

actionable_rules:
- Trade only when model edge is positive versus implied probability.
- Default to fractional Kelly in production candidates (half or quarter) before full Kelly.
- Add a leverage/exposure cap layer on top of Kelly output.
- Evaluate robustness using randomized windows and Monte Carlo outcome distributions.

required_data:
- Market implied probabilities by timestamp.
- Strategy probability estimates per market.
- Historical return series for volatility and drawdown estimation.
- Position-level PnL and exposure history for Kelly stress tests.

entry_exit_logic:
- Entry: positive EV and non-trivial edge over market probability.
- Size: bankroll fraction from fractional Kelly after global cap.
- Exit: hold to resolution or use pre-defined z-score/volatility-based de-risk rule.

risk_notes:
- Formula details in embedded images are not OCR-extracted (`image_text_not_ocr`).
- Kelly is highly sensitive to probability estimation error.
- Monte Carlo setup details are partially image-based, so replication can drift.

observe_only_test_plan:
- Replay recent binary markets with three sizing modes: full/half/quarter Kelly.
- Run Monte Carlo resampling over randomized windows and record tail drawdowns.
- Compare CAGR proxy, max drawdown, and risk-adjusted expectancy.
- Keep all outputs in `logs/` and avoid live orders.

open_questions:
- Exact z-score exit scaling formula from image content is still missing.
- Exact lookback window used for volatility input is not explicitly specified.
- Whether "Cap 3" is leverage cap or notional cap in their implementation remains unclear.
