#!/usr/bin/env python3
"""
Observe-safe .env bootstrap for Polymarket MM / CLOB scripts.

The repo has historically read credentials only from ``os.environ`` (no
python-dotenv anywhere). This helper adds a small, dependency-free ``.env``
loader so the MM/CLOB client reliably picks up ``PM_PRIVATE_KEY`` / ``PM_FUNDER``
from a local ``.env`` file, regardless of how the process was launched.

Design (all deliberate, security-relevant):
- **Non-override**: never clobbers a var already present in the process/User
  environment. The existing DPAPI/User-scope wiring keeps precedence; ``.env``
  only fills gaps. (And ``lib/clob_auth.py`` reads ``PM_PRIVATE_KEY`` before the
  DPAPI fallback, so a ``.env`` key naturally takes effect when present.)
- **Never enables live trading**: any key whose name contains ``EXECUTE`` or
  ``CONFIRM_LIVE`` is refused from ``.env``. A ``.env`` is for credentials/config,
  not a place that can flip the bot live.
- **Alias absorption**: maps common alternative names declared in the ``.env``
  (e.g. ``POLYMARKET_PRIVATE_KEY`` / ``PRIVATE_KEY`` -> ``PM_PRIVATE_KEY``) onto
  the canonical names the auth code reads.
- **Secret-safe**: returns/logs KEY NAMES ONLY, never values.

Search order (first-found file per path; all found files are applied
non-override):
  1. ``$POLYMARKET_MM_ENV_FILE`` (explicit file, or a dir containing ``.env``)
  2. repo-root ``.env``
  3. ``scripts/.env``
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Keys that must NEVER be sourced from a .env: live-execution / confirmation gates.
_DENY_SUBSTRINGS: Tuple[str, ...] = ("EXECUTE", "CONFIRM_LIVE")

# Canonical name -> accepted alias names (declared in the .env). The canonical
# name is what lib/clob_auth.py actually reads.
_ALIASES: Dict[str, Tuple[str, ...]] = {
    "PM_PRIVATE_KEY": (
        "PM_PRIVATE_KEY",
        "POLYMARKET_PRIVATE_KEY",
        "POLY_PRIVATE_KEY",
        "CLOB_PRIVATE_KEY",
        "PRIVATE_KEY",
    ),
    "PM_PRIVATE_KEY_FILE": ("PM_PRIVATE_KEY_FILE", "POLYMARKET_PRIVATE_KEY_FILE"),
    "PM_PRIVATE_KEY_DPAPI_FILE": (
        "PM_PRIVATE_KEY_DPAPI_FILE",
        "POLYMARKET_PRIVATE_KEY_DPAPI_FILE",
    ),
    "PM_FUNDER": (
        "PM_FUNDER",
        "PM_PROXY_ADDRESS",
        "POLYMARKET_FUNDER",
        "POLY_FUNDER",
        "FUNDER",
        "PROXY_ADDRESS",
    ),
    "PM_API_KEY": ("PM_API_KEY", "POLYMARKET_API_KEY", "CLOB_API_KEY"),
    "PM_API_SECRET": ("PM_API_SECRET", "POLYMARKET_API_SECRET", "CLOB_API_SECRET"),
    "PM_API_PASSPHRASE": (
        "PM_API_PASSPHRASE",
        "POLYMARKET_API_PASSPHRASE",
        "CLOB_API_PASSPHRASE",
    ),
    "PM_SIGNATURE_TYPE": ("PM_SIGNATURE_TYPE", "POLYMARKET_SIGNATURE_TYPE"),
}


def _repo_root() -> Path:
    # scripts/lib/env_bootstrap.py -> parents[2] == repo root
    return Path(__file__).resolve().parents[2]


def _candidate_paths(extra: Optional[List[str]] = None) -> List[Path]:
    out: List[Path] = []
    pointer = str(os.environ.get("POLYMARKET_MM_ENV_FILE", "") or "").strip()
    if pointer:
        pp = Path(pointer)
        out.append((pp / ".env") if pp.is_dir() else pp)
    root = _repo_root()
    out.append(root / ".env")
    out.append(root / "scripts" / ".env")
    for e in extra or []:
        if e:
            out.append(Path(e))
    seen = set()
    uniq: List[Path] = []
    for x in out:
        k = str(x)
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq


def _parse_env_file(path: Path) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return pairs
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line[:7].lower() == "export ":
            line = line[7:].strip()
        if "=" not in line:
            continue
        name, val = line.split("=", 1)
        name = name.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if name:
            pairs.append((name, val))
    return pairs


def _is_denied(name: str) -> bool:
    up = name.upper()
    return any(s in up for s in _DENY_SUBSTRINGS)


def load_dotenv_files(
    extra_paths: Optional[List[str]] = None, override: bool = False
) -> Dict[str, object]:
    """Load .env file(s) into os.environ (observe-only, secret-safe).

    Returns a summary dict with KEY NAMES ONLY (never values):
      {files, set_names, alias_applied, denied_execute_keys}
    """
    loaded_files: List[str] = []
    set_names: List[str] = []
    denied_names: List[str] = []
    raw_present: set = set()

    for path in _candidate_paths(extra_paths):
        try:
            if not path.is_file():
                continue
        except Exception:
            continue
        pairs = _parse_env_file(path)
        if pairs:
            loaded_files.append(str(path))
        for name, val in pairs:
            raw_present.add(name)
            if _is_denied(name):
                denied_names.append(name)
                continue
            if (not override) and os.environ.get(name) not in (None, ""):
                continue
            os.environ[name] = val
            set_names.append(name)

    # Alias absorption: only from names actually declared in the .env file(s).
    alias_applied: List[str] = []
    for canonical, names in _ALIASES.items():
        if os.environ.get(canonical):
            continue
        for a in names:
            if a in raw_present and os.environ.get(a):
                os.environ[canonical] = os.environ[a]
                alias_applied.append(f"{a}->{canonical}")
                break

    return {
        "files": loaded_files,
        "set_names": sorted(set(set_names)),
        "alias_applied": alias_applied,
        "denied_execute_keys": sorted(set(denied_names)),
    }
