#!/usr/bin/env bash
# Create a worktree for ~/.claude work: ~/worktrees/claude-<slug>, on a new
# branch off freshly fetched origin/master.
#
#   scripts/claude-worktree.sh <slug>   →  ~/worktrees/claude-<slug>, feat/<slug>
#
# The primary checkout is the stable integration and scheduled-pull target, so
# it stays on master and is never a development workspace. Repository Git hooks
# pin its HEAD and reject commits there; linked worktrees stay unrestricted.

set -euo pipefail

WORKTREE_ROOT="${CLAUDE_WORKTREE_ROOT:-$HOME/worktrees}"

die() { printf 'claude-worktree: %s\n' "$*" >&2; exit 1; }

[ $# -eq 1 ] || die "usage: $(basename "$0") <slug>"
slug="$1"

case "$slug" in
  -*|.*)                    die "invalid slug '$slug' — must not start with '-' or '.'" ;;
  *[!A-Za-z0-9._-]*)        die "invalid slug '$slug' — allowed: A-Z a-z 0-9 . _ -" ;;
esac

branch="feat/$slug"

# The charset check above is necessary but not sufficient: git also rejects
# '..' anywhere, a trailing '.', a '.lock' suffix, and a few other patterns
# no simple charset check can see (git-check-ref-format(1)). Validate the
# exact ref this script is about to create, before fetching anything, so a
# bad slug fails immediately instead of mid-flow at `worktree add` with a
# raw git error.
git check-ref-format "refs/heads/$branch" >/dev/null 2>&1 \
  || die "invalid slug '$slug' — branch '$branch' is not a valid git ref name (git check-ref-format rejected it; common causes: '..', a trailing '.', or a '.lock' suffix — see git-check-ref-format(1))"

# Anchor on the repo this script lives in. Refs live in the shared git dir, so
# any worktree of the repo is an equally valid anchor for fetch + worktree add.
script_dir="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
repo="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null)" \
  || die "not inside a git checkout: $script_dir"

# A clone or freshly merged checkout may not have installed its local hooks
# yet. Activate them before touching refs so this helper cannot create a
# worktree while protection is silently disabled.
installer="$repo/scripts/install-worktree-policy.sh"
[ -x "$installer" ] || die "policy installer is missing or not executable: $installer"
"$installer" "$repo" >/dev/null \
  || die "could not activate repository worktree policy"

dest="$WORKTREE_ROOT/claude-$slug"

# Refuse rather than clobber: an existing path or branch means somebody else's
# work, possibly a live session's — never hand out a ready-made removal
# command for a path that may not be the caller's to remove.
if [ -e "$dest" ]; then
  die "already exists: $dest — pick another slug. If it's your own abandoned worktree, remove it yourself first: 'git -C $repo worktree remove $dest'"
fi
if git -C "$repo" show-ref --verify --quiet "refs/heads/$branch"; then
  die "branch already exists: $branch — pick another slug"
fi

printf 'claude-worktree: fetching origin/master…\n' >&2
git -C "$repo" fetch --quiet origin master \
  || die "fetch failed — cannot branch off a stale origin/master"

mkdir -p "$WORKTREE_ROOT"
git -C "$repo" worktree add -b "$branch" "$dest" origin/master

cat <<EOF

Worktree ready: $dest
Branch:         $branch (off freshly fetched origin/master)

  cd $dest

After the PR is merged:

  git -C $repo worktree remove $dest
EOF
