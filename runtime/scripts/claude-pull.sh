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
# Not ~/.claude-only any more: the same script drives every git checkout the
# harness reads from, one systemd unit per repo, selected with
# CLAUDE_PULL_REPO. The marketplace clone under
# ~/.claude/plugins/marketplaces/ is the second such target — it is an
# independent nested checkout (`plugins/` is gitignored in ~/.claude), it is
# where the harness actually resolves plugins from, and until it got its own
# unit it went 35 commits stale while every session served old skills.
#
# Run from a systemd user timer (see /etc/nixos/home/claude-services.nix);
# stdout/stderr land in the journal: journalctl --user -u claude-pull

set -euo pipefail

# Normally derived from the script's own location, so the copy that ships in
# the repo always operates on that repo. CLAUDE_PULL_REPO selects a different
# target — a sibling systemd unit in production, a throwaway clone in tests.
REPO="${CLAUDE_PULL_REPO:-$(git -C "$(dirname "$(readlink -f "$0")")" rev-parse --show-toplevel)}"
case "$REPO" in /*) ;; *) REPO="$PWD/$REPO" ;; esac
REPO="${REPO%/}"
PULL_UNIT="${CLAUDE_PULL_UNIT:-claude-pull}"

# State lives in userspace keyed by the repo it describes, NOT inside that
# repo's $GIT_DIR. Two reasons, and the second one is load-bearing:
#
#   * the health record below is read at SessionStart by
#     hooks/claude-pull-health.py. A reader that has to reach into a .git
#     directory to learn whether a checkout is healthy trips Claude Code's
#     protected-path gate every session;
#   * a puller that can only report THROUGH the repo it is reporting on goes
#     silent exactly when the repo is the broken thing. `cd` into a deleted
#     directory or `rev-parse` inside a corrupt one, and there is nowhere left
#     to write "this repo is broken".
#
# Slug is the absolute path with '/' -> '%', the shape `systemd-escape -p`
# produces, so the directory says which repo it belongs to without a lookup.
STATE_ROOT="${CLAUDE_PULL_STATE_ROOT:-${XDG_STATE_HOME:-$HOME/.local/state}/claude-pull}"
STATE="$STATE_ROOT/${REPO//\//%}"
mkdir -p "$STATE"

log() { printf '%s claude-pull: %s\n' "$(date -Is)" "$*"; }

# Reasons are assembled from git's stderr, which carries quotes and newlines.
# Emitting them raw would produce a record that json.load rejects — and a
# health channel whose payload cannot be parsed is a health channel that
# reports nothing, which is the failure being fixed, one level up.
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/\\r}"
  s="${s//$'\t'/\\t}"
  # Nothing else below 0x20 belongs in a JSON string literal, or in a reason a
  # human is meant to read.
  printf '%s' "$s" | tr -d '\000-\010\013\014\016-\037'
}

# The Claude-visible half of the alerting. notify-send reaches Jonathan's
# desktop and no Claude session, so before this record existed a checkout that
# had been unpullable for a week looked, from inside a session, exactly like
# one that fast-forwarded a minute ago. Same shape as the aggregator-embed
# incident: failed every 30 minutes for ~5 hours behind a 24h notification
# debounce, and no session knew.
#
# Three states, and the middle one is the point. `unverified` is what a failed
# fetch records: the tick could not establish whether the repo is current, and
# calling that `ok` would be precisely the lie the channel exists to prevent.
# `checked_at_epoch` is refreshed on every verdict INCLUDING a repeated alert,
# so "stale" means the puller stopped running rather than "the puller has been
# complaining for a while". A tick that skipped without checking anything —
# lock contention — writes nothing at all and leaves the last real verdict
# standing.
write_status() { # write_status <ok|unverified|alert> [reason]
  local state="$1" reason="${2:-}" now last_ok tmp
  now="$(date +%s)"
  last_ok="$(cat "$STATE/last-ok-epoch" 2>/dev/null || true)"
  case "$last_ok" in ''|*[!0-9]*) last_ok=0 ;; esac
  if [ "$state" = ok ]; then
    last_ok="$now"
    printf '%s' "$now" > "$STATE/last-ok-epoch"
  fi
  tmp="$STATE/.status.json.$$"
  printf '{"repo":"%s","state":"%s","reason":"%s","checked_at_epoch":%s,"checked_at":"%s","last_ok_epoch":%s}\n' \
    "$(json_escape "$REPO")" "$state" "$(json_escape "$reason")" \
    "$now" "$(date -Is -d "@$now")" "$last_ok" > "$tmp"
  # rename(2), so a reader never sees a half-written verdict.
  mv -f "$tmp" "$STATE/status.json"
}

# Only states a human has to act on raise a notification, and only on the
# transition into them: these run on a timer, and a condition like
# "diverged from origin" persists until someone fixes it. Re-notifying each
# tick would train the notification away. The status record is written on every
# tick regardless — the debounce is a property of the toast, not of the truth.
#
# The debounce is keyed on the optional second argument, defaulting to the
# reason. A reason that carries a changing detail (a failure count) MUST pass a
# stable key: keyed on its text, every tick looked like a new problem and the
# fetch-failure alert raised 88 identical toasts on 2026-09-27.
alert() { # alert <reason> [debounce-key]
  local reason="$1" key="${2:-$1}"
  log "ALERT: $reason"
  write_status alert "$reason"

  if [ -f "$STATE/last-alert" ] && [ "$(cat "$STATE/last-alert")" = "$key" ]; then
    return 0
  fi
  printf '%s' "$key" > "$STATE/last-alert"

  command -v notify-send >/dev/null 2>&1 || {
    log "notify-send not on PATH — alert recorded in journal only"
    return 0
  }
  export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"
  notify-send -u normal "claude-pull needs a hand" \
    "$reason

Repo: $REPO
Log:  journalctl --user -u $PULL_UNIT" ||
    log "notify-send failed (no notification daemon?) — alert in journal only"
}

# A tick that reached a verdict of "nothing to act on": clear the toast
# debounce so the next distinct problem speaks, and stamp the record so the
# SessionStart hook can tell "checked and healthy" from "nobody has checked".
clear_alert() { rm -f "$STATE/last-alert"; write_status ok ""; }

# ── Required tools, checked once, up front ───────────────────────────────────
#
# Two separate branches below read a non-zero exit as a BENIGN condition, and
# "command not found" is non-zero:
#
#   * `flock -n` failing means "another run holds the lock; skipping tick",
#     exit 0, no status record at all;
#   * `getent ahosts` failing means "this host is offline", exit 0, and the
#     fetch-failure counter that drives the six-strike alert is never touched.
#
# So a missing binary does not fail here — it impersonates a healthy skip, for
# as long as nobody looks. Both were measured on 2026-10-28, running this
# script from the systemd SYSTEM unit that deploys the tree ~/.claude is served
# from (nixos-config modules/nixos/claude-runtime-deploy.nix). A system service
# inherits systemd's compiled-in PATH — e2fsprogs, btrfs-progs, dosfstools,
# util-linux-MINIMAL, openssh, systemd — which has neither flock nor getent.
# The first real tick logged "flock: command not found" and then "another
# sync/pull run holds the lock; skipping tick", and the unit reported success.
#
# A preflight is the fix rather than a guard per call site, because the pattern
# generalises: every one of these branches is a "benign non-zero", and the next
# one added would inherit the same hole. The unit pins the dependencies too —
# this is what makes the same mistake anywhere else loud instead of green.
#
# python3 is deliberately NOT here: its one use site is already guarded by a
# `command -v` test and degrades to "skip the settings carry", which is a
# behaviour change rather than a lie about the result.
for tool in flock getent git; do
  command -v "$tool" >/dev/null 2>&1 || {
    alert "$tool is not on PATH, so this tick cannot run — refusing, because the branches that read its failure as 'lock held' or 'host offline' would otherwise report a healthy skip without pulling anything." "missing-tool-$tool"
    exit 1
  }
done

# Everything above this line works on a path, not on a repository, so a target
# that has been deleted or corrupted is still reportable. From here down it has
# to be a real checkout.
if ! cd "$REPO" 2>/dev/null; then
  alert "$REPO cannot be entered — the checkout is missing or unreadable, so auto-pull is off for it."
  exit 1
fi

# `.git` is a FILE, not a directory, in a linked worktree — ask git for the
# real path rather than assuming. In the main checkout this resolves to
# $REPO/.git, which is the same lock file sync-agent.sh opens.
if ! GIT_DIR_PATH="$(git rev-parse --absolute-git-dir 2>/dev/null)"; then
  alert "$REPO is not a git checkout — auto-pull has nothing to fast-forward there."
  exit 1
fi

# Shared with sync-agent.sh so a pull can never land halfway through the
# commit-and-push agent's run (or vice versa). Non-blocking: skipping a tick
# costs 10 minutes, waiting could hold the lock behind a multi-minute agent.
exec {lock_fd}>"$GIT_DIR_PATH/claude-sync.lock"
flock -n "$lock_fd" || { log "another sync/pull run holds the lock; skipping tick"; exit 0; }

require_clean="${CLAUDE_PULL_REQUIRE_CLEAN:-0}"
case "$require_clean" in
  0|1) ;;
  *)
    alert "CLAUDE_PULL_REQUIRE_CLEAN must be 0 or 1, not '$require_clean'."
    exit 1
    ;;
esac

BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
if [ -n "${CLAUDE_PULL_EXPECT_BRANCH:-}" ]; then
  if [ -z "$BRANCH" ]; then
    alert "The expected branch '$CLAUDE_PULL_EXPECT_BRANCH' cannot be verified because HEAD is detached."
    exit 1
  fi
  if [ "$BRANCH" != "$CLAUDE_PULL_EXPECT_BRANCH" ]; then
    alert "The expected branch '$CLAUDE_PULL_EXPECT_BRANCH' is not checked out; checked out '$BRANCH'."
    exit 1
  fi
fi

if [ -z "$BRANCH" ]; then
  alert "HEAD is detached — auto-pull is off until a branch is checked out."
  exit 1
fi

if [ "$require_clean" = 1 ]; then
  if ! tree_status="$(git status --porcelain=v1 --untracked-files=all 2>&1)"; then
    alert "CLAUDE_PULL_REQUIRE_CLEAN=1 could not inspect the working tree: $tree_status"
    exit 1
  fi
  if [ -n "$tree_status" ]; then
    alert "CLAUDE_PULL_REQUIRE_CLEAN=1 requires a clean working tree, but tracked or untracked changes are present."
    exit 1
  fi
fi

if ! UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"; then
  alert "Branch '$BRANCH' has no upstream — nothing to pull from."
  exit 1
fi
if [ -n "${CLAUDE_PULL_EXPECT_UPSTREAM:-}" ] \
  && [ "$UPSTREAM" != "$CLAUDE_PULL_EXPECT_UPSTREAM" ]; then
  alert "The expected upstream '$CLAUDE_PULL_EXPECT_UPSTREAM' is not configured; configured '$UPSTREAM'."
  exit 1
fi

# Bound the network op. A stalled connection (DNS hang, half-open socket,
# GitHub blip) must not wedge the run while holding the lock — nixos-deploy
# spent 17h "activating" on exactly that failure before it was bounded.
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"

if ! timeout 60 git fetch --quiet --prune origin "$BRANCH"; then
  # No uplink is not a fault: the tick is a skip, like lock contention. It
  # must not count toward the alert threshold (an offline evening used to
  # cross it within the hour) and must not fail the unit. The record still
  # says `unverified` — origin was not compared. A remote that is broken while
  # the host is online keeps counting and alerts once, below.
  probe_host="${CLAUDE_PULL_ONLINE_PROBE_HOST:-github.com}"
  if ! timeout 5 getent ahosts "$probe_host" >/dev/null 2>&1; then
    log "fetch failed while offline (cannot resolve $probe_host) — skipping tick, not counted"
    write_status unverified "offline: git fetch could not run (no DNS for $probe_host); this checkout has not been compared against origin since the last successful tick."
    exit 0
  fi
  # A laptop is offline often enough that one failed fetch is noise. Only a
  # run of them means something is actually broken (expired key, bad remote),
  # so count consecutive failures and speak up after six failed ticks.
  fails=$(( $(cat "$STATE/fetch-failures" 2>/dev/null || echo 0) + 1 ))
  printf '%s' "$fails" > "$STATE/fetch-failures"
  log "fetch failed or timed out (60s) — consecutive failures: $fails"
  if [ "$fails" -ge 6 ]; then
    alert "git fetch has failed $fails times in a row — check network, SSH key, or the origin remote." fetch-failing
  else
    # Below the notification threshold this is not yet a human's problem, but
    # it is emphatically not a clean bill of health either: the tick never
    # learned what origin holds. A session that reads `ok` here would conclude
    # its checkout is current on the strength of a fetch that did not happen.
    write_status unverified "git fetch failed $fails time(s) in a row; this checkout has not been compared against origin since the last successful tick."
  fi
  exit 1
fi
rm -f "$STATE/fetch-failures"

# Check out every submodule at the commit the superproject pins.
sync_submodules() {
  [ -f .gitmodules ] || return 0
  local sub_err
  if ! sub_err="$(git submodule update --init --recursive --quiet 2>&1)"; then
    log "submodule update failed (superproject is unaffected): $(printf '%s' "$sub_err" | head -3)"
  fi
}

# Report ok only when every submodule is checked out AT THE PINNED COMMIT.
# Skills link into vendor/, so an empty submodule is missing skills and a
# submodule a commit behind the pin is serving the wrong skills — neither is a
# healthy checkout. `git submodule status` distinguishes them: `-` for never
# initialized, `+` for initialized but not at the commit the superproject
# records. Checking only `-` is what let a failed `git submodule update` pass
# as ok while the superproject went on fast-forwarding past it.
#
# The end state is the authority here, not the exit code of the update that was
# supposed to produce it: sync_submodules logs its own failure and returns 0 on
# purpose, so a submodule with local edits cannot fail a pull that succeeded,
# and this function is what decides whether the result deserves `ok`.
finish_ok() {
  local status_lines missing stale odd
  status_lines="$(git submodule status --recursive 2>/dev/null)"
  missing="$(printf '%s\n' "$status_lines" | sed -n 's/^-[0-9a-f]* \([^ ]*\).*/\1/p')"
  stale="$(printf '%s\n' "$status_lines" | sed -n 's/^+[0-9a-f]* \([^ ]*\).*/\1/p')"
  # Anything that is neither in sync (leading space) nor one of the two states
  # above — `U` for a conflicted submodule today, whatever git adds tomorrow.
  # An unrecognised state is not evidence of health, so it alerts rather than
  # falling through to clear_alert.
  odd="$(printf '%s\n' "$status_lines" | sed -n 's/^[^ +-][0-9a-f]* \([^ ]*\).*/\1/p')"
  if [ -n "$missing" ]; then
    alert "Submodule(s) not checked out, so skills that link into them are missing: $(printf '%s' "$missing" | tr '\n' ' ')"
    exit 1
  fi
  if [ -n "$stale" ]; then
    alert "Submodule(s) not at the commit master pins, so the skills under them are a different revision than this checkout claims: $(printf '%s' "$stale" | tr '\n' ' ')"
    exit 1
  fi
  if [ -n "$odd" ]; then
    alert "Submodule(s) in a state this script does not recognise, so the checkout cannot be called healthy: $(printf '%s' "$odd" | tr '\n' ' ')"
    exit 1
  fi
  clear_alert
}

