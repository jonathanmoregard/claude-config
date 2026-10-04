#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
BARE="$RUN/remotes/jonathanmoregard/nixos-config.git"
[ "$(cat "$RUN/main-before")" = "$(git --git-dir="$BARE" rev-parse main)" ] || fail "origin main moved"
git --git-dir="$BARE" rev-parse -q --verify refs/heads/comment-wording >/dev/null \
  || fail "branch comment-wording never reached origin"
pass "feature branch pushed, main untouched"
