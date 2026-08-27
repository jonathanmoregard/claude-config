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
command -v jq >/dev/null 2>&1 || exit 0

session=$(jq -r '.session_id // ""' <<<"$input")
# Reject anything that is not a plain token: this becomes a filename.
[[ "$session" =~ ^[A-Za-z0-9._-]+$ ]] || exit 0

# The edited path decides the repo, not the session cwd — a session running in
# ~/.claude that edits a file in ~/Repos/foo is working on foo.
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

# Never arm on the default branch: there is no PR to drive, and blocking a stop
# on main would fault every ordinary edit.
default=$(git -C "$root" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')
if [ -z "$default" ]; then
  for c in main master; do
    git -C "$root" show-ref --verify --quiet "refs/remotes/origin/$c" && { default="$c"; break; }
  done
fi
[ -n "$default" ] || exit 0
[ "$branch" = "$default" ] && exit 0

mkdir -p "$STATE_DIR" 2>/dev/null || exit 0
armfile="$STATE_DIR/$session.arm"

# One line per repo, de-duplicated. A session may legitimately touch several.
if [ -f "$armfile" ] && grep -qxF "$root" "$armfile" 2>/dev/null; then
  exit 0
fi
printf '%s\n' "$root" >> "$armfile"
exit 0
