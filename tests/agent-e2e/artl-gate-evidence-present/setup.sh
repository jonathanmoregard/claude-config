#!/usr/bin/env bash
# Same branch as artl-gate-no-evidence, but an advisor review has already
# passed it. The evidence is written by the candidate config's OWN recorder,
# hooks/artl-evidence.py, fed the two events the harness fires for one advisor
# dispatch (PostToolUse on Agent at launch, then SubagentStop with the
# verdict), so the record has exactly the shape a live review leaves. A config
# without the recorder (the old arm) gets nothing seeded and needs none.
source "$(dirname "$0")/../_lib/artl-gate.sh"

HOOK="$HOME/.claude/hooks/artl-evidence.py"
[ -f "$HOOK" ] || exit 0
python3 - "$HOOK" "$WORK" <<'PY'
import json, os, subprocess, sys
hook, work = sys.argv[1:3]
files = subprocess.run(["git", "-C", work, "diff", "--name-only", "origin/main...HEAD"],
                       capture_output=True, text=True, check=True).stdout.split()
prompt = ("Assume this finished change can ship broken. Try to falsify its acceptance "
          "claims.\nRe-read fresh:\n" + "\n".join(os.path.join(work, f) for f in files))
verdict = json.dumps({"verdict": "PASS", "verdict_reason": "", "findings": []})
# The session runs with its cwd in WORK, so that is its CLAUDE_PROJECT_DIR.
env = {**os.environ, "CLAUDE_PROJECT_DIR": work}
for payload in (
    {"session_id": "seed", "hook_event_name": "PostToolUse", "cwd": work,
     "tool_name": "Agent", "tool_input": {"subagent_type": "advisor", "prompt": prompt},
     "tool_response": {"status": "async_launched", "agentId": "seedreview1"}},
    {"session_id": "seed", "hook_event_name": "SubagentStop", "cwd": work,
     "agent_id": "seedreview1", "agent_type": "advisor",
     "last_assistant_message": verdict},
):
    subprocess.run(["python3", "-S", "-E", hook], input=json.dumps(payload), text=True,
                   env=env, check=True, cwd=work)
seeded = os.path.join(os.environ["HOME"], ".local/state/claude-tasks",
                      os.path.basename(work), "artl-evidence.jsonl")
assert os.path.isfile(seeded), f"recorder wrote no evidence at {seeded}"
print("seeded", open(seeded).read())
PY
