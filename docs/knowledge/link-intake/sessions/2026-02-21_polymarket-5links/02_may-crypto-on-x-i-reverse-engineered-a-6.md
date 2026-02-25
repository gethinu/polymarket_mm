# Link 02

source_url:
- `https://x.com/xmayeth/status/2024509163632533826`

captured_claims:
- Describes a binary sports arbitrage bot using two pair structures:
- `YES(A)+YES(B) < payout_after_fee`
- `NO(A)+NO(B) < payout_after_fee`
- Claims scan cadence of 30-60 seconds with execution in near-simultaneous paired orders.
- Recommends FOK-style protection so one-sided fills do not leave directional exposure.
- Referenced wallet URL captured by targeted follow-up:
- `https://polymarket.com/@0xD9E0AACa471f48F91A26E8669A805f2?via=maycrypto`

actionable_rules:
- Restrict universe to true binary matches for this pattern.
- Compute fee-adjusted threshold before candidate promotion.
- Require sufficient ask depth for both legs at target price.
- Submit paired orders with cancel/unwind path on partial fill.

required_data:
- Per-token top-of-book and depth snapshots.
- Market metadata to confirm binary structure and sports filtering.
- Fee model actually applied on settlement/trade path.
- Fill reconciliation data to detect partial execution risk.

entry_exit_logic:
- Entry: pair cost + slippage + fixed cost is below fee-adjusted payout.
- Execution: paired buy attempt on both legs, atomic-like behavior via FOK/reconciliation.
- Exit: hold to resolution for pure arb, or immediate flatten on failed pair completion.

risk_notes:
- Performance figures are third-party claims and not independently verified in this intake.
- Image panels may include omitted constraints (`image_text_not_ocr`).
- Draw/no-contest and resolution edge cases can break naive binary assumptions.

observe_only_test_plan:
- Run `polymarket_clob_arb_realtime.py` in observe mode with pair strategy.
- Log raw candidate edge and slippage-adjusted edge separately.
- Simulate paired fill probability using observed depth and stale-book checks.
- Track expected edge distribution and invalid-candidate reasons.

open_questions:
- Exact fee assumptions behind the `98c` threshold need venue-level confirmation.
- Whether quoted examples include draw markets or strict two-outcome events is not explicit.
- No primary source audit of the wallet PnL claim was included in this URL alone.
