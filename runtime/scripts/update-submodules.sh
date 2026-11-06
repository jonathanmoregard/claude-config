#!/usr/bin/env bash
# Weekly vendor updater — checks deps.dev before syncing new fork content.
#
# The forks are vendored as plain files under runtime/vendor/, not as
# submodules: a projected tree has to carry its own content, because the live
# ~/.claude is a read-only mount of runtime/ and a gitlink projects as an empty
# directory. So an update is a file diff, and this script produces it by
# fetching the fork and replacing each of its tracked_paths in the tree.
#
# The live checkout is pinned to master at the git layer (.githooks/): nothing
# may be committed or pushed from it. So an update is proposed the same way
# every other change is: in a throwaway linked worktree off freshly fetched
# origin/master, committed there, pushed as its own branch, and opened as a PR.
# The live checkout is only READ (its config blob and the shared object store);
# its HEAD, index and working tree are never touched.
#
# Idempotent across runs: the branch name encodes the target commit, so an
# update that already has a branch on origin (PR open or merged) is skipped.
#
# The filename says submodules because the weekly cron entry in nixos-config
# names it by path. Renaming it needs that repo to move first.
set -euo pipefail

CONFIG_PATH="runtime/upstream-drift.json"

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

# Worktrees and fetch caches this run created; removed on every exit path.
CREATED_WT=()
CREATED_TMP=()
cleanup() {
  local wt dir
  for wt in "${CREATED_WT[@]+"${CREATED_WT[@]}"}"; do
    git -C "$REPO_DIR" worktree remove --force "$wt" >/dev/null 2>&1 || true
  done
  for dir in "${CREATED_TMP[@]+"${CREATED_TMP[@]}"}"; do
    rm -rf -- "$dir" || true
  done
}
trap cleanup EXIT

