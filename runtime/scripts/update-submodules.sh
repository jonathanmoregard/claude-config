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

# Where the checkout is: `~/.claude` before the runtime-projection cutover, the
# backing clone after it. scripts/config-repo.sh owns that rule and explains
# why the script's own location stops answering. CLAUDE_SUBMODULE_REPO stays
# the override this script has always honoured.
#
# This one fails QUIETLY if it gets the answer wrong, which is why it matters:
# it runs from cron (Mondays 10:00), nothing watches its exit status, and its
# only output goes to a log file nobody opens.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/config-repo.sh"
if [ -n "${CLAUDE_SUBMODULE_REPO:-}" ]; then
  REPO_DIR="$CLAUDE_SUBMODULE_REPO"
else
  REPO_DIR="$(claude_config_repo "$script_dir")" || exit 1
fi

# The log stays at ~/.claude/logs, NOT $REPO_DIR/logs. Post-cutover REPO_DIR is
# a git worktree, and `logs/` is not ignored there, so deriving the log path
# from it would start writing an un-ignored file inside a repository that
# automation commits from. `logs` is not part of the runtime subset, so the
# live path stays an ordinary local directory that home-manager creates.
LOG="${CLAUDE_SUBMODULE_LOG:-$HOME/.claude/logs/submodule-update.log}"
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
  local title="chore: auto-update $short to ${latest:0:8}"
  local body="Automated weekly bump of \`$path\` from \`${current:0:8}\` to \`${latest:0:8}\` (upstream HEAD of $url). deps.dev reported no critical/high advisories.

Opened by scripts/update-submodules.sh from a throwaway worktree; the live ~/.claude checkout was not touched. After merge, claude-pull.sh fast-forwards it and runs \`git submodule update --init\`."

  # This function runs as `update_submodule ... || rc=1`, a context in which
  # bash ignores `set -e`; every step below therefore checks its own status.
  # Without that, a commit blocked by the gitleaks pre-commit would fall
  # through to pushing an empty branch.
  if git -C "$REPO_DIR" ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
    # The branch alone does not prove a PR exists: an earlier run may have
    # pushed it and then failed at `gh pr create`. Skipping on the branch
    # would make that bump invisible forever, so ask GitHub.
    local prs
    prs=$(cd "$REPO_DIR" && gh pr list --head "$branch" --state all --json number --jq length 2>/dev/null)
    if ! [[ "$prs" =~ ^[0-9]+$ ]]; then
      log "  $branch is on origin but its PR state could not be read (gh pr list failed)"
      return 1
    fi
    if [ "$prs" != 0 ]; then
      log "  $branch already on origin (PR open or merged) — nothing to do"
      return 0
    fi
    local pr_url
    if pr_url=$(cd "$REPO_DIR" && gh pr create --head "$branch" --title "$title" --body "$body" 2>&1); then
      log "  $branch was on origin without a PR (earlier run failed); opened $pr_url"
      return 0
    fi
    log "  $branch is on origin but still has no PR: $pr_url"
    return 1
  fi

  # Throwaway linked worktree off fresh origin/master. A pre-existing local
  # branch of that name is a leftover of an earlier failed run of this script.
  local wt
  wt=$(mktemp -d "$WT_PARENT/claude-submodule-bump.XXXXXX") || return 1   # empty dir: worktree add accepts it
  git -C "$REPO_DIR" branch -D "$branch" >/dev/null 2>&1 || true
  if ! git -C "$REPO_DIR" worktree add --quiet --no-track -b "$branch" "$wt" origin/master; then
    log "  could not create the throwaway worktree $wt"
    rmdir "$wt" 2>/dev/null || true
    return 1
  fi
  CREATED_WT+=("$wt")

  # Move the gitlink without cloning the submodule: the pin is just the commit
  # id recorded in the superproject's tree.
  if ! git -C "$wt" update-index --cacheinfo "160000,$latest,$path" \
    || ! git -C "$wt" commit --quiet -m "$title"; then
    log "  could not commit the bump in $wt — nothing pushed"
    return 1
  fi
  if ! git -C "$wt" push --quiet -u origin "$branch"; then
    log "  could not push $branch"
    return 1
  fi

  local pr_url
  if pr_url=$(cd "$wt" && gh pr create --head "$branch" --title "$title" --body "$body" 2>&1); then
    log "  Pushed $branch and opened $pr_url"
  else
    log "  Pushed $branch but could not open the PR (the next run retries): $pr_url"
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
