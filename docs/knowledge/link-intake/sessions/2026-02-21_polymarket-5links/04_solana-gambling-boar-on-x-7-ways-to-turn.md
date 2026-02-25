# Link 04

source_url:
- `https://x.com/bored2boar/status/2020216249725395022`

captured_claims:
- Lists seven strategy families: news trading, niche dominance, dispute trading, copy trading, sniping, bonding, arbitrage.
- Strong recommendation to specialize by niche instead of broad cross-category trading.
- Emphasizes resolution-mechanics literacy for dispute/bond-like edges.
- States arbitrage edge is thin and automation-dependent.

actionable_rules:
- Convert this source into a strategy-selection checklist, not immediate order logic.
- For news/dispute strategies, enforce source verification and resolution-rule checks before signal acceptance.
- For copy strategies, prefer behavior filters (smooth curve, frequency, niche) over headline PnL.
- For arbitrage strategies, require automation and execution-quality metrics before deployment.

required_data:
- Source reliability feeds and timestamped news items.
- Market wording plus oracle/resolution history.
- Wallet performance curves and drawdown behavior.
- Execution telemetry: latency, fill quality, and slippage.

entry_exit_logic:
- No concrete numerical entry/exit formula is specified.
- Operational interpretation:
- entry only after strategy-specific preconditions are met
- exit via predefined drawdown/cooldown and invalidation logic

risk_notes:
- Content is mostly qualitative and motivational.
- Several referenced examples are image/video based and not OCR-extracted (`image_text_not_ocr`).
- Performance examples likely include survivorship and selection bias.

observe_only_test_plan:
- Create one observe experiment per strategy family rather than mixing all at once.
- Track per-family KPIs:
- signal precision proxy
- median hold time
- drawdown profile
- execution slippage proxy
- Keep cross-family A/B separated for clean attribution.

open_questions:
- No explicit thresholds for any of the seven strategies.
- No standardized definition of "winning wallets" in copy-trading section.
- Dispute and bonding examples lack concrete reproducible case IDs in extracted text.
