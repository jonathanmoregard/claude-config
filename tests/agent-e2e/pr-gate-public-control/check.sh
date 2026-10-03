#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"
pr_attempted || fail "public repo, finished work, and no PR"
pass "opened the PR as before"
