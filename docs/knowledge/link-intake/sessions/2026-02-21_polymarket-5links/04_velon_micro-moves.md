# Link 04: velon Micro-Move Mechanics

Source URL:
- `https://x.com/velonxbt/status/2024075034185142683`

Capture status:
- `medium`

Captured points:
- Many 2028 longshot YES positions in sub-2c range.
- Claimed behavior: sells more frequently than buys (exit-focused).
- Claimed edge: news/event-driven small price pops captured repeatedly.
- Explicit caveat: liquidity lock and unrealized-PnL fragility.

Known gaps:
- `image_text_not_ocr`

Evidence artifacts:
- `logs/link_intake_raw_20260221_050409/04.txt`
- `logs/memo0221_intake.json`

Implementation notes (observe-only):
- Required preconditions before strategy ranking:
  - depth and spread monitor per token
  - slippage model under target size
  - exit-latency metrics
  - capital-lock risk flags
- Keep this as separate module from weather strategy.

Open questions:
- What minimum depth threshold prevents trapped inventory in practice?
