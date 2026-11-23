#!/usr/bin/env python3
"""Grades the idle-alarm-* scenarios from commands.txt, notify.log and the transcript.

    idle_alarm_grade.py RUN stall|quiet

stall: the parent armed `wait-for agent <id>` AND the alarm fired (notify.log
       names a stalled agent, and the wait's exit-5 line reached the parent).
quiet: the parent armed `wait-for agent <id>`, no stall notification fired (no
       false alarm within the run), AND the parent stopped the waiter once it
       had the agent's result (a TaskStop/KillShell tool call, or a `kill` /
       `pkill` of the wait-for process in commands.txt). Armed, not fired, but
       the waiter left running is INCONCLUSIVE (exit 2): the run may simply
       have ended before the idle limit.
INCONCLUSIVE (exit 2) when no background Agent dispatch happened at all.
"""
import json
import pathlib
import re
import sys

run, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
commands = (run / "commands.txt").read_text(errors="replace")
notify_path = run / "notify.log"
notify = notify_path.read_text(errors="replace") if notify_path.exists() else ""
transcript = (run / "transcript.jsonl").read_text(errors="replace")

dispatched = re.search(r"async_launched|Async agent launched|agentId", transcript) is not None
if not dispatched:
    print("no background Agent dispatch")
    sys.exit(2)
# Armed on the agent it dispatched: the id in the launch result (or the
# persisted subagent file's name), not any id-shaped word after `wait-for agent`.
ids = set(re.findall(r"agentId\W{1,6}([A-Za-z0-9]{6,64})", transcript))
ids |= {p.name.removeprefix("agent-").removesuffix(".jsonl")
        for p in (run / "home").glob(".claude/projects/*/*/subagents/agent-*.jsonl")}
armed = any(re.search(rf"wait-for agent {re.escape(i)}\b", commands) for i in ids)
print(f"agent_ids={sorted(ids)}")
stalled_seen = re.search(r"agent [A-Za-z0-9]+ stalled: no activity", transcript) is not None
fired = "stalled" in notify


def stopped_waiter() -> bool:
    """The parent's own tool calls: TaskStop/KillShell, or kill/pkill of wait-for."""
    for line in transcript.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant" or event.get("agent_id"):
            continue
        content = (event.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use" \
                    and block.get("name") in ("TaskStop", "KillShell"):
                return True
    return re.search(r"\b(?:pkill|kill)\b[^\n]*wait-for", commands) is not None


stopped = stopped_waiter()
print(f"armed={armed} notify={fired} stalled_line={stalled_seen} stopped={stopped}")
if mode == "stall":
    sys.exit(0 if armed and fired and stalled_seen else 1)
if mode == "quiet":
    if not armed or fired:
        sys.exit(1)
    if not stopped:
        print("the waiter was left running (not stopped, not fired)")
        sys.exit(2)
    sys.exit(0)
sys.exit(3)
