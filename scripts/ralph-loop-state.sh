#!/usr/bin/env bash
# Where a session-bound Ralph loop keeps its state. SINGLE SOURCE OF TRUTH.
#
# `~/.local/state/claude-ralph-loop/<session_id>.md`, following the convention
# self-compact was migrated to (`~/.claude/self-compact/` →
# `~/.local/state/claude-self-compact/`): agent runtime state lives under
# `~/.local/state/claude-<thing>/`, code stays in `~/.claude`.
#
# This replaces TWO wrong placements, and the second is the one that kept
# getting missed:
#
#   1. cwd-relative `.claude/ralph-loop.local.md` — lands inside whatever repo
#      or worktree the session happened to be in. In a public repo with an
#      auto-commit sync cron, an un-ignored file there is not a risk of
#      publication, it is a guarantee of it. It also keys the stop gate to a
#      DIRECTORY, so every session stopping there is gated, and the session
#      that armed the loop is gated nowhere else.
#   2. `~/.claude/ralph-loop/` — `.claude` is a protected path in Claude Code,
#      gated outside `permissions.*`, so every write prompts and no allow rule
#      can fix it; it also collides with harness-managed storage.
#
# SOURCE this file; never re-spell the path. The defect that produced it was a
# migration that moved the writer and left four readers behind, so writer and
# readers disagreed about where the state was and neither one errored.

# CLAUDE_RALPH_STATE_DIR overrides the location. Two reasons, neither cosmetic:
# a test must not write into the live loop directory, and there is now a reader
# that CANNOT source this file — finishing-up-gate.py, which yields when a ralph
# loop owns the session. Python cannot source bash, so that reader agrees with
# this one by sharing the ENV VAR rather than by copying the literal path. If
# this default ever moves, change it here and the Python reader keeps working.
RALPH_STATE_DIR="${CLAUDE_RALPH_STATE_DIR:-$HOME/.local/state/claude-ralph-loop}"

# Absolute path of the state file for a session id. The CALLER validates the id
# before calling — this must never be handed an unvalidated `../..` string.
ralph_state_file() {
  printf '%s/%s.md\n' "$RALPH_STATE_DIR" "$1"
}

# Read one frontmatter scalar out of a state file, quotes stripped.
#
# `awk` with an explicit `sub`, never `eval`/`source`: state-file content is
# DATA. A corrupted file carrying `$(...)` must render as text, not execute.
ralph_state_field() {  # $1=file $2=field
  awk -v k="^$2:" '$0 ~ k { sub(/^[^:]*: */, ""); gsub(/"/, ""); print; exit }' "$1"
}

# The state file of the loop armed in a given directory, if any; rc 1 if none.
#
# State is keyed by SESSION, not by directory, so "is a loop armed here" can no
# longer be answered by testing a path. It is answered by the `armed_in:` field
# that ralph-loop-setup.sh records — which is why that field exists, and why
# cwd-shaped consumers (compact-reinject, idle-handoff) must come through here
# rather than rebuilding the glob themselves.
ralph_state_for_dir() {  # $1=directory
  local dir="$1" f armed
  [[ -d "$RALPH_STATE_DIR" ]] || return 1
  for f in "$RALPH_STATE_DIR"/*.md; do
    # An unmatched glob stays literal; -f rejects it without needing nullglob,
    # which is a shell-global setting this function must not leave switched on.
    [[ -f "$f" ]] || continue
    armed=$(ralph_state_field "$f" armed_in)
    if [[ -n "$armed" && "$armed" == "$dir" ]]; then
      printf '%s\n' "$f"
      return 0
    fi
  done
  return 1
}
