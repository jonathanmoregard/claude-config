#!/usr/bin/env python3
"""Sandbox aid for Bash. FAIL-OPEN: it grants nothing and blocks nothing.

When a sandboxed command's output carries an error the sandbox explains, name
the route that works. Before a command that asks to leave the sandbox, log the request,
so the routes still missing can be ranked from real use.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

SETTINGS = Path(__file__).resolve().parent.parent / "settings.json"
LOG = Path.home() / ".local" / "state" / "claude-sandbox" / "events.jsonl"

HINTS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "git-auth",
        re.compile(r"Authentication failed for 'https://github\.com"
                   r"|could not read Username for 'https://github\.com"),
        "The shell is sandboxed: the GitHub token is injected for gh and API "
        "calls only, so git cannot authenticate. Use the mcp__git_broker__git "
        "tool for fetch, pull and push.",
    ),
    (
        "bus",
        re.compile(r"Failed to connect to (user|system) scope bus|Failed to connect to bus"),
        "The shell is sandboxed and has no systemd bus. To read unit state "
        "use `~/.claude/scripts/unit-read [--user] <verb> [unit...]` (verbs: "
        "status, show, cat, is-active, is-failed, is-enabled, list-units, "
        "list-timers, ...) or `~/.claude/scripts/wait-for unit <name> "
        "[--user]`. Each runs outside the sandbox only as the whole command: "
        "no pipe, file redirect, `&&`, `;`, `cd` or variable prefix. Long "
        "jobs: `~/.claude/scripts/wait-for job <slug> -- <cmd>`. `journalctl` "
        "works as it is.",
    ),
    (
        "trash",
        re.compile(r"across filesystem boundaries|Unable to create trashing info file"),
        "The shell is sandboxed, where `gio trash` cannot reach the trash. "
        "Use `~/.claude/scripts/trash <path>...` as the whole command (no "
        "`&&`, `;`, pipe or `cd`); it takes paths under ~/Repos and "
        "~/worktrees.",
    ),
    (
        "read-only",
        re.compile(r"Read-only file system", re.I),
        "The shell is sandboxed: it can write under ~/Repos, ~/worktrees, "
        "/tmp, ~/.local/state/claude-tasks and the nix, uv and cargo caches "
        "only.",
    ),
]


def sandbox_enabled() -> bool:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))["sandbox"]["enabled"] is True
    except Exception:
        return False


def log(event: dict, **fields: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": int(time.time()), "session": event.get("session_id"),
                  "cwd": event.get("cwd"), **fields}
        fd = os.open(LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        print(f"sandbox-aid: cannot write {LOG}: {e}", file=sys.stderr)


def main() -> int:
    event = json.load(sys.stdin)
    if event.get("tool_name") != "Bash":
        return 0
    tool_input = event.get("tool_input") or {}
    command = str(tool_input.get("command") or "")[:300]
    unsandboxed = tool_input.get("dangerouslyDisableSandbox") is True
    if event.get("hook_event_name") == "PreToolUse":
        if unsandboxed:
            log(event, kind="hatch", command=command)
        return 0
    if unsandboxed or not sandbox_enabled():
        return 0
    text = json.dumps([event.get("tool_response"), event.get("error")])
    for name, pattern, hint in HINTS:
        if pattern.search(text):
            log(event, kind="hint", hint=name, command=command)
            sys.stderr.write(hint + "\n")
            return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
