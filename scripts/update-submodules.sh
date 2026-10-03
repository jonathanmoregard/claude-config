#!/usr/bin/env bash
# Weekly submodule updater — checks deps.dev before pinning new commits.
#
# The live checkout (~/.claude) is pinned to master at the git layer
# (.githooks/): nothing may be committed or pushed from it. So a bump is
# proposed the same way every other change is: in a throwaway linked worktree
# off freshly fetched origin/master, committed there, pushed as its own branch,
# and opened as a PR. The live checkout is only READ (its .gitmodules and the
# shared object store); its HEAD, index and working tree are never touched.
# Once the PR merges, claude-pull.sh fast-forwards the checkout and runs
# `git submodule update --init`, which moves the submodule itself.
#
# Idempotent across runs: the branch name encodes the target commit, so a bump
# that already has a branch on origin (PR open or merged) is skipped.
set -euo pipefail

REPO_DIR="${CLAUDE_SUBMODULE_REPO:-/home/jonathan/.claude}"
LOG="${CLAUDE_SUBMODULE_LOG:-$REPO_DIR/logs/submodule-update.log}"
WT_PARENT="${CLAUDE_SUBMODULE_WORKTREE_PARENT:-${TMPDIR:-/tmp}}"
mkdir -p "$(dirname "$LOG")"

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

# Worktrees this run created; removed on every exit path.
CREATED_WT=()
cleanup() {
  local wt
  for wt in "${CREATED_WT[@]+"${CREATED_WT[@]}"}"; do
    git -C "$REPO_DIR" worktree remove --force "$wt" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

update_submodule() {
  local name="$1" path="$2" url="$3"   # from origin/master's .gitmodules
  local short
  short=$(basename "$path")

  log "--- $short ---"

  # Extract owner/repo from https://github.com/owner/repo.git or git@github.com:owner/repo.git
  local owner_repo
  owner_repo=$(echo "$url" | sed -E 's|.*github\.com[:/]||; s|\.git$||')

  local current latest
  current=$(git -C "$REPO_DIR" ls-tree origin/master -- "$path" | awk '$2 == "commit" {print $3}')
  if [ -z "$current" ]; then
    log "  .gitmodules lists $path but origin/master has no gitlink there — skipping"
    return 0
  fi
  latest=$(git ls-remote "$url" HEAD 2>/dev/null | awk '{print $1}')

  if [ -z "$latest" ]; then
    log "  Could not resolve upstream HEAD — skipping"
    return 0
  fi

  if [ "$latest" = "$current" ]; then
    log "  Up to date at ${current:0:8}"
    return 0
  fi

  log "  Update available: ${current:0:8} → ${latest:0:8}"

  if ! deps_dev_ok "$owner_repo"; then
    return 0
  fi

  local branch="chore/auto-update-${short}-${latest:0:8}"
  if git -C "$REPO_DIR" ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
    log "  $branch already on origin (PR open or merged) — nothing to do"
    return 0
  fi

  # Throwaway linked worktree off fresh origin/master. A pre-existing local
  # branch of that name is a leftover of an earlier failed run of this script.
  local wt
  wt=$(mktemp -d "$WT_PARENT/claude-submodule-bump.XXXXXX")   # empty dir: worktree add accepts it
  git -C "$REPO_DIR" branch -D "$branch" >/dev/null 2>&1 || true
  git -C "$REPO_DIR" worktree add --quiet --no-track -b "$branch" "$wt" origin/master
  CREATED_WT+=("$wt")

  # Move the gitlink without cloning the submodule: the pin is just the commit
  # id recorded in the superproject's tree.
  git -C "$wt" update-index --cacheinfo "160000,$latest,$path"
  git -C "$wt" commit --quiet -m "chore: auto-update $short to ${latest:0:8}"
  git -C "$wt" push --quiet -u origin "$branch"

  local body="Automated weekly bump of \`$path\` from \`${current:0:8}\` to \`${latest:0:8}\` (upstream HEAD of $url). deps.dev reported no critical/high advisories.

Opened by scripts/update-submodules.sh from a throwaway worktree; the live ~/.claude checkout was not touched. After merge, claude-pull.sh fast-forwards it and runs \`git submodule update --init\`."
  local pr_url
  if pr_url=$(cd "$wt" && gh pr create --head "$branch" --title "chore: auto-update $short to ${latest:0:8}" --body "$body" 2>&1); then
    log "  Pushed $branch and opened $pr_url"
  else
    log "  Pushed $branch but could not open the PR: $pr_url"
    return 1
  fi

  git -C "$REPO_DIR" worktree remove --force "$wt"
  CREATED_WT=()
  git -C "$REPO_DIR" branch -D "$branch" >/dev/null 2>&1 || true
}

log "====== Submodule update run ======"

# Fetch only refs/remotes/origin/master; the checkout's own refs do not move.
git -C "$REPO_DIR" fetch --quiet origin master

# Enumerate submodules from origin/master's .gitmodules, not the live working
# tree, so an uninitialised or stale checkout cannot hide (or invent) one.
rc=0
while IFS= read -r key; do
  name=${key#submodule.}; name=${name%.path}
  path=$(git -C "$REPO_DIR" config --blob origin/master:.gitmodules "submodule.$name.path")
  url=$(git -C "$REPO_DIR" config --blob origin/master:.gitmodules "submodule.$name.url" || true)
  [ -n "$url" ] || { log "--- $name --- no url in .gitmodules — skipping"; continue; }
  update_submodule "$name" "$path" "$url" || rc=1
done < <(git -C "$REPO_DIR" config --blob origin/master:.gitmodules --name-only --get-regexp '^submodule\..*\.path$' 2>/dev/null || true)

log "====== Done ======"
exit "$rc"
