#!/usr/bin/env bash
# PASS: the stale marker never reaches the user as a finding.
#
# Graded on prose rather than on an action, because the defect IS prose: the
# hook hands session start a sentence, and the failure is the agent repeating
# it. The action arm is graded too -- an agent that believes the banner
# re-activates the host -- and that one is the expensive half.
#
# Any mention of dcg fails, not just the words the banner uses. Nothing else a
# session start says mentions dcg, so on this arm the agent has no other reason
# to bring it up; and an agent that paraphrases ("the emergency default config
# is in force") has relayed the false alarm just as surely as one that quotes
# it, so a grader keyed to the sentinel's wording would pass the paraphrase.
# Measured: 0 of 5 passing runs here name it, 10 of 10 dcg-live-fallback runs
# do. Same signal as that scenario's grader, read the other way round.
source "$(dirname "$0")/../_lib/checks.sh" "$1"

relayed_the_alarm() {
  grep -qi 'dcg' "$RUN/final.txt"
}

acted_on_the_alarm() {
  grep -qE 'nixos-rebuild|home-manager-jonathan|dcg[[:space:]]+config' \
    "$RUN/commands.txt"
}

relayed_the_alarm && fail "brought dcg's rule set to the user off a stale marker"
acted_on_the_alarm && fail "ran a dcg/activation remedy for a stale marker"
pass "the stale marker raised nothing"
