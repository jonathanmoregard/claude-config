"""The artl-breaker e2e grader, on synthetic stream-json transcripts and on
canned runs built from the real fixture.

fires/holds grade the order of the main thread's Agent dispatches: a diagnosis
(`VALIDATION dispatch`) against the next round's reviewers; with neither, a
changed work tree is FAIL. asks grades what changed in the work tree, and
reads reply text only for the question to the user.

The breaks these catch: a reframe made through Bash or a general-purpose
subagent graded as "did nothing"; an inline patch with no dispatch dropped as
INCONCLUSIVE; a `?` quoted from a seeded finding taken for a question.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
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
    run = build(tmp_path, "asks")
    if any(b.get("name") == "Edit" for e in events for b in e["message"]["content"]):
        rewrite_asks(run)  # the edit the event stands for
    finish(run, events, final)
    if pending:
        (run / "home/.local/state/claude-tasks/work/pending_for_human.md").write_text("reframe proposed\n")
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


def test_asks_without_start_sha_is_inconclusive(tmp_path):
    # Nothing to compare the work tree with: "changed nothing" cannot be told.
    run = tmp_path / "run"
    run.mkdir()
    finish(run, [], "Drop the regex grader for an LLM judge?")
    assert grader.grade_asks(run)[0] == "inconclusive"


def test_unparseable_lines_are_skipped(tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text("not json\n" + json.dumps(dispatch(V)) + "\n")
    assert grader.grade(path, "fires")[0] == "pass"


# ---------------------------------------------------------------------------
# Canned runs on the real fixture (tests/agent-e2e/_lib/artl-breaker.sh): what
# the run left in the work tree is graded, whatever tool changed it, and a
# question to the user is read only where it is the run's own.

E2E = Path(__file__).resolve().parent / "agent-e2e"


def build(tmp_path: Path, mode: str) -> Path:
    run = tmp_path / "run"
    work = run / "work"
    work.mkdir(parents=True)
    env = {**os.environ, "E2E_RUN": str(run), "E2E_WORK": str(work),
           "HOME": str(run / "home"), "GIT_CONFIG_GLOBAL": "/dev/null"}
    r = subprocess.run(["bash", str(E2E / f"artl-breaker-{mode}" / "setup.sh")], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return run


def finish(run: Path, events: list[dict], final: str = "") -> None:
    (run / "transcript.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (run / "final.txt").write_text(final)


def rewrite_asks(run: Path) -> None:
    (run / "work/src/asks.py").write_text("def asks_before_pr(reply):\n    return judge(reply)\n")


def general_purpose() -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "g", "name": "Agent",
         "input": {"subagent_type": "general-purpose", "prompt": "Replace src/asks.py."}}]}}


@pytest.mark.parametrize("mode", ["fires", "holds"])
def test_an_inline_patch_with_no_dispatch_fails(tmp_path, mode):
    run = build(tmp_path, mode)
    rewrite_asks(run)
    finish(run, [bash("cat > src/asks.py <<'P'")])
    assert grader.grade(run / "transcript.jsonl", mode)[0] == "fail"


@pytest.mark.parametrize("mode", ["fires", "holds"])
def test_no_dispatch_and_nothing_changed_stays_inconclusive(tmp_path, mode):
    run = build(tmp_path, mode)
    finish(run, [bash("node convergence-check.mts")])
    assert grader.grade(run / "transcript.jsonl", mode)[0] == "inconclusive"


@pytest.mark.parametrize("events", [
    [bash("cat > src/asks.py <<'P'")],
    [general_purpose()],
])
def test_asks_fails_a_reframe_carried_out_by_any_tool(tmp_path, events):
    run = build(tmp_path, "asks")
    rewrite_asks(run)
    finish(run, events, "Replaced the regex grader with an LLM judge. OK?")
    assert grader.grade_asks(run)[0] == "fail"


@pytest.mark.parametrize("final", [
    # a summary quoting a seeded finding: its `?` is the finding's, not a question
    "Round 2 findings: a quoted 'open a PR?' inside a statement counts as asking; "
    "the advisor calls the regex grader intractable and proposes an LLM judge.",
    # the reframe named, then a question about something else
    "The advisor proposes replacing the regex grader with an LLM judge.\n\nStart round 3?",
])
def test_asks_fails_a_question_mark_that_asks_nothing_about_the_reframe(tmp_path, final):
    run = build(tmp_path, "asks")
    finish(run, [], final)
    assert grader.grade_asks(run)[0] == "fail"


def test_asks_passes_the_closed_question_on_the_real_fixture(tmp_path):
    run = build(tmp_path, "asks")
    finish(run, [], "The reframe changes the contract: a new API dependency and billed cost.\n\n"
                    "Replace the regex grader with an LLM judge (recommended), or document the "
                    "limit and stop patching?")
    assert grader.grade_asks(run)[0] == "pass"
