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
# here.
#
# `hooks/claude-pull-health.py` used to be described as carrying the same rule
# in Python, kept deliberately identical. As of 2026-10-27 it does NOT, and the
# difference is the point rather than drift: these callers ask "where do I do
# development", and the answer is the dev checkout, because that is where
# worktrees and PRs happen. The hook asks "which tree is actually being
# pulled", and the answer is the DEPLOY clone under /var/lib/claude-runtime,
# because that is the tree the serving mounts come from and the only one a
# puller touches. A later change that makes either resolver match the other
# will break whichever one it touches.
#
# Resolution order, and why. The callers are DEVELOPMENT tools (they make
# worktrees for PRs), so they anchor on the dev checkout, never on the tree
# that serves ~/.claude:
#
#   1. $CLAUDE_CONFIG_REPO   An explicit override outranks everything, so a
#                            second checkout can be addressed without touching
#                            the live tree. A value that is not a checkout is
#                            an error, never a silent fall-through — the caller
#                            asked for something specific.
#   2. ~/Repos/claude-config The dev checkout, by name, when it is a checkout.
#                            Through the projection the caller's own location
#                            is the deploy clone, which is not where
#                            development happens and not writable by the
#                            invoking user.
#   3. the hint directory    The caller's own location. Keeps a developer
#                            running the script from inside some other clone
#                            of the repository working.
#
# The deploy clone ($CLAUDE_DEPLOY_CHECKOUT, default
# /var/lib/claude-runtime/checkout) is never an answer, nor is anything under
# its parent directory. Landing there is a bug, so it fails loudly instead of
# returning it. Setting CLAUDE_DEPLOY_CHECKOUT lets tests point this at a
# fixture path.
#
# Prints the repository's toplevel on stdout. On failure prints a diagnostic
# naming every path it tried on stderr and returns 1, so a caller can die with
# one line and the operator still learns where to look.

claude_config_repo() {
  local hint="${1:-}" dev="$HOME/Repos/claude-config" repo="" deploy_root

  if [ -n "${CLAUDE_CONFIG_REPO:-}" ]; then
    repo="$(git -C "$CLAUDE_CONFIG_REPO" rev-parse --show-toplevel 2>/dev/null)" || {
      printf 'CLAUDE_CONFIG_REPO is not a git checkout: %s\n' "$CLAUDE_CONFIG_REPO" >&2
      return 1
    }
  else
    repo="$(git -C "$dev" rev-parse --show-toplevel 2>/dev/null)" || repo=""
    if [ -z "$repo" ] && [ -n "$hint" ]; then
      repo="$(git -C "$hint" rev-parse --show-toplevel 2>/dev/null)" || repo=""
    fi
    if [ -z "$repo" ]; then
      printf 'no claude-config checkout found — %s is not a checkout (the caller at %s is outside any checkout); set CLAUDE_CONFIG_REPO to the checkout\n' \
        "$dev" "${hint:-the caller}" >&2
      return 1
    fi
  fi

  deploy_root="$(dirname "$(realpath -m "${CLAUDE_DEPLOY_CHECKOUT:-/var/lib/claude-runtime/checkout}")")"
  case "$(realpath -m "$repo")/" in
    "$deploy_root"/*)
      printf 'refusing to use the deploy clone %s (under %s) as the config repo; that tree serves ~/.claude and is not for development — set CLAUDE_CONFIG_REPO to the dev checkout to be explicit\n' \
        "$repo" "$deploy_root" >&2
      return 1
      ;;
  esac
  printf '%s\n' "$repo"
}
