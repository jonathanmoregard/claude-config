#!/usr/bin/env bash
# Where the claude-config checkout is. SINGLE SOURCE OF TRUTH for scripts that
# are reached through the runtime projection.
#
# Sourced, not executed (mode 0644, like scripts/ralph-loop-state.sh):
#
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config-repo.sh"
#   repo="$(claude_config_repo "$some_hint_dir")" || exit 1
#
# WHY THIS EXISTS. Before the cutover, `~/.claude` IS the checkout, so every
# script could find the repository by asking git about its own location. After
# it, `~/.claude/scripts` is a symlink into `~/.claude/runtime`, a bind mount
# exposing the checkout's `runtime/` subtree alone, and the checkout's `.git`
# sits at the root of the backing clone — above the mount, where walking up
# from the resolved path never reaches it. Discovery then fails outright, which
# is the better half of the failure: a script cannot pick up the wrong
# repository. It still fails.
#
# Two scripts needed the same answer (claude-worktree.sh, update-submodules.sh)
# and a third copy of a rule is how the copies drift apart, so the rule lives
# here. The hook `hooks/claude-pull-health.py` carries the same rule in Python;
# that one cannot share this file, so the two are kept deliberately identical
# in order: an explicit override, then the caller's own location, then the
# backing checkout by name.
#
# Resolution order, and why:
#
#   1. $CLAUDE_CONFIG_REPO   An explicit override outranks everything, so a
#                            second checkout can be addressed without touching
#                            the live tree. A value that is not a checkout is
#                            an error, never a silent fall-through — the caller
#                            asked for something specific.
#   2. the hint directory    The caller's own location. Unchanged pre-cutover
#                            behaviour, and what keeps any linked worktree of
#                            the repository a valid anchor for its own scripts.
#   3. ~/Repos/claude-config The backing checkout, by name. The only answer
#                            left once the projection hides the location.
#
# Prints the repository's toplevel on stdout. On failure prints a diagnostic
# naming every path it tried on stderr and returns 1, so a caller can die with
# one line and the operator still learns where to look.

claude_config_repo() {
  local hint="${1:-}" fallback repo

  if [ -n "${CLAUDE_CONFIG_REPO:-}" ]; then
    repo="$(git -C "$CLAUDE_CONFIG_REPO" rev-parse --show-toplevel 2>/dev/null)" || {
      printf 'CLAUDE_CONFIG_REPO is not a git checkout: %s\n' "$CLAUDE_CONFIG_REPO" >&2
      return 1
    }
    printf '%s\n' "$repo"
    return 0
  fi

  if [ -n "$hint" ]; then
    repo="$(git -C "$hint" rev-parse --show-toplevel 2>/dev/null)" || repo=""
    if [ -n "$repo" ]; then
      printf '%s\n' "$repo"
      return 0
    fi
  fi

  fallback="$HOME/Repos/claude-config"
  repo="$(git -C "$fallback" rev-parse --show-toplevel 2>/dev/null)" || {
    printf 'no claude-config checkout found — %s is not inside one (expected after the runtime-projection cutover) and neither is %s; set CLAUDE_CONFIG_REPO to the checkout\n' \
      "${hint:-the caller}" "$fallback" >&2
    return 1
  }
  printf '%s\n' "$repo"
}
