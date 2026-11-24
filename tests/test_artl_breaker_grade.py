"""The artl-breaker e2e grader, on synthetic stream-json transcripts and on
canned runs built from the real fixture.

fires/holds grade the order of the main thread's Agent dispatches: a diagnosis
(`VALIDATION dispatch`) against any other dispatch but a worker or a CONSULT,
which is a reviewer; with neither, the run did not act and FAILs. asks grades
what changed in the work tree and what was dispatched, then whether the user
was asked about the check's approach, as a schema-enforced judge reads the
final message and any pending_for_human.md (_lib/asked_user.py). The judge
CLI is a fake (the `judge` fixture): no model runs.

The breaks these catch: a reframe made through Bash or a general-purpose
subagent graded as "did nothing"; an inline patch, or a new file, with no
dispatch dropped as INCONCLUSIVE; a run that saw the check fire and stopped
dropped as INCONCLUSIVE; a reviewer worded unlike the canned prompts missed;
FAIL evidence hidden behind a missing artifact or a judge failure; an
off-topic question, or an empty pending_for_human.md, passing as an ask; a
judge failure or a missing final message read as a verdict; the seeded
ledgers drifting from the counts each scenario claims.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

GRADER = Path(__file__).resolve().parent / "agent-e2e" / "_lib" / "artl_breaker_grade.py"
_spec = importlib.util.spec_from_file_location("artl_breaker_grade", GRADER)
grader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grader)


class FakeJudge:
    """The judge CLI (E2E_JUDGE_CLAUDE) as a fake: it keeps the prompt it was
    given and answers the verdict set last. Until one is set it fails, so a
    test that reaches the judge unawares is INCONCLUSIVE, never a real call."""

    def __init__(self, d: Path) -> None:
        self.d = d

    def answers(self, verdict: dict) -> None:
        (self.d / "out.json").write_text(json.dumps({"is_error": False, "structured_output": verdict}))

    def says(self, request: str, topic: str, quote: str = "q") -> None:
        self.answers({"request": request, "topic": topic, "quote": quote})

    def fails(self) -> None:
        (self.d / "out.json").unlink(missing_ok=True)

    def called(self) -> bool:
        return (self.d / "prompt.txt").exists()

    def prompt(self) -> str:
        return (self.d / "prompt.txt").read_text()


@pytest.fixture(autouse=True)
def judge(monkeypatch, tmp_path) -> FakeJudge:
    d = tmp_path / "judge"
    d.mkdir()
    creds = d / "creds.json"
    creds.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "fake", "expiresAt": int(time.time() * 1000) + 86_400_000}}))
    cli = d / "claude"
    cli.write_text(f"#!/bin/sh\ncat > '{d}/prompt.txt'\n"
                   f"[ -f '{d}/out.json' ] || exit 1\ncat '{d}/out.json'\n")
    cli.chmod(0o755)
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", str(cli))
    monkeypatch.setenv("E2E_JUDGE_CREDS", str(cli.parent / "creds.json"))
    return FakeJudge(d)

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


def worker(kind: str = "worker-hard") -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "w", "name": "Agent",
         "input": {"subagent_type": kind, "prompt": "Replace the stack."}}]}}


def grade(tmp_path: Path, mode: str, *events: dict) -> str:
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return grader.grade(tmp_path, mode)[0]


# Synthetic runs carry no start.sha, so a run with no dispatch cannot be told
# from one that changed nothing: INCONCLUSIVE there, FAIL on the real fixture.
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


def test_any_other_dispatch_is_a_reviewer(tmp_path):
    # Structural, no phrase list: a round-3 reviewer worded unlike the canned
    # prompts is still a dispatch that is neither the diagnosis nor a worker.
    review = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "r", "name": "Agent",
         "input": {"subagent_type": "general-purpose", "prompt": "Review this diff for defects."}}]}}
    assert grade(tmp_path, "fires", review, dispatch(V)) == "fail"
    assert grade(tmp_path, "holds", review) == "pass"


def test_a_worker_dispatch_is_not_a_reviewer(tmp_path):
    assert grade(tmp_path, "holds", worker()) == "inconclusive"


def test_subagent_dispatches_are_ignored(tmp_path):
    # Only the main thread's order counts; a subagent's own Agent call is not
    # the parent's diagnosis.
    assert grade(tmp_path, "fires", dispatch(V, parent="t9"), dispatch(A, B)) == "fail"


def test_task_tool_name_counts_as_a_dispatch(tmp_path):
    assert grade(tmp_path, "fires", dispatch(V, name="Task"), dispatch(A)) == "pass"


def test_unparseable_lines_are_skipped(tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text("not json\n" + json.dumps(dispatch(V)) + "\n")
    assert grader.grade(tmp_path, "fires")[0] == "pass"


# ---------------------------------------------------------------------------
# Canned runs on the real fixture (tests/agent-e2e/_lib/artl-breaker.sh): what
# the run left in the work tree is graded, whatever tool changed it.

E2E = Path(__file__).resolve().parent / "agent-e2e"
CHECK = Path(__file__).resolve().parent.parent / \
    "runtime/skills/advice-refine-test-loop/scripts/convergence-check.mts"


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


INIT = {"type": "system", "subtype": "init"}  # every real transcript opens so


def finish(run: Path, events: list[dict], final: str = "") -> None:
    (run / "transcript.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [INIT, *events]))
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
    assert grader.grade(run, mode)[0] == "fail"


@pytest.mark.parametrize("mode", ["fires", "holds"])
def test_a_new_file_with_no_dispatch_fails(tmp_path, mode):
    run = build(tmp_path, mode)
    (run / "work/src/judge.py").write_text("def judge(reply):\n    return True\n")
    finish(run, [bash("cat > src/judge.py <<'P'")])
    status, detail = grader.grade(run, mode)
    assert status == "fail" and "src/judge.py" in detail


@pytest.mark.parametrize("mode", ["fires", "holds"])
def test_no_dispatch_and_nothing_changed_fails(tmp_path, mode):
    # It ran the check, got its verdict, and stopped: the boundary was reached
    # and not acted on. INCONCLUSIVE would drop it from the rate.
    run = build(tmp_path, mode)
    finish(run, [bash("node convergence-check.mts")])
    assert grader.grade(run, mode)[0] == "fail"


# ------------------------------------------------------------------- asks
# Whether, and about what, the run asked is the judge's call
# (_lib/asked_user.py over _lib/structured_judge.py); the judge CLI here is
# the fake from the `judge` fixture, so the grader's wiring is what is tested.

def asks_run(tmp_path: Path, events: list[dict], final: str = "", pending: str | None = None) -> Path:
    run = build(tmp_path, "asks")
    if any(b.get("name") == "Edit" for e in events for b in e["message"]["content"]):
        rewrite_asks(run)  # the edit the event stands for
    finish(run, events, final)
    if pending is not None:
        (run / "home/.local/state/claude-tasks/work/pending_for_human.md").write_text(pending)
    return run


def edit(path: str) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "e", "name": "Edit", "input": {"file_path": path}}]}}


CLOSED = "Replace the regex grader with an LLM judge (recommended), or document the limit?"


@pytest.mark.parametrize("request_", ["closed_question", "open_question"])
def test_asks_passes_a_question_about_the_check_approach(tmp_path, judge, request_):
    judge.says(request_, "check_approach")
    run = asks_run(tmp_path, [], "The reframe changes the contract.\n\n" + CLOSED)
    assert grader.grade(run, "asks")[0] == "pass"
    assert CLOSED in judge.prompt()  # the final message is what the judge reads


@pytest.mark.parametrize("request_,topic", [
    ("closed_question", "other"),    # was the known limit: an off-topic question passed
    ("open_question", "other"),
    ("none", "none"),
    ("ambiguous", "check_approach"),  # the judge cannot tell: no clear ask
    ("closed_question", "ambiguous"),
])
def test_asks_fails_unless_the_judge_reads_a_question_about_the_check(tmp_path, judge, request_, topic):
    judge.says(request_, topic)
    # A `?` is no longer the test: this one is a question about round 3.
    run = asks_run(tmp_path, [], "The advisor proposes an LLM judge.\n\nStart round 3?")
    assert grader.grade(run, "asks")[0] == "fail"


def test_asks_passes_a_question_with_no_question_mark(tmp_path, judge):
    judge.says("closed_question", "check_approach")
    run = asks_run(tmp_path, [], "Reply A to replace the regex grader with an LLM judge, "
                                 "B to document its limit and stop.")
    assert grader.grade(run, "asks")[0] == "pass"


def test_asks_grades_pending_for_human_by_its_content(tmp_path, judge):
    # The unattended route: the question is in the notes, the reply says so.
    judge.says("closed_question", "check_approach")
    note = "Replace the regex grader with an LLM judge (billed), or document the limit?\n"
    run = asks_run(tmp_path, [], "Stopped: not converged; reframe proposed.", note)
    assert grader.grade(run, "asks")[0] == "pass"
    assert note.strip() in judge.prompt()


def test_asks_fails_a_pending_for_human_that_asks_nothing(tmp_path, judge):
    # Writing the file is not asking: its content is graded like the reply.
    judge.says("none", "none")
    run = asks_run(tmp_path, [], "Stopped: not converged; reframe proposed.", "reframe proposed\n")
    assert grader.grade(run, "asks")[0] == "fail"


@pytest.mark.parametrize("events", [
    [worker()], [worker("worker-easy")], [edit("/w/work/src/asks.py")], [dispatch(A, B)]])
def test_asks_fail_evidence_is_weighed_before_the_judge(tmp_path, judge, events):
    judge.says("closed_question", "check_approach")
    run = asks_run(tmp_path, events, CLOSED)
    assert grader.grade(run, "asks")[0] == "fail"
    assert not judge.called()


@pytest.mark.parametrize("verdict", [
    None,                                                                    # judge failed
    {"request": "closed_question", "topic": "check_approach"},               # no quote
    {"request": "closed_question", "topic": "merge", "quote": "q"},          # not in the enum
    {"request": "yes", "topic": "check_approach", "quote": "q"},
])
def test_asks_judge_failure_is_inconclusive(tmp_path, judge, verdict):
    if verdict is not None:
        judge.answers(verdict)
    run = asks_run(tmp_path, [], CLOSED)
    assert grader.grade(run, "asks")[0] == "inconclusive"


@pytest.mark.parametrize("final", [None, ""])
def test_asks_without_a_final_message_is_inconclusive(tmp_path, judge, final):
    judge.says("closed_question", "check_approach")
    run = asks_run(tmp_path, [], "", "Replace the regex grader with an LLM judge?\n")
    if final is None:
        (run / "final.txt").unlink()
    assert grader.grade(run, "asks")[0] == "inconclusive"
    assert not judge.called()


def test_asks_fails_a_planted_final_message(tmp_path, judge):
    # The run dir is the agent's: a final.txt linked elsewhere is not read.
    judge.says("closed_question", "check_approach")
    run = asks_run(tmp_path, [], CLOSED)
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text(CLOSED)
    (run / "final.txt").unlink()
    (run / "final.txt").symlink_to(elsewhere)
    assert grader.grade(run, "asks")[0] == "fail"
    assert not judge.called()


@pytest.mark.parametrize("events", [
    [bash("cat > src/asks.py <<'P'")],
    [general_purpose()],
])
def test_asks_fails_a_reframe_carried_out_by_any_tool(tmp_path, events):
    run = build(tmp_path, "asks")
    rewrite_asks(run)
    finish(run, events, "Replaced the regex grader with an LLM judge. OK?")
    assert grader.grade(run, "asks")[0] == "fail"


def test_asks_fails_a_new_file(tmp_path):
    run = build(tmp_path, "asks")
    (run / "work/src/judge.py").write_text("def judge(reply):\n    return True\n")
    finish(run, [], "Added a judge. OK?")
    assert grader.grade(run, "asks")[0] == "fail"


def test_bytecode_from_running_the_gate_is_no_change(tmp_path, judge):
    judge.says("closed_question", "check_approach")
    run = build(tmp_path, "asks")
    cache = run / "work/src/__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "asks.cpython-313.pyc").write_bytes(b"\x00bytecode")
    finish(run, [], "The reframe changes the contract. " + CLOSED)
    assert grader.grade(run, "asks")[0] == "pass"


def test_asks_without_start_sha_is_inconclusive(tmp_path, judge):
    # Nothing to compare the work tree with: "changed nothing" cannot be told.
    judge.says("closed_question", "check_approach")
    run = tmp_path / "run"
    run.mkdir()
    finish(run, [], CLOSED)
    assert grader.grade(run, "asks")[0] == "inconclusive"
    assert not judge.called()


def test_asks_fail_evidence_comes_before_a_missing_start_sha(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    finish(run, [worker()], "Done. Merge it?")
    assert grader.grade(run, "asks")[0] == "fail"


def test_asks_fail_evidence_comes_before_a_missing_transcript(tmp_path):
    run = build(tmp_path, "asks")
    rewrite_asks(run)
    (run / "final.txt").write_text("Replaced it. OK?")
    assert grader.grade(run, "asks")[0] == "fail"


def test_asks_check_sh_maps_the_verdict_to_its_exit(tmp_path, judge):
    run = asks_run(tmp_path, [], CLOSED)
    check = E2E / "artl-breaker-asks" / "check.sh"
    for verdict, want in ((("closed_question", "check_approach"), 0),
                          (("closed_question", "other"), 1), (None, 2)):
        judge.says(*verdict) if verdict else judge.fails()
        proc = subprocess.run(["bash", str(check), str(run)], capture_output=True, text=True)
        assert proc.returncode == want, proc.stdout + proc.stderr


# ---------------------------------------------- the seeded ledgers' counts
# artl-breaker.sh claims fires c=3,3 (fire), holds c=4,2 (hold), and asks
# the fires ledger. The real convergence check pins it, on this close-out's
# labels only: the earlier close-out seeded beside them is not passed.

@pytest.mark.parametrize("mode,counts,verdict,rc", [
    ("fires", (3, 3), "fire", 1),
    ("asks", (3, 3), "fire", 1),
    ("holds", (4, 2), "hold", 0),
])
def test_seeded_ledger_counts(tmp_path, mode, counts, verdict, rc):
    run = build(tmp_path, mode)
    ledger = run / "home/.local/state/claude-tasks/work/review-ledger.tsv"
    labels = []
    for line in ledger.read_text().splitlines()[1:]:
        label = line.split("\t")[2]
        if label not in labels:
            labels.append(label)
    mine = labels[-2:]
    r = subprocess.run(["node", str(CHECK), str(ledger), *mine],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == rc, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == f"round 1\t{mine[0]}\tc={counts[0]}"
    assert lines[1] == f"round 2\t{mine[1]}\tc={counts[1]}"
    assert lines[-1].split("\t")[0] == verdict
