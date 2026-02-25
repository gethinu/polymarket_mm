# Link Intake Output Design

Purpose:
- Store link research outputs as reusable knowledge, not ad-hoc memos.

Directory layout:
- `docs/knowledge/link-intake/sessions/<date>_<topic>/00_overview.md`
- `docs/knowledge/link-intake/sessions/<date>_<topic>/01_<slug>.md`
- `docs/knowledge/link-intake/sessions/<date>_<topic>/02_<slug>.md`
- ...

Naming rules:
- Use date prefix in ISO form: `YYYY-MM-DD`.
- Keep topic short and stable (for example `polymarket-5links`).
- Keep per-link files prefixed with two-digit order from the user input.
- Keep slug length short enough for readable filenames (target <= 40 chars).

Per-link file schema:
- `Source URL`
- `Capture status` (`high|medium|low|none`)
- `Captured points`
- `Known gaps`
- `Evidence artifacts`
- `Implementation notes (observe-only)`
- `Open questions`

Session overview schema:
- Scope/date
- Link list with confidence
- Cross-link synthesis (after per-link completion)
- Prioritized next actions

Automation:
- `python C:\Users\stair\.codex\skills\polymarket-link-intake\scripts\intake_links.py --urls-file logs/link_queue.txt --session-topic polymarket-5links`
- This writes both `logs/` artifacts and canonical session docs under `docs/knowledge/link-intake/sessions/`.
