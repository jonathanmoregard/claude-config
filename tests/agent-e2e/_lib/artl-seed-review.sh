#!/usr/bin/env bash
# Seeds a passing advisor review of the branch in $WORK. Source after
# _lib/artl-gate.sh. The evidence is written by the candidate config's OWN
# recorder, hooks/artl-evidence.py, fed the events the harness fires for one
# advisor dispatch: PostToolUse on Agent at launch, PostToolUse on Read for
# each file the advisor itself reads (agent_type advisor, measured), then the
# verdict -- so the record has exactly the shape a live review leaves. The
# verdict goes both ways an arm may take it: PostToolUse on the advisor's
# submit_verdict call (the current recorder's only channel), then SubagentStop
# carrying it as text (an older recorder's). Each recorder ignores the other's
# event. A config without the recorder gets nothing seeded and needs none.
HOOK="$HOME/.claude/hooks/artl-evidence.py"
[ -f "$HOOK" ] || exit 0
python3 - "$HOOK" "$WORK" <<'PY'
import glob, json, os, subprocess, sys
hook, work = sys.argv[1:3]
# Text files only: an advisor's Read cannot review a binary (numstat "-").
numstat = subprocess.run(["git", "-C", work, "diff", "--numstat", "-z", "--no-renames",
                          "origin/main...HEAD"],
                         capture_output=True, text=True, check=True).stdout.split("\0")
# And present ones only: a live advisor's Read of a deleted file fails.
files = [row.split("\t", 2)[2] for row in numstat
         if row.count("\t") >= 2 and not row.startswith("-\t")]
files = [f for f in files if os.path.exists(os.path.join(work, f))]
prompt = ("Assume this finished change can ship broken. Try to falsify its acceptance "
          "claims.\nRe-read fresh:\n" + "\n".join(os.path.join(work, f) for f in files))
verdict = json.dumps({"verdict": "PASS", "verdict_reason": "", "findings": []})
# The session runs with its cwd in WORK, so that is its CLAUDE_PROJECT_DIR.
env = {**os.environ, "CLAUDE_PROJECT_DIR": work}
steps = [{"session_id": "seed", "hook_event_name": "PostToolUse", "cwd": work,
          "tool_name": "Agent", "tool_input": {"subagent_type": "advisor", "prompt": prompt},
          "tool_response": {"status": "async_launched", "agentId": "seedreview1"}}]
steps += [{"session_id": "seed", "hook_event_name": "PostToolUse", "cwd": work,
           "tool_name": "Read", "tool_input": {"file_path": os.path.join(work, f)},
           "tool_response": {"type": "text"}, "agent_id": "seedreview1",
           "agent_type": "advisor"} for f in files]
steps.append({"session_id": "seed", "hook_event_name": "PostToolUse", "cwd": work,
              "tool_name": "mcp__artl_verdict__submit_verdict",
              "tool_input": json.loads(verdict),
              "tool_response": [{"type": "text", "text": "Verdict recorded."}],
              "agent_id": "seedreview1", "agent_type": "advisor"})
steps.append({"session_id": "seed", "hook_event_name": "SubagentStop", "cwd": work,
              "agent_id": "seedreview1", "agent_type": "advisor",
              "last_assistant_message": verdict})
for payload in steps:
    subprocess.run(["python3", "-S", "-E", hook], input=json.dumps(payload), text=True,
                   env=env, check=True, cwd=work)
home = os.environ["HOME"]
name = os.path.basename(work)
seeded = (glob.glob(f"{home}/.local/state/claude-pr-open-gate/records/{name}/artl-evidence.jsonl")
          + glob.glob(f"{home}/.local/state/claude-tasks/{name}/artl-evidence.jsonl"))
assert seeded, "the recorder wrote no evidence"
print("seeded", seeded[0], open(seeded[0]).read())
PY
