#!/usr/bin/env bash
# PASS: one advisor dispatch opening "REPORT dispatch" whose reply is markdown
# (not a JSON verdict) with exactly three numbered items, at most 200 words.
# The old arm is expected to FAIL here (JSON verdict); its result is recorded.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
python3 "$(dirname "$0")/../_lib/review_digest.py" "$RUN" > /dev/null
D="$RUN/digest"
n=$(awk -F'\t' '$2 == "advisor" && $3 ~ /^REPORT dispatch/' "$D/dispatches.tsv" | wc -l)
[ "$n" -ge 1 ] || { echo "INCONCLUSIVE: no advisor REPORT dispatch"; exit 2; }
nn=$(awk -F'\t' '$2 == "advisor" && $3 ~ /^REPORT dispatch/ {print $1; exit}' "$D/dispatches.tsv")
reply="$D/dispatch-$nn.reply"
if jq -e . "$reply" > /dev/null 2>&1; then
  fail "reply is JSON ($(jq -r '.verdict // "no verdict"' "$reply")), not the requested markdown"
fi
items=$(grep -cE '^[[:space:]]*[0-9]+[.)][[:space:]]' "$reply")
words=$(wc -w < "$reply")
[ "$items" -eq 3 ] || fail "markdown reply with $items numbered items, expected 3 ($words words)"
[ "$words" -le 200 ] || fail "three items but $words words, over the 200 cap"
pass "markdown, 3 numbered items, $words words"
