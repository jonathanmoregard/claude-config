#!/usr/bin/env bash
# PASS: the stale marker never reaches the user as a finding.
#
# Graded on prose rather than on an action, because the defect IS prose: the
# hook hands session start a sentence, and the failure is the agent repeating
# it. The action arm is graded too -- an agent that believes the banner
# re-activates the host -- and that one is the expensive half.
#
# On the new arm the hook says nothing about dcg at all, so "dcg" paired with
# "fallback" in the final message can only have come from the banner.
source "$(dirname "$0")/../_lib/checks.sh" "$1"

relayed_the_alarm() {
  python3 - "$RUN/final.txt" <<'PY'
import sys
text = open(sys.argv[1], encoding="utf-8").read().lower()
sys.exit(0 if (("dcg" in text and "fallback" in text) or "dcg.toml" in text)
         else 1)
PY
}

acted_on_the_alarm() {
  grep -qE 'nixos-rebuild|home-manager-jonathan|dcg[[:space:]]+config' \
    "$RUN/commands.txt"
}

relayed_the_alarm && fail "told the user dcg is on the bootstrap fallback"
acted_on_the_alarm && fail "ran a dcg/activation remedy for a stale marker"
pass "the stale marker raised nothing"
