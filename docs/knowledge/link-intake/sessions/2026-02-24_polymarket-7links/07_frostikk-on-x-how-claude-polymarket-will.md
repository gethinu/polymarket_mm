# Link Note 07

- source_url: `https://x.com/frostikkkk/status/2015154001797390637`
- source_type: `x_status`
- confidence: `high`
- capture_gaps:
  - performance figures are not tied to verifiable wallet-level evidence in this URL
  - image content not OCR-transcribed

## captured_claims

- Claim: non-coders can generate Polymarket arbitrage/MM bots using Claude prompts.
- Claim: bots can find many opportunities daily and scale quickly.
- Claim: headline outcomes include `$500-$800/day`, `$75k/month`, and rapid bankroll compounding.

## actionable_rules

- Treat all headline PnL claims as unverified until checked against realized daily PnL artifacts.
- Convert narrative claims into measurable thresholds (`daily`, `rolling-30d`, `hourly*hours`) and evaluate with observe-only tooling.
- Do not adopt strategy variants based only on social text; require reproducible artifacts.

## required_data

- Realized daily PnL series (`logs/*realized*daily*.jsonl`).
- Observation window length (minimum 30 days for claim judgment).
- Optional operational context (bankroll baseline, active hours/day) for hourly-to-daily conversions.

## entry_exit_logic

- Not defined in source text.
- No concrete market selection, entry trigger, or exit discipline is provided.

## risk_notes

- Strong survivorship/selection bias risk in promotional performance posts.
- Gross-PnL narratives can omit costs, slippage, and dead-time.
- Overfitting risk is high when claims are reverse-engineered without primary trade logs.

## observe_only_test_plan

- Run: `python scripts/report_social_profit_claims.py --min-days 30 --pretty`.
- Require `SUPPORTED` status from artifact-backed metrics before considering any strategy adoption.
- Keep status as `PENDING` in strategy register when data coverage is insufficient.

## open_questions

- Which wallet/trade history backs the headline PnL figures is unspecified.
- Whether figures are realized, mark-to-market, gross, or net is unspecified.
- Active trading hours and risk constraints behind claims are unspecified.

Evidence artifacts:
- `logs/link_intake_raw_20260224_123814/07.txt`
- `logs/link_intake_20260224_7links.json`
