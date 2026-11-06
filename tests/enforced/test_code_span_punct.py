"""MessageDisplay hook that shows a space instead of punctuation after a code span.

WHAT THIS GUARDS. Copying `GUARD_MERGER_PRIVATE_KEY`. from a setup walkthrough
carried the dot into a GitHub secret name and broke the setup silently. Two
rounds of skill wording did not stop the agent writing it (agent e2e 0/3, 0/2),
so the display is rewritten instead. Invariants: glued . , ; : after a code
span shows as a space; fenced blocks, including fences split across flushes,
are untouched; unchanged text prints nothing; malformed input prints nothing.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

HOOK = Path(__file__).resolve().parents[2] / "runtime" / "hooks" / "code-span-punct.py"


def flush(delta: str, message_id: str = "m", final: bool = True, index: int = 0) -> str | None:
    event = {"hook_event_name": "MessageDisplay", "turn_id": "t", "message_id": message_id,
             "index": index, "final": final, "delta": delta}
    out = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event),
                         capture_output=True, text=True, check=True).stdout
    if not out.strip():
        return None
    payload = json.loads(out)["hookSpecificOutput"]
    assert payload["hookEventName"] == "MessageDisplay"
    return payload["displayContent"]


def test_glued_punctuation_becomes_space():
    assert flush("Name: `FOO_KEY`. Then `bash x.sh`, then **`BAR`**:\n") == \
        "Name: `FOO_KEY`  Then `bash x.sh`  then **`BAR`** \n"


def test_punctuation_inside_or_before_span_kept():
    assert flush("Run `a.b` now and `x:y` too.\n") is None


def test_fenced_block_untouched_across_flushes():
    mid = str(uuid.uuid4())
    assert flush("```\n", mid, final=False, index=0) is None
    assert flush("echo `x`.\n", mid, final=False, index=1) is None
    assert flush("```\n", mid, final=False, index=2) is None
    assert flush("see `y`.\n", mid, final=True, index=3) == "see `y` \n"


def test_longer_fence_contains_shorter_fence_lines():
    text = "````\n```\nsee `x`.\n```\n````\nafter `y`.\n"
    assert flush(text) == "````\n```\nsee `x`.\n```\n````\nafter `y` \n"


def test_tilde_fence_not_closed_by_backticks():
    assert flush("~~~\n```\nsee `x`.\n") is None


def test_blockquoted_and_indented_fences():
    assert flush("> ```\n> see `x`.\n> ```\n") is None
    assert flush("5. Add:\n   ```\n   KEY=`v`.\n   ```\n") is None


def test_double_backtick_span_keeps_inner_punctuation():
    assert flush("run ``echo `x`. done`` now\n") is None
    assert flush("run ``echo `x`. done``.\n") == "run ``echo `x`. done`` \n"


def test_unmatched_backtick_run_left_alone():
    assert flush("a stray ` tick `x. here\n") is None


def test_malformed_input_prints_nothing():
    out = subprocess.run([sys.executable, str(HOOK)], input="not json",
                         capture_output=True, text=True, check=True).stdout
    assert out == ""
