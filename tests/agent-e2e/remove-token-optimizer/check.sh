#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"

data="$RUN/home/.claude/token-optimizer"
if [ -d "$data" ] && find "$data" -type f -print -quit | grep -q .; then
  fail "Token Optimizer hooks wrote runtime data under the staged HOME"
fi
pass "no Token Optimizer hook activity under the staged HOME"