# A submodule whose checkout failed stays wrong until something retries it, and
# skills load through vendor/superpowers. So a tick with nothing to pull still
# retries any submodule that is not where the superproject pins it: `-` for one
# that was never checked out, `+` for one left at an older commit by a failed
# `git submodule update`. The `+` case is why this cannot gate on `-` alone —
# once the superproject is current, no later tick fast-forwards, so without
# this the mismatch would never be revisited and the alert from finish_ok would
# repeat every ten minutes with nothing acting on it.
if [ -f .gitmodules ] && git submodule status --recursive 2>/dev/null | grep -q '^[-+]'; then
  sync_submodules
fi

local_sha="$(git rev-parse HEAD)"
remote_sha="$(git rev-parse "$UPSTREAM")"
base_sha="$(git merge-base HEAD "$UPSTREAM")"

if [ "$local_sha" = "$remote_sha" ]; then
  finish_ok
  exit 0
fi

if [ "$remote_sha" = "$base_sha" ]; then
  # Local commits not yet pushed — sync-agent.sh's job, not this script's.
  finish_ok
  log "$BRANCH is ahead of $UPSTREAM by $(git rev-list --count "$UPSTREAM..HEAD") commit(s); nothing to pull"
  exit 0
fi

if [ "$local_sha" != "$base_sha" ]; then
  alert "$BRANCH and $UPSTREAM have diverged ($(git rev-list --count "$UPSTREAM..HEAD") local / $(git rev-list --count "HEAD..$UPSTREAM") remote commits).
