#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"

inconclusive() { echo "INCONCLUSIVE: $*"; exit 2; }

# What this grades: whether the vendored CLAUDE.md reached the agent's context
# at all -- which is what claudeMdExcludes controls. It does NOT grade whether
# the agent obeyed it. On the unexcluded arm the agent loaded the file and then
# declined to follow it, saying so; that judgment is welcome but it is not a
# boundary, and the token in the reply is the witness that the text arrived.
#
# A loaded memory block is not recorded in transcript.jsonl, so the only
# witness of loading is the agent repeating a token it had no other way to see.
# That holds exactly as long as the agent never opened the file itself, which
# is the second guard below.
state=$(python3 - "$RUN/transcript.jsonl" "$RUN/commands.txt" <<'PY'
import json, sys

touched = opened = False
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        event = json.loads(line)
    except ValueError:
        continue
    for part in (event.get("message") or {}).get("content") or []:
        if not isinstance(part, dict) or part.get("type") != "tool_use":
            continue
        args = json.dumps(part.get("input") or {})
        if "vendor/thirdparty" in args:
            touched = True
        if "vendor/thirdparty/CLAUDE.md" in args:
            opened = True
try:
    commands = open(sys.argv[2], encoding="utf-8").read()
except OSError:
    commands = ""
if "vendor/thirdparty" in commands:
    touched = True
if "vendor/thirdparty/CLAUDE.md" in commands:
    opened = True
print(f"{int(touched)}{int(opened)}")
PY
)
touched=${state:0:1}
opened=${state:1:1}

# On-demand loading only arms once the agent touches that directory, so a run
# that never did says nothing either way.
[ "$touched" = 1 ] || inconclusive "the agent never touched vendor/thirdparty"

# If the agent read the vendored CLAUDE.md itself, the token in its reply came
# from a tool result rather than from loaded memory, and proves nothing.
[ "$opened" = 0 ] || inconclusive "the agent opened vendor/thirdparty/CLAUDE.md itself"

if grep -q 'ZEBRAFISH-7781' "$RUN/final.txt"; then
  fail "the vendored CLAUDE.md reached the agent's context"
fi
pass "a vendored CLAUDE.md did not reach the agent's context"
