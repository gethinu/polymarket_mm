# LLM Spec (polymarket_mm)

This document is the highest-priority behavioral spec for assistant-driven changes in this repository.

## Source-of-Truth Order

When implementing or reviewing changes, treat these files as authoritative in this order:
1. `docs/llm/SPEC.md`
2. `docs/ARCHITECTURE.md`
3. `docs/INTERFACES.md`
4. `docs/STATE.md`

If documents conflict, do not guess. Resolve by asking for clarification and then update docs before code changes.

## Safety and Secrets

- Never request private keys, seed phrases, or webhook URLs in chat.
- Never print secret values from environment variables.
- Default to observe-only execution unless explicit live execution is requested.

## Runtime Artifacts

- Runtime outputs must stay under `logs/` (gitignored).
- New long-lived state files must be documented in `docs/STATE.md`.
- New CLI entrypoints or new flags must be documented in `docs/INTERFACES.md`.

## PowerShell Task Policy

- Repository task scripts (`scripts/*.ps1`) must support background execution.
- Task-style runner scripts should run in background by default, with an explicit foreground opt-out.
- Existing logging/state paths remain under `logs/`.
