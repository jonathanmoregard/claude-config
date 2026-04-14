---
name: repo-autosync
description: Set up automatic git sync for a repo via a cron-driven Claude agent. Use when the user asks to "keep a repo in sync automatically", "auto-commit changes", or "set up auto-sync".
user-invocable: true
---

# Repo Auto-Sync Setup

This skill sets up a `sync-agent.sh` script inside a repo that auto-commits and pushes changes on a schedule, using a headless Claude Sonnet agent to decide how to group commits logically.

## Prerequisites

- `claude` CLI on PATH (for the headless agent)
- `gitleaks` on PATH (the script aborts if not installed — secret scanning is mandatory before any auto-push). Install: `curl -fsSL https://github.com/gitleaks/gitleaks/releases/latest/download/gitleaks_$(curl -s https://api.github.com/repos/gitleaks/gitleaks/releases/latest | grep tag_name | cut -d '"' -f 4 | sed 's/v//')_linux_x64.tar.gz | tar -xz -C ~/.local/bin gitleaks && chmod +x ~/.local/bin/gitleaks`

## Step 1: Confirm the target repo

If not already clear from context, ask which repo to set up auto-sync for and what cron schedule to use (default: daily at a random minute past the hour to avoid thundering herd).

## Step 2: Copy sync-agent.sh into the repo root

The canonical script lives next to this skill on disk. Copy it into the target repo:

```bash
cp ~/.claude/skills/repo-autosync/sync-agent.sh ./sync-agent.sh
chmod +x sync-agent.sh
```

The script auto-detects its repo path from its own location, so no edits are needed after copying. Do NOT inline or rewrite the script body — always copy from the canonical source so all repos stay in sync when the script is improved.

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
