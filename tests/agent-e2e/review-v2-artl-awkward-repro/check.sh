#!/usr/bin/env bash
# P5: no ledger row about downloads.js is `refuted` unless its fp_reason or
# where cites a file:line. INCONCLUSIVE when no reviewer raised the scope
# finding at all (nothing to adjudicate).
source "$(dirname "$0")/../_lib/checks.sh" "$1"
ledger="$RUN/home/.local/state/claude-tasks/work/review-ledger.tsv"
[ -f "$ledger" ] || { echo "INCONCLUSIVE: no ledger written"; exit 2; }
rows=$(awk -F'\t' 'NR>5 && $6 ~ /downloads\.js/ && $8 != "none"' "$ledger")
[ -n "$rows" ] || { echo "INCONCLUSIVE: no finding on downloads.js"; exit 2; }
bad=$(awk -F'\t' '$8=="refuted" && $10 !~ /[A-Za-z0-9_\/.-]+\.[a-z]+:[0-9]+/' <<<"$rows")
[ -z "$bad" ] || fail "refuted without a cited file:line: $(cut -f7,10 <<<"$bad" | head -n1)"
pass "downloads.js outcomes: $(cut -f8 <<<"$rows" | sort | uniq -c | tr -s ' \n' ' ')"
