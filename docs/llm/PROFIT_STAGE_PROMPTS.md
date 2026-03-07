# Profit-Stage Prompts (2026-03-01)

Use these prompts when you want Codex to push strategies from observe/testing into practical profit-ready operation with explicit gates.

## Common System Prompt (prepend to every run)

```
You are operating in C:\Repos\polymarket_mm.
Follow docs/llm/CANON.md first, then read KPI/status only from logs/strategy_register_latest.json.
Default to observe-only unless I explicitly request live execution.
Never print secrets.
For every strategy decision, output: current_state, gate_check, blockers, next_actions, go_no_go.
```

## Prompt A: No-Longshot Gate-To-Live (priority #1)

```
Goal: move no_longshot_daily_observe from observe-only to tiny staged live if and only if gates are met.

Do this:
1) Execute CANON daily refresh sequence (1->4) exactly.
2) Read authority keys from logs/strategy_register_latest.json.
3) Judge gate with these conditions:
   - no_longshot_status.rolling_30d_resolved_trades >= 30
   - realized_30d_gate.decision_3stage == READY_FINAL
   - check_strategy_gate_alarm current_capital_gate_core == ELIGIBLE_REVIEW
4) If any condition fails:
   - keep observe-only
   - output exact missing conditions and remaining gap
5) If all pass:
   - run one tiny live pass only:
     powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -NoBackground -StrictRealizedBandOnly -RealizedFastYesMin 0.16 -RealizedFastYesMax 0.20 -RealizedFastMaxHoursToEnd 72 -RealizedFastMaxPages 120 -LiveExecute -LiveConfirm YES -LiveMaxOrders 1 -LiveOrderSizeShares 5 -LiveMaxDailyNotionalUsd 10 -LiveMaxOpenPositions 10 -LiveMaxEntryNoPrice 0.84 -LivePriceBufferCents 0.2
   - then refresh snapshot and report post-run KPI/status.

Output format:
- gate_status
- unmet_conditions (if any)
- executed_commands
- post_run_kpi
- recommendation (continue staged live / revert observe-only)
```

## Prompt B: Pending Strategy Graduation Sweep (priority #2)

```
Goal: evaluate all PENDING strategies for promotion readiness using conservative cost assumptions.

Strategies:
- social_profit_claim_validation_observe
- btc_shortwindow_yesno_arb_observe
- btc_shortwindow_panic_observe
- btc_shortwindow_lag_observe
- copytrade_latency_sim_observe

For each strategy:
1) run/refresh its latest observe artifacts
2) compute whether evidence is sufficient for practical profit stage
3) classify as one of:
   - HOLD_PENDING
   - READY_SHADOW_LIVE (tiny-size paper/live-sim equivalent)
   - READY_PROMOTION (to ADOPTED observe-first)
4) provide strict blockers in measurable terms (sample count, expectancy after costs, drawdown bounds, latency robustness).

Output as a table:
strategy_id | classification | key_metrics | blockers | concrete_next_step
```

## Prompt C: Registry Hygiene For Profit Focus (priority #3)

```
Goal: keep strategy register focused on direct profit-making strategies.

Do this:
1) move non-trading support pipelines out of counted strategy register entries
2) keep them documented as auxiliary pipelines
3) regenerate logs/strategy_register_latest.json and logs/strategy_register_latest.html
4) report ADOPTED/PENDING/REJECTED counts before and after

Do not change live execution settings in this step.
```
