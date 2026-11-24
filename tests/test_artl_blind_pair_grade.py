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
])
def test_blindness_is_graded_in_round_one(tmp_path, events, want):
    assert grade(tmp_path, events) == want
