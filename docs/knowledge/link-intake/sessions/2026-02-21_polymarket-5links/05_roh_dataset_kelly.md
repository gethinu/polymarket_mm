# Link 05: RohOnChain Dataset and Empirical Kelly

Source URL:
- `https://x.com/RohOnChain/status/2023781142663754049`

Capture status:
- `medium`

Captured points:
- Long-form post on prediction-market dataset usage and institutional-style risk sizing.
- Includes setup flow for large historical dataset and tooling.
- Emphasizes uncertainty-aware sizing (empirical/Monte-Carlo-adjusted Kelly).

Known gaps:
- `image_text_not_ocr`

Evidence artifacts:
- `logs/link_intake_raw_20260221_050409/05.txt`
- `logs/memo0221_intake.json`

Implementation notes (observe-only):
- Treat this source as methodology layer (research + sizing), not direct entry signal.
- Require reproducible dataset pipeline and parameter traceability before adoption.
- Integrate as simulator-only first; validate drawdown behavior before any promotion.

Open questions:
- Which subset of this methodology is practical with current repo data/compute constraints?
