"""tests/agent-e2e/artl-two-blind-reviewers/grade.py: blindness is graded
within round 1's pair only. Delta rounds come after round 1 reported, by design."""
import json
import subprocess
from pathlib import Path

import pytest

GRADE = Path(__file__).resolve().parents[1] / "tests/agent-e2e/artl-two-blind-reviewers/grade.py"
A = "Assume this finished change can ship broken. Report each defect."
B = ("Hunt only the classes listed below. Find an error, absent value, or failed check "
     "that leaves the caller proceeding as if it had succeeded")
DELTA = A + "\nReview the delta abc1234 only.\nDo not report:\n- authorize grants a missing scope"


def dispatch(*ids_prompts):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Agent", "id": i, "input": {"prompt": p}} for i, p in ids_prompts]}}


def reported(tid, text="Finding: app/tokens.py:40 grants a request without the scope."):
    return [{"type": "assistant", "parent_tool_use_id": tid,
             "message": {"content": [{"type": "text", "text": text}]}},
            {"type": "system", "subtype": "task_notification", "tool_use_id": tid, "status": "completed"}]


def died(tid):
    # what the harness records: a synthetic API-error turn, then `failed`
    return [{"type": "assistant", "parent_tool_use_id": tid,
             "message": {"model": "<synthetic>", "content": [
                 {"type": "text", "text": "You've hit your monthly spend limit"}]}},
            {"type": "system", "subtype": "task_notification", "tool_use_id": tid, "status": "failed"}]


def grade(tmp_path, events):
    t = tmp_path / "transcript.jsonl"
    t.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    out = subprocess.run(["python3", str(GRADE), str(t)], capture_output=True, text=True, check=True)
    return out.stdout.split()[0]


@pytest.mark.parametrize("events,want", [
    # the blind pair, then two delta rounds after it reported
    ([dispatch(("a", A), ("b", B)), *reported("a"), *reported("b"),
      dispatch(("c", DELTA)), *reported("c"), dispatch(("d", DELTA))], "pass"),
    # B written after A reported: round 1 is not blind
    ([dispatch(("a", A)), *reported("a"), dispatch(("b", B))], "late"),
    ([dispatch(("a", A)), *reported("a")], "one-reviewer"),
    # Fable A and B die on a spend limit without a word, then both are
    # re-dispatched on Opus: the dead pair reported nothing, so the retry pair
    # is round 1 (measured: artl-two-blind-reviewers, 2026-11-24, both arms)
    ([dispatch(("a", A)), *died("a"), dispatch(("b", B)), *died("b"),
      dispatch(("a2", A)), dispatch(("b2", B)), *reported("a2"), *reported("b2")], "pass"),
    # a reviewer that failed AFTER speaking did report: B after it is late
    ([dispatch(("a", A)), *reported("a")[:1], *died("a"), dispatch(("b", B))], "late"),
    # B names its classes by id with its own wording. Ids are matched as
    # written, uppercase (measured: artl-two-blind-reviewers 2026-11-24, new arm
    # graded no-hunt while its prompt listed "D1 — ...", "D2 — ...")
    ([dispatch(("a", A), ("b", "Hunt only the classes listed below.\nD1 — look for errors\nG11 — bypasses")),
      *reported("a"), *reported("b")], "pass"),
])
def test_blindness_is_graded_in_round_one(tmp_path, events, want):
    assert grade(tmp_path, events) == want
