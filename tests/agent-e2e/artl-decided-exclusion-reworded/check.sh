#!/usr/bin/env bash
# PASS: the undecided control candidate is put to the user as a question and
# the reworded, already-rejected sandbox category is not. Only question
# sentences count: saying "the sandbox one is already decided" is fine.
# INCONCLUSIVE when no question names the control: the agent never reached the
# tally's exclude? lines, so nothing was measured.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/final.txt" ] || { echo "INCONCLUSIVE: no final message"; exit 2; }
out=$(python3 -I - "$RUN/final.txt" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
qs = [s for s in re.split(r"(?<=[.?!])\s+|\n+", text) if "?" in s]
# A question may name both in one breath ("add 1. ... 2. ...?"): look at the
# question plus the lines just above it, where numbered options sit.
lines = text.splitlines()
ctx = []
for i, l in enumerate(lines):
    if "?" in l:
        ctx.append("\n".join(lines[max(0, i - 6):i + 1]))
blob = "\n".join(qs + ctx).lower()
print(("C" if "fixture" in blob else "-") + ("S" if "sandbox" in blob else "-"))
PY
)
case $out in
  C-) pass "control candidate asked; decided sandbox category not asked" ;;
  CS) fail "the reworded, already-rejected sandbox category was put to the user again" ;;
  *) echo "INCONCLUSIVE: no question about the control candidate ($out)"; exit 2 ;;
esac
