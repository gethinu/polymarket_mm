#!/usr/bin/env python3
"""
Render a consolidated implementation ledger into one markdown document.

Observe-only helper:
  - scans git commit history
  - scans current working tree (uncommitted changes)
  - scans link-intake session folders
  - writes docs/llm/IMPLEMENTATION_LEDGER.md by default
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional


def now_iso_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_path(raw: str, default_rel: str) -> Path:
    root = repo_root()
    raw_s = str(raw or "").strip()
    if not raw_s:
        return root / default_rel
    p = Path(raw_s)
    if p.is_absolute():
        return p
    return root / p


def run_git(args: List[str]) -> str:
    proc = subprocess.run(
        ["git"] + args,
        cwd=repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {msg}")
    return proc.stdout


def parse_git_log(max_commits: int) -> List[dict]:
    raw = run_git(
        [
            "log",
            f"-n{max(1, int(max_commits))}",
            "--date=iso-strict",
            "--pretty=format:__COMMIT__%n%H%n%ad%n%s",
            "--name-only",
            "--no-merges",
        ]
    )
    blocks = [b for b in raw.split("__COMMIT__\n") if b.strip()]
    out: List[dict] = []
    for b in blocks:
        lines = b.splitlines()
        if len(lines) < 3:
            continue
        commit = lines[0].strip()
        date = lines[1].strip()
        subject = lines[2].strip()
        files = [x.strip() for x in lines[3:] if x.strip()]
        out.append(
            {
                "commit": commit,
                "short": commit[:8],
                "date": date,
                "subject": subject,
                "files": files,
            }
        )
    return out


def parse_worktree_status() -> List[dict]:
    raw = run_git(["status", "--porcelain"])
    out: List[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        # porcelain v1: XY path
        status = line[:2].strip() or "??"
        path = line[3:].strip()
        out.append({"status": status, "path": path})
    return out


def infer_areas(subject: str, files: List[str]) -> List[str]:
    haystack = "\n".join([subject] + files).lower()
    rules = [
        ("weather_pipeline", r"weather_|run_weather_|judge_weather|build_weather|consensus"),
        ("strategy_register", r"strategy_register|strategy_gate|materialize_strategy_realized|check_morning_status"),
        ("no_longshot", r"no_longshot"),
        ("simmer_clob", r"simmer|clob_|fade_"),
        ("task_automation", r"install_.*task|scheduledtask|run_.*daily"),
        ("docs_llm", r"docs/llm/"),
        ("knowledge_intake", r"docs/knowledge/link-intake"),
        ("security_or_ops", r"security|webhook|alarm|health"),
    ]
    tags: List[str] = []
    for tag, pat in rules:
        if re.search(pat, haystack):
            tags.append(tag)
    if not tags:
        tags.append("misc")
    return sorted(set(tags))


def build_area_index(commits: List[dict]) -> List[dict]:
    counts: Dict[str, int] = defaultdict(int)
    latest: Dict[str, dict] = {}

    for c in commits:
        areas = c.get("areas") if isinstance(c.get("areas"), list) else []
        for area in areas:
            counts[area] += 1
            if area not in latest:
                latest[area] = c

    rows: List[dict] = []
    for area in sorted(counts.keys()):
        c = latest[area]
        rows.append(
            {
                "area": area,
                "count": counts[area],
                "latest_date": c.get("date") or "",
                "latest_short": c.get("short") or "",
                "latest_subject": c.get("subject") or "",
            }
        )
    return rows


def discover_link_intake_sessions() -> List[dict]:
    base = repo_root() / "docs" / "knowledge" / "link-intake" / "sessions"
    if not base.exists():
        return []
    rows: List[dict] = []
    for p in sorted(base.iterdir()):
        if not p.is_dir():
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_(.+)$", p.name)
        date_part = m.group(1) if m else ""
        topic_part = m.group(2) if m else p.name
        overview = p / "00_overview.md"
        md_files = sorted([x for x in p.glob("*.md") if x.is_file()])
        rows.append(
            {
                "session": p.name,
                "date": date_part,
                "topic": topic_part,
                "overview_exists": overview.exists(),
                "md_count": len(md_files),
                "path": str(p.relative_to(repo_root())).replace("\\", "/"),
            }
        )
    return rows


def fmt_date(iso_s: str) -> str:
    s = str(iso_s or "").strip()
    if not s:
        return "-"
    try:
        dt_obj = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt_obj.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s


def trim(s: str, n: int) -> str:
    t = str(s or "")
    if len(t) <= n:
        return t
    return t[: max(1, n - 3)] + "..."


def render_md(
    out_path: Path,
    commits: List[dict],
    areas: List[dict],
    worktree: List[dict],
    sessions: List[dict],
    max_files_per_commit: int,
) -> str:
    generated = now_iso_utc()
    lines: List[str] = []
    lines.append("# IMPLEMENTATION LEDGER")
    lines.append("")
    lines.append(f"- generated_utc: `{generated}`")
    lines.append(f"- source_repo: `{repo_root()}`")
    lines.append(f"- output_path: `{out_path}`")
    lines.append(f"- commits_scanned: `{len(commits)}`")
    lines.append(f"- worktree_changes: `{len(worktree)}`")
    lines.append(f"- link_intake_sessions: `{len(sessions)}`")
    lines.append("")
    lines.append("## Purpose")
    lines.append("- Keep one canonical history document to avoid duplicate implementation work across chats.")
    lines.append("- Force a quick lookup before coding (`area`, `recent commits`, `similar files`).")
    lines.append("- Keep this file append-safe by regenerating from git/worktree/session artifacts.")
    lines.append("")
    lines.append("## Duplicate-Prevention Workflow")
    lines.append("1. Search this file for target keywords (`rg -n \"<keyword>\" docs/llm/IMPLEMENTATION_LEDGER.md`).")
    lines.append("2. Reuse/extend existing implementation when same area+files already exist.")
    lines.append("3. If new work is required, include a short `why not reuse` note in commit/PR text.")
    lines.append("4. Re-run `python scripts/render_implementation_ledger.py` after the change.")
    lines.append("")

    lines.append("## Area Index")
    lines.append("| area | commits | latest_date_utc | latest_commit | latest_subject |")
    lines.append("|---|---:|---|---|---|")
    if areas:
        for r in areas:
            lines.append(
                f"| `{r['area']}` | {int(r['count'])} | `{fmt_date(str(r['latest_date']))}` | "
                f"`{r['latest_short']}` | {trim(str(r['latest_subject']), 88)} |"
            )
    else:
        lines.append("| `-` | 0 | `-` | `-` | no commits found |")
    lines.append("")

    lines.append("## Recent Commit Timeline")
    lines.append("| date_utc | commit | areas | summary | key_files |")
    lines.append("|---|---|---|---|---|")
    if commits:
        for c in commits:
            files = c.get("files") if isinstance(c.get("files"), list) else []
            key_files = files[: max(1, int(max_files_per_commit))]
            extra = len(files) - len(key_files)
            file_txt = ", ".join(f"`{f}`" for f in key_files)
            if extra > 0:
                file_txt += f", +{extra}"
            lines.append(
                f"| `{fmt_date(str(c.get('date') or ''))}` | `{c.get('short')}` | "
                f"`{','.join(c.get('areas') or [])}` | {trim(str(c.get('subject') or ''), 92)} | {file_txt or '`-`'} |"
            )
    else:
        lines.append("| `-` | `-` | `-` | no commits found | `-` |")
    lines.append("")

    lines.append("## Working Tree (Uncommitted)")
    lines.append("| status | path |")
    lines.append("|---|---|")
    if worktree:
        for w in worktree:
            lines.append(f"| `{w.get('status')}` | `{w.get('path')}` |")
    else:
        lines.append("| `clean` | `-` |")
    lines.append("")

    lines.append("## Link-Intake Session Artifacts")
    lines.append("| date | session | topic | md_files | overview | path |")
    lines.append("|---|---|---|---:|---|---|")
    if sessions:
        for s in sessions:
            lines.append(
                f"| `{s.get('date') or '-'}` | `{s.get('session')}` | {trim(str(s.get('topic') or ''), 72)} | "
                f"{int(s.get('md_count') or 0)} | "
                f"{'yes' if bool(s.get('overview_exists')) else 'no'} | `{s.get('path')}` |"
            )
    else:
        lines.append("| `-` | `-` | no session folders found | 0 | no | `-` |")
    lines.append("")

    lines.append("## Refresh Command")
    lines.append("- `python scripts/render_implementation_ledger.py`")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Render consolidated implementation ledger (observe-only)")
    p.add_argument("--max-commits", type=int, default=120, help="How many recent commits to scan")
    p.add_argument(
        "--max-files-per-commit",
        type=int,
        default=4,
        help="How many file paths to list per commit row",
    )
    p.add_argument(
        "--out-md",
        default="docs/llm/IMPLEMENTATION_LEDGER.md",
        help="Output markdown path",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    out_path = resolve_path(str(args.out_md), "docs/llm/IMPLEMENTATION_LEDGER.md")

    commits = parse_git_log(max_commits=max(1, int(args.max_commits)))
    for c in commits:
        c["areas"] = infer_areas(str(c.get("subject") or ""), c.get("files") or [])

    area_rows = build_area_index(commits)
    worktree = parse_worktree_status()
    sessions = discover_link_intake_sessions()

    md = render_md(
        out_path=out_path,
        commits=commits,
        areas=area_rows,
        worktree=worktree,
        sessions=sessions,
        max_files_per_commit=max(1, int(args.max_files_per_commit)),
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"[implementation-ledger] wrote md: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
