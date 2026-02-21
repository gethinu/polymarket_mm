# Link 03: c0O0 Profile and Wallet Evidence

Source URL:
- `https://polymarket.com/@c0O0OLI0O0O0`

Capture status:
- `none` (direct fetch unstable on 2026-02-21)

Captured points (fallback path):
- Profile HTML snapshot includes wallet mapping:
  - `proxyAddress/baseAddress/primaryAddress = 0xfedc381bf3fb5d20433bb4a0216b15dbbc5c6398`
- Trade history fetched from Data API by wallet.
- Wallet autopsy confirms high profitable-time ratio and short-horizon style.

Known gaps:
- Profile page text extraction unavailable at capture time (`404/timeout` instability).

Evidence artifacts:
- `logs/profile_raw.html`
- `logs/memo0221_c0_trades.json`
- `logs/memo0221_c0_summary.json`

Implementation notes (observe-only):
- Use wallet trades as primary behavior evidence when profile page fetch is unstable.
- Treat strategy as high-turnover micro-move harvesting, not hold-to-resolution.

Key observed stats (autopsy):
- `classification=SNIPER_TIMING`
- `time_profitable_pct=95.43`
- `buy_notional=58034.5911`
- `sell_notional=132814.0090`
- `yes_avg_entry=0.0025523` on `319620.67` shares

Open questions:
- Does profile UI currently expose additional fields not captured in fallback flow?
