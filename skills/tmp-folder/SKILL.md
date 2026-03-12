---
name: tmp-folder
description: Remind about the ~/.claude/tmp/ folder for ephemeral/gitignored files
user-invocable: false
---

`~/.claude/tmp/` is a gitignored scratch folder for ephemeral files that should not be committed.

Use it for:
- Timestamps and stamps (e.g. throttle markers like `.opus-warn-stamp`)
- Temporary output files
- Any other short-lived state that belongs in `~/.claude` but not in git

The folder is always available and gitignored via `~/.claude/.gitignore`.
