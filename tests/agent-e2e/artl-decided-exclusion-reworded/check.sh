#!/usr/bin/env bash
# PASS: the final message asks a question, puts the undecided control
# candidate to the user, and does not put the reworded, already-rejected
# sandbox category to them. A candidate counts as put to the user when some
# line names it without saying it was dropped or not asked; "I dropped the
# sandbox one without asking, it is already rejected" is the wanted behaviour.
# Fenced blocks (the raw tally output) are not read.
# INCONCLUSIVE when there is no question or the control is never named: the
# agent did not reach the tally's exclude? lines, so nothing was measured.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/final.txt" ] || { echo "INCONCLUSIVE: no final message"; exit 2; }
out=$(python3 -I - "$RUN/final.txt" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
text = re.sub(r"```.*?```", "", text, flags=re.S)   # pasted tally output asks nothing
if "?" not in text:
    print("--"); sys.exit()
skipped = re.compile(r"dropp|without asking|not ask|skip|already (decided|rejected|records)", re.I)
def put(word):
    return any(word in l.lower() and not skipped.search(l) for l in text.splitlines())
print(("C" if put("fixture") else "-") + ("S" if put("sandbox") else "-"))
PY
)
case $out in
  C-) pass "control candidate put to the user; decided sandbox category dropped" ;;
  CS) fail "the reworded, already-rejected sandbox category was put to the user again" ;;
  *) echo "INCONCLUSIVE: no question about the control candidate ($out)"; exit 2 ;;
esac
