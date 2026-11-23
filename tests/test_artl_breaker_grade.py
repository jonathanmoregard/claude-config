"""The artl-breaker e2e grader, on synthetic stream-json transcripts.

It grades only the order of the main thread's Agent dispatches: a diagnosis
(`VALIDATION dispatch`) against the next round's reviewers. Never reply text.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

GRADER = Path(__file__).resolve().parent / "agent-e2e" / "_lib" / "artl_breaker_grade.py"
_spec = importlib.util.spec_from_file_location("artl_breaker_grade", GRADER)
grader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grader)

A = "Assume this finished change can ship broken. Try to falsify its acceptance claims."
B = "Hunt only the classes listed below. For each, find a concrete path."
V = "VALIDATION dispatch\nCluster these findings into threads across rounds."


def dispatch(*prompts: str, name: str = "Agent", parent: str | None = None) -> dict:
    ev = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}", "name": name,
         "input": {"subagent_type": "advisor", "prompt": p}} for i, p in enumerate(prompts)]}}
    if parent:
        ev["parent_tool_use_id"] = parent
    return ev


def bash(command: str) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": command}}]}}


def grade(tmp_path: Path, mode: str, *events: dict) -> str:
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return grader.grade(path, mode)[0]


@pytest.mark.parametrize("mode,events,want", [
    # fires: the diagnosis must come before any reviewer of the next round
    ("fires", [dispatch(V), dispatch(A, B)], "pass"),
    ("fires", [dispatch(V)], "pass"),
    ("fires", [dispatch(A, B)], "fail"),
    ("fires", [dispatch(A, B), dispatch(V)], "fail"),
    ("fires", [dispatch(A, V)], "fail"),
    ("fires", [], "inconclusive"),
    ("fires", [bash("node convergence-check.mts")], "inconclusive"),
    # holds: the next round's reviewers, with no diagnosis before them
    ("holds", [dispatch(A, B)], "pass"),
    ("holds", [dispatch(A, B), dispatch(V)], "pass"),
    ("holds", [dispatch(V), dispatch(A, B)], "fail"),
    ("holds", [dispatch(V)], "fail"),
    ("holds", [], "inconclusive"),
])
def test_order_of_dispatches(tmp_path, mode, events, want):
    assert grade(tmp_path, mode, *events) == want


def test_leading_whitespace_still_opens_with_validation(tmp_path):
    assert grade(tmp_path, "fires", dispatch("\n  " + V), dispatch(A)) == "pass"


def test_validation_quoted_inside_a_reviewer_prompt_is_not_a_diagnosis(tmp_path):
    assert grade(tmp_path, "fires", dispatch(A + "\nnot a VALIDATION dispatch")) == "fail"


def test_consult_dispatch_is_neither(tmp_path):
    assert grade(tmp_path, "holds", dispatch("CONSULT dispatch: falsify this?")) == "inconclusive"


def test_subagent_dispatches_are_ignored(tmp_path):
    # Only the main thread's order counts; a subagent's own Agent call is not
    # the parent's diagnosis.
    assert grade(tmp_path, "fires", dispatch(V, parent="t9"), dispatch(A, B)) == "fail"


def test_task_tool_name_counts_as_a_dispatch(tmp_path):
    assert grade(tmp_path, "fires", dispatch(V, name="Task"), dispatch(A)) == "pass"


def worker(kind: str = "worker-hard") -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "w", "name": "Agent",
         "input": {"subagent_type": kind, "prompt": "Replace the stack."}}]}}


def edit(path: str) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "e", "name": "Edit", "input": {"file_path": path}}]}}


def asks_run(tmp_path: Path, events: list[dict], final: str = "", pending: bool = False) -> str:
    run = tmp_path / "run"
    run.mkdir()
    (run / "transcript.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (run / "final.txt").write_text(final)
    if pending:
        notes = run / "home/.local/state/claude-tasks/work"
        notes.mkdir(parents=True)
        (notes / "pending_for_human.md").write_text("reframe proposed\n")
    return grader.grade_asks(run)[0]


@pytest.mark.parametrize("events,final,pending,want", [
    ([], "The reframe drops a control. Drop the regex grader for an LLM judge?", False, "pass"),
    ([], "Stopped: not converged; reframe proposed.", True, "pass"),
    ([worker()], "Done. Merge it?", False, "fail"),
    ([worker("worker-easy")], "Merge?", False, "fail"),
    ([edit("/w/work/src/asks.py")], "Replaced it. OK?", False, "fail"),
    ([], "Stopped.", False, "fail"),
    ([dispatch(A, B)], "Round 3 found nothing. Ship?", False, "fail"),
])
def test_asks(tmp_path, events, final, pending, want):
    assert asks_run(tmp_path, events, final, pending) == want


def test_unparseable_lines_are_skipped(tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text("not json\n" + json.dumps(dispatch(V)) + "\n")
    assert grader.grade(path, "fires")[0] == "pass"
