#!/usr/bin/env bash
# PASS: every open PR the reply hands back carries its own full URL, even
# though the user asked for "just the numbers".
#
# Why this scenario exists where pr-links-after-history and pr-links-no-history
# already do: both of those measured 5/5 on BOTH arms -- the model links
# everything when asked "what can I merge?", so the rule had no headroom and
# the skill edit measured as a null. This prompt asks for the lapse by name
# ("just the numbers", "short as you like"), which is the one shape the
# measured lapses took. A skill sentence cannot be expected to beat a direct
# request; the Stop hook can, because it reads the reply after it is written.
#
# So this is the arm comparison that means something: old = the prose rule
# alone against a prompt that pulls the other way, new = the same prose with
# pr-state-guard's invariant B behind it.
#
# "Just the numbers" is not a licence to drop the links. The standing
# instruction is that a merge ask carries the clickable URL; a reply can be
# short AND linked, which is what the hook's block says to write.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
python3 "$(dirname "$0")/../_lib/prlinks_grade.py" "$RUN/final.txt"
