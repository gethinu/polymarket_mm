<INSTRUCTIONS>
This repository contains automation code that can place trades.

- Never ask the user to paste private keys / seed phrases / webhook URLs into chat.
- Never print secrets from environment variables.
- Default to observe-only modes unless the user explicitly requests live execution.
- Keep runtime outputs under `logs/` (gitignored).
- If you add new long-lived state files, document them in `docs/STATE.md`.
- If you add new CLI entrypoints or flags, document them in `docs/INTERFACES.md`.
- Before implementing anything non-trivial, check `docs/llm/IMPLEMENTATION_LEDGER.md` to avoid duplicate work, and refresh it after changes (`python scripts/render_implementation_ledger.py`).
- For KPI/status reporting or no-longshot operations, invoke `$polymarket-canon-first` and follow `docs/llm/CANON.md` first.
</INSTRUCTIONS>
