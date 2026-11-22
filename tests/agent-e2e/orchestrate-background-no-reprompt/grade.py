"""Grade one orchestrate-background-no-reprompt run.

    python3 grade.py RUN

Reads RUN/transcript.jsonl (stream-json), RUN/final.txt, RUN/integration.log
and RUN/expected-result.txt. Writes RUN/reprompt-metrics.json.

  reprompts  events carrying the Ralph Stop hook's block message
             ("Ralph iteration N"); the hook is the only emitter of that text
  armed      main-session Bash commands that ran ralph-loop-setup.sh
  bg_launch  main-session Agent/Task calls with run_in_background: true
  result_ok  the final message reports this run's integration token

PASS: a background subagent was launched, Ralph never re-prompted or armed,
and the final message carries the token (work still continues to completion
on the subagent's notification). FAIL: any re-prompt or arming, or the token
missing. INCONCLUSIVE (2): the orchestrate skill text never loaded, no
background subagent was launched, or the integration script never started, so
the situation under test was not reached.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.excepthook = lambda t, e, tb: (sys.__excepthook__(t, e, tb), sys.exit(2))

run = pathlib.Path(sys.argv[1])
events = []
for line in (run / "transcript.jsonl").read_text(encoding="utf-8").splitlines():
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if isinstance(ev, dict):
        events.append(ev)

reprompts = 0
armed = 0
bg_launch = 0
notifications = 0
orchestrate_loaded = False
for ev in events:
    raw = json.dumps(ev, ensure_ascii=False)
    if "Ralph iteration" in raw:
        reprompts += 1
    if "# Autonomous Orchestration" in raw:
        orchestrate_loaded = True
    if "<task-notification>" in raw or '"task_notification"' in raw:
        notifications += 1
    if ev.get("parent_tool_use_id"):
        continue
    msg = ev.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        continue
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "tool_use":
            continue
        inp = part.get("input") or {}
        if part.get("name") in ("Agent", "Task") and inp.get("run_in_background") is True:
            bg_launch += 1
        if part.get("name") == "Bash" and "ralph-loop-setup" in str(inp.get("command", "")):
            armed += 1

token = (run / "expected-result.txt").read_text().strip()
final = (run / "final.txt").read_text(encoding="utf-8") if (run / "final.txt").exists() else ""
result_ok = bool(token) and token in final
log = (run / "integration.log").read_text() if (run / "integration.log").exists() else ""
started = "start" in log

metrics = {
    "orchestrate_loaded": orchestrate_loaded,
    "reprompts": reprompts,
    "armed": armed,
    "bg_launch": bg_launch,
    "notifications": notifications,
    "integration_started": started,
    "integration_done": "done" in log,
    "result_ok": result_ok,
}
(run / "reprompt-metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
summary = " ".join(f"{k}={v}" for k, v in metrics.items())

if not orchestrate_loaded:
    print(f"INCONCLUSIVE: the orchestrate skill never loaded ({summary})")
    sys.exit(2)
if bg_launch == 0 or not started:
    print(f"INCONCLUSIVE: no background subagent ran the check ({summary})")
    sys.exit(2)
if reprompts or armed:
    print(f"FAIL: Ralph re-prompted or armed ({summary})")
    sys.exit(1)
if not result_ok:
    print(f"FAIL: final message lacks the integration RESULT ({summary})")
    sys.exit(1)
print(f"PASS: no Ralph re-prompt; background result reported ({summary})")
sys.exit(0)
