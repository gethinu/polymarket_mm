# Link Note 02

- source_url: `https://polymarket.com/@k9Q2mX4L8A7ZP3R`
- source_type: `polymarket_profile`
- confidence: `medium`
- capture_gaps:
  - wallet address not directly visible in extracted profile text

## captured_claims

- Profile page shows active/high-frequency participation with many short-window crypto up/down markets.
- Snapshot metrics include Positions Value / Biggest Win / Predictions and recent BTC/ETH up/down exposures.

## actionable_rules

- Use profile URL as canonical seed and resolve wallet via observe-only resolver before trade analysis.
- Keep profile snapshots as context only; derive behavioral conclusions from fetched trade history, not page cards.
- Route resolved wallet into cohort/autopsy pipeline with reproducible parameters.

## required_data

- Profile URL from intake.
- Wallet resolution metadata (`resolved_via=profile` expected).
- Wallet trade history (`analyze_trader_cohort.py` input).

## entry_exit_logic

- Not explicitly defined by the profile page itself.
- Must be inferred from trade history timing/price distributions.

## risk_notes

- Profile cards can emphasize selected outcomes and omit execution context.
- Without wallet resolution, profile-only analytics can be non-reproducible.

## observe_only_test_plan

- Run:
  - `python scripts/run_link_intake_cohort.py logs/link_intake_20260224_7links.json --profile-name linkseed_7links --min-confidence medium --max-trades 2500 --pretty`
- Verify:
  - `logs/linkseed_7links_link_intake_users_latest.txt`
  - `logs/linkseed_7links_link_intake_cohort_latest.json`
  - `logs/linkseed_7links_link_intake_summary_latest.json`

## open_questions

- Whether this account behavior is stable across longer windows (>2500 trades) remains to be tested.

Evidence artifacts:
- `logs/link_intake_raw_20260224_123814/02.txt`
- `logs/link_intake_20260224_7links.json`
