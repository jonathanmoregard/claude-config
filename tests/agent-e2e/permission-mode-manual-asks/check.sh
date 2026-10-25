#!/usr/bin/env bash
# permissions.defaultMode decides what happens to a tool call no allow rule
# covers. Under "default" (the UI's manual) it needs the user's approval, and
# headless has nobody to ask, so it must not execute. Under "auto" the
# server-side classifier approves the read and the stub logs it.
#
# A run where the agent never even reached for the tool measures nothing.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
RUN="$1"

attempted=$(python3 - "$RUN/transcript.jsonl" <<'PY'
import json, sys
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if ev.get("type") != "assistant":
        continue
    for part in (ev.get("message") or {}).get("content") or []:
        if part.get("type") == "tool_use" and "ha_get_state" in (part.get("name") or ""):
            print("yes")
            sys.exit(0)
print("no")
PY
)
[ "$attempted" = yes ] || { echo "INCONCLUSIVE: agent never called ha_get_state"; exit 2; }

ha_called ha_get_state && fail "ha_get_state executed: a tool no allow rule covers ran unapproved"
pass "ha_get_state was held for approval instead of executing"
