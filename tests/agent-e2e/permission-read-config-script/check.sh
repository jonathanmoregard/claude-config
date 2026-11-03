#!/usr/bin/env bash
# PASS: a Read of a config path this change covers returned content.
# FAIL: every such Read came back a permission denial.
#
# Graded on the Read's own outcome, not on the answer text. The first version
# of this check graded the relayed answer and scored 5/5 on BOTH arms: denied
# on the live projection, the advisor read a worktree copy of the same script
# (`~/worktrees/**` is allowed) and answered correctly anyway. The answer
# therefore says nothing about the rule; the tool result says everything.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }

verdict=$(python3 - "$RUN/transcript.jsonl" <<'PY'
import json, sys

COVERED = ("/.claude/scripts", "/.claude/runtime", "/.claude/proposals",
           "/.local/state/claude-proposals")
reads, outcomes = {}, []
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    msg = ev.get("message")
    if not isinstance(msg, dict):
        continue
    content = msg.get("content")
    if not isinstance(content, list):
        continue
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use" and b.get("name") == "Read":
            path = (b.get("input") or {}).get("file_path", "")
            if any(c in path for c in COVERED):
                reads[b.get("id")] = path
        elif b.get("type") == "tool_result" and b.get("tool_use_id") in reads:
            outcomes.append(bool(b.get("is_error")))

if not outcomes:
    print("none")
elif all(outcomes):
    print("denied")
else:
    print("allowed")
PY
) || { echo "INCONCLUSIVE: could not parse transcript"; exit 2; }

case "$verdict" in
  none)    echo "INCONCLUSIVE: no agent read a path this change covers"; exit 2 ;;
  allowed) pass "Read of the covered config path returned content" ;;
  denied)  fail "every Read of the covered config path was denied" ;;
esac
echo "INCONCLUSIVE: unexpected verdict '$verdict'"; exit 2
