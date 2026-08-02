---
name: settings-management
description: Guidelines for managing ~/.claude settings, git hygiene, and ephemeral files
user-invocable: false
---

## Settings are version controlled

`~/.claude` is a git repository. All persistent config changes (settings, hooks, skills) should be committed and pushed to keep the backup in sync.

After modifying any tracked file (e.g. `settings.json`, `fable-warn.sh`, skills):

```bash
cd ~/.claude && git add -A && git commit -m "chore: <describe change>" && git push
```

## Tracked files

- `settings.json` — model, hooks, global preferences
- `fable-warn.sh` — hook script for Fable model warning
- `skills/` — skill definitions

## Ephemeral files → tmp/

`~/.claude/tmp/` is a gitignored scratch folder for short-lived state.

Use it for:
- Throttle stamps (e.g. `.fable-warn-stamp`)
- Temporary output files
- Any runtime state that belongs in `~/.claude` but not in git
