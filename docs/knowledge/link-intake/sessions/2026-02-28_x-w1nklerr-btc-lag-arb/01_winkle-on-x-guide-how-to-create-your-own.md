# Link 01

source_url:
- `https://x.com/w1nklerr/status/2018453276279070952`

capture:
- confidence: `high`
- source_type: `x_post`
- fetched_via: `r_jina_ai`
- title: `winkle. on X: "Guide: How to Create Your Own ClawdBot That Can Earn $1,000 per Week" / X`
- published_time: `Sat, 28 Feb 2026 14:33:24 GMT`
- word_count: `557`
- char_count: `4038`

captured_claims:
- A Polymarket account allegedly earns by exploiting pricing/latency inefficiencies (vs “guessing direction”).
- The account allegedly opens “thousands of trades in a fraction of a second”, with “microscopic profits”.
- The described target market is “15-minute BTC up/down predictions” where Polymarket prices lag external signals.
- The post proposes a prompt-defined strategy:
  - Monitor BTC “price predictions” (15m forecasts) from sources like TradingView/CryptoQuant.
  - When prediction vs Polymarket contract price diverges by `>0.3%`, trade within `<100ms` before adjustment.
  - “Thousands of micro-trades per second” with `0.3–0.8%` per trade (claims; not independently verified).
- Risk controls claimed: `0.5%` capital per trade, `2%` daily loss cap, plus regime sizing (sideways vs trend).

actionable_rules (as described; needs verification/translation to concrete rules):
- Signal: `abs(external_prediction - polymarket_price) > 0.3%` (exact definition of “prediction” unspecified).
- Execution: minimize detection-to-trade latency (`<100ms` target).
- Sizing: cap per-trade risk at ~`0.5%` of capital; cap daily loss at ~`2%`.
- Throughput target: handle high order rate (claim: `1000+ orders/sec`).

required_data:
- Polymarket BTC up/down short-window market identification (event/market IDs per window).
- Polymarket CLOB pricing feed (best bid/ask + depth), ideally via WebSocket for latency.
- External BTC signal feed(s):
  - The post names TradingView + CryptoQuant, but does not specify endpoints, sampling rate, or semantics.
  - A minimal proxy for observe-only is external spot aggregation (e.g., Coinbase/Kraken/Bitstamp) + a model.
- Latency measurements (wall-clock) for: feed -> signal -> order submit -> ack/fill (if executing).
- Rate limit / throttling constraints for Polymarket API (not provided in source).

entry_exit_logic (inferred from post; underspecified):
- Entry: when divergence threshold trips, take the side that benefits if Polymarket “catches up” to external signal.
- Exit: implied “micro-trades” suggests quick convergence exit, but concrete TP/SL logic is not provided.
- Alternative interpretation: hold-to-settlement directional bet in short windows; conflicts with “micro-trades” claim.

risk_notes:
- Claims are marketing-style and not independently validated by this source (no audited PnL, no code, no fills).
- “1000+ orders/sec” + `<100ms` is likely constrained by network + venue throttles; may be infeasible/ToS risky.
- Any live trading implementation must consider slippage/adverse selection, fees, and API rate limits.
- This repo defaults to observe-only; do not implement live execution based solely on this post.

observe_only_test_plan:
- Use an observe-only monitor to measure lag/divergence frequency on BTC short windows and record metrics.
- Simulate paper entries with conservative fill + fee assumptions; track day/total PnL and win/loss.
- Compare multiple “external signal” proxies (spot median vs model-derived fair probability vs other feeds).
- Produce a dashboard that shows equity curve + event log for operator review.

open_questions:
- What exactly is “BTC prediction” here (spot, forecast, order-flow signal, or something else)?
- Which Polymarket market(s) are traded (single 15m window, rolling windows, multiple markets)?
- Is the strategy maker-style (posting) or taker-style (crossing) and how are fills measured?
- What are the actual rate limits / order throttles, and how is “1000+ orders/sec” achieved?
- Where is the evidence for the claimed return curve / win rate (screenshots are referenced but not captured here)?

evidence_artifacts:
- `logs\link_intake_raw_20260228_143218\01.txt`
- `logs\link_intake_20260228_143218.json`
- `logs\link_intake_20260228_143218.md`
- referenced_urls:
  - https://pbs.twimg.com/media/HAL7Ed-XwAE3tZH?format=png&name=small
  - https://x.com/w1nklerr/article/2018453276279070952/media/2018451637199683585
  - https://pbs.twimg.com/media/HAL7sYGWoAAkg8c?format=png&name=small
  - https://x.com/w1nklerr/article/2018453276279070952/media/2018452322817318912
