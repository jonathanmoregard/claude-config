#!/usr/bin/env bash
# Control: both PRs are still open, so listing both as awaiting is correct.
source "$(dirname "$0")/../_lib/prstate.sh" \
  "jonathanmoregard/nixos-config#339 OPEN" "jonathanmoregard/.claude#152 OPEN"
