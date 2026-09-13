#!/usr/bin/env bash
# PostToolUse(Write|Edit|MultiEdit|NotebookEdit) — arm the finishing-up gate.
#
# The trigger is evidence, not judgement: an edit landing in a git repo on a
# non-default branch IS a diff, and a diff has a definition of done. Sessions
# that only read, research or answer never write a file, so they never arm and
# never pay for the gate. That is the whole reason this is a hook and not a
# skill description — no classifier decides whether the work counts.
#
# Deliberately cheap and local: no network, no gh, no model. All it does is
# record "this session touched repo R". The expensive check runs once, at Stop,
# and only when this file exists.
set -uo pipefail

STATE_DIR="${CLAUDE_DOD_STATE_DIR:-$HOME/.local/state/claude-dod}"

input=$(cat)
if ! command -v jq >/dev/null 2>&1; then
  # Silent, this switched the ENTIRE enforcement system off with no signal: no
  # session ever arms, so the Stop gate never checks anything and every turn
  # ends looking clean. A degrade that total has to be loud.
  echo "⚠️  finishing-up-arm: jq is not on PATH — no session will arm, so the definition-of-done gate will never run" >&2
  # Non-zero, or the warning is not surfaced: a PostToolUse hook's stderr is
  # shown on a non-zero exit, and at exit 0 this message went nowhere. 1, never
  # 2 — 2 blocks the tool call itself, and a hook that cannot arm must not stop
  # the user's edit from landing.
  exit 1
fi

session=$(jq -r '.session_id // ""' <<<"$input")
# Reject anything that is not a plain token: this becomes a filename.
[[ "$session" =~ ^[A-Za-z0-9._-]+$ ]] || exit 0

# The edited path decides the repo, not the session cwd — a session running in
# ~/.claude that edits a file in ~/Repos/foo is working on foo.
#
# SUBAGENTS SHARE THE PARENT'S session_id. Measured 2026-08-27 against 2.1.247:
# a parent edited one repo, dispatched a Task subagent that edited a second, and
# both PostToolUse payloads carried the identical session_id — so the subagent's
# edit arms the PARENT for the second repo.
#
# That is intended, not a leak. Work dispatched to a subagent is still the
# parent's work, and the parent is the session that will be asked whether it is
# finished; a fleet that can edit repos the gate never looks at is the same
# opt-out the default branch used to be. The cost is bounded and known: if a
# subagent touches a repo the parent cannot finish, the parent blocks up to its
# budget and then stands down. Scratch dirs are excluded below, so notes and
# state files do not arm anything.
path=$(jq -r '.tool_input.file_path // .tool_input.notebook_path // ""' <<<"$input")
[ -n "$path" ] || exit 0

dir=$(dirname "$path")
[ -d "$dir" ] || exit 0

root=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -n "$root" ] || exit 0

branch=$(git -C "$root" rev-parse --abbrev-ref HEAD 2>/dev/null) || exit 0
[ -n "$branch" ] && [ "$branch" != "HEAD" ] || exit 0

