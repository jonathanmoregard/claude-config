"""The idle-alarm e2e grader, on synthetic stream-json runs.

Each case pins a false PASS or false FAIL a review found in an earlier,
text-matching version of the grader.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

GRADER = Path(__file__).resolve().parent / "agent-e2e" / "_lib" / "idle_alarm_grade.py"
_spec = importlib.util.spec_from_file_location("idle_alarm_grade", GRADER)
grader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grader)

AGENT = "a6798b73e325e6a27"


def sys_event(subtype: str, **kw: object) -> dict:
    return {"type": "system", "subtype": subtype, **kw}


def bash(tool_id: str, command: str) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": "Bash",
         "input": {"command": command, "run_in_background": True}}]}}


def say(text: str = "ok") -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def launched() -> list[dict]:
    return [sys_event("task_started", task_id=AGENT, task_type="local_agent",
                      is_backgrounded=True, tool_use_id="t0")]


def armed(task: str = "b1", tool: str = "t1",
          command: str = f"~/.claude/scripts/wait-for agent {AGENT}") -> list[dict]:
    return [bash(tool, command),
            sys_event("task_started", task_id=task, task_type="local_bash", tool_use_id=tool)]


def exited(task: str, code: int, tool: str = "t1", desc: str = "wait") -> dict:
    return sys_event("task_notification", task_id=task, tool_use_id=tool, status="failed",
                     summary=f'Background command "{desc}" failed with exit code {code}')


def agent_done() -> list[dict]:
    return [sys_event("task_updated", task_id=AGENT, patch={"status": "completed"}),
            sys_event("task_notification", task_id=AGENT, tool_use_id="t0", status="completed",
                      summary="done")]


def killed(task: str = "b1", desc: str = "Arm idle alarm") -> list[dict]:
    """A TaskStop: the task is killed and its notification's summary is the
    model-written Bash description, verbatim."""
    return [sys_event("task_updated", task_id=task, patch={"status": "killed"}),
            sys_event("task_notification", task_id=task, status="stopped", summary=desc)]


def time_limit(task: str = "b1") -> list[dict]:
    return [sys_event("task_updated", task_id=task, patch={"status": "killed"}),
            sys_event("task_notification", task_id=task, status="stopped",
                      summary='Background command "wait" was stopped after reaching its '
                              "background time limit")]


def grade(tmp: Path, events: list[dict], mode: str, *, alarm: bool = False) -> int:
    (tmp / "transcript.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    if alarm:
        (tmp / "notify.log").write_text(f"agent {AGENT} stalled: no activity\n")
    return grader.grade(tmp, mode)[0]


def test_stall_pass(tmp_path: Path) -> None:
    events = launched() + armed() + [exited("b1", 5), say("the idle alarm went off")]
    assert grade(tmp_path, events, "stall", alarm=True) == 0


def test_stall_needs_a_parent_turn_after_the_exit(tmp_path: Path) -> None:
    events = launched() + armed() + [exited("b1", 5)]
    assert grade(tmp_path, events, "stall", alarm=True) == 1


def test_reply_text_about_exit_5_is_not_the_alarm(tmp_path: Path) -> None:
    events = launched() + armed() + [say("it will exit 5 if quiet; the alarm fired before")]
    assert grade(tmp_path, events, "stall", alarm=True) == 1


def test_arming_variants_count(tmp_path: Path) -> None:
    for n, command in enumerate([
            f"WAIT_FOR_AGENT_IDLE=600 ~/.claude/scripts/wait-for agent {AGENT}",
            f'"$HOME/.claude/scripts/wait-for" agent "{AGENT}" --idle 900',
            f"timeout 2h bash ~/.claude/scripts/wait-for agent {AGENT}"]):
        d = tmp_path / str(n)
        d.mkdir()
        events = launched() + armed(command=command) + agent_done() + killed()
        assert grade(d, events, "quiet") == 0, command


def test_foreground_pgrep_is_not_arming(tmp_path: Path) -> None:
    events = launched() + [bash("t9", f'pgrep -f "wait-for agent {AGENT}"')]
    assert grade(tmp_path, events, "quiet") == 1


def test_subagent_waiter_is_not_the_parents(tmp_path: Path) -> None:
    events = launched() + [
        dict(bash("t1", f"~/.claude/scripts/wait-for agent {AGENT}"), parent_tool_use_id="t0"),
        sys_event("task_started", task_id="b1", task_type="local_bash", tool_use_id="t1",
                  owned_by_subagent=True)]
    assert grade(tmp_path, events, "quiet") == 1


def test_quiet_pass_needs_the_waiter_itself_killed(tmp_path: Path) -> None:
    other = [bash("t5", "./feed-watch.sh"),
             sys_event("task_started", task_id="b5", task_type="local_bash", tool_use_id="t5")]
    assert grade(tmp_path, launched() + armed() + other + agent_done() + killed("b5"),
                 "quiet") == 2
    assert grade(tmp_path, launched() + armed() + agent_done() + killed("b1"), "quiet") == 0


def test_quiet_stop_before_the_result_is_not_a_pass(tmp_path: Path) -> None:
    assert grade(tmp_path, launched() + armed() + killed() + agent_done(), "quiet") == 2


def test_quiet_rearmed_waiter_left_running_is_not_a_pass(tmp_path: Path) -> None:
    events = (launched() + armed() + killed()
              + armed(task="b2", tool="t2", command=f"~/.claude/scripts/wait-for agent {AGENT} --idle 900")
              + agent_done())
    assert grade(tmp_path, events, "quiet") == 2


def test_description_text_cannot_fake_a_system_stop(tmp_path: Path) -> None:
    events = launched() + armed() + agent_done() + killed(desc="Stall watcher (2h time limit)")
    assert grade(tmp_path, events, "quiet") == 0


def test_description_text_cannot_fake_exit_5(tmp_path: Path) -> None:
    events = launched() + armed() + [exited("b1", 3, desc="Wait (exit code 5 = stalled)"), say()]
    assert grade(tmp_path, events, "stall", alarm=True) == 1


def test_system_stop_is_not_the_parent_stopping_it(tmp_path: Path) -> None:
    events = launched() + armed() + agent_done() + time_limit()
    assert grade(tmp_path, events, "quiet") == 2


def test_stall_cut_short_by_the_headless_time_limit_is_inconclusive(tmp_path: Path) -> None:
    assert grade(tmp_path, launched() + armed() + time_limit(), "stall") == 2


def test_waiter_that_died_at_once_fails_quiet(tmp_path: Path) -> None:
    events = launched() + armed() + [exited("b1", 64)] + agent_done() + killed()
    assert grade(tmp_path, events, "quiet") == 1


def test_missing_transcript_is_inconclusive(tmp_path: Path) -> None:
    assert grader.grade(tmp_path, "stall")[0] == 2
