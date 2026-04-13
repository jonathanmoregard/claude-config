#!/usr/bin/env bash
# Weekly submodule updater — checks deps.dev before pinning new commits
set -euo pipefail

REPO_DIR="/home/jonathan/.claude"
LOG="$REPO_DIR/logs/submodule-update.log"

log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG"; }

deps_dev_ok() {
  local owner_repo="$1"   # e.g. "nidhinjs/prompt-master"
  local encoded
  encoded=$(python3 -c "import urllib.parse; print(urllib.parse.quote('github.com/$owner_repo', safe=''))")
  local url="https://api.deps.dev/v3/projects/${encoded}"

  local response
  response=$(curl -sf --max-time 15 "$url" 2>/dev/null) || {
    log "  deps.dev: no data for $owner_repo — skipping security check (allowing update)"
    return 0
  }

  local score vulns
  score=$(echo "$response" | jq -r '.scorecard.score // "n/a"')
  vulns=$(echo "$response" | jq '[.advisories // [] | .[] | select(.severity == "CRITICAL" or .severity == "HIGH")] | length')

  log "  deps.dev score: $score  |  critical/high advisories: $vulns"

  if [ "$vulns" -gt 0 ]; then
    log "  BLOCKED: $vulns critical/high advisory/ies. Manual review required."
    return 1
  fi
  return 0
}

update_submodule() {
  local path="$1"           # relative to REPO_DIR, e.g. "skills/prompt-master"
  local name
  name=$(basename "$path")

  log "--- $name ---"

  # Resolve upstream remote URL → derive owner/repo for deps.dev
  local remote_url
  remote_url=$(git -C "$REPO_DIR/$path" remote get-url origin 2>/dev/null) || {
    log "  No git remote — skipping"
    return
  }

  # Extract owner/repo from https://github.com/owner/repo.git or git@github.com:owner/repo.git
  local owner_repo
  owner_repo=$(echo "$remote_url" | sed -E 's|.*github\.com[:/]||; s|\.git$||')

  local current latest
  current=$(git -C "$REPO_DIR" ls-tree HEAD "$path" | awk '{print $3}')
  latest=$(git ls-remote "https://github.com/${owner_repo}.git" HEAD 2>/dev/null | awk '{print $1}')

  if [ -z "$latest" ]; then
    log "  Could not resolve upstream HEAD — skipping"
    return
  fi

  if [ "$latest" = "$current" ]; then
    log "  Up to date at ${current:0:8}"
    return
  fi

  log "  Update available: ${current:0:8} → ${latest:0:8}"

  if ! deps_dev_ok "$owner_repo"; then
    return
  fi

  # Update pin
  git -C "$REPO_DIR/$path" fetch --quiet origin
  git -C "$REPO_DIR/$path" checkout --quiet "$latest"
  git -C "$REPO_DIR" add "$path"
  git -C "$REPO_DIR" commit -m "chore: auto-update $name to ${latest:0:8}"
  git -C "$REPO_DIR" push --quiet

  log "  Pinned to ${latest:0:8} and pushed."
}

log "====== Submodule update run ======"

# Enumerate all submodule paths from .gitmodules
while IFS= read -r path; do
  update_submodule "$path"
done < <(git -C "$REPO_DIR" submodule foreach --quiet --recursive 'echo $displaypath')

log "====== Done ======"
