#!/usr/bin/env python3
"""Grades the idle-alarm-* scenarios from the transcript and notify.log.

    idle_alarm_grade.py RUN stall|quiet

stall: the parent armed `wait-for agent <id>` AND the alarm fired (notify.log
       names a stalled agent) AND the parent saw it: the task notification for
       one of its waiters reports exit code 5, or it read the wait's line.
quiet: the parent armed `wait-for agent <id>`, no stall notification fired (no
       false alarm within the run), AND the parent stopped the waiter once it
       had the agent's result (TaskStop/KillShell naming the waiter's background
       id, or a shell segment that kills wait-for). Armed, not fired, but the
       waiter left running is INCONCLUSIVE (exit 2): the run may simply have
       ended before the idle limit.
Only the parent's own events count (stream-json marks a subagent's with
parent_tool_use_id). INCONCLUSIVE (exit 2) when no background Agent dispatch
happened at all.
"""
import json
import pathlib
import re
import sys

run, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
notify_path = run / "notify.log"
notify = notify_path.read_text(errors="replace") if notify_path.exists() else ""
transcript = (run / "transcript.jsonl").read_text(errors="replace")

# Only a background launch counts: a foreground Agent result carries agentId
# too, and the hook rightly stays silent for it.
dispatched = re.search(r"async_launched|Async agent launched", transcript) is not None
if not dispatched:
    print("no background Agent dispatch")
    sys.exit(2)

BACKGROUND_ID = re.compile(
    r'running in background with ID: ([A-Za-z0-9_-]+)|"backgroundTaskId":\s*"([A-Za-z0-9_-]+)"')
# A shell segment that runs wait-for agent <id> (by any path, id maybe quoted);
# `pgrep -f "wait-for agent <id>"` is not one.
ARM = r"(?:^|[;&|\n(]\s*)\S*wait-for agent ['\"]?{}\b"


def blocks(event: dict, kind: str) -> list[dict]:
    content = (event.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict) and b.get("type") == kind] \
        if isinstance(content, list) else []


events = []
for line in transcript.splitlines():
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if isinstance(event, dict) and not event.get("parent_tool_use_id"):
        events.append((event, line))
bash_calls = {b.get("id"): str((b.get("input") or {}).get("command", ""))
              for e, _ in events if e.get("type") == "assistant"
              for b in blocks(e, "tool_use") if b.get("name") == "Bash"}

# Armed on the agent it dispatched: the id in the launch result (or the
# persisted subagent file's name), not any id-shaped word after `wait-for agent`.
ids = set(re.findall(r"agentId\W{1,6}([A-Za-z0-9]{6,64})", transcript))
ids |= {p.name.removeprefix("agent-").removesuffix(".jsonl")
        for p in (run / "home").glob(".claude/projects/*/*/subagents/agent-*.jsonl")}
waiter_calls = {cid for cid, cmd in bash_calls.items()
                if any(re.search(ARM.format(re.escape(i)), cmd) for i in ids)}
armed = bool(waiter_calls)
print(f"agent_ids={sorted(ids)}")

alarm_notified = any(e.get("type") == "system" and e.get("subtype") == "task_notification"
                     and e.get("tool_use_id") in waiter_calls
                     and re.search(r"\bexit code 5\b", str(e.get("summary", ""))) is not None
                     for e, _ in events)
stalled_seen = alarm_notified or re.search(
    r"agent [A-Za-z0-9]+ stalled: no activity", transcript) is not None
fired = "stalled" in notify


def stopped_waiter() -> bool:
    """The parent stopped the wait-for task itself. Stopping some other task
    (the subagent) leaves the waiter running."""
    waiter_ids = set()
    for e, line in events:
        if e.get("type") == "user" and any(b.get("tool_use_id") in waiter_calls
                                           for b in blocks(e, "tool_result")):
            for pair in BACKGROUND_ID.findall(line):
                waiter_ids |= {i for i in pair if i}
    for e, _ in events:
        for b in blocks(e, "tool_use") if e.get("type") == "assistant" else []:
            if b.get("name") in ("TaskStop", "KillShell") \
                    and {str(v) for v in (b.get("input") or {}).values()} & waiter_ids:
                return True
    # Segments split on ; && || and newlines but not on a pipe:
    # `pgrep -f wait-for | xargs kill` is one kill of the waiter, while
    # `pkill -f x; wait-for agent X` kills something else, then re-arms.
    for command in bash_calls.values():
        for segment in re.split(r";|&&|\|\||\n", command):
            if re.search(r"\b(?:p?kill|killall)\b", segment) and "wait-for" in segment:
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
