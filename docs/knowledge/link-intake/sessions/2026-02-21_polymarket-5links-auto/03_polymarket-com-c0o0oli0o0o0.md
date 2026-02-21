# Link 03

Source URL:
- `https://polymarket.com/@c0O0OLI0O0O0`

Capture status:
- `low` (fallback capture via wallet evidence)

Automated capture facts:
- source_type: `polymarket_profile`
- fetched_via: `wallet_fallback` (direct profile fetch failed)
- title: `Profile URL unstable; wallet fallback evidence captured`
- published_time: `(none)`
- word_count: `0` (profile text not captured)
- char_count: `0` (profile text not captured)

Captured points:
- Profile mapping observed in fallback artifacts:
  - `proxyAddress/baseAddress/primaryAddress = 0xfedc381bf3fb5d20433bb4a0216b15dbbc5c6398`
- Trade history can be fetched via Data API using the wallet above when profile URL is unstable.
- Refreshed wallet fetch (2026-02-21) succeeded:
  - `trade_count=479` (all markets, observe-only)
- Autopsy summary indicates short-horizon, high-turnover behavior:
  - `classification=SNIPER_TIMING`
  - `time_profitable_pct=95.43`
  - `buy_notional=58034.5911`
  - `sell_notional=132814.0090`
  - `yes_avg_entry=0.0025523` on `319620.67` shares

Known gaps:
- Direct profile page extraction unstable on capture date (`404/timeout`).
- No current screenshot/text capture from the profile UI itself.

Evidence artifacts:
- `logs\memo0221_intake_auto.json`
- `logs\memo0221_intake_auto.md`
- `docs\knowledge\link-intake\sessions\2026-02-21_polymarket-5links\03_c0_profile_wallet.md`
- `logs\profile_raw.html`
- `logs\memo0221_c0_trades.json`
- `logs\memo0221_c0_summary.json`
- `logs\c0_wallet_trades_20260221.json`
- `logs\c0_wallet_trades_20260221_summary.json`

Implementation notes (observe-only):
- Prefer wallet-based trade evidence when profile URLs are unstable.
- Treat this actor as a candidate benchmark for micro-move execution styles.
- Keep all analysis observe-only (no execution assumptions from social/profile metadata).

Open questions:
- Can we robustly resolve profile handle -> wallet without relying on fragile page HTML?
- Should intake pipeline auto-fallback to Data API wallet-centric capture for `polymarket_profile` failures?