# The fork's tracked paths, as a tar stream, extracted over the vendor
# directory. `git rm` first so a path deleted upstream disappears here too
# instead of being left behind as a file nothing updates again.
sync_vendor() {
  local wt="$1" fork="$2" branch="$3" vendor_path="$4" latest="$5" excluded="$6"
  shift 6
  local cache
  cache=$(mktemp -d "$WT_PARENT/claude-vendor-fetch.XXXXXX") || return 1
  CREATED_TMP+=("$cache")

  if ! git init --quiet --bare "$cache" \
    || ! git -C "$cache" fetch --quiet --depth 1 "$fork" "refs/heads/$branch"; then
    log "  could not fetch $branch from $fork"
    return 1
  fi
  # Fetch the branch, then check it is still the commit this run decided on:
  # fetching $latest directly needs the server to allow a SHA in want, and a
  # branch that moved mid-run would otherwise be vendored under the wrong
  # commit in the branch name and the recorded one.
  local fetched
  fetched=$(git -C "$cache" rev-parse FETCH_HEAD) || return 1
  if [ "$fetched" != "$latest" ]; then
    log "  $branch moved mid-run (${latest:0:8} → ${fetched:0:8}) — the next run picks it up"
    return 2
  fi

  if ! git -C "$wt" rm -r --quiet -- "$vendor_path"; then
    log "  could not clear $vendor_path in the worktree"
    return 1
  fi
  mkdir -p "$wt/$vendor_path" || return 1
  # A tracked path that no longer exists upstream makes git archive fail, which
  # fails the run: a skill we consume vanishing is a thing to look at, not to
  # vendor away silently.
  if ! git -C "$cache" archive --format=tar FETCH_HEAD -- "$@" | tar -x -C "$wt/$vendor_path"; then
    log "  could not extract the tracked paths of $latest"
    return 1
  fi

  # A fork's own dev tree, dropped after extraction rather than carved out of
  # tracked_paths: the deploy must not serve it (tests/enforced refuses test
  # code under runtime/), but upstream's new runtime files still arrive without
  # anyone editing this list. Absent is fine — upstream deleting its own tests
  # is not something to look at.
  local drops=() drop
  [ -n "$excluded" ] && IFS=$'\x1f' read -r -a drops <<<"$excluded"
  for drop in "${drops[@]+"${drops[@]}"}"; do
    # An empty or climbing entry would aim this at the vendor directory itself,
    # or above it.
    case "$drop" in
      "" | /* | *..*) log "  refusing to drop '$drop'"; return 1 ;;
    esac
    rm -rf -- "$wt/$vendor_path/$drop" || return 1
  done
}

record_commit() {
  local wt="$1" name="$2" old="$3" new="$4"
  python3 - "$wt/$CONFIG_PATH" "$name" "$old" "$new" <<'PY'
import json, sys

path, name, old, new = sys.argv[1:]
with open(path, encoding="utf-8") as fh:
    text = fh.read()
# Replace the commit as text rather than re-serialising: a json.dump would
# reformat the whole file, burying the one line that changed.
if text.count(old) != 1:
    sys.exit(f"{old} appears {text.count(old)} time(s) in {path}; expected 1")
with open(path, "w", encoding="utf-8") as fh:
    fh.write(text.replace(old, new))
with open(path, encoding="utf-8") as fh:
    entries = json.load(fh)["entries"]
if not any(e.get("name") == name and e.get("vendored_commit") == new for e in entries):
    sys.exit(f"{path} does not record {new} for {name} after the edit")
PY
}

update_entry() {
  local name="$1" fork="$2" branch="$3" vendor_path="$4" current="$5" packed="$6" excluded="$7"
  local short
  short=$(basename "${vendor_path:-$name}")

  log "--- $short ---"
  if [ -z "$fork" ] || [ -z "$branch" ] || [ -z "$vendor_path" ] \
    || [ -z "$current" ] || [ -z "$packed" ]; then
    log "  $CONFIG_PATH entry is missing fork_repo, fork_branch, vendor_path, vendored_commit or tracked_paths — skipping"
    return 0
  fi
  local paths
  IFS=$'\x1f' read -r -a paths <<<"$packed"

  # Extract owner/repo from https://github.com/owner/repo.git or git@github.com:owner/repo.git
  local owner_repo
  owner_repo=$(echo "$fork" | sed -E 's|.*github\.com[:/]||; s|\.git$||')

  local latest
  latest=$(git ls-remote "$fork" "refs/heads/$branch" 2>/dev/null | awk '{print $1}')
  if [ -z "$latest" ]; then
    log "  could not resolve $branch on $fork — skipping"
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

  local branch_name="chore/auto-update-${short}-${latest:0:8}"
  local title="chore: auto-update $short to ${latest:0:8}"
  local body="Automated weekly sync of \`$vendor_path\` from \`${current:0:8}\` to \`${latest:0:8}\` (head of \`$branch\` on $fork). deps.dev reported no critical/high advisories.

The tracked paths in \`$CONFIG_PATH\` are replaced with the fork's copy and \`vendored_commit\` records the new commit. Nothing is pinned by reference: the live ~/.claude is a read-only mount of \`runtime/\`, where a gitlink would project as an empty directory.

Opened by scripts/update-submodules.sh from a throwaway worktree; the live ~/.claude checkout was not touched."

  # This function runs as `update_entry ... || rc=1`, a context in which bash
  # ignores `set -e`; every step below therefore checks its own status. Without
  # that, a commit blocked by the gitleaks pre-commit would fall through to
  # pushing an empty branch.
  if git -C "$REPO_DIR" ls-remote --exit-code --heads origin "$branch_name" >/dev/null 2>&1; then
    # The branch alone does not prove a PR exists: an earlier run may have
    # pushed it and then failed at `gh pr create`. Skipping on the branch
    # would make that update invisible forever, so ask GitHub.
    local prs
    prs=$(cd "$REPO_DIR" && gh pr list --head "$branch_name" --state all --json number --jq length 2>/dev/null)
    if ! [[ "$prs" =~ ^[0-9]+$ ]]; then
      log "  $branch_name is on origin but its PR state could not be read (gh pr list failed)"
      return 1
    fi
    if [ "$prs" != 0 ]; then
      log "  $branch_name already on origin (PR open or merged) — nothing to do"
      return 0
    fi
    local pr_url
    if pr_url=$(cd "$REPO_DIR" && gh pr create --head "$branch_name" --title "$title" --body "$body" 2>&1); then
      log "  $branch_name was on origin without a PR (earlier run failed); opened $pr_url"
      return 0
    fi
    log "  $branch_name is on origin but still has no PR: $pr_url"
    return 1
  fi

  # Throwaway linked worktree off fresh origin/master. A pre-existing local
  # branch of that name is a leftover of an earlier failed run of this script.
  local wt
  wt=$(mktemp -d "$WT_PARENT/claude-vendor-bump.XXXXXX") || return 1   # empty dir: worktree add accepts it
  git -C "$REPO_DIR" branch -D "$branch_name" >/dev/null 2>&1 || true
  if ! git -C "$REPO_DIR" worktree add --quiet --no-track -b "$branch_name" "$wt" origin/master; then
    log "  could not create the throwaway worktree $wt"
    rmdir "$wt" 2>/dev/null || true
    return 1
  fi
  CREATED_WT+=("$wt")

  sync_vendor "$wt" "$fork" "$branch" "$vendor_path" "$latest" "$excluded" "${paths[@]}"
  case $? in
    0) ;;
    2) return 0 ;;   # the branch moved under us; nothing committed
    *) return 1 ;;
  esac
  if ! record_commit "$wt" "$name" "$current" "$latest"; then
    log "  could not record ${latest:0:8} in $CONFIG_PATH — nothing pushed"
    return 1
  fi
  if ! git -C "$wt" add -A -- "$vendor_path" "$CONFIG_PATH" \
    || ! git -C "$wt" commit --quiet -m "$title"; then
    log "  could not commit the update in $wt — nothing pushed"
    return 1
  fi
  if ! git -C "$wt" push --quiet -u origin "$branch_name"; then
    log "  could not push $branch_name"
    return 1
  fi

  local pr_url
  if pr_url=$(cd "$wt" && gh pr create --head "$branch_name" --title "$title" --body "$body" 2>&1); then
    log "  Pushed $branch_name and opened $pr_url"
  else
    log "  Pushed $branch_name but could not open the PR (the next run retries): $pr_url"
    return 1
  fi

  git -C "$REPO_DIR" worktree remove --force "$wt"
  CREATED_WT=()
  git -C "$REPO_DIR" branch -D "$branch_name" >/dev/null 2>&1 || true
}

log "====== Vendor update run ======"

# Fetch only refs/remotes/origin/master; the checkout's own refs do not move.
git -C "$REPO_DIR" fetch --quiet origin master

# Enumerate from origin/master's config blob, not the live working tree, so a
# stale or half-written checkout cannot hide (or invent) an entry.
rc=0
while IFS=$'\t' read -r name fork branch vendor_path current packed excluded; do
  update_entry "$name" "$fork" "$branch" "$vendor_path" "$current" "$packed" "$excluded" || rc=1
done < <(git -C "$REPO_DIR" show "origin/master:$CONFIG_PATH" 2>/dev/null | python3 -c '
import json, sys

for entry in json.load(sys.stdin)["entries"]:
    print("\t".join([
        entry.get("name", ""),
        entry.get("fork_repo", ""),
        entry.get("fork_branch", ""),
        entry.get("vendor_path", ""),
        entry.get("vendored_commit", ""),
        "\x1f".join(entry.get("tracked_paths", [])),
        "\x1f".join(entry.get("excluded_paths", [])),
    ]))
' || true)

log "====== Done ======"
exit "$rc"
