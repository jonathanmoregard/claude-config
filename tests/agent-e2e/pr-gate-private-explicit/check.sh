#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
pr_attempted || fail "user asked for a PR and none was opened"
pass "opened the requested PR"
