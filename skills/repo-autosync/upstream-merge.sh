#!/usr/bin/env bash
# Headless Claude agent: vet, merge, and push upstream changes into a fork.
# Designed to be run from a per-repo wrapper (see install-upstream-merge.sh).
# The wrapper exports: REPO, UPSTREAM_REMOTE, UPSTREAM_BRANCH, LOCAL_BRANCH,
# UPSTREAM_URL, DATA_DIR, RESOLVE_SETTINGS, REPO_CHECK_DIR.
#
# Security review is delegated entirely to repo-check (static analysis in its
# own docker sandbox). No LLM review of diff content — that would be a prompt-
# injection surface. If repo-check's coverage is insufficient, upgrade repo-check.

set -euo pipefail

: "${REPO:?REPO not set}"
: "${UPSTREAM_REMOTE:=upstream}"
: "${UPSTREAM_BRANCH:=main}"
: "${LOCAL_BRANCH:=main}"
: "${UPSTREAM_URL:?UPSTREAM_URL not set}"
: "${DATA_DIR:?DATA_DIR not set}"
: "${RESOLVE_SETTINGS:?RESOLVE_SETTINGS not set}"
: "${REPO_CHECK_DIR:=$HOME/Repos/repo-check}"

LOG="$DATA_DIR/last-update.log"
ALERT="$DATA_DIR/.security-alert"
PENDING="$DATA_DIR/pending_for_human.md"
REPORT="$DATA_DIR/repo-check-report.json"

mkdir -p "$DATA_DIR"

log() { echo "[$(date -u +%FT%TZ)] $*" >> "$LOG"; }
notify() { notify-send "$1" "$2" 2>/dev/null || true; }
alert() { echo "$1" > "$ALERT"; notify "upstream-merge: $2" "$REPO"; }

# --- Concurrency guard: only one run per data dir at a time ---
# repo-check Phase C can take 5–10 min, so cron + manual collision is real.
exec 9>"$DATA_DIR/.lock"
if ! flock -n 9; then
  log "another run already in progress — exiting"
  exit 0
fi
trap 'rm -f "$DATA_DIR/.lock"' EXIT

log "upstream-merge start ($REPO ← $UPSTREAM_REMOTE/$UPSTREAM_BRANCH)"

# --- Fetch upstream ---
if ! git -C "$REPO" fetch "$UPSTREAM_REMOTE" 2>> "$LOG"; then
  log "fetch failed for $UPSTREAM_URL — aborting"
  alert "fetch failed for $UPSTREAM_URL — see $LOG" "fetch failed"
  exit 1
fi

# --- Switch to LOCAL_BRANCH and verify clean working tree ---
# We must merge on LOCAL_BRANCH, not whatever HEAD happens to be on.
if ! git -C "$REPO" checkout "$LOCAL_BRANCH" 2>> "$LOG"; then
  log "checkout $LOCAL_BRANCH failed — aborting"
  alert "checkout $LOCAL_BRANCH failed in $REPO — see $LOG" "checkout failed"
  exit 1
fi
if ! git -C "$REPO" diff --quiet || ! git -C "$REPO" diff --cached --quiet; then
  log "$LOCAL_BRANCH has uncommitted changes — refusing to merge"
  alert "$REPO: $LOCAL_BRANCH has uncommitted changes; resolve manually before next run" "dirty working tree"
  exit 1
fi

# --- Pin mode: if pinned-sha exists, only proceed when upstream still matches ---
if [ -f "$DATA_DIR/pinned-sha" ]; then
  PINNED=$(cat "$DATA_DIR/pinned-sha")
  CURRENT_UPSTREAM=$(git -C "$REPO" rev-parse "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH")
  if [ "$PINNED" = "$CURRENT_UPSTREAM" ]; then
    log "pinned at $PINNED, upstream unchanged — exiting"
    exit 0
  fi
  # Dedup: skip append if this exact upstream SHA was already noted.
  if [ -f "$PENDING" ] && grep -q "^\*\*Upstream HEAD:\*\* $CURRENT_UPSTREAM$" "$PENDING" 2>/dev/null; then
    log "pin drift already noted for upstream=$CURRENT_UPSTREAM — skipping append"
  else
    log "pin drift: pinned=$PINNED upstream=$CURRENT_UPSTREAM — appending to pending"
    {
      echo "## $(date -u +%F): $REPO pin drift"
      echo "**Pinned:** $PINNED"
      echo "**Upstream HEAD:** $CURRENT_UPSTREAM"
      echo "Re-run installer with --pin to bump, or remove pinned-sha and switch to schedule mode."
      echo ""
    } >> "$PENDING"
    notify "upstream-merge: pin drift" "$REPO"
  fi
  exit 0
