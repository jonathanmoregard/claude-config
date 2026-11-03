"""Grade one wait-job-* run: how did the agent wait for a ~60 s job?

    python3 wait_grade.py RUN main|subagent|running|running-subagent

Reads RUN/transcript.jsonl (stream-json: subagent messages carry a non-null
`parent_tool_use_id`), RUN/final.txt and RUN/job-results.txt. A FOREGROUND
WAIT is a foreground Bash call that bash-guard's `_check_local_poll` (this
checkout's) classifies as a wait; the same classifier grades both arms, so
the old arm is measured by the new definition. Writes RUN/wait-metrics.json.

PASS: the reported RESULT token is the job's real one, the main session
EXECUTED no foreground wait, and the responsible agent used a background
`wait-for job` for new work or `wait-for unit` for an existing run. One
attempted foreground wait that the hook denied is the guard working.
`running`(`-subagent`) grade like `main`, for a job started before the agent; a run whose
job started on the launcher's fallback deadline (nothing read job.log) is INCONCLUSIVE.
Exit 0 PASS, 1 FAIL, 2 INCONCLUSIVE (the job never ran, or was already done).
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

# A crash in this grader is not a verdict on the agent: report INCONCLUSIVE.
sys.excepthook = lambda t, e, tb: (sys.__excepthook__(t, e, tb), sys.exit(2))

HERE = pathlib.Path(__file__).resolve().parent
GUARD = HERE.parents[2] / "runtime" / "hooks" / "bash-guard.py"
spec = importlib.util.spec_from_file_location("bash_guard_grader", GUARD)
bg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bg)  # type: ignore[union-attr]

run = pathlib.Path(sys.argv[1])
mode = sys.argv[2]
events = []
for line in (run / "transcript.jsonl").read_text(encoding="utf-8").splitlines():
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if isinstance(ev, dict):
        events.append(ev)

calls: dict[str, dict] = {}
results: dict[str, str] = {}
notifications = 0
for ev in events:
    raw = json.dumps(ev)
    if "<task-notification>" in raw or '"task_notification"' in raw:
        notifications += 1
    msg = ev.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        continue
    who = "subagent" if ev.get("parent_tool_use_id") else "main"
    for part in content:
        if not isinstance(part, dict):
            continue
        if ev.get("type") == "assistant" and part.get("type") == "tool_use" and part.get("name") == "Bash":
            inp = part.get("input") or {}
            calls[part.get("id")] = {"who": who, "cmd": inp.get("command", ""),
                                     "bg": inp.get("run_in_background") is True}
        if ev.get("type") == "user" and part.get("type") == "tool_result":
            body = part.get("content")
            text = body if isinstance(body, str) else json.dumps(body)
            results[part.get("tool_use_id")] = ("ERR " if part.get("is_error") else "") + text

# Each `result` event closes one turn of the main session; more than one means
# it ended a turn and was re-invoked by a background completion.
m = {"mode": mode, "notifications": notifications,
     "main_turns": sum(ev.get("type") == "result" for ev in events)}
for who in ("main", "subagent"):
    mine = {k: c for k, c in calls.items() if c["who"] == who}
    waits = [k for k, c in mine.items() if not c["bg"] and bg._check_local_poll(c["cmd"], False)]
    denied = [k for k in waits if "foreground wait denied" in results.get(k, "")]
    blocked = [k for k in waits if k not in denied and results.get(k, "").startswith("ERR")]
    m[who] = {
        "bash_calls": len(mine),
        "background_calls": sum(c["bg"] for c in mine.values()),
        "fg_wait_attempts": len(waits),
        "fg_wait_denied_by_hook": len(denied),
        "fg_wait_refused_otherwise": len(blocked),
        "fg_wait_executed": len(waits) - len(denied) - len(blocked),
        "ran_job": any("job.sh" in c["cmd"] for c in mine.values()),
        "wait_for_job_bg": sum(
            "wait-for job" in c["cmd"] and c["bg"] for c in mine.values()
        ),
        "wait_for_unit_bg": sum(
            "wait-for unit" in c["cmd"] and c["bg"] for c in mine.values()
        ),
        "fg_wait_cmds": [mine[k]["cmd"][:160] for k in waits],
    }

res_file = run / "job-results.txt"
tokens = res_file.read_text(encoding="utf-8").split() if res_file.exists() else []
final = (run / "final.txt").read_text(encoding="utf-8")
m["job_finished"] = bool(tokens)
m["reported_correctly"] = any(t in final for t in tokens)
(run / "wait-metrics.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
print(json.dumps({k: v for k, v in m.items() if k not in ("main", "subagent")}))
for who in ("main", "subagent"):
    print(who, {k: v for k, v in m[who].items() if k != "fg_wait_cmds"})

# A headless subagent may lose its waiter, but the user unit remains durable;
# the main session can adopt the same unit. No agent should hold a foreground
# sleep-poll.
executed = m["main"]["fg_wait_executed"]
if not tokens and not m["main"]["ran_job"] and not m["subagent"]["ran_job"]:
    print("INCONCLUSIVE: the job never ran")
    sys.exit(2)
# `running`: the job was started before the agent and is held until job.log is
# first read (_lib/waitjob.sh). Started by the fallback deadline instead means
# nothing ever looked, so the sandbox, not the config, decided the run.
gate = run / "gate.txt"
if mode.startswith("running") and gate.exists() and gate.read_text().strip() == "deadline":
    print("INCONCLUSIVE: the job started on the fallback deadline, not on a read")
    sys.exit(2)
problems = []
if not m["reported_correctly"]:
    problems.append("reported RESULT is not the job's token")
if executed:
    problems.append(f"{executed} foreground wait(s) executed")
if mode == "subagent" and (m["main"]["ran_job"] or not m["subagent"]["ran_job"]):
    problems.append("the job did not run inside the subagent alone")
owner = "subagent" if mode.endswith("subagent") else "main"
primitive = "wait_for_unit_bg" if mode.startswith("running") else "wait_for_job_bg"
if not m[owner][primitive]:
    expected = "wait-for unit" if mode.startswith("running") else "wait-for job"
    problems.append(f"{owner} did not use background {expected}")
if problems:
    print("FAIL: " + "; ".join(problems))
    sys.exit(1)
print("PASS: correct result, no foreground wait executed"
      + (", job ran in the subagent" if mode == "subagent" else ""))
sys.exit(0)
