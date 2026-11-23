#!/usr/bin/env python3
"""Grades the idle-alarm-* scenarios from the run's stream-json task events.

    idle_alarm_grade.py RUN stall|quiet

The harness reports every background Bash as a task: `task_started` (with the
Bash call's tool_use_id), `task_updated` (status `killed` when stopped) and
`task_notification` (status `failed` + 'Background command "<desc>" failed
with exit code N', or status `stopped` + '... was stopped after reaching its
background time limit' when Claude Code itself stopped it). A waiter is a
background task the parent started from a Bash call that runs
`wait-for ... agent <id>` for the agent it dispatched. Grading on these events,
not on command text, means a foreground `pgrep -f "wait-for agent X"`, a
denied TaskStop or a kill of some other task cannot count.

stall: PASS = a waiter, notify.log shows the alarm, and a waiter's
       notification reports exit code 5 followed by a parent turn.
       INCONCLUSIVE = no alarm because Claude Code stopped the waiter (headless
       time limit, memory) and nothing else went wrong. Else FAIL.
quiet: PASS = a waiter, no alarm, and the parent's own TaskStop/KillShell
       killed a waiter after the agent it watches completed, with no waiter
       left running. FAIL = not armed, alarm fired, or a waiter exited with
       any other code (arming broken). Else INCONCLUSIVE (exit 2): stopped
       too early, left running, or the run ended before the idle limit.
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

    def watches_agent(cmd: str, agent: str) -> bool:
        return "wait-for" in cmd and re.search(
            rf"\bagent\s+['\"]?{re.escape(agent)}\b", cmd) is not None

    waiter_tool = {e["task_id"]: e.get("tool_use_id") for e in events
                   if e.get("type") == "system" and e.get("subtype") == "task_started"
                   and e.get("task_type") == "local_bash" and e.get("is_backgrounded") is not False
                   and not e.get("owned_by_subagent")
                   and isinstance(e.get("task_id"), str)
                   and any(watches_agent(commands.get(e.get("tool_use_id"), ""), i) for i in ids)}
    waiters = set(waiter_tool)
    # Summaries quote the model-written Bash description first, so match the
    # harness's own wording after the closing quote, to the end. A TaskStop'd
    # task's summary is the bare description, which proves nothing either way.
    exit_line = re.compile(r'^Background command ".*" failed with exit code (\d+)$', re.S)
    system_line = re.compile(r'^Background command ".*" (?:was stopped after reaching its '
                             r"background time limit|stopped because the system is running "
                             r"low on memory)$", re.S)
    notes = [(n, e) for n, e in enumerate(events) if e.get("subtype") == "task_notification"
             and e.get("task_id") in waiters]
    system_stopped = {e["task_id"] for _, e in notes if e.get("status") == "stopped"
                      and system_line.match(str(e.get("summary", "")))}
    exits = []  # (index, task_id, exit code) for each waiter that exited non-zero
    for n, e in notes:
        code = exit_line.match(str(e.get("summary", ""))) if e.get("status") == "failed" else None
        if code:
            exits.append((n, e["task_id"], int(code.group(1))))
    kills = {e.get("task_id"): n for n, e in enumerate(events)
             if e.get("subtype") == "task_updated" and e.get("task_id") in waiters
             and (e.get("patch") or {}).get("status") == "killed"}
    # A stop is the parent's only when one of its TaskStop/KillShell calls
    # named that task; anything else that killed it was not the parent.
    asked_stop = {str(v) for e in events if e.get("type") == "assistant"
                  for b in ((e.get("message") or {}).get("content") or [])
                  if isinstance(b, dict) and b.get("type") == "tool_use"
                  and b.get("name") in ("TaskStop", "KillShell")
                  for v in (b.get("input") or {}).values()}
    parent_kills = {t: n for t, n in kills.items() if t in asked_stop and t not in system_stopped}
    ended = set(kills) | {t for _, t, _ in exits}
    # Each waiter watches the agent its command names; "after the result" is
    # after that agent completed, not after any agent did.
    done_at = {}
    for n, e in enumerate(events):
        status = e.get("status") or (e.get("patch") or {}).get("status")
        if e.get("task_id") in ids and status == "completed" \
                and e.get("subtype") in ("task_updated", "task_notification"):
            done_at.setdefault(e["task_id"], n)
    watches = {t: next((i for i in ids if watches_agent(commands.get(tool, ""), i)), None)
               for t, tool in waiter_tool.items()}

    seen = any(code == 5 and any(e.get("type") == "assistant" for e in events[n + 1:])
               for n, _, code in exits)
    broken = sorted({code for _, _, code in exits if code != 5})
    live = sorted(waiters - ended)
    stopped_after = any(watches.get(t) in done_at and n > done_at[watches[t]]
                        for t, n in parent_kills.items())

    summary = (f"agent_ids={sorted(ids)} waiters={sorted(waiters)} notify={fired} "
               f"alarm_seen={seen} parent_killed={sorted(parent_kills)} "
               f"system_stopped={sorted(system_stopped)} live={live} other_exits={broken}")
    armed = bool(waiters)
    if broken == [3] and not fired and not any((run / "home").glob(".claude/projects/*/*/subagents/agent-*.jsonl")):
        # No subagent transcript was ever written: the harness did not persist
        # the session, so wait-for could only say "no transcript". Not a verdict.
        return 2, summary + " (no subagent transcript persisted)"
    if mode == "stall":
        if armed and fired and seen:
            return 0, summary
        if armed and not fired and system_stopped and not broken and not parent_kills:
            # Claude Code stopped the waiter before it could fire: a resource
            # result of the headless run, not a verdict.
            return 2, summary + " (waiter stopped by Claude Code before the alarm)"
        return 1, summary
    if mode == "quiet":
        if not armed or fired or broken or any(code == 5 for _, _, code in exits):
            return 1, summary
        if live or not stopped_after:
            return 2, summary + " (waiter not stopped after the result, or one left running)"
        return 0, summary
    return 3, f"unknown mode {mode}"


if __name__ == "__main__":
    rc, text = grade(pathlib.Path(sys.argv[1]), sys.argv[2])
    print(text)
    sys.exit(rc)
