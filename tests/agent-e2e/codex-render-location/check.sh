#!/usr/bin/env bash
# PASS: the answer routes a Codex-only rendering change to the renderer under
# codex/ in the config repo claude-config, and names no place to edit inside
# the retired ai-client-config repository.
#
# A bare mention of ai-client-config is not graded: a correct answer may name
# the retired repo to rule it out, or name the ai-client-config-codex-sync
# unit. A path into it (~/Repos/ai-client-config, ai-client-config/...) is a
# place to edit, so it fails even beside a correct-looking claude-config line.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/final.txt" ] || { echo "INCONCLUSIVE: no final message"; exit 2; }
grep -Eq '(^|[^A-Za-z_.-])claude-config([^A-Za-z_.-]|$)' "$RUN/final.txt" \
  || fail "config repo claude-config not named"
grep -Eq '(^|[^A-Za-z_.-])codex/' "$RUN/final.txt" || fail "codex/ directory not named"
grep -Eq 'Repos/ai-client-config|ai-client-config/' "$RUN/final.txt" \
  && fail "names a place to edit inside ai-client-config"
pass "names claude-config and codex/"
