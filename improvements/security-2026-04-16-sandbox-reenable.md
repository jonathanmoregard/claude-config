---
status: proposed
category: security
date: 2026-04-16
source: manual
project: global
---

## What it was

Sandbox re-enabled at user scope (`~/.claude/settings.json`, `sandbox.enabled: true`, strict mode) on 2026-04-16. Disabled same day after `claude mcp add --scope user github ...` failed with `EROFS: read-only file system, open '/home/jonathan/.claude.json'`. CC's own config-write CLI was blocked by its own sandbox.

## Problem

Sandbox allowlist covers `~/.claude/` (directory) but not `~/.claude.json` (file at `~/`). Any CC subcommand that mutates `~/.claude.json` (`claude mcp add/remove`, plugin install, etc.) fails under strict sandbox. Same will hit other state files at `~/` and other CC-managed paths outside the allowlist.

Strict mode + auto-allow-bash made debugging hard — every non-excluded command must wrap, and CC's own commands aren't excluded by default.

## Proposal

Before re-enabling:

1. Catalog CC config/state write paths: `~/.claude.json`, `~/.claude/.credentials.json`, any others. Check `claude-code/src` for `writeFileSync` / settings.json writers targeting `~/`.
2. Add those paths to `sandbox.filesystem.allowWrite` in user settings. OR add `claude mcp *`, `claude plugin *`, etc. to `sandbox.excludedCommands`.
3. Verify: `/sandbox` toggle, `/hooks`, `~/.claude/todo-add --login`, plugin install all still work.
4. Decide mode: strict (every bash wrapped unless excluded) vs non-strict. Strict breaks tools that expect direct FS access; excluded-commands list gets long fast. Prefer non-strict + targeted excludes for first re-enable.
5. Re-flip `sandbox.enabled` to `true` in `~/.claude/settings.json` (structure already in place).

## Research

- `~/.claude/research/security-2026-04-16-prompt-injection-posture.md` — kernel-containment is posture layer 5
- `/home/jonathan/Repos/claude-code/src/utils/sandbox/sandbox-adapter.ts` — `excludedCommands`, `filesystem.allowWrite` schema
