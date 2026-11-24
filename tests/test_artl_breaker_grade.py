"""The artl-breaker e2e grader, on synthetic stream-json transcripts and on
canned runs built from the real fixture.

fires/holds grade the order of the main thread's Agent dispatches: a diagnosis
(`VALIDATION dispatch`) against any other dispatch but a worker or a CONSULT,
which is a reviewer; with neither, the run did not act and FAILs. asks grades
what changed in the work tree and what was dispatched, then whether the user
was asked at all (_lib/asked_user.py).

The breaks these catch: a reframe made through Bash or a general-purpose
subagent graded as "did nothing"; an inline patch, or a new file, with no
dispatch dropped as INCONCLUSIVE; a run that saw the check fire and stopped
dropped as INCONCLUSIVE; a reviewer worded unlike the canned prompts missed;
FAIL evidence hidden behind a missing artifact; the seeded ledgers drifting
from the counts each scenario claims.
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

def asks_run(tmp_path: Path, events: list[dict], final: str = "", pending: bool = False) -> str:
    run = build(tmp_path, "asks")
    if any(b.get("name") == "Edit" for e in events for b in e["message"]["content"]):
        rewrite_asks(run)  # the edit the event stands for
    finish(run, events, final)
    if pending:
        (run / "home/.local/state/claude-tasks/work/pending_for_human.md").write_text("reframe proposed\n")
    return grader.grade(run, "asks")[0]


def edit(path: str) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "e", "name": "Edit", "input": {"file_path": path}}]}}


@pytest.mark.parametrize("events,final,pending,want", [
    ([], "The reframe drops a control. Drop the regex grader for an LLM judge?", False, "pass"),
    ([], "Stopped: not converged; reframe proposed.", True, "pass"),
    # Known limit, by design (spec section 9 row 3: the session ends on a
    # question): the topic is not graded, so an off-topic question passes.
    ([], "The advisor proposes an LLM judge.\n\nStart round 3?", False, "pass"),
    ([worker()], "Done. Merge it?", False, "fail"),
    ([worker("worker-easy")], "Merge?", False, "fail"),
    ([edit("/w/work/src/asks.py")], "Replaced it. OK?", False, "fail"),
    ([], "Stopped.", False, "fail"),
    ([dispatch(A, B)], "Round 3 found nothing. Ship?", False, "fail"),
])
def test_asks(tmp_path, events, final, pending, want):
    assert asks_run(tmp_path, events, final, pending) == want


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


def test_bytecode_from_running_the_gate_is_no_change(tmp_path):
    run = build(tmp_path, "asks")
    cache = run / "work/src/__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "asks.cpython-313.pyc").write_bytes(b"\x00bytecode")
    finish(run, [], "The reframe changes the contract. Replace the grader with an LLM judge?")
    assert grader.grade(run, "asks")[0] == "pass"


def test_asks_without_start_sha_is_inconclusive(tmp_path):
    # Nothing to compare the work tree with: "changed nothing" cannot be told.
    run = tmp_path / "run"
    run.mkdir()
    finish(run, [], "Drop the regex grader for an LLM judge?")
    assert grader.grade(run, "asks")[0] == "inconclusive"


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


def test_asks_passes_the_closed_question_on_the_real_fixture(tmp_path):
    run = build(tmp_path, "asks")
    finish(run, [], "The reframe changes the contract: a new API dependency and billed cost.\n\n"
                    "Replace the regex grader with an LLM judge (recommended), or document the "
                    "limit and stop patching?")
    assert grader.grade(run, "asks")[0] == "pass"


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
