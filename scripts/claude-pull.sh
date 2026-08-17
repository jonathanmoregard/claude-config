#!/usr/bin/env bash
# Continuous delivery for the ~/.claude repo: fast-forward the live
# checkout to whatever has landed on origin.
#
# The counterpart to sync-agent.sh, which only ever pushes. Without a pull
# side, a PR merged in the GitHub UI never reaches the machine that runs
# the config: skills, hooks and settings stay at whatever the last local
# commit was, and the next auto-push run fails non-fast-forward once the
# two sides diverge.
#
# Deliberately NOT modelled on nixos-deploy's `git reset --hard`. That repo
# is a deploy target with no local authorship; ~/.claude is a live working
# tree that Claude sessions write to continuously, so a hard reset would
# silently destroy in-flight work. Everything here is fast-forward-only and
# fails loudly instead of resolving anything by force.
#
# Run from a systemd user timer (see /etc/nixos/home/claude-services.nix);
# stdout/stderr land in the journal: journalctl --user -u claude-pull

set -euo pipefail

# Normally derived from the script's own location, so the copy that ships in
# the repo always operates on that repo. CLAUDE_PULL_REPO overrides it for
# tests against a throwaway clone.
REPO="${CLAUDE_PULL_REPO:-$(git -C "$(dirname "$(readlink -f "$0")")" rev-parse --show-toplevel)}"
cd "$REPO"

# `.git` is a FILE, not a directory, in a linked worktree — ask git for the
# real path rather than assuming. In the main checkout this resolves to
# $REPO/.git, which is the same lock file sync-agent.sh opens.
GIT_DIR_PATH="$(git rev-parse --absolute-git-dir)"
STATE="$GIT_DIR_PATH/claude-pull"
mkdir -p "$STATE"

log() { printf '%s claude-pull: %s\n' "$(date -Is)" "$*"; }

# Only states a human has to act on raise a notification, and only on the
# transition into them: these run every 10 minutes, and a condition like
# "diverged from origin" persists until someone fixes it. Re-notifying each
# tick would train the notification away.
alert() {
  local reason="$1"
  log "ALERT: $reason"

  if [ -f "$STATE/last-alert" ] && [ "$(cat "$STATE/last-alert")" = "$reason" ]; then
    return 0
  fi
  printf '%s' "$reason" > "$STATE/last-alert"

  command -v notify-send >/dev/null 2>&1 || {
    log "notify-send not on PATH — alert recorded in journal only"
    return 0
  }
  export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"
  notify-send -u normal "claude-pull needs a hand" \
    "$reason

Repo: $REPO
Log:  journalctl --user -u claude-pull" ||
    log "notify-send failed (no notification daemon?) — alert in journal only"
}

clear_alert() { rm -f "$STATE/last-alert"; }

# Shared with sync-agent.sh so a pull can never land halfway through the
# commit-and-push agent's run (or vice versa). Non-blocking: skipping a tick
# costs 10 minutes, waiting could hold the lock behind a multi-minute agent.
exec {lock_fd}>"$GIT_DIR_PATH/claude-sync.lock"
flock -n "$lock_fd" || { log "another sync/pull run holds the lock; skipping tick"; exit 0; }

BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
if [ -z "$BRANCH" ]; then
  alert "HEAD is detached — auto-pull is off until a branch is checked out."
  exit 1
fi

if ! UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"; then
  alert "Branch '$BRANCH' has no upstream — nothing to pull from."
  exit 1
fi

# Bound the network op. A stalled connection (DNS hang, half-open socket,
# GitHub blip) must not wedge the run while holding the lock — nixos-deploy
# spent 17h "activating" on exactly that failure before it was bounded.
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"

if ! timeout 60 git fetch --quiet --prune origin "$BRANCH"; then
  # A laptop is offline often enough that one failed fetch is noise. Only a
  # run of them means something is actually broken (expired key, bad remote),
  # so count consecutive failures and speak up after an hour of them.
  fails=$(( $(cat "$STATE/fetch-failures" 2>/dev/null || echo 0) + 1 ))
  printf '%s' "$fails" > "$STATE/fetch-failures"
  log "fetch failed or timed out (60s) — consecutive failures: $fails"
  if [ "$fails" -ge 6 ]; then
    alert "git fetch has failed $fails times in a row — check network, SSH key, or the origin remote."
  fi
  exit 1
fi
rm -f "$STATE/fetch-failures"

local_sha="$(git rev-parse HEAD)"
remote_sha="$(git rev-parse "$UPSTREAM")"
base_sha="$(git merge-base HEAD "$UPSTREAM")"

if [ "$local_sha" = "$remote_sha" ]; then
  clear_alert
  exit 0
fi

if [ "$remote_sha" = "$base_sha" ]; then
  # Local commits not yet pushed — sync-agent.sh's job, not this script's.
  clear_alert
  log "$BRANCH is ahead of $UPSTREAM by $(git rev-list --count "$UPSTREAM..HEAD") commit(s); nothing to pull"
  exit 0
fi

if [ "$local_sha" != "$base_sha" ]; then
  alert "$BRANCH and $UPSTREAM have diverged ($(git rev-list --count "$UPSTREAM..HEAD") local / $(git rev-list --count "HEAD..$UPSTREAM") remote commits).
Auto-pull will not rebase or merge that for you — resolve it by hand."
  exit 1
fi

# Fast-forward only. If the incoming commits touch files that are modified
# or untracked here, git refuses and changes nothing — which is the desired
# outcome: local work outranks an automated update.
incoming="$(git rev-list --count "HEAD..$UPSTREAM")"
if ! ff_err="$(git merge --ff-only --quiet "$UPSTREAM" 2>&1)"; then
  alert "Cannot fast-forward $BRANCH to $UPSTREAM ($incoming commit(s) waiting) — local changes are in the way:
$(printf '%s' "$ff_err" | head -5)"
  exit 1
fi

log "fast-forwarded $BRANCH: $(git rev-parse --short "$base_sha") -> $(git rev-parse --short HEAD) ($incoming commit(s))"

# Submodule pointers move with the superproject, so a pull that bumps one
# leaves the submodule at the old commit until it is checked out. Best
# effort: a submodule with local edits is a reason to log, not to fail the
# pull that already succeeded.
if [ -f .gitmodules ]; then
  if ! sub_err="$(git submodule update --init --recursive --quiet 2>&1)"; then
    log "submodule update failed (superproject is still fast-forwarded): $(printf '%s' "$sub_err" | head -3)"
  fi
fi

clear_alert
