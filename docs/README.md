# Documentation Rules

- Use `docs/` as the canonical destination for all new documentation outputs.
- Put temporary scratch notes under `docs/tmp/` when needed.
- Put reusable knowledge artifacts under `docs/knowledge/`.
- For link-intake research outputs, use:
  - `docs/knowledge/link-intake/sessions/<date>_<topic>/`
  - one file per source URL + one session overview
- Canonical implementation-history document:
  - `docs/llm/IMPLEMENTATION_LEDGER.md`
  - refresh with `python scripts/render_implementation_ledger.py`
- Canonical multi-chat operations document:
  - `docs/llm/CANON.md`
- Do not add new files under `doc/` (legacy path).
