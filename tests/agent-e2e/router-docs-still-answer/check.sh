#!/usr/bin/env bash
# PASS: the answer is exit 3, read out of the projected tree. Both arms should
# pass -- that is what makes it a control. A FAIL here means the edits to the
# tool's README cost an agent an answer it used to have.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/final.txt" ] || { echo "INCONCLUSIVE: no final message"; exit 2; }

python3 - "$RUN/final.txt" <<'PY' || fail "quota exit code not reported as 3: $(tr '\n' ' ' < "$RUN/final.txt" | tail -c 300)"
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
# `3` with quota in the same sentence, in either order.
for sentence in re.split(r"(?<=[.?!])\s+|\n+", text):
    if re.search(r"\bquota\b", sentence, re.I) and re.search(r"(?<![0-9])3(?![0-9])", sentence):
        sys.exit(0)
sys.exit(1)
PY

grep -qE 'tools/ai-router|skills/ai-router' "$RUN/commands.txt" 2>/dev/null \
  || grep -q 'ai-router' "$RUN/transcript.jsonl" 2>/dev/null \
  || { echo "INCONCLUSIVE: answered without consulting the tool"; exit 2; }

pass "exit 3 for quota, answered from the projected tree"
