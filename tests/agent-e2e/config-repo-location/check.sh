#!/usr/bin/env bash
# PASS: the answer names the config repo ~/Repos/claude-config and the file
# under runtime/ (runtime/CLAUDE.md), not ~/.claude as the repository.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/final.txt" ] || { echo "INCONCLUSIVE: no final message"; exit 2; }
grep -q 'Repos/claude-config' "$RUN/final.txt" || fail "config repo not named"
grep -q 'runtime/CLAUDE.md' "$RUN/final.txt" || fail "runtime/CLAUDE.md not named"
pass "names ~/Repos/claude-config and runtime/CLAUDE.md"
