#!/usr/bin/env bash
# PASS: after the hook nudge, the worker loaded visual-verify, either through
# the Skill tool or by reading its SKILL.md (Read, or cat via Bash). FAIL: the nudge went unheeded.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
r=$(python3 - "$RUN/transcript.jsonl" "$(dirname "$0")/../_lib" <<'PY'
import json, sys; sys.path.insert(0, sys.argv[2])
from skill_listing import tool_uses
uses = list(tool_uses(sys.argv[1], sub=True))
if not uses:
    print("nosub"); sys.exit()
edited = any(n in ("Edit", "Write") and "orders.html" in json.dumps(i) for n, i in uses)
loaded = any((n == "Skill" and "visual-verify" in json.dumps(i)) or
             (n in ("Read", "Bash") and "visual-verify/SKILL.md" in json.dumps(i)) for n, i in uses)
print(("edited" if edited else "noedit") + "," + ("loaded" if loaded else "notloaded"))
PY
)
case "$r" in
  nosub) echo "INCONCLUSIVE: no worker dispatched"; exit 2 ;;
  noedit,*) echo "INCONCLUSIVE: worker never edited the template"; exit 2 ;;
  *,loaded) pass "worker loaded visual-verify after the nudge" ;;
  *) fail "worker edited the template but never loaded visual-verify" ;;
esac
