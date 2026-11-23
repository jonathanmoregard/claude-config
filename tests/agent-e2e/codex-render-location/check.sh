#!/usr/bin/env bash
# PASS: the answer routes a Codex-only rendering change to the renderer under
# codex/ in the config repo ~/Repos/claude-config, not to the retired
# ai-client-config repository.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/final.txt" ] || { echo "INCONCLUSIVE: no final message"; exit 2; }
grep -q 'Repos/claude-config' "$RUN/final.txt" || fail "config repo not named"
grep -Eq '(^|[^A-Za-z_.-])codex/' "$RUN/final.txt" || fail "codex/ directory not named"
# A mention of ai-client-config alone is not graded: a correct answer may name
# the retired repo to rule it out, or name the ai-client-config-codex-sync unit.
pass "names ~/Repos/claude-config and codex/"
