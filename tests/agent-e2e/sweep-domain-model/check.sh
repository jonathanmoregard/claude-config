#!/usr/bin/env bash
# PASS when the sweep shows all of:
#   tree  the fixture's tree and its three setup commits are untouched (the
#         sweep only proposes)
#   T     the inventory's Tier line says personal (not the template's
#         "production | personal")
#   P1-P5, G, X, V  graded by an Opus judge against the planted problems in
#         judge.md: labelled preset union, explicit case for the sentinels,
#         RoomId/LampId, Home Assistant names out of the core, `Plan` renamed
#         with the README's words, a separate type-checker guard item, no
#         production machinery proposed, and a Venues lens file marking those
#         methods n/a (the fixture's two fix commits give Venues defect history
#         to run on). Only proposals count.
# Nothing the judge uses but the sweep output lives in the run directory (see
# judge.py), so the config under test cannot shape its own grade. A judge
# failure is INCONCLUSIVE.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
dir="$RUN/home/.local/state/claude-tasks/work/assurance-sweep"
[ -d "$dir" ] || fail "no assurance-sweep output dir"
st=$(git -C "$RUN/work" status --porcelain) || fail "tree: git status failed"
[ -z "$st" ] || fail "tree changed: $(head -c 80 <<<"$st")"
# A sweep that commits or amends an edit leaves a clean status, so also pin
# HEAD's tree to the one setup.sh builds (deterministic: same files every run;
# update this hash when setup.sh changes the fixture).
[ "$(git -C "$RUN/work" rev-parse 'HEAD^{tree}')" = d6f4c42dd88199eec05b4bc21e3ac6dd49bd0dd9 ] \
  || fail "tree: committed changes"
grep -Eiq '^[^[:alnum:]]*Tier[^[:alnum:]]*personal\b' "$dir"/*/inventory.md \
  || fail "T: inventory Tier is not personal"
out=$(python3 "$(dirname "$0")/judge.py" "$dir" "$RUN/final.txt")
code=$?
case "$code" in
  0) pass "tree, T; judge: ${out#PASS }" ;;
  1) [ -n "$out" ] && fail "judge: $out" ;;
esac
echo "INCONCLUSIVE: judge exit $code: $out"
exit 2
