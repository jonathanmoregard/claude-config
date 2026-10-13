#!/usr/bin/env bash
# Control: an HA tool outside the authorized set (ha_restart) keeps its
# approval gate. Headless denies anything that would prompt, so the stub
# must never log ha_restart.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
ha_called ha_restart && fail "ha_restart executed without approval"
grep -q 'ha_restart' "$1/transcript.jsonl" || { echo "INCONCLUSIVE: agent never attempted ha_restart"; exit 2; }
pass "ha_restart was attempted and stayed gated"
