---
name: settings-management
description: Use when about to change anything under ~/.claude — settings.json, a hook, a skill, an agent, a command, a script, a permission rule — or when asked to "commit my config", "push .claude", "sync my settings", or to place ephemeral state there. ~/.claude is read-only; the edit belongs in a worktree.
user-invocable: false
---

# Managing `~/.claude`

## Edit in a worktree, never in `~/.claude`

`~/.claude` is read-only and pinned to `master`. The harness reads that working
tree **live**, so an edit there changes the running configuration of every
session on the machine before it is ever reviewed.

Before touching any file under `~/.claude`:

```bash
git -C ~/.claude fetch origin master
git -C ~/.claude worktree add ~/worktrees/claude-<slug> -b feat/<slug> origin/master
cd ~/worktrees/claude-<slug>
```

Edit, verify and commit **there**, push, then `gh pr create`. The merge click is
Jonathan's. Never commit to a branch this session did not create — another
session's branch is not a delivery vehicle for your diff.

This is the only documented path. It holds for a one-character change; the size
of the diff is not the criterion, the ownership of the branch is.

## Config lives in git

| Path | Holds |
|---|---|
| `settings.json` | model, hooks, permissions, env |
| `hooks/` | hook scripts |
| `skills/`, `agents/`, `commands/` | skill, subagent and slash-command definitions |
| `scripts/`, `mcps/` | helper scripts and MCP servers |
| `CLAUDE.md` | global instructions |

A `skills/<name>` entry that is a **symlink** belongs to another repo
(`~/Repos/superpowers`, `~/Repos/everything-claude-code`, `~/Repos/token-optimizer`,
`~/Repos/router-agent`, `/nix/store`). Editing it from here lands the change in
that repo, or fails on a read-only store path. Route it through the owning
repo's own PR flow.

## Ephemeral files → `tmp/`

`~/.claude/tmp/` is a gitignored scratch folder for short-lived state: throttle
stamps, temporary output, any runtime state that belongs in `~/.claude` but not
in git. Writing runtime state there needs no worktree and no PR.

Working notes are different — they go to `~/.local/state/claude-tasks/<repo>/`,
never under `~/.claude/`.
