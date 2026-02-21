# Link 01: Aleiah Weather Tutorial

Source URL:
- `https://x.com/AleiahLock/status/2024049808055431356`

Capture status:
- `medium`

Captured points:
- Weather-focused trader examples and profile references.
- Claimed heuristic ranges (example: YES under 15c, NO above 45c in specific contexts).
- Candidate data/tool stack for forecasting and settlement prep (NOAA/Open-Meteo/Windy/Tropical Tidbits).

Known gaps:
- `image_text_not_ocr`

Evidence artifacts:
- `logs/link_intake_raw_20260221_050409/01.txt`
- `logs/memo0221_intake.json`

Implementation notes (observe-only):
- Build weather-market screener first for major settlement stations (NYC/London).
- Add multi-model agreement gate before signal acceptance.
- Keep position size micro and highly diversified.

Open questions:
- Which exact settlement station rules per market should be encoded first?
- What model-agreement threshold is robust across seasons?
