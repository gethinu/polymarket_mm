# Merged Strategy Layers (Post Per-Link Completion)

This file merges common strategy layers only after all five per-link notes were completed.
Execution mode remains observe-only unless explicitly changed by user request.

## Source Map

- `L1`: `01_mikita-ahnianchykau-on-x-kelly-monte-car.md`
- `L2`: `02_may-crypto-on-x-i-reverse-engineered-a-6.md`
- `L3`: `03_solana-gambling-boar-on-x-prediction-mar.md`
- `L4`: `04_solana-gambling-boar-on-x-7-ways-to-turn.md`
- `L5`: `05_cvxv666-on-x-smart-money-radar-for-polym.md`

## Merged Common Layers

### Layer A: Market Eligibility and Specialization

- Rule A1: Start from strict market eligibility (binary structure, resolution clarity, fee-awareness), then apply niche filters.
- Traceability: `L2`, `L4`.
- Implementation target: `scripts/polymarket_clob_arb_realtime.py` universe filters and strategy gates.

### Layer B: Edge Generation

- Rule B1: Keep two edge families separate:
- family 1: structural mispricing (sum-to-one pair arbitrage).
- family 2: model-based probabilistic EV edge.
- Traceability: `L1`, `L2`, `L3`.
- Implementation target: separate observe metrics streams for pair-arb edge and EV edge.

### Layer C: Position Sizing

- Rule C1: Use fractional Kelly by default, then apply hard notional/exposure caps.
- Rule C2: Require calibration testing before raising Kelly fraction.
- Traceability: `L1`; risk posture reinforced by `L4`.
- Implementation target: sizing module with mode switch (`quarter`, `half`, `full`) plus cap layer.

### Layer D: Execution Quality Controls

- Rule D1: Pair entries require depth checks on all legs before submission.
- Rule D2: Use atomic-like behavior (FOK/reconcile/cancel/unwind) to avoid one-sided inventory.
- Rule D3: Track stale-book and slippage-adjusted edge, not only raw edge.
- Traceability: `L2`, `L3`, `L4`.
- Implementation target: existing reconciliation and unwind paths in `scripts/polymarket_clob_arb_realtime.py`.

### Layer E: Information and Wallet Intelligence Filters

- Rule E1: Add pre-trade filters from wallet behavior and holder concentration.
- Rule E2: Treat wallet/whale signals as ranking features, not standalone triggers.
- Traceability: `L3`, `L4`, `L5`.
- Implementation target: integrate outputs from `scripts/fetch_trades.py` and `scripts/analyze_trades.py` into candidate scoring.

### Layer F: Validation Framework

- Rule F1: Validate by randomized chunks and Monte Carlo stress tests, not a single continuous window.
- Rule F2: Run separate observe experiments per strategy family to avoid attribution blur.
- Traceability: `L1`, `L4`.
- Implementation target: experiment runner and report schema under `logs/`.

## Explicit Disagreements / Tensions

- T1: `L2` emphasizes near-riskless structural arb; `L1` assumes uncertain probability estimation and sizing risk.
- Resolution: keep pair-arb and EV-model systems separated end-to-end.

- T2: `L3/L4` strongly prioritize speed; `L1` prioritizes calibration and robustness testing.
- Resolution: enforce pre-deployment robustness gate before any speed-focused optimization.

- T3: `L5` suggests whale-following intelligence; `L4` warns naive copying can fail.
- Resolution: require behavior-quality filters (stability, drawdown profile, niche consistency) before using wallet signals.

## Remaining Capture Gaps (Affecting Confidence)

- All five sources have `image_text_not_ocr`.
- Practical impact:
- exact formulas, chart scales, or hidden constraints inside images may be missing.
- Any numeric threshold from image-only content must be treated as unverified.

## Observe-Only Integration Order

1. Baseline pair-arb observe stream (raw edge + slippage-adjusted edge + fillability proxy).
2. Sizing sandbox with fractional Kelly over replayed data only.
3. Wallet-intelligence feature layer as additive ranking signal.
4. Unified reporting with per-layer attribution and conflict checks.

## Merge Output Status

- Per-link extraction: complete.
- Cross-link merge: complete.
- Ready for implementation planning with explicit traceability.
