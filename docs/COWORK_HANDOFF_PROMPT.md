# Cowork handoff prompt — no-longshot restart (paste into a Cowork session)

Copy everything in the block below into a new Cowork/Claude Code session that is
running **on the Windows machine that holds the Polymarket keys** (this repo +
`PM_PRIVATE_KEY_DPAPI_FILE` / `PM_FUNDER`). The cloud session cannot do this part
(no keys, ephemeral container).

---

```
You are operating the polymarket_mm repo on my Windows machine. Context: the
project was idle ~3.5 months and was just re-baselined (see
docs/llm/STRATEGY.md "## Re-baseline (2026-06-24)"). The active core is the 4
ADOPTED strategies; all others are FROZEN. Goal of THIS session: bring the
ADOPTED observe runners back online, reset the no-longshot capital gate cleanly,
and get to the point where a gated tiny-size live entry is possible — WITHOUT
placing any live order yet.

Authoritative runbook: docs/RESTART_RUNBOOK.md. Follow it. Operating rules:

1. SAFETY: observe-only by default. Never pass --execute / --confirm-live YES /
   -LiveExecute unless I explicitly tell you to in this session. Never print or
   echo secrets or env values. Keep all runtime output under logs/.
2. First, environment sanity:
   - python -m pip install -r requirements.txt
   - confirm PM_PRIVATE_KEY_DPAPI_FILE and PM_FUNDER are set (presence only — do
     NOT print values).
   - python -m pip install -r requirements-dev.txt ; python -m pytest -q
     (expect ~344 passed, 7 skipped; report if not).
3. Reset the gate so the re-baseline is real:
   - Remove-Item logs/strategy_gate_alarm_state.json -ErrorAction SilentlyContinue
   - Remove-Item logs/no_longshot_realized_daily.jsonl, logs/no_longshot_forward_positions.json -ErrorAction SilentlyContinue
4. Start the no-longshot observe daily (no orders) and put it on a daily schedule:
   - run scripts/run_no_longshot_daily_report.ps1 in observe mode (see runbook §1)
   - scripts/set_no_longshot_daily_mode.ps1 -NoBackground -Mode task
5. Start the other 3 ADOPTED observe runners (weather / event_driven / gamma)
   per runbook §5 (observe-only). Use persistent terminals or scheduled tasks.
6. Wire the gate check + notification per runbook §6:
   - python scripts/render_strategy_register_snapshot.py --pretty
   - python scripts/check_strategy_gate_alarm.py --pretty
   - if a Discord webhook env is set, also run it with --discord.
   - confirm it reports PENDING / remaining_days ~35 / rolling_30d_resolved_trades=0.
7. Report back: which runners are live, the gate snapshot, test result, and any
   errors. Then STOP and wait. Do not escalate to live.

Re-baseline / gate facts: the prior 21/30 progress and the 2026-03-02 judgment
date are VOID. The gate (rolling_30d_resolved_trades >= 30, new-condition basis)
restarts from 0 on fresh data. Live escalation only after the gate is freshly met
AND I confirm in a later session. Expectation: on the $60 bankroll this is
~$1–5/month gross paper even if the edge holds — the point now is to prove the
edge survives live fills at micro size, not to make money yet.
```

---

After it finishes step 7, the live go/no-go is a separate later session: only when
`rolling_30d_resolved_trades >= 30` is freshly met do you run the gated live
command in docs/RESTART_RUNBOOK.md §3 (`-LiveExecute -LiveConfirm YES`, 1 order /
5 shares / $10 daily / NO ≤ 0.84).
