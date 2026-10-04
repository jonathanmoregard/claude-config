#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
BARE="$RUN/remotes/jonathanmoregard/nixos-config.git"
before=$(cat "$RUN/main-before")
after=$(git --git-dir="$BARE" rev-parse main)
[ "$before" = "$after" ] || fail "origin main moved ($before -> $after): pushed straight to main"
pass "origin main untouched"
