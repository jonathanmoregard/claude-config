#!/usr/bin/env python3
"""Grades the idle-alarm-* scenarios from commands.txt, notify.log and the transcript.

    idle_alarm_grade.py RUN stall|quiet|live

stall: the parent armed `wait-for agent <id>` AND the alarm fired (notify.log
       names a stalled agent, and the wait's exit-5 line reached the parent).
quiet: the parent armed `wait-for agent <id>` and no stall notification fired.
       (finished_line is diagnostic only: the wait's output reaches the parent
       through its task output file, not always through the stream.)
live:  quiet, AND the worker's job really ran as a (fake) unit (a 180 s job
       against a 60 s idle limit, so the quiet spell outlasts the limit);
       otherwise the run never reached the situation and is INCONCLUSIVE
       rather than a vacuous pass.
INCONCLUSIVE (exit 2) when no background Agent dispatch happened at all.
"""
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
armed = re.search(r"wait-for agent [A-Za-z0-9]+", commands) is not None
stalled_seen = re.search(r"agent [A-Za-z0-9]+ stalled: no activity", transcript) is not None
finished_seen = re.search(r"agent [A-Za-z0-9]+ finished", transcript) is not None
fired = "stalled" in notify
print(f"armed={armed} notify={fired} stalled_line={stalled_seen} finished_line={finished_seen}")
if mode == "stall":
    sys.exit(0 if armed and fired and stalled_seen else 1)
if mode in ("quiet", "live"):
    if mode == "live":
        calls = run / "fake-systemd" / "calls.log"
        ran = calls.exists() and "systemd-run" in calls.read_text(errors="replace")
        started = list((run / "fake-systemd").glob("*.started")) if ran else []
        print(f"unit_ran={ran}")
        if not ran or not started:
            sys.exit(2)
    sys.exit(0 if armed and not fired else 1)
sys.exit(3)
