# Operations Canon (Single Source)

This file is the operator runbook for multi-chat and multi-session work.

It defines the workflow. It does not redefine schemas, flags, or runtime file contracts.

## 1. Authority

Always treat these as authoritative, in this order:

1. `docs/llm/SPEC.md`
2. `docs/llm/ARCHITECTURE.md`
3. `docs/llm/INTERFACES.md`
4. `docs/llm/STATE.md`
5. `docs/llm/CANON.md`
6. `docs/llm/STRATEGY.md`

If conflict is found, stop implementation and resolve docs first.

## 2. Single Status Surface

All strategy status and KPI readings must come from generated artifacts:

- Machine source: `logs/strategy_register_latest.json`
- Human viewer: `logs/strategy_register_latest.html`

Do not hand-edit KPI values in markdown files.

## 3. Required Daily Refresh Sequence

Run in this order before making status claims:

1. Refresh no-longshot daily summary:
   - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -NoBackground -SkipRefresh`
2. Refresh realized snapshot used by register:
   - `python scripts/record_simmer_realized_daily.py --pretty`
3. Rebuild strategy register snapshot:
   - `python scripts/render_strategy_register_snapshot.py --pretty`
4. Refresh gate transition alarm state/log:
   - `python scripts/check_strategy_gate_alarm.py --pretty`
5. If code/docs changed, refresh implementation ledger:
   - `python scripts/render_implementation_ledger.py`

Then read/report KPI only from `logs/strategy_register_latest.json`.

## 4. KPI Authority Keys

Monthly return authority:

- `no_longshot_status.monthly_return_now_text`
- `no_longshot_status.monthly_return_now_source`

Also report when needed:

- `no_longshot_status.rolling_30d_monthly_return_text`
- `realized_30d_gate.decision`

## 5. Weather 24h Alarm Flow

Set/cancel alarm (preferred local waiter):

- Set: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/set_weather_24h_alarm.ps1`
- Cancel: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/cancel_weather_24h_alarm.ps1`

When alarm fires:

1. `scripts/run_weather_24h_alarm_action.ps1` appends alarm history and marker.
2. `scripts/run_weather_24h_postcheck.ps1` runs follow-up usefulness check.
3. Read:
   - `logs/weather24h_postcheck_latest.json`
   - `logs/weather24h_postcheck_latest.txt`

If alarm time has passed but no notification arrived, run manual postcheck:

- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_24h_postcheck.ps1 -NoBackground -Hours 24`

## 6. Usefulness and Adoption Gate

For weather 24h postcheck default gate:

- `Samples >= 300`
- `positive_0c_pct >= 30.0`
- `positive_5c_pct >= 10.0`

Decision handling:

- `ADOPT`: keep strategy as `ADOPTED` in `docs/llm/STRATEGY.md`
- `REVIEW`: keep observe-only and do not promote
- `REJECTED`: remove from active operation

Sample-shortfall handling (`gate_reasons` contains `samples_below_min`):

- Treat as evidence-insufficient `REVIEW`, not immediate reject.
- Keep observe process running and recheck later.
- Recommended recheck alarm (separate waiter state so existing alarm is not overwritten):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/set_weather_24h_alarm.ps1 -AlarmAt "<local datetime>" -Message "Weather usefulness gate recheck (samples>=300?)" -WaiterStateFile logs/weather24h_gate_recheck_waiter_state.json -LogFile logs/alarm_weather24h_gate_recheck.log -MarkerFile logs/alarm_weather24h_gate_recheck.marker`

## 7. Strategy Change Protocol

When changing strategy status/policy:

1. Update `docs/llm/STRATEGY.md` first.
2. Regenerate snapshot:
   - `python scripts/render_strategy_register_snapshot.py --pretty`
3. Confirm reflected status in `logs/strategy_register_latest.json`.
4. Refresh implementation ledger if code/docs changed:
   - `python scripts/render_implementation_ledger.py`

## 8. Duplicate-Run Guard

No-longshot must use one mode only:

- Scheduled task: `NoLongshotDailyReport`
- Daemon: `scripts/no_longshot_daily_daemon.py`

If daemon is active, keep scheduled task disabled.
