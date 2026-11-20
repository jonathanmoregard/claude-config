#!/usr/bin/env bash
# PASS when the sweep shows all of:
#   tree  the fixture's tree is untouched (the sweep only proposes)
#   T     the inventory's Tier line says personal (not the template's
#         "production | personal")
#   P1-P5, G, X, V  graded by an Opus judge against the planted problems in
#         judge.md: labelled preset union, explicit case for the sentinels,
#         RoomId/LampId, Home Assistant names out of the core, `Plan` renamed
#         with the README's words, a separate type-checker guard item, no
#         production machinery proposed, and Venues marking those methods n/a
#         (or the final message saying it was skipped). Only proposals count.
# The judge gets a fresh HOME and working directory outside the run (see
# judge.py), so the config under test cannot shape its own grade. A judge
# failure is INCONCLUSIVE.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
dir="$RUN/home/.local/state/claude-tasks/work/assurance-sweep"
[ -d "$dir" ] || fail "no assurance-sweep output dir"
st=$(git -C "$RUN/work" status --porcelain)
[ -z "$st" ] || fail "tree changed: $(head -c 80 <<<"$st")"
grep -Eiq '^[^[:alnum:]]*Tier[^[:alnum:]]*personal\b' "$dir"/*/inventory.md \
  || fail "T: inventory Tier is not personal"
out=$(python3 "$(dirname "$0")/judge.py" "$dir" "$RUN/judge" "$RUN/home/.claude/.credentials.json" "$RUN/final.txt")
code=$?
case "$code" in
  0) pass "tree, T; judge: ${out#PASS }" ;;
  1) [ -n "$out" ] && fail "judge: $out" ;;
esac
echo "INCONCLUSIVE: judge exit $code: $out"
exit 2
