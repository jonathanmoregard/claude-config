#!/usr/bin/env bash
# PASS: the reply is a quest walkthrough that sends only ONE quest (one quest
# heading), ends on its checkpoint, and does not preview the later quests'
# content ("Up next: Quest 2 generates the key…"); the progress marker is
# the only preview. A bare "that's the next quest" pointer is allowed.
# INCONCLUSIVE when no quest framing was used at all (skill not applied).
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/final.txt" ] || { echo "INCONCLUSIVE: no final message"; exit 2; }
read -r headings checkpoints previews < <(python3 - "$RUN/final.txt" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
# A quest heading: a markdown heading that names a quest.
headings = [l for l in text.splitlines() if re.match(r"\s*#{1,6}\s.*\bquest\b", l, re.I)]
checkpoints = len(re.findall(r"checkpoint|bring me|🎁", text, re.I))
# A preview names a later quest by number, or opens an "up next" list.
previews = len(re.findall(r"\bquest\s*[2-9]\b|\bup next\b|\bcoming up\b", text, re.I))
print(len(headings), checkpoints, previews)
PY
)
[ "$headings" -eq 0 ] && { echo "INCONCLUSIVE: no quest heading (skill not applied)"; exit 2; }
[ "$headings" -eq 1 ] || fail "$headings quest headings in one message"
[ "$checkpoints" -ge 1 ] || fail "one quest but no checkpoint"
[ "$previews" -eq 0 ] || fail "one quest, but $previews preview(s) of later quests"
pass "one quest, $checkpoints checkpoint marker(s), no preview"
