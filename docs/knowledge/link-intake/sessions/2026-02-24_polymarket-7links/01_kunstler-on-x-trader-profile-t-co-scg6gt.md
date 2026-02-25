# Link Note 01

- source_url: `https://x.com/kunst13r/status/2022707250956243402`
- source_type: `x_status`
- confidence: `medium`
- capture_gaps:
  - image card content not OCR-transcribed

## captured_claims

- The post links to a concrete Polymarket profile (`@k9Q2mX4L8A7ZP3R`) and wallet string (`0xd0d6053c3c37e727402d84c14069780d360993aa`).
- The message is pointer-style (identity reference), not a full strategy explanation.

## actionable_rules

- Treat profile URL + wallet mention as seed identifiers for observe-only autopsy workflows.
- Always verify that profile resolution and wallet string agree before downstream analysis.
- Do not infer trading edge from this link alone; use it only for entity seeding.

## required_data

- Intake extraction evidence (`logs/link_intake_20260224_7links.json`).
- Resolved wallet output from `extract_link_intake_users.py` / `run_link_intake_cohort.py`.
- Trade history from Data API for the resolved wallet.

## entry_exit_logic

- Not provided in this source.

## risk_notes

- Identity links can contain abbreviated handles in social text.
- OCR gaps can hide additional qualifiers in image cards.

## observe_only_test_plan

- Run one-shot seed-to-cohort:
  - `python scripts/run_link_intake_cohort.py logs/link_intake_20260224_7links.json --profile-name linkseed_7links --min-confidence medium --max-trades 2500 --pretty`
- Confirm that extracted users include the referenced wallet and cohort output is reproducible.

## open_questions

- Whether this wallet is the full strategy source or only one account pointer is not stated.

Evidence artifacts:
- `logs/link_intake_raw_20260224_123814/01.txt`
- `logs/link_intake_20260224_7links.json`
