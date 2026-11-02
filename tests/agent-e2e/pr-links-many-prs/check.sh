#!/usr/bin/env bash
# PASS: every PR the reply hands back carries its own full URL.
#
# "Every" is the point. The lapse this guards was not a missing link, it was
# four numbers in a row and no links at all, so a grader satisfied by one URL
# would score the exact failure as a pass.
#
# INCONCLUSIVE when the reply never asks for a merge or review: the rule
# governs that ask, and a run that answered some other way has not exercised
# it.
source "$(dirname "$0")/../_lib/checks.sh" "$1"

python3 - "$RUN/final.txt" <<'PY'
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()

if not re.search(r"\bmerge\b|\breview\b|your click|awaiting you", text, re.I):
    print("INCONCLUSIVE: the reply never asked for a merge or review")
    sys.exit(2)

# Which PRs did the reply actually bring up? Grade only those: a reply that
# legitimately covers three of the four is judged on the three.
mentioned = {int(m) for m in re.findall(r"(?:#|\bPR\s*)([1-4])\b", text)}
linked = {int(m) for m in
          re.findall(r"https://github\.com/acme/widget/pull/([1-4])\b", text)}

if not mentioned and not linked:
    print("INCONCLUSIVE: the reply named no PR at all")
    sys.exit(2)

missing = sorted(mentioned - linked)
if missing:
    print(f"FAIL: PR(s) {missing} asked about with no URL "
          f"(linked: {sorted(linked) or 'none'})")
    sys.exit(1)

print(f"PASS: every PR handed back carries its URL ({sorted(linked)})")
sys.exit(0)
PY
