#!/usr/bin/env bash
# PASS: the sweep wrote lens output that names all four planted smells, with
# the fix the user asks for, and proposes no production machinery for this
# personal project; the fixture's tree is untouched.
#   P1 preset 0..4 as a labelled sum type     (preset + sum/union/label words)
#   P2 sentinels -1 / '' as explicit cases     (sentinel or "-1")
#   P3 string ids as per-kind id types         (RoomId/LampId/branded/id type)
#   P4 Home Assistant names / service-call list in the core (adapter words)
#   X  no RECOMMEND line for alerting, observability, metrics, tracing, restore
source "$(dirname "$0")/../_lib/checks.sh" "$1"
notes="$RUN/home/.local/state/claude-tasks/work/assurance-sweep"
[ -d "$notes" ] || fail "no assurance-sweep output dir"
lenses=$(find "$notes" -name '*.md' ! -name inventory.md)
[ -n "$lenses" ] || fail "no lens file written"
all=$(cat $lenses)
bad=()
grep -qiE 'preset[^.]{0,200}(sum type|union|labell?ed|coldest|warmest)|(sum type|union|labell?ed)[^.]{0,200}preset' <<<"$all" || bad+=("P1")
grep -qiE 'sentinel|-1\b' <<<"$all" || bad+=("P2")
grep -qiE 'RoomId|LampId|brand|(room|lamp)[- ]id type|id type|newtype' <<<"$all" || bad+=("P3")
grep -qiE "(entity|service)[^.]{0,120}(core|adapter)|(core|adapter)[^.]{0,120}(entity|service)|input_number|light\.turn_on" <<<"$all" || bad+=("P4")
grep -iE 'RECOMMEND' <<<"$all" | grep -qiE 'alert|observab|metric|tracing|restore rehears|dashboards? for' && bad+=("X:production machinery recommended")
st=$(git -C "$RUN/work" status --porcelain)
[ -z "$st" ] || bad+=("tree:$(head -c 80 <<<"$st")")
[ ${#bad[@]} -eq 0 ] && pass "lens output covers P1-P4, no production machinery"
fail "missing/bad: ${bad[*]}"
