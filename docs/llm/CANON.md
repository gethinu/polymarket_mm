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
   - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_no_longshot_daily_report.ps1 -NoBackground -SkipRefresh -StrictRealizedBandOnly -RealizedFastYesMin 0.16 -RealizedFastYesMax 0.20 -RealizedFastMaxHoursToEnd 72 -RealizedFastMaxPages 120`
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

Core KPI authority (primary):

- `kpi_core.daily_realized_pnl_usd`
- `kpi_core.monthly_return_now_text`
- `kpi_core.max_drawdown_30d_text`

Compatibility/diagnostic keys (secondary):

- `no_longshot_status.monthly_return_now_text`
- `no_longshot_status.monthly_return_now_source`
- `no_longshot_status.monthly_return_now_new_condition_text`（新条件専用表示）
- `no_longshot_status.monthly_return_now_all_text`（全体比較値）
- `no_longshot_status.rolling_30d_monthly_return_text`
- `realized_30d_gate.decision`

## 5. Weather 24h Alarm Flow

Set/cancel alarm (preferred local waiter):

- Set: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/set_weather_24h_alarm.ps1`
- Cancel: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/cancel_weather_24h_alarm.ps1`

When alarm fires:

1. `scripts/run_weather_24h_alarm_action.ps1` appends alarm history and marker.
2. `scripts/run_weather_24h_postcheck.ps1` runs follow-up usefulness check.
3. `python scripts/render_strategy_register_snapshot.py --pretty` is refreshed by alarm action.
4. Read:
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
- Preferred one-shot:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_weather_24h_postcheck.ps1 -NoBackground -AutoRecheckOnSamplesShortfall`
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

Preferred mode switch command (enforces task/daemon/off consistently):

- Task mode: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/set_no_longshot_daily_mode.ps1 -NoBackground -Mode task`
- Daemon mode: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/set_no_longshot_daily_mode.ps1 -NoBackground -Mode daemon`
- Off mode: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/set_no_longshot_daily_mode.ps1 -NoBackground -Mode off`
- `-Mode task` / `-Mode off` は、repo配下で実行中の `no_longshot_daily_daemon.py`（pythonプロセス）も停止して片系運用を強制する。

## 9. Pending Release Runbook (gamma event-pair)

Use this flow for `PENDING` strategy release monitoring and manual promotion.

1. Scheduled monitoring (read-only, thin wrapper):
   - Base monitor (higher frequency):
     - `python scripts/run_pending_release_alarm_batch.py --strategy gamma_eventpair_exec_edge_filter_observe`
   - Conservative monitor (lower frequency):
     - `python scripts/run_pending_release_alarm_batch.py --strategy gamma_eventpair_exec_edge_filter_observe --run-conservative`
2. Optional transition notification:
   - `python scripts/run_pending_release_alarm_batch.py --strategy gamma_eventpair_exec_edge_filter_observe --discord`
3. Scheduler exit policy:
   - treat `0` as normal (includes `HOLD` / `NOOP` / `RELEASE_READY`)
   - treat `4` (lock busy) as warning; use `--fail-on-lock-busy` only when strict failure is required
   - treat `20` as hard error
4. When transition to release-ready is detected:
   - Re-check directly with checker JSON:
     - `python scripts/check_pending_release.py --strategy gamma_eventpair_exec_edge_filter_observe --pretty`
   - Confirm `release_check == "RELEASE_READY"` and `release_ready == true` from JSON payload.
5. Manual promotion only (no auto-apply in alarm wrapper):
   - `python scripts/check_pending_release.py --strategy gamma_eventpair_exec_edge_filter_observe --apply --pretty`
6. Post-apply verification:
   - `python scripts/render_strategy_register_snapshot.py --pretty`
   - Verify status in `logs/strategy_register_latest.json`.
   - If code/docs changed, run `python scripts/render_implementation_ledger.py`.

Notes:
- `check_pending_release.py` exit code `0` includes both `NOOP` and `RELEASE_READY`; automation must branch on JSON `release_check` / `release_ready`.
- `check_pending_release_alarm.py` is read-only and uses `logs/pending_release_alarm.lock` to avoid concurrent run overwrite.
- `--discord` 指定時の既定 webhook env は `POLYMARKET_PENDING_RELEASE_DISCORD_WEBHOOK`。`--discord-webhook-env` 指定時は override を優先する。
- webhook 未設定/送信失敗は通知経路の問題として扱い、release 判定の exit code を変更しない。
