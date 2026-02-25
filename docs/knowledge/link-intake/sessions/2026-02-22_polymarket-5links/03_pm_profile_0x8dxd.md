# Link Note 03

- source_url: `https://polymarket.com/@0x8dxd?via=deniskursakov`
- source_type: `polymarket_profile`
- confidence: `high`
- resolved_wallet: `0x63ce342161250d705dc0b16df89036c8e5f9ba9a`

## captured_claims

- Profile `@0x8dxd` maps to one proxy/base/primary wallet address.
- Profile hydration snapshot includes high trade count and high aggregate volume/PnL fields.
- Activity feed contains many short-window crypto up/down trades.

## actionable_rules

- Use this wallet as an observe-only reference signal source.
- Continuously fetch and classify trades by slug family (`btc-updown-5m`, `btc-updown-15m`, etc.).
- Compare reference wallet activity timing against local candidate signals.

## required_data

- Data API trades by wallet and market/slug family.
- Condition-level holder tables for optional peer-quality scoring.
- Rolling window stats: side balance, avg entry price, inter-trade interval.

## entry_exit_logic

- Profile itself does not define executable logic.
- Logic must be inferred from time-series behavior and then validated out-of-sample.

## risk_notes

- Profile metrics are snapshots and can change rapidly.
- Public stats may mix realized/unrealized effects.
- Copying a wallet without latency model can underperform materially.

## observe_only_test_plan

- Pull latest trades periodically (read-only).
- Score market-family concentration and turnover metrics.
- Correlate wallet activity with strategy candidates before any execution discussion.

## open_questions

- Exact PnL accounting methodology for profile fields is not fully specified.
- Full historical position pages were not fully expanded in this capture.
