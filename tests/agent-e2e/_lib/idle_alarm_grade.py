#!/usr/bin/env python3
"""Grades the idle-alarm-* scenarios from the run's stream-json task events.

    idle_alarm_grade.py RUN stall|quiet

The harness reports every background Bash as a task: `task_started` (with the
Bash call's tool_use_id), `task_updated` (status `killed` when stopped) and
`task_notification` (summary "... exit code N" when it exits non-zero). A
waiter is a background task the parent started from a Bash call that runs
`wait-for ... agent <id>` for the agent it dispatched. Grading on these events,
not on command text, means a foreground `pgrep -f "wait-for agent X"`, a
denied TaskStop or a kill of some other task cannot count.

stall: a waiter, AND notify.log shows the alarm, AND a waiter's notification
       reports exit code 5 followed by a parent turn (the parent saw it).
quiet: a waiter, no alarm, AND a waiter was killed (the parent stopped it once
       it had the result). A waiter that failed with any other code means
       arming was broken: FAIL. Armed, not fired, left running: INCONCLUSIVE
       (exit 2), the run may have ended before the idle limit.
INCONCLUSIVE (exit 2) with no transcript or no background Agent dispatch.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys


def grade(run: pathlib.Path, mode: str) -> tuple[int, str]:
    try:
        transcript = (run / "transcript.jsonl").read_text(errors="replace")
    except OSError:
        return 2, "no transcript"
    notify_path = run / "notify.log"
    fired = notify_path.exists() and "stalled" in notify_path.read_text(errors="replace")

    events = []
    for line in transcript.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        # stream-json marks a subagent's events with parent_tool_use_id.
        if isinstance(event, dict) and not event.get("parent_tool_use_id"):
            events.append(event)
    if not any(e.get("type") == "system" and e.get("subtype") == "task_started"
               and e.get("task_type") == "local_agent" and e.get("is_backgrounded")
               for e in events) and "async_launched" not in transcript:
        return 2, "no background Agent dispatch"

    ids = set(re.findall(r"agentId\W{1,6}([A-Za-z0-9]{6,64})", transcript))
    ids |= {e.get("task_id") for e in events if e.get("subtype") == "task_started"
            and e.get("task_type") == "local_agent" and isinstance(e.get("task_id"), str)}
    ids |= {p.name.removeprefix("agent-").removesuffix(".jsonl")
            for p in (run / "home").glob(".claude/projects/*/*/subagents/agent-*.jsonl")}

    commands = {}
    for e in events:
        content = (e.get("message") or {}).get("content") if e.get("type") == "assistant" else None
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Bash":
                commands[b.get("id")] = str((b.get("input") or {}).get("command", ""))

    def runs_wait_for(cmd: str) -> bool:
        return "wait-for" in cmd and any(
            re.search(rf"\bagent\s+['\"]?{re.escape(i)}\b", cmd) for i in ids)

    waiters = {e["task_id"] for e in events
               if e.get("type") == "system" and e.get("subtype") == "task_started"
               and e.get("task_type") == "local_bash" and not e.get("owned_by_subagent")
               and isinstance(e.get("task_id"), str)
               and runs_wait_for(commands.get(e.get("tool_use_id"), ""))}
    # Claude Code itself stops tasks too (headless background ceiling, memory
    # pressure); those are not the parent stopping its waiter.
    system_stopped = {e.get("task_id") for e in events if e.get("subtype") == "task_notification"
                      and re.search(r"time limit|low on memory", str(e.get("summary", "")))}
    killed = ({e.get("task_id") for e in events if e.get("subtype") == "task_updated"
               and (e.get("patch") or {}).get("status") == "killed"} & waiters) - system_stopped
    exits = []  # (index, task_id, exit code) for each waiter that exited non-zero
    for n, e in enumerate(events):
        if e.get("subtype") == "task_notification" and e.get("task_id") in waiters:
            code = re.search(r"\bexit code (\d+)\b", str(e.get("summary", "")))
            if code:
                exits.append((n, e["task_id"], int(code.group(1))))
    seen = any(code == 5 and any(e.get("type") == "assistant" for e in events[n + 1:])
               for n, _, code in exits)
    broken = sorted({code for _, _, code in exits if code != 5})

    summary = (f"agent_ids={sorted(ids)} waiters={sorted(waiters)} notify={fired} "
               f"alarm_seen={seen} killed={sorted(killed)} other_exits={broken}")
    armed = bool(waiters)
    if mode == "stall":
        return (0 if armed and fired and seen else 1), summary
    if mode == "quiet":
        if not armed or fired or broken:
            return 1, summary
        if not killed:
            return 2, summary + " (waiter left running: not stopped, not fired)"
        return 0, summary
    return 3, f"unknown mode {mode}"


if __name__ == "__main__":
    rc, text = grade(pathlib.Path(sys.argv[1]), sys.argv[2])
    print(text)
    sys.exit(rc)
