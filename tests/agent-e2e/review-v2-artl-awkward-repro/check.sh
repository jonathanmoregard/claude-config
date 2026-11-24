#!/usr/bin/env bash
# P5: the planted defect is real, so no ledger row about downloads.js may end
# `refuted`. INCONCLUSIVE when no reviewer raised a finding there at all.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
ledger="$RUN/home/.local/state/claude-tasks/work/review-ledger.tsv"
[ -f "$ledger" ] || { echo "INCONCLUSIVE: no ledger written"; exit 2; }
rows=$(awk -F'\t' 'NR>5 && $6 ~ /downloads\.js/ && $8 != "none"' "$ledger")
[ -n "$rows" ] || { echo "INCONCLUSIVE: no finding on downloads.js"; exit 2; }
bad=$(awk -F'\t' '$8=="refuted"' <<<"$rows")
[ -z "$bad" ] || fail "a real defect was refuted: $(cut -f7,10 <<<"$bad" | head -n1)"
pass "downloads.js outcomes: $(cut -f8 <<<"$rows" | sort | uniq -c | tr -s ' \n' ' ')"
