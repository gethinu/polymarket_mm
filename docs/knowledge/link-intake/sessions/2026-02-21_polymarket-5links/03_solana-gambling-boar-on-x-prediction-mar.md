# Link 03

source_url:
- `https://x.com/bored2boar/status/2024129294889300244`

captured_claims:
- Core thesis: prediction markets reward information processing speed plus execution quality.
- Highlights recurring micro-edge categories: arbitrage, latency edge, volatility compression, mean reversion.
- Recommends wallet-level behavioral analysis as a "copy-trading 2.0" input.
- Quoted context references link 04 and was ingested separately in this same session.

actionable_rules:
- Treat this source as directional thesis, not direct executable logic.
- Build observability first: latency to repricing, orderbook imbalance, wallet concentration shifts.
- Use wallet-tracking signals as ranking features, not sole trigger conditions.

required_data:
- Event/news timestamp feed with source credibility labels.
- Tick-level or frequent orderbook snapshots.
- Wallet-level position and PnL proxies for specialist traders.
- Market resolution metadata to tie signal quality to realized outcomes.

entry_exit_logic:
- No explicit numeric trigger in this source.
- Practical use: pre-trade filter layer and signal prioritization layer only.

risk_notes:
- High-level narrative, promotional framing, and no concrete thresholds.
- Image content not OCR-extracted (`image_text_not_ocr`).
- Original quote marker existed; context was supplemented by separate capture of link 04.

observe_only_test_plan:
- Run an observe pipeline that logs:
- signal arrival time
- market repricing delay
- orderbook imbalance at signal time
- Measure whether faster reaction windows correlate with higher expected edge.
- Do not enable execution from this source alone.

open_questions:
- No hard entry/exit thresholds were provided.
- "Smart money" definition (PnL window, minimum sample size, niche filter) is not specified.
- Contribution of each proposed edge type is not quantified.
