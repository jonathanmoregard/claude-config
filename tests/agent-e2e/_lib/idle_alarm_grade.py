#!/usr/bin/env python3
"""Grades the idle-alarm-* scenarios from commands.txt, notify.log and the transcript.

    idle_alarm_grade.py RUN stall|quiet|live

stall: the parent armed `wait-for agent <id>` AND the alarm fired (notify.log
       names a stalled agent, and the wait's exit-5 line reached the parent).
quiet: the parent armed `wait-for agent <id>`, no stall notification fired,
       and the staged wait-for, replayed over the run's persisted subagent
       files, reads the agent as finished (exit 0). finished_line is
       diagnostic only: the wait's output stays in the sandbox's private /tmp.
live:  quiet, AND the worker's job really ran as a (fake) unit AND its
       transcript stayed quiet for at least the 60 s idle limit; otherwise the
       run never reached the situation and is INCONCLUSIVE, not a vacuous pass.
INCONCLUSIVE (exit 2) when no background Agent dispatch happened at all.
"""
import datetime
import json
import os
import pathlib
import re
import subprocess
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
finished_seen = re.search(r"agent [A-Za-z0-9]+ finished", transcript) is not None
fired = "stalled" in notify
print(f"armed={armed} notify={fired} stalled_line={stalled_seen} finished_line={finished_seen}")
if mode == "stall":
    sys.exit(0 if armed and fired and stalled_seen else 1)
if mode in ("quiet", "live"):
    home = run / "home"
    transcripts = sorted(home.glob(".claude/projects/*/*/subagents/agent-*.jsonl"))
    if len(transcripts) != 1:
        print(f"expected one subagent transcript, found {len(transcripts)}")
        sys.exit(2)
    agent = transcripts[0].name.removeprefix("agent-").removesuffix(".jsonl")
    if mode == "live":
        calls = run / "fake-systemd" / "calls.log"
        ran = calls.exists() and "systemd-run" in calls.read_text(errors="replace")
        started = list((run / "fake-systemd").glob("*.started")) if ran else []
        # The situation under test: the worker stayed quiet past the idle limit
        # (60 s) while its unit ran. A worker that polled faster never reached it.
        stamps = []
        for line in transcripts[0].read_text(errors="replace").splitlines():
            try:
                ts = json.loads(line).get("timestamp")
            except (ValueError, AttributeError):
                continue
            if isinstance(ts, str):
                stamps.append(datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")))
        gap = max(((b - a).total_seconds() for a, b in zip(stamps, stamps[1:])), default=0)
        print(f"unit_ran={ran} max_quiet_gap={gap:.0f}s")
        if not ran or not started or gap < 60:
            sys.exit(2)
    # The wait's own output stays in the sandbox's private /tmp, so its exit is
    # pinned by replaying the staged wait-for over the run's persisted files:
    # the agent's end state must read as finished (exit 0).
    replay = subprocess.run(
        [str(home / ".claude" / "scripts" / "wait-for"), "agent", agent, "--idle", "60", "--timeout", "5"],
        env={**os.environ, "HOME": str(home)}, capture_output=True, text=True, timeout=60)
    print(f"replay_exit={replay.returncode} {replay.stdout.strip()}")
    sys.exit(0 if armed and not fired and replay.returncode == 0 else 1)
sys.exit(3)