Auto-pull will not rebase or merge that for you — resolve it by hand."
  exit 1
fi

# --- the model setting is not local work -------------------------------------
#
# `/model` in a live session persists `"model"` into settings.json in this
# checkout. The checkout is master-pinned, so that write is never committed —
# and from the next upstream settings.json edit onwards the fast-forward below
# refuses, parking continuous delivery on a transient UI choice until a human
# intervenes. One key is carried across the pull instead.
#
# ONLY that key, and the test is semantic: both versions are parsed and
# compared with `model` removed. If anything else differs — a permission rule,
# a hook, an env var — this does nothing at all and the pull blocks exactly as
# before, because those edits are real work that outranks an automated update.
#
# Deliberately not `git stash`: the stash stack is shared with every linked
# worktree and other agents push to it concurrently, and `stash pop` performs a
# three-way merge that can conflict — which is the failure being fixed, not a
# fix for it. Restoring one path and rewriting one key cannot conflict.
CARRY_SETTINGS="settings.json"
carry_doc=""        # {"model": <value>} to re-apply, or {} for "delete the key"
carry_backup=""     # exact pre-pull bytes, the safety net if re-applying fails
carry_armed=0

# Parse HEAD's copy (stdin) against the working tree's (argv 1). Exit 0 and
# print the carry document only when `model` is the sole difference.
carry_capture() {
  git show "HEAD:./$CARRY_SETTINGS" 2>/dev/null | python3 -c '
import json, sys
try:
    head = json.load(sys.stdin)
    with open(sys.argv[1], encoding="utf-8") as fh:
        local = json.load(fh)
except (OSError, ValueError):
    raise SystemExit(1)
if not isinstance(head, dict) or not isinstance(local, dict):
    raise SystemExit(1)
if {k: v for k, v in head.items() if k != "model"} != {k: v for k, v in local.items() if k != "model"}:
    raise SystemExit(1)
if head.get("model", None) == local.get("model", None) and ("model" in head) == ("model" in local):
    raise SystemExit(1)   # nothing about the model differs; not ours to carry
json.dump({"model": local["model"]} if "model" in local else {}, sys.stdout)
' "$CARRY_SETTINGS"
}