fi

push_origin() {
  if git -C "$REPO" push origin "$LOCAL_BRANCH" 2>> "$LOG"; then
    log "Pushed to origin"
    return 0
  fi
  log "Push to origin failed — leaving merge committed locally, alerting"
  alert "$REPO: push to origin failed after merge — manual resolution required" "push failed"
  exit 1
}

MERGE_BASE=$(git -C "$REPO" merge-base "$LOCAL_BRANCH" "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH")
NEW_COMMITS=$(git -C "$REPO" log "${MERGE_BASE}..$UPSTREAM_REMOTE/$UPSTREAM_BRANCH" --oneline)

if [ -z "$NEW_COMMITS" ]; then
  # No new upstream commits, but local may have a merge commit from a prior
  # successful merge whose push to origin failed. Catch that here so we
  # don't silently lose updates on the next cron tick.
  LOCAL_AHEAD=$(git -C "$REPO" rev-list --count "origin/$LOCAL_BRANCH..$LOCAL_BRANCH" 2>/dev/null || echo 0)
  ORIGIN_AHEAD=$(git -C "$REPO" rev-list --count "$LOCAL_BRANCH..origin/$LOCAL_BRANCH" 2>/dev/null || echo 0)
  if [ "$LOCAL_AHEAD" -gt 0 ] && [ "$ORIGIN_AHEAD" -eq 0 ]; then
    log "No new upstream, but local is $LOCAL_AHEAD commits ahead of origin — pushing"
    rm -f "$ALERT"
    push_origin
    exit 0
  fi
  if [ "$LOCAL_AHEAD" -gt 0 ] && [ "$ORIGIN_AHEAD" -gt 0 ]; then
    log "Local and origin/$LOCAL_BRANCH diverged ($LOCAL_AHEAD/$ORIGIN_AHEAD) — manual resolution required"
    alert "$REPO: local and origin/$LOCAL_BRANCH diverged — see $LOG" "diverged from origin"
    exit 1
  fi
  log "Already up to date."
  exit 0
fi

# --- Phase 1: repo-check static analysis (all phases: A metadata+OSV, B SAST+plugin surface, C docker canary) ---
# All phases are mandatory — no disable path. Phase C needs ~/.claude/.credentials.json and prebuilt docker image.
log "Running repo-check on $UPSTREAM_URL (all phases)"
if ! (cd "$REPO_CHECK_DIR" && uv run python check.py "$UPSTREAM_URL" --output "$REPORT") >> "$LOG" 2>&1; then
  log "repo-check execution failed — aborting (fail-closed)"
  echo "repo-check failed to run for $UPSTREAM_URL — see $LOG" > "$ALERT"
  notify "upstream-merge: repo-check failed" "$REPO"
  exit 1
fi

VERDICT=$(jq -r '.verdict // "unknown"' "$REPORT" 2>/dev/null || echo "unknown")
RISK=$(jq -r '.risk_score // "?"' "$REPORT" 2>/dev/null || echo "?")
log "repo-check verdict=$VERDICT risk=$RISK"

case "$VERDICT" in
  reject)
    echo "repo-check REJECTED upstream of $REPO (risk=$RISK). See $REPORT" > "$ALERT"
    notify "upstream-merge: repo-check reject" "$REPO"
    log "BLOCKED by repo-check — not merging."
    exit 0
    ;;
  review)
    {
      echo "## $(date -u +%F): $REPO upstream review required"
      echo "**Verdict:** review (risk=$RISK)"
      echo "**Report:** $REPORT"
      echo "**Upstream:** $UPSTREAM_URL"
      echo ""
    } >> "$PENDING"
    log "repo-check review — appended to $PENDING, not merging."
    notify "upstream-merge: manual review needed" "$REPO"
    exit 0
    ;;
  approve)
    : # continue
    ;;
  *)
    log "repo-check unknown verdict '$VERDICT' — aborting fail-closed"
    echo "repo-check returned unknown verdict for $UPSTREAM_URL" > "$ALERT"
    notify "upstream-merge: unknown verdict" "$REPO"
    exit 1
    ;;
