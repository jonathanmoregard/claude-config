#!/bin/bash
# Headless Claude agent: review + merge upstream token-optimizer changes
set -euo pipefail

REPO="/home/jonathan/Repos/token-optimizer"
DATA_DIR="/home/jonathan/.claude/token-optimizer"
WORKER_DIR="$(dirname "$(realpath "$0")")"
LOG="$DATA_DIR/.last-update.log"
ALERT="$DATA_DIR/.security-alert"

log() { echo "[$(date -u +%FT%TZ)] $*" >> "$LOG"; }

log "token-optimizer-updater start"

git -C "$REPO" fetch upstream 2>> "$LOG"

MERGE_BASE=$(git -C "$REPO" merge-base HEAD upstream/main)
NEW_COMMITS=$(git -C "$REPO" log "${MERGE_BASE}..upstream/main" --oneline)

if [ -z "$NEW_COMMITS" ]; then
  log "Already up to date."
  exit 0
fi

DIFF=$(git -C "$REPO" diff "${MERGE_BASE}" upstream/main)
COMMIT_LOG=$(git -C "$REPO" log "${MERGE_BASE}..upstream/main" --stat)
LOCAL_DIFF=$(git -C "$REPO" diff "${MERGE_BASE}" HEAD)

# Phase 1: security review (read-only Claude, strict output)
SECURITY_PROMPT="You are a supply-chain security reviewer. Analyse this git diff from the token-optimizer project (upstream changes only, not local customizations).

## New upstream commits
${COMMIT_LOG}

## Diff
\`\`\`diff
${DIFF}
\`\`\`

Use the /security-review skill if available. Specifically flag:
- New network calls to any host except github.com/alexgreensh
- eval(), exec(), subprocess with shell=True, Function() constructor
- File writes outside /home/jonathan/Repos/token-optimizer or /home/jonathan/.claude/token-optimizer
- Base64-decoded payloads or obfuscated string construction in sensitive operations
- Environment variable exfiltration to external hosts
- Security-relevant code removals (e.g. removing input sanitization, authentication checks, credential scrubbing)

Respond with EXACTLY one of:
PASS
or
BLOCK: <one-line reason>

Nothing else. No preamble."

VERDICT=$(claude \
  --settings "$WORKER_DIR/settings.json" \
  --permission-mode dontAsk \
  --max-turns 10 \
  --output-format text \
  -p "$SECURITY_PROMPT" 2>> "$LOG")

log "Claude verdict: $VERDICT"

if echo "$VERDICT" | grep -q "^BLOCK"; then
  echo "$VERDICT" > "$ALERT"
  notify-send "token-optimizer security alert" "Upstream blocked: $VERDICT" 2>/dev/null || true
  log "BLOCKED — not merging. Alert written to $ALERT"
  exit 0
fi

push_origin() {
  git -C "$REPO" push origin main 2>> "$LOG" && log "Pushed to origin" || log "Push to origin failed"
}

# Phase 2: fast-forward (no conflict possible)
if git -C "$REPO" merge --ff-only upstream/main 2>> "$LOG"; then
  N=$(echo "$NEW_COMMITS" | wc -l | tr -d ' ')
  log "Merged ${N} commits from upstream/main (ff)"
  rm -f "$ALERT"
  push_origin
  exit 0
fi

# Phase 3: non-ff merge — try it; if conflict, hand off to Claude to resolve
git -C "$REPO" merge upstream/main -m "merge: upstream token-optimizer updates" 2>> "$LOG" && {
  N=$(echo "$NEW_COMMITS" | wc -l | tr -d ' ')
  log "Merged ${N} commits from upstream/main"
  rm -f "$ALERT"
  push_origin
  exit 0
}

# Conflict — Claude resolves it
log "Merge conflict detected — invoking Claude to resolve"
CONFLICT_FILES=$(git -C "$REPO" diff --name-only --diff-filter=U)

RESOLVE_PROMPT="You are resolving a git merge conflict in the token-optimizer fork at ${REPO}.

## Context
This is a local fork of https://github.com/alexgreensh/token-optimizer.
We have local customizations committed on top of upstream. The merge of upstream/main into our branch produced conflicts.

## Our local changes (what we added on top of upstream)
\`\`\`diff
${LOCAL_DIFF}
\`\`\`

## What upstream changed
\`\`\`diff
${DIFF}
\`\`\`

## Conflicted files
${CONFLICT_FILES}

Your job:
1. Read the conflicted files in ${REPO} (they contain <<<<<<< / ======= / >>>>>>> markers)
2. Resolve each conflict: preserve the intent of BOTH sides where possible. Our local changes take priority for the files we customised (hooks/python-launcher.sh, skills/token-optimizer/scripts/archive_result.py).
3. Edit the files to remove conflict markers and produce correct merged content.
4. Run: git -C ${REPO} add <resolved-files>
5. Run: git -C ${REPO} commit --no-edit
6. Append a one-line summary to ${LOG}: [timestamp] Resolved conflict and merged N commits from upstream/main

Do NOT run git merge --abort. Do NOT notify-send. Just resolve and commit."

claude \
  --settings "$WORKER_DIR/resolve-settings.json" \
  --permission-mode dontAsk \
  --max-turns 30 \
  --output-format text \
  -p "$RESOLVE_PROMPT" >> "$LOG" 2>&1

# Check if conflict was resolved
if git -C "$REPO" diff --name-only --diff-filter=U | grep -q .; then
  log "CONFLICT unresolved after Claude attempt — alerting"
  echo "Merge conflict unresolved — check ${REPO} and resolve manually" > "$ALERT"
  notify-send "token-optimizer merge conflict" "Claude could not resolve — manual fix needed" 2>/dev/null || true
else
  log "Conflict resolved by Claude"
  rm -f "$ALERT"
  push_origin
fi
