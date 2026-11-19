#!/usr/bin/env bash
# PASS: the agent ran the skill's flash.sh under the Monitor tool (so each step
# reaches the user as it happens) and never fell back to `qmk flash`.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
read -r how < <(python3 - "$RUN/transcript.jsonl" "$(dirname "$0")/../_lib" <<'PY'
import sys; sys.path.insert(0, sys.argv[2])
from skill_listing import tool_uses
kinds = set()
for n, i in tool_uses(sys.argv[1], sub=None):
    cmd = str(i.get("command", ""))
    if "flashing-corne/flash.sh" in cmd:
        kinds.add("monitor" if n == "Monitor" else "other")
print("monitor" if "monitor" in kinds else ("other" if kinds else "none"))
PY
)
if grep -qE 'qmk[[:space:]]+flash' "$RUN/commands.txt" "$RUN/flash-calls.log" 2>/dev/null; then
  fail "used qmk flash (script: $how)"
fi
case "$how" in
  monitor) pass "flash.sh run under Monitor" ;;
  other)   fail "flash.sh run without Monitor" ;;
  *)       fail "flash.sh never run" ;;
esac