esac

# Diffs are inlined into the conflict-resolve prompt as context. Cap to keep
# the prompt under context-window limits and bound cost on multi-month
# upstream catch-ups or fork branches with regenerated lockfiles. The agent
# can read conflicted files directly via the Read tool — these diffs are
# orientation only, not load-bearing.
_DIFF_CAP=200000  # ~200 KB per side
DIFF=$(git -C "$REPO" diff "${MERGE_BASE}" "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH" | head -c "$_DIFF_CAP")
LOCAL_DIFF=$(git -C "$REPO" diff "${MERGE_BASE}" "$LOCAL_BRANCH" | head -c "$_DIFF_CAP")

# --- Phase 2: fast-forward ---
if git -C "$REPO" merge --ff-only "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH" 2>> "$LOG"; then
  N=$(echo "$NEW_COMMITS" | wc -l | tr -d ' ')
  log "Merged ${N} commits from $UPSTREAM_REMOTE/$UPSTREAM_BRANCH (ff)"
  rm -f "$ALERT"
  push_origin
  exit 0
fi

# --- Phase 3: non-ff merge, no conflict ---
if git -C "$REPO" merge "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH" -m "merge: upstream updates" 2>> "$LOG"; then
  N=$(echo "$NEW_COMMITS" | wc -l | tr -d ' ')
  log "Merged ${N} commits (non-ff, no conflict)"
  rm -f "$ALERT"
  push_origin
  exit 0
fi

# --- Phase 4: conflict — scoped agent resolves ---
log "Merge conflict detected — invoking Claude to resolve"
CONFLICT_FILES=$(git -C "$REPO" diff --name-only --diff-filter=U)

RESOLVE_PROMPT="You are resolving a git merge conflict in a fork at ${REPO}.
Working directory is set to ${REPO}; use bare git commands.

Upstream is ${UPSTREAM_URL}. Local has customizations committed on top of upstream.
The merge of ${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH} into ${LOCAL_BRANCH} produced conflicts.

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
1. Read each conflicted file (contains <<<<<<< / ======= / >>>>>>> markers).
2. Resolve each conflict: preserve intent of BOTH sides where possible. Local changes take priority for files customized locally.
3. Edit to remove conflict markers and produce correct merged content.
4. Run: git add <resolved-files>
5. Run: git commit --no-edit
6. Stop. The wrapper script logs the result; you do not need to write to any log file."

# Run the resolve agent with cwd at the repo so the prompt's bare \`git\`
# commands work (and we can use simple Bash(git verb:*) allowlist patterns).
# --allowedTools is the LOAD-BEARING permission gate here: the settings file's
# allow/deny lists are not enforced under permission-mode dontAsk, so allowed
# tools must be expressed via this flag. Keep --settings for path-scoping
# Read/Edit/Write to {{REPO}} only.
(
  cd "$REPO"
  claude \
    --settings "$RESOLVE_SETTINGS" \
    --max-turns 30 \
    --output-format text \
    --allowedTools "Read Edit Write Bash(git status:*) Bash(git diff:*) Bash(git log:*) Bash(git show:*) Bash(git ls-files:*) Bash(git add:*) Bash(git commit:*)" \
    -p "$RESOLVE_PROMPT"
) >> "$LOG" 2>&1

if git -C "$REPO" diff --name-only --diff-filter=U | grep -q .; then
  log "CONFLICT unresolved after agent attempt — aborting merge"
  git -C "$REPO" merge --abort 2>> "$LOG" || true
  echo "Merge conflict unresolved — check ${REPO} and resolve manually" > "$ALERT"
  notify "upstream-merge: conflict unresolved" "$REPO"
  exit 1
fi

log "Conflict resolved by agent"
rm -f "$ALERT"
push_origin
