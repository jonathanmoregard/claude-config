#!/usr/bin/env python3
"""PreToolUse gate: refuse agent file-tool writes to protected live paths.

The safe-bash `.claude` worktree lock and the `.githooks/reference-transaction`
hook guard git verbs and ref moves. Neither sees a Write/Edit/NotebookEdit call,
and the harness reads ~/.claude's working tree live — so an uncommitted write
there is in force on every session on the machine before any git command the
other layers would refuse is ever issued. This closes that surface.

Scope is the versioned surface. Two rules, unioned:

  * a fixed list of trees the harness loads as configuration — these are
    guarded even for a file that does not exist yet, because creating a new
    hook in the live tree is exactly as live as editing an existing one;
  * anything git TRACKS under ~/.claude. An allowlist of directory names
    always lags the repo: measured 2026-08-31, `permission-ledger/` (19
    tracked files, including the `on-decision` callback the intake gate treats
    as a security boundary), `recursive-self-improvement/config/config.json`,
    `tools/`, `workers/`, `egress_guard/`, `dev-container/` and `tests/` were
    all writable and none of them gitignored.

Untracked runtime state under ~/.claude (tmp/, projects/, homunculus/,
reflections/, logs/, the permission ledger's own queue and decision files) is
left alone, and so is the proposal spine even where git still tracks it — that
is sibling-owned data the nightly reviewer writes by design.

Everything under live ~/.codex is refused to agent file tools. Shared config
comes from ~/.claude; Codex-specific mappings come from ai-client-config; the
sync service publishes them. Codex's own runtime writes do not pass through
agent file tools and remain unaffected.

Emits a PreToolUse deny naming the worktree flow. Fails open on anything it
cannot parse, per the house rule for hook input handling.
"""

import json
import os
import subprocess
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

# Sibling-owned data trees that live under ~/.claude for historical reasons and
# are written by design. They are exempt from the tracked-file rule because on
# master today the spine is still tracked: without this the guard would refuse
# the nightly reviewer, which is a writer it was never aimed at. After the
# cutover both names are symlinks into ~/.local/state/claude-proposals and the
# question stops arising.
EXEMPT_DIRS = (
    "proposals",
    "recursive-self-improvement/proposals",
    "projects",
    "homunculus",
    "reflections",
    "tmp",
)

# The tracked-file query is one `git ls-files` (425 paths, 17 KB, 4 ms on this
# repo) per file-tool call, and only for a path under ~/.claude that the
# directory rule did not already settle. The cap is half the hook's registered
# 10 s timeout in settings.json, so a wedged git leaves room for the decision
# to still be written rather than the whole hook being killed.
GIT_TIMEOUT_S = 5

REASON = (
    "Refused: {rel} is part of the versioned ~/.claude checkout — git tracks "
    "it, or it sits in a tree the harness loads as configuration. ~/.claude is "
    "read-only and pinned to master; the harness reads that working tree live, "
    "so this write would take effect on every session on this machine "
    "unreviewed.\n\n"
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

CODEX_REASON = (
    "Refused: {rel} is under live ~/.codex. Agents must not hand-edit Codex "
    "configuration or runtime state. Change shared configuration through the "
    "~/.claude worktree/PR flow; change Codex-specific rendering in the "
    "ai-client-config repository. Let ai-client-config-codex-sync.service "
    "publish generated output."
)


def config_root():
    return os.path.join(os.path.expanduser("~"), ".claude")


def codex_root():
    return os.path.join(os.path.expanduser("~"), ".codex")


def relpaths_under(path, root):
    """Return lexical paths under root, including a symlink-resolved root."""
    if not path:
        return []
    abs_path = os.path.abspath(os.path.expanduser(str(path)))
    candidates = []
    for base in {root, os.path.realpath(root)}:
        try:
            rel = os.path.relpath(abs_path, base)
        except ValueError:  # different drive on Windows
            continue
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            continue
        candidates.append(rel)
    return candidates


def relpath_under(path, root):
    """Return one lexical or resolved path under root, else None."""
    candidates = relpaths_under(path, root)
    if candidates:
        return candidates[0]
    if not path:
        return None
    resolved = os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))
    candidates = relpaths_under(resolved, root)
    return candidates[0] if candidates else None


def tracked_paths(root):
    """Every path git tracks under `root`, or None when git cannot say.

    None (git absent, not a checkout, wedged) falls back to the directory rule
    alone. That is a deliberate fail-open on the ADDITIONAL half of the guard:
    the config trees stay refused either way, and a hook that hangs or crashes
    on every Write is a worse outcome than one that guards a smaller surface
    for as long as git is broken.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "--"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", "replace").split("\0")
    return {p for p in out if p}


def guarded_relpath(path, root):
    """Return the path relative to ~/.claude if it is guarded, else None."""
    # Compare against both the literal and the symlink-resolved config root, so
    # a symlinked $HOME does not open a bypass. The target path itself is NOT
    # resolved: a write aimed at ~/.claude/skills/<symlinked-skill>/SKILL.md is
    # exactly the case that must be refused and redirected to the owning repo.
    candidates = relpaths_under(path, root)
    if not candidates:
        return None

    for rel in candidates:
        if is_exempt(rel):
            return None
    for rel in candidates:
        head = rel.split(os.sep, 1)[0]
        if rel in GUARDED_FILES or head in GUARDED_DIRS:
            return rel

    # Only now — the directory rule answers most calls without forking git.
    tracked = tracked_paths(root)
    if tracked is None:
        return None
    for rel in candidates:
        if rel.replace(os.sep, "/") in tracked:
            return rel
    return None


def is_exempt(rel):
    rel = rel.replace(os.sep, "/")
    return any(rel == d or rel.startswith(d + "/") for d in EXEMPT_DIRS)


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
    codex_rel = relpath_under(target, codex_root())
    if codex_rel is not None:
        reason = CODEX_REASON.format(rel=codex_rel)
    else:
        rel = guarded_relpath(target, config_root())
        if rel is None:
            return 0
        reason = REASON.format(rel=rel)

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
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
