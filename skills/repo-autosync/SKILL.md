---
name: repo-autosync
description: Set up automatic git sync for a repo via a cron-driven Claude agent. Use when the user asks to "keep a repo in sync automatically", "auto-commit changes", or "set up auto-sync".
user-invocable: true
---

# Repo Auto-Sync Setup

This skill sets up a `sync-agent.sh` script inside a repo that auto-commits and pushes changes on a schedule, using a headless Claude Sonnet agent to decide how to group commits logically.

## Step 1: Confirm the target repo

If not already clear from context, ask which repo to set up auto-sync for and what cron schedule to use (default: daily at a random minute past the hour to avoid thundering herd).

## Step 2: Create sync-agent.sh in the repo root

Write this script verbatim (the path auto-detects from script location):

```bash
#!/usr/bin/env bash
# Spins off a Claude agent to commit and push any changes in a git repo.
# Designed to be run from system cron — no Claude Code session required.
# Place this script inside the repo it should sync; path is inferred automatically.

set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
CLAUDE=/home/jonathan/.local/bin/claude

# Skip if nothing to commit
cd "$REPO"
if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
  exit 0
fi

PROMPT="There are uncommitted changes in $REPO. Group the changes into logical commits using your own judgment — related files should go together, unrelated changes should be separate commits. Use concise conventional commit messages (e.g. 'chore: sync ghostty config'). After all commits are done, push origin main once. Do nothing else."

"$CLAUDE" \
  --model claude-sonnet-4-6 \
  --dangerously-skip-permissions \
  -p "$PROMPT" \
  --cwd "$REPO"
```

Make it executable: `chmod +x sync-agent.sh`

## Step 3: Add a crontab entry

Pick a schedule appropriate to the repo (ask if unclear). Append to the user's crontab:

```
MINUTE HOUR * * * /path/to/repo/sync-agent.sh >> /path/to/repo/sync.log 2>&1
```

Use `crontab -e` or `(crontab -l; echo "...") | crontab -` to add without clobbering.

Add `CRON_TZ=Europe/Stockholm` at the top if not already present.

## Step 4: Add sync.log to .gitignore

Make sure `sync.log` is in the repo's `.gitignore` so the log file doesn't get committed on each sync.

## Step 5: Test it

Run the script manually once to confirm it works before relying on cron:

```bash
/path/to/repo/sync-agent.sh
```

## Notes

- The script is self-contained and portable — copy it to any repo and it works
- Claude uses its own judgment to group commits; no hardcoded split logic needed
- `--dangerously-skip-permissions` is required for unattended cron use
- If the repo uses a branch other than `main`, update the PROMPT accordingly
