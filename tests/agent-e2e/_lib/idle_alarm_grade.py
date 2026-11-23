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

# Only a background launch counts: a foreground Agent result carries agentId
# too, and the hook rightly stays silent for it.
dispatched = re.search(r"async_launched|Async agent launched", transcript) is not None
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


BACKGROUND_ID = re.compile(
    r'running in background with ID: ([A-Za-z0-9_-]+)|"backgroundTaskId":\s*"([A-Za-z0-9_-]+)"')


def blocks(event: dict, kind: str) -> list[dict]:
    content = (event.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict) and b.get("type") == kind] \
        if isinstance(content, list) else []


def stopped_waiter() -> bool:
    """The parent stopped the wait-for task itself: TaskStop/KillShell naming the
    background id of its `wait-for agent` Bash call, or kill/pkill of wait-for.
    Stopping some other task (the subagent) leaves the waiter running."""
    events = []
    for line in transcript.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        # stream-json marks a subagent's events with parent_tool_use_id.
        if isinstance(event, dict) and not event.get("parent_tool_use_id"):
            events.append((event, line))
    waiter_calls = {b.get("id") for e, _ in events if e.get("type") == "assistant"
                    for b in blocks(e, "tool_use") if b.get("name") == "Bash"
                    and "wait-for agent" in str((b.get("input") or {}).get("command", ""))}
    waiter_ids = set()
    for e, line in events:
        if e.get("type") == "user" and any(b.get("tool_use_id") in waiter_calls
                                           for b in blocks(e, "tool_result")):
            for pair in BACKGROUND_ID.findall(line):
                waiter_ids |= {i for i in pair if i}
    for e, _ in events:
        if e.get("type") != "assistant":
            continue
        for b in blocks(e, "tool_use"):
            if b.get("name") in ("TaskStop", "KillShell"):
                given = {str(v) for v in (b.get("input") or {}).values()}
                if given & waiter_ids:
                    return True
    # Or the parent's own Bash killed it: one shell segment that is a kill
    # naming wait-for (`pkill -f x; wait-for agent X` is a re-arm, not a stop).
    for e, _ in events:
        if e.get("type") != "assistant":
            continue
        for b in blocks(e, "tool_use"):
            if b.get("name") != "Bash":
                continue
            command = str((b.get("input") or {}).get("command", ""))
            for segment in re.split(r"[;&|\n]+", command):
                if re.match(r"\s*(?:sudo\s+)?p?kill\b", segment) and "wait-for" in segment:
                    return True
    return False


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
