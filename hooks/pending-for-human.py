#!/usr/bin/env python3
"""SessionStart: surface this repo's open pending_for_human.md entries.

WHY. CLAUDE.md has sessions log human-only blockers to
``~/.local/state/claude-tasks/<repo>/pending_for_human.md``. Nothing read them
back, so a blocker stayed open indefinitely while each new session re-probed
it and appended a duplicate entry. This hook closes the loop the same way the
other SessionStart health hooks do: the fact goes into Claude's context
(additionalContext) and, on startup, a one-line notice reaches Jonathan
(systemMessage).

SCOPE. Only the current repo: the notes dir named after the git toplevel of
``cwd`` and, for a linked worktree, the one named after the primary checkout,
bare or not (a leading dot becomes ``dot-``, so ~/.claude -> dot-claude). Other repos'
backlogs stay out of this session.

WHAT COUNTS. Each level-2 heading (``## ``) is one entry, per
templates/pending-for-human.md. Headings that start with RESOLVED or DONE are
skipped. No open entries -> nothing printed.

Stdlib only, fail-open. Test: tests/test_pending_for_human_hook.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

FILENAME = "pending_for_human.md"
MAX_LISTED = 15
MAX_TITLE = 140
MAX_BYTES = 2_000_000
CLOSED = re.compile(r"^(resolved|done)\b", re.IGNORECASE)
DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def notes_root() -> Path:
    override = os.environ.get("CLAUDE_TASKS_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "claude-tasks"


def notes_name(path: Path) -> str:
    base = path.name
    return "dot-" + base[1:] if base.startswith(".") else base


def git_out(cwd: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=3
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def repo_names(cwd: str) -> list[str]:
    top = git_out(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return []
    names = [notes_name(Path(top))]
    common = git_out(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        common_path = Path(common)
        if common_path.name == ".git":
            primary_dir = common_path.parent
        else:
            # Bare primary (nixos-config: ~/Repos/nixos-config is bare, its
            # worktrees live elsewhere). Its own dir name is the repo name.
            primary_dir = common_path.with_name(common_path.name.removesuffix(".git"))
        primary = notes_name(primary_dir)
        if primary not in names:
            names.append(primary)
    return names


def read_capped(path: Path) -> str | None:
    """Regular files only, at most MAX_BYTES: a fifo or device never stalls startup."""
    try:
        if not path.is_file():
            return None
        with path.open("rb") as fh:
            return fh.read(MAX_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None


def open_entries(text: str) -> list[str]:
    titles = []
    for line in text.splitlines():
        if not line.startswith("## "):
            continue
        title = line[3:].strip()
        if not title or CLOSED.match(title):
            continue
        titles.append(title)
    return titles


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    cwd = data.get("cwd") or os.getcwd()
    source = data.get("source", "startup")

    root = notes_root()
    found: list[tuple[Path, list[str]]] = []
    for name in repo_names(cwd):
        path = root / name / FILENAME
        text = read_capped(path)
        if text is None:
            continue
        titles = open_entries(text)
        if titles:
            found.append((path, titles))
    if not found:
        return 0

    total = sum(len(t) for _, t in found)
    dates = sorted(d for _, ts in found for t in ts for d in DATE.findall(t)[:1])
    oldest = f", oldest {dates[0]}" if dates else ""
    paths = ", ".join(str(p) for p, _ in found)

    lines = [
        f"pending_for_human: {total} open human-only blocker(s) logged for this repo{oldest}.",
        "These blockers are already known and recorded; per"
        " templates/pending-for-human.md a resolved entry is deleted from the file.",
    ]
    for path, titles in found:
        lines.append(f"{path} ({len(titles)} open):")
        for title in titles[:MAX_LISTED]:
            short = title if len(title) <= MAX_TITLE else title[: MAX_TITLE - 1] + "…"
            lines.append(f"  - {short}")
        if len(titles) > MAX_LISTED:
            lines.append(f"  - … {len(titles) - MAX_LISTED} more in the file")

    out: dict = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }
    }
    if source in ("startup", "clear"):
        out["systemMessage"] = f"pending_for_human: {total} open{oldest} — {paths}"
    json.dump(out, sys.stdout)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)
