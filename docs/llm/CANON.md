# Operations Canon (Single Source)

This file is the one operational canon for multi-chat / multi-session work.

## Objective

Keep strategy status, KPI reading, and run-control decisions consistent across chats.

## Canon Rules

1. Read current KPI/status from generated snapshot only:
   - `logs/strategy_register_latest.json`
   - Viewer: `logs/strategy_register_latest.html`
2. Do not hand-edit KPI numbers in markdown.
3. Strategy policy text and gates are maintained in:
   - `docs/llm/STRATEGY.md`
4. Interface/flags truth is maintained in:
   - `docs/llm/INTERFACES.md`
5. Long-lived runtime state definitions are maintained in:
   - `docs/llm/STATE.md`

## Monthly Return Authority

Use only:
- `no_longshot_status.monthly_return_now_text`
- `no_longshot_status.monthly_return_now_source`

from `logs/strategy_register_latest.json`.

## Update Sequence (Required)

1. Refresh no-longshot report:
   - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -NoBackground -SkipRefresh`
2. Rebuild strategy register snapshot:
   - `python scripts/render_strategy_register_snapshot.py --pretty`
3. Then report KPI from `logs/strategy_register_latest.json`.

## Duplicate-Run Guard

Use one mode for no-longshot, not both:
- `NoLongshotDailyReport` scheduled task, or
- `scripts/no_longshot_daily_daemon.py`

If daemon is active, keep `NoLongshotDailyReport` disabled.
