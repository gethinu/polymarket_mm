# Link 05

source_url:
- `https://x.com/antpalkin/status/2024129354574254410`

captured_claims:
- Describes a market analysis tool that surfaces top holders, share concentration, and wallet-level PnL context.
- Intended workflow is market URL input and report generation (terminal + HTML/charts).
- Targeted follow-up captured repository link:
- `https://github.com/cvxv666/polyclaudescraper`

actionable_rules:
- Treat this as an upstream analytics/pre-filter component.
- Extract holder concentration and high-PnL wallet alignment as model features.
- Use output as ranking/gating signal before any trade candidate enters execution path.

required_data:
- Market holder distributions by outcome.
- Wallet-level historical PnL/trade behavior.
- Position-size versus PnL relationship metrics.
- Report output artifacts (CSV/JSON/HTML) for pipeline ingestion.

entry_exit_logic:
- This source does not provide direct entry/exit rules.
- Practical role is pre-trade screening:
- allowlist/denylist markets based on holder and wallet-behavior diagnostics

risk_notes:
- Tool quality, data freshness, and methodology are not audited in this intake.
- Image-heavy tutorial sections are partially missing due no OCR (`image_text_not_ocr`).
- Copying whale footprints can introduce crowding and delayed-entry risk.

observe_only_test_plan:
- Run the referenced tool on a fixed sample of markets.
- Save outputs under `logs/` and compare derived signals against subsequent market movement.
- Evaluate feature stability across categories (crypto, politics, weather).
- Keep this stage strictly non-execution while calibrating feature usefulness.

open_questions:
- Exact schema of generated CSV/JSON output is not specified in extracted text.
- No benchmark or accuracy test for "smart money radar" signals is provided.
- Need reproducible criteria for "best traders" versus noisy high-PnL outliers.
