#!/usr/bin/env python3
"""PreToolUse gate: refuse file-tool writes to the live ~/.claude config surface.

The safe-bash `.claude` worktree lock and the `.githooks/reference-transaction`
hook guard git verbs and ref moves. Neither sees a Write/Edit/NotebookEdit call,
and the harness reads ~/.claude's working tree live — so an uncommitted write
there is in force on every session on the machine before any git command the
other layers would refuse is ever issued. This closes that surface.

Scope is the versioned config surface only. Untracked runtime state under
~/.claude (tmp/, projects/, homunculus/, reflections/, logs/) and the
sibling-owned data trees (proposals/, permission-ledger/) are left alone.

Emits a PreToolUse deny naming the worktree flow. Fails open on anything it
cannot parse, per the house rule for hook input handling.
"""

import json
import os
import sys

FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Paths under ~/.claude that the harness loads as configuration. Files first,
# then directory prefixes.
GUARDED_FILES = {
    "settings.json",
    "settings-patch.json",
    "keybindings.json",
    "mcp-servers.json",
    "CLAUDE.md",
}
GUARDED_DIRS = (
    "hooks",
    "skills",
    "agents",
    "commands",
    "scripts",
    "mcps",
    "templates",
    "bin",
    ".githooks",
)

REASON = (
    "Refused: {rel} is live Claude Code configuration. ~/.claude is read-only "
    "and pinned to master; the harness reads that working tree live, so this "
    "write would take effect on every session on this machine unreviewed.\n\n"
    "Do it in a worktree instead:\n"
    "  git -C ~/.claude fetch origin master\n"
    "  git -C ~/.claude worktree add ~/worktrees/claude-<slug> -b feat/<slug> "
    "origin/master\n"
    "  cd ~/worktrees/claude-<slug>\n\n"
    "Edit ~/worktrees/claude-<slug>/{rel} there, commit, push, then "
    "`gh pr create`. The merge click is Jonathan's. Never commit to a branch "
    "this session did not create.\n\n"
    "If {rel} is a symlink into another repo (~/Repos/superpowers, "
    "~/Repos/everything-claude-code, /nix/store, ...), edit it in that repo "
    "and route it through that repo's own PR flow."
)


def config_root():
    return os.path.join(os.path.expanduser("~"), ".claude")


def guarded_relpath(path, root):
    """Return the path relative to ~/.claude if it is guarded config, else None."""
    if not path:
        return None
    abs_path = os.path.abspath(os.path.expanduser(str(path)))

    # Compare against both the literal and the symlink-resolved config root, so
    # a symlinked $HOME does not open a bypass. The target path itself is NOT
    # resolved: a write aimed at ~/.claude/skills/<symlinked-skill>/SKILL.md is
    # exactly the case that must be refused and redirected to the owning repo.
    for base in {root, os.path.realpath(root)}:
        try:
            rel = os.path.relpath(abs_path, base)
        except ValueError:  # different drive on Windows
            continue
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            continue
        head = rel.split(os.sep, 1)[0]
        if rel in GUARDED_FILES or head in GUARDED_DIRS:
            return rel
    return None


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0

    if payload.get("tool_name") not in FILE_TOOLS:
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    rel = guarded_relpath(target, config_root())
    if rel is None:
        return 0

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": REASON.format(rel=rel),
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OSError:
        sys.exit(0)
