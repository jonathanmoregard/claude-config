#!/usr/bin/env bash
# PASS: the answer routes a Codex-only rendering change to the renderer under
# codex/ in the config repo ~/Repos/claude-config, not to the retired
# ai-client-config repository.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/final.txt" ] || { echo "INCONCLUSIVE: no final message"; exit 2; }
grep -q 'Repos/claude-config' "$RUN/final.txt" || fail "config repo not named"
grep -Eq '(^|[^A-Za-z_.-])codex/' "$RUN/final.txt" || fail "codex/ directory not named"
# Naming the sync unit (ai-client-config-codex-sync) is fine; any other
# mention of ai-client-config points at the retired repository.
grep -oi 'ai-client-config[A-Za-z_.-]*' "$RUN/final.txt" \
  | grep -viq '^ai-client-config-codex-sync' \
  && fail "routes the change to ai-client-config"
pass "names ~/Repos/claude-config and codex/"
