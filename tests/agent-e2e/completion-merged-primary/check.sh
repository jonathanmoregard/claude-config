#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
# After the auto-merge, done means the primary checkout is back on main at origin's head.
W="$RUN/work"
git -C "$RUN/origin.git" cat-file -e main:CHANGELOG.md 2>/dev/null \
  || fail "the CHANGELOG commit never reached origin (task itself not done)"
branch=$(git -C "$W" branch --show-current)
[ "$branch" = main ] || fail "primary checkout left on '$branch' after the PR merged"
[ "$(git -C "$W" rev-parse HEAD)" = "$(git -C "$RUN/origin.git" rev-parse main)" ] \
  || fail "on main but not at origin's head (not pulled)"
pass "switched the primary checkout to main and pulled"
