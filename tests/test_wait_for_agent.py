"""`wait-for agent` and the hook that asks the parent to arm it.

A synthetic subagent transcript under a temporary HOME stands in for a real
background agent; `systemctl`, `notify-send` and `sleep` are stubs, so every
case finishes in well under a second.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNTIME = REPO / "runtime" if (REPO / "runtime").is_dir() else REPO
WAIT_FOR = RUNTIME / "scripts" / "wait-for"
HOOK = RUNTIME / "hooks" / "agent-idle-alarm.py"

SYSTEMCTL = """#!/usr/bin/env bash
# Answers `systemctl --user show <unit> ...` from $STUB_DIR/units/<unit>:
# the file holds the number of further polls the unit stays running.
unit=""
for a in "$@"; do case $a in --user|show|-p|ActiveState|SubState) ;; *) unit=$a ;; esac; done
f="$STUB_DIR/units/$unit"
echo "$unit" >> "$STUB_DIR/systemctl.log"
if [ -f "$f" ]; then
  n=$(cat "$f")
  if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$f"; printf 'ActiveState=active\\nSubState=running\\n'; exit 0; fi
  printf 'ActiveState=active\\nSubState=exited\\n'; exit 0
fi
printf 'ActiveState=inactive\\nSubState=dead\\n'
"""


def make_env(tmp: Path) -> dict[str, str]:
    stub = tmp / "stub"
    (stub / "bin").mkdir(parents=True)
    (stub / "units").mkdir()
    for name, body in {
        "systemctl": SYSTEMCTL,
        "notify-send": '#!/bin/sh\necho "$*" >> "$STUB_DIR/notify.log"\n',
        "sleep": "#!/bin/sh\nexit 0\n",
    }.items():
        p = stub / "bin" / name
        p.write_text(body)
        p.chmod(0o755)
    return {
        **os.environ,
        "HOME": str(tmp / "home"),
        "STUB_DIR": str(stub),
        "PATH": f"{stub / 'bin'}:{os.environ['PATH']}",
    }


def write_agent(tmp: Path, agent: str, tool_uses: list[dict], *, finished: bool,
                age: int = 600) -> Path:
    d = tmp / "home" / ".claude" / "projects" / "p" / "sess" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    t = d / f"agent-{agent}.jsonl"
    lines = [json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", **u}]}}) for u in tool_uses]
    t.write_text("\n".join(lines) + '\n{"partial')
    m = d / f"agent-{agent}.meta.json"
    meta = {"agentType": "worker-hard"}
    if finished:
        meta["finished"] = True
    m.write_text(json.dumps(meta))
    old = time.time() - age
    for f in (t, m):
        os.utime(f, (old, old))
    return t


def run(env: dict[str, str], *args: str) -> tuple[int, str]:
    p = subprocess.run([str(WAIT_FOR), "agent", *args], env=env,
                       capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout


def bash(cmd: str, bg: bool = False) -> dict:
    return {"name": "Bash", "input": {"command": cmd, "run_in_background": bg}}


def notified(env: dict[str, str]) -> str:
    log = Path(env["STUB_DIR"]) / "notify.log"
    return log.read_text() if log.exists() else ""


def test_finished_agent_exits_0_without_notification(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "aaa1", [bash("sleep infinity", bg=True)], finished=True)
    rc, out = run(env, "aaa1", "--idle", "1")
    assert rc == 0 and "finished" in out
    assert notified(env) == ""


def test_quiet_agent_with_no_unit_is_stalled(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "bbb2", [bash("ls"), bash('until ! pgrep -f "x"; do sleep 5; done', bg=True),
                                   bash("cat log")], finished=False)
    rc, out = run(env, "bbb2", "--idle", "60")
    assert rc == 5, out
    assert out.startswith("agent bbb2 stalled: no activity for 10m")
    assert 'last: background `until ! pgrep -f "x"; do sleep 5; done`' in out
    assert "bbb2 stalled" in notified(env)


def test_recent_activity_is_not_a_stall(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "ccc3", [bash("sleep infinity", bg=True)], finished=False, age=0)
    rc, out = run(env, "ccc3", "--idle", "7200", "--timeout", "2")
    assert rc == 124, out
    assert notified(env) == ""


def test_running_unit_holds_off_the_alarm_until_it_ends(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "ddd4", [
        bash("systemd-run --user --unit=build-x -- make"),
        bash("~/.claude/scripts/wait-for unit build-x --user", bg=True),
    ], finished=False)
    (Path(env["STUB_DIR"]) / "units" / "build-x").write_text("3")
    # A unit seen running counts as activity, so it is looked at again only
    # after another full idle period: one poll, no stall within the timeout.
    rc, out = run(env, "ddd4", "--idle", "60", "--timeout", "2")
    assert rc == 124, out
    polls = (Path(env["STUB_DIR"]) / "systemctl.log").read_text().split()
    assert polls.count("build-x") == 1
    assert notified(env) == ""


def test_stall_after_a_unit_ends_counts_from_its_end(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "ddd5", [bash("systemd-run --user --unit=build-y -- make")], finished=False)
    (Path(env["STUB_DIR"]) / "units" / "build-y").write_text("1")
    started = time.time()
    rc, out = run(env, "ddd5", "--idle", "2", "--timeout", "20")
    assert rc == 5, out
    assert time.time() - started >= 2  # a full idle period after the unit ended


def test_units_from_every_launcher_are_checked(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "eee5", [
        bash("systemd-run --user --unit 'quoted-u' true"),
        bash("~/.claude/scripts/wait-for job my-job -- ./run.sh", bg=True),
        {"name": "mcp__jobs_broker__start", "input": {"slug": "nixbuild"}},
    ], finished=False)
    rc, _ = run(env, "eee5", "--idle", "60")
    assert rc == 5
    polled = set((Path(env["STUB_DIR"]) / "systemctl.log").read_text().split())
    assert polled == {"quoted-u", "my-job", "claude-job-nixbuild"}


def bg_launch(transcript: Path, tool_id: str, task: str, cmd: str, *, done: bool,
              age: int = 600) -> None:
    """Append a background Bash launch, its launch result and maybe its completion."""
    rows = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tool_id,
         "name": "Bash", "input": {"command": cmd, "run_in_background": True}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id,
         "content": f"Command running in background with ID: {task}. Output is being written to: /tmp/x/{task}.output"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "I'll wait for that notification."}],
         "stop_reason": "end_turn"}},
    ]
    if done:
        rows.append({"type": "user", "message": {"content":
                     f"<task-notification>\n<task-id>{task}</task-id>\n<status>completed</status>"}})
    body = transcript.read_text().removesuffix('{"partial')
    transcript.write_text(body + "".join(json.dumps(r) + "\n" for r in rows))
    old = time.time() - age
    os.utime(transcript, (old, old))


def test_turn_ended_on_a_pending_background_command_is_a_stall(tmp_path: Path) -> None:
    # The 2026-11-22 shape: meta.json says finished, but the wait loop the
    # worker launched in the background never reported back.
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "ggg7", [bash("cargo build")], finished=True)
    bg_launch(t, "toolu_1", "by457htew", 'until ! pgrep -f "cargo build" >/dev/null; do sleep 2; done',
              done=False)
    rc, out = run(env, "ggg7", "--idle", "60")
    assert rc == 5, out
    assert 'last: pending background `until ! pgrep -f "cargo build"' in out
    assert "ggg7 stalled" in notified(env)


def test_pending_background_command_that_is_recent_is_not_a_stall(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "hhh8", [], finished=True)
    bg_launch(t, "toolu_2", "bq1", "./long.sh", done=False, age=0)
    rc, _ = run(env, "hhh8", "--idle", "7200", "--timeout", "2")
    assert rc == 124
    assert notified(env) == ""


def test_finished_once_every_background_command_reported(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "iii9", [], finished=True)
    bg_launch(t, "toolu_3", "bq2", "./a.sh", done=True)
    bg_launch(t, "toolu_4", "bq3", "./b.sh", done=True)
    rc, out = run(env, "iii9", "--idle", "60")
    assert rc == 0 and "finished" in out


def test_background_command_the_agent_stopped_is_not_pending(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "jjj10", [], finished=True)
    bg_launch(t, "toolu_5", "bq4", "sleep infinity", done=False)
    body = t.read_text()
    t.write_text(body + json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_6", "name": "TaskStop", "input": {"task_id": "bq4"}}]}}) + "\n")
    rc, out = run(env, "jjj10", "--idle", "60")
    assert rc == 0, out


def test_unknown_agent_and_bad_id(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rc, out = run(env, "fff6", "--idle", "1")
    assert rc == 3 and "no transcript" in out
    rc, _ = run(env, "a;b")
    assert rc == 64


def test_idle_is_agent_only(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    p = subprocess.run([str(WAIT_FOR), "unit", "x", "--idle", "5"], env=env,
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 64


def hook(payload: object) -> str:
    p = subprocess.run(["python3", "-S", "-E", str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 0
    return p.stdout


def test_hook_names_the_wait_for_background_dispatch() -> None:
    out = json.loads(hook({"tool_name": "Agent", "tool_response": {
        "status": "async_launched", "agentId": "a0eb7360ac337731d"}}))
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PostToolUse"
    assert "`~/.claude/scripts/wait-for agent a0eb7360ac337731d`" in spec["additionalContext"]


def test_hook_silent_otherwise() -> None:
    launched = {"status": "async_launched", "agentId": "abc"}
    assert hook({"tool_name": "Agent", "tool_response": {"status": "completed", "agentId": "abc"}}) == ""
    assert hook({"tool_name": "Agent", "agent_id": "sub", "tool_response": launched}) == ""
    assert hook({"tool_name": "Bash", "tool_response": launched}) == ""
    assert hook({"tool_name": "Agent", "tool_response": {**launched, "agentId": "a b;c"}}) == ""
    p = subprocess.run(["python3", "-S", "-E", str(HOOK)], input="not json",
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 0 and p.stdout == ""
