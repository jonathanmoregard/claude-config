#!/usr/bin/env bash
# PASS when the sweep shows all of:
#   tree  the fixture's files (node_modules aside) are what setup.sh wrote (the
#         sweep only proposes); read from disk, never through git
#   T     the inventory's Tier line says personal (not the template's
#         "production | personal")
#   P1-P5, G, X, V  graded by an Opus judge against the planted problems in
#         judge.md: labelled preset union, explicit case for a sentinel,
#         RoomId/LampId, Home Assistant names out of the core, `Plan` renamed
#         with the README's words, a separate type-checker guard item, no
#         production machinery proposed, and a Venues lens file marking those
#         methods n/a (the fixture's two fix commits give Venues defect history
#         to run on). Only proposals count.
# judge.py says how the judge is kept from being shaped by the config under
# test. A judge failure is INCONCLUSIVE.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
dir="$RUN/home/.local/state/claude-tasks/work/assurance-sweep"
[ -d "$dir" ] || fail "no assurance-sweep output dir"
# The files on disk must be exactly the ones setup.sh writes. Read without git:
# the agent can write .git (config, index flags, excludes), and git run here,
# outside the sandbox, would obey it. Python runs isolated (-I) for the same
# reason: HOME here is the run's, and its user site-packages are writable.
# Update the digest when setup.sh changes the fixture (python3 tree_digest.py
# on a fresh setup).
[ "$(python3 -I "$(dirname "$0")/tree_digest.py" "$RUN/work")" = \
  50870d976c0c8c24c8469261a2a0933e74a201fed0a3e2a639fed257b2c13ae5 ] \
  || fail "tree: the fixture's files changed"
grep -Eiq '^[^[:alnum:]]*Tier[^[:alnum:]]*personal\b' "$dir"/*/inventory.md \
  || fail "T: inventory Tier is not personal"
out=$(python3 -I "$(dirname "$0")/judge.py" "$dir" "$RUN/final.txt")
code=$?
case "$code" in
  0) pass "tree, T; judge: ${out#PASS }" ;;
  1) [ -n "$out" ] && fail "judge: $out" ;;
esac
echo "INCONCLUSIVE: judge exit $code: $out"
exit 2