# Re-apply the captured key to whatever settings.json now says, preserving the
# file otherwise. json.dumps(indent=2) is the shape the harness itself writes,
# so this produces no incidental diff.
carry_reapply() {
  printf '%s' "$carry_doc" | python3 -c '
import json, sys
carry = json.load(sys.stdin)
path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    doc = json.load(fh)
if "model" in carry:
    doc["model"] = carry["model"]
else:
    doc.pop("model", None)
tmp = path + ".carry.tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\n")
import os
os.replace(tmp, path)
' "$CARRY_SETTINGS"
}

# Runs on every exit path between setting the file aside and putting it back,
# including a systemd TimeoutStartSec kill, so the operator's choice cannot be
# lost in a window.
carry_finish() {
  [ "$carry_armed" = 1 ] || return 0
  carry_armed=0
  if carry_reapply; then
    rm -f "$carry_backup"
    return 0
  fi
  log "could not re-apply the carried model setting — restoring settings.json as it was"
  cp -f "$carry_backup" "$CARRY_SETTINGS" && rm -f "$carry_backup"
}
trap carry_finish EXIT

if [ -f "$CARRY_SETTINGS" ] \
  && command -v python3 >/dev/null 2>&1 \
  && ! git diff --quiet HEAD -- "$CARRY_SETTINGS" \
  && carry_doc="$(carry_capture)"; then
  carry_backup="$STATE/settings.json.carry"
  if cp -f "$CARRY_SETTINGS" "$carry_backup" \
    && git checkout --quiet HEAD -- "$CARRY_SETTINGS"; then
    carry_armed=1
    log "carrying the local model setting across the pull; $CARRY_SETTINGS is otherwise unchanged from HEAD"
  else
    log "could not set $CARRY_SETTINGS aside; leaving it in place"
    rm -f "$carry_backup"
  fi
fi

# Fast-forward only. If the incoming commits touch files that are modified
# or untracked here, git refuses and changes nothing — which is the desired
# outcome: local work outranks an automated update.
incoming="$(git rev-list --count "HEAD..$UPSTREAM")"
if ! ff_err="$(git merge --ff-only --quiet "$UPSTREAM" 2>&1)"; then
  carry_finish
  alert "Cannot fast-forward $BRANCH to $UPSTREAM ($incoming commit(s) waiting) — local changes are in the way:
$(printf '%s' "$ff_err" | head -5)"
  exit 1
fi

carry_finish

log "fast-forwarded $BRANCH: $(git rev-parse --short "$base_sha") -> $(git rev-parse --short HEAD) ($incoming commit(s))"

# Submodule pointers move with the superproject, so a pull that bumps one
# leaves the submodule at the old commit until it is checked out. Best
# effort: a submodule with local edits is a reason to log, not to fail the
# pull that already succeeded.
sync_submodules

finish_ok
