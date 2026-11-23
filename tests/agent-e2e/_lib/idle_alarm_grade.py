#!/usr/bin/env python3
"""Grades the idle-alarm-* scenarios from commands.txt, notify.log and the transcript.

    idle_alarm_grade.py RUN stall|quiet

stall: the parent armed `wait-for agent <id>` AND the alarm fired (notify.log
       names a stalled agent, and the wait's exit-5 line reached the parent).
quiet: the parent armed `wait-for agent <id>` and no stall notification fired.
INCONCLUSIVE (exit 2) when no background Agent dispatch happened at all.
"""
import json
import pathlib
import re
import sys

run, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
commands = (run / "commands.txt").read_text(errors="replace")
notify = (run / "notify.log").read_text(errors="replace") if (run / "notify.log").exists() else ""
transcript = (run / "transcript.jsonl").read_text(errors="replace")

dispatched = re.search(r"async_launched|Async agent launched|agentId", transcript) is not None
if not dispatched:
    print("no background Agent dispatch"); sys.exit(2)
armed = re.search(r"wait-for agent [A-Za-z0-9]+", commands) is not None
stalled_seen = re.search(r"agent [A-Za-z0-9]+ stalled: no activity", transcript) is not None
print(f"armed={armed} notify={'stalled' in notify} stalled_line_in_transcript={stalled_seen}")
if mode == "stall":
    sys.exit(0 if armed and "stalled" in notify and stalled_seen else 1)
if mode == "quiet":
    sys.exit(0 if armed and "stalled" not in notify else 1)
sys.exit(3)