# Scratch, state and cache dirs are not deliverables.
case "$root" in
  "$HOME/.local/state/"*|/tmp/*|"$HOME/.cache/"*) exit 0 ;;
esac

# The default branch USED to be skipped here, on the reasoning that there is no
# PR to drive. That made the whole gate opt-out: never open a PR and nothing is
# ever measured, so commits could sit unpushed on main indefinitely and read as
# finished. Where pushing straight to the default branch is how work ships, that
# IS the delivery path and it gets the shorter ladder instead — committed,
# pushed, level with origin. dod-check decides which ladder applies; this hook's
# only job is to notice that a repo was touched.
#
# A default branch must still be resolvable: without one there is no ladder to
# pick and nothing to compare against.
default=$(git -C "$root" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')
if [ -z "$default" ]; then
  for c in main master; do
    git -C "$root" show-ref --verify --quiet "refs/remotes/origin/$c" && { default="$c"; break; }
  done
fi
[ -n "$default" ] || exit 0

# Same total impact as the missing-jq branch above — no session arms, so the
# gate never checks anything — and this one used to say nothing on any channel.
# A non-zero exit is what makes a PostToolUse hook's stderr visible at all; 1
# rather than 2, because 2 is the code that BLOCKS the tool call, and failing to
# arm must not stop the user's edit from landing.
mkdir -p "$STATE_DIR" 2>/dev/null || {
  echo "⚠️  finishing-up-arm: cannot create $STATE_DIR — no session will arm, so the definition-of-done gate will never run" >&2
  exit 1
}

# Task reset and repository arming must be one ordered state transition. Without
# this stable lock, an explicit UserPromptSubmit reset can read the old armfile,
# this hook can append a newly landed edit, and reset can then unlink both — the
# exact long-running-work escape task scoping must not create. The lock is
# state-directory-wide so it survives armfile deletion and contains no task
# data. Waiting is bounded below both hooks' registered five-second slots.
if ! command -v flock >/dev/null 2>&1; then
  echo "⚠️  finishing-up-arm: flock is not on PATH — $root was NOT recorded, so the definition-of-done gate cannot check this edit" >&2
  exit 1
fi
scope_lock="$STATE_DIR/.task-scope.lock"
exec 8>>"$scope_lock" || {
  echo "⚠️  finishing-up-arm: cannot open task-scope lock $scope_lock — $root was NOT recorded, so the definition-of-done gate cannot check this edit" >&2
  exit 1
}
if ! flock -w 4 8; then
  echo "⚠️  finishing-up-arm: task-scope lock stayed busy — $root was NOT recorded, so the definition-of-done gate cannot check this edit" >&2
  exit 1
fi
armfile="$STATE_DIR/$session.arm"

# One line per repo, de-duplicated. A session may legitimately touch several.
#
# Check-then-append is a RACE, and this hook is somewhere it actually fires:
# SUBAGENTS SHARE THE PARENT'S session_id (see above), so two PostToolUse hooks
# run concurrently against one armfile, both miss the grep, and both append. The
# gate then spends its single wall-clock budget measuring one repository twice,
# and the repository at the end of the list is the one reported NOT MEASURED.
#
# flock makes the check and the append one operation. It is taken on the armfile
# itself rather than a sidecar .lock, so the gate's reaper — which only knows
# *.arm and *.blocks — has nothing new to leak. Where flock is missing the old
# unserialised path is taken, and the gate de-duplicates what it reads: this is
# defence at the source, not the only defence.
#
# The redirection is on the SUBSHELL, never on `exec`: a failed `exec`
# redirection exits a non-interactive shell outright, which would turn an
# unwritable state dir into a hook that reports failure to the user.
# NO blanket `2>/dev/null` on this subshell, and the result is CHECKED. The
# suppression hid the write half specifically: an open failure escapes it anyway
# (bash applies 9>> before 2>), but a failed append — ENOSPC, a quota, an
# rlimit — printed nothing at all and the unconditional `exit 0` discarded the
# status. Measured against /dev/full: `printf >&9` returns 1 with the message
# swallowed. No armfile means the gate has nothing to check and ends the turn
# with no output and no log line, which is enforcement off rather than a hook
# that failed. That silence was introduced by this round's own flock fix.
if ! (
  if command -v flock >/dev/null 2>&1; then
    # -w so a stuck holder can never hang a tool call; timing out just falls
    # through to the unserialised path, which is where this started.
    flock -w 5 9 2>/dev/null || true
  fi
  if grep -qxF "$root" "$armfile" 2>/dev/null; then
    # Touch it. The gate's reaper is age-based, and a long session editing ONE
    # repo would otherwise carry an armfile whose mtime froze at the first edit —
    # reapable mid-work, which costs an unguarded turn until the next new repo.
    touch "$armfile" 2>/dev/null || true
  else
    printf '%s\n' "$root" >&9
  fi
) 9>>"$armfile"; then
  echo "⚠️  finishing-up-arm: could not record $root in $armfile — this session will NOT be checked by the definition-of-done gate" >&2
  exit 1
fi
exit 0
