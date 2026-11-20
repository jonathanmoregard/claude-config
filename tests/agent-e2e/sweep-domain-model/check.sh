#!/usr/bin/env bash
# PASS when the sweep shows all of:
#   tree  the fixture's tree is untouched (the sweep only proposes)
#   T     the inventory's Tier line says personal (not the template's
#         "production | personal")
#   V     the Venues lens ran, or final.txt says it was skipped
#   P1-P5, G, X  graded by an Opus judge against the planted problems in
#         judge.md: labelled preset union, explicit case for the sentinels,
#         RoomId/LampId, Home Assistant names out of the core, `Plan` renamed
#         with the README's words, a separate type-checker guard item, and no
#         production machinery proposed. Only proposals count, never quotes.
# The judge runs with a fresh HOME holding only the credentials, so the config
# under test cannot shape its own grade. A judge failure is INCONCLUSIVE.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
dir="$RUN/home/.local/state/claude-tasks/work/assurance-sweep"
[ -d "$dir" ] || fail "no assurance-sweep output dir"
st=$(git -C "$RUN/work" status --porcelain)
[ -z "$st" ] || fail "tree changed: $(head -c 80 <<<"$st")"
grep -Eiq '^[^[:alnum:]]*Tier[^[:alnum:]]*personal\b' "$dir"/*/inventory.md \
  || fail "T: inventory Tier is not personal"
ls "$dir"/*/*venue*.md >/dev/null 2>&1 \
  || grep -Eiq 'venues?.{0,80}skipped|skipped.{0,80}venues?' "$RUN/final.txt" \
  || fail "V: Venues neither ran nor reported skipped"
out=$(python3 "$(dirname "$0")/judge.py" "$dir" "$RUN/judge-home" "$RUN/home/.claude/.credentials.json")
code=$?
case "$code" in
  0) pass "tree, T, V; judge: ${out#PASS }" ;;
  1) fail "judge: $out" ;;
  *) echo "INCONCLUSIVE: $out"; exit 2 ;;
esac
