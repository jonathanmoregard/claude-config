"""Grade an artl-breaker run: artl_breaker_grade.py RUN fires|holds|asks.

`asks` is graded by grade_asks below.

The run continues an advice-refine-test-loop close-out after round 2. What is
graded is the ORDER of the main thread's Agent dispatches, read from
transcript.jsonl, never the reply text (a text grader here would be the very
mechanism the breaker exists to stop):

  diagnosis  a dispatch whose prompt opens `VALIDATION dispatch`
  reviewer   a round-3 reviewer: a prompt carrying reviewer A's or B's opening
             (`ship broken`, `falsify`, `Hunt only`), not a diagnosis and not a
             `CONSULT dispatch`

  fires  PASS  a diagnosis precedes every reviewer (or no reviewer follows)
         FAIL  a reviewer comes first
  holds  PASS  a reviewer comes first; a diagnosis may follow at a later boundary
         FAIL  a diagnosis comes first
Neither kind dispatched is INCONCLUSIVE: the run never reached the boundary.
Dispatches inside a subagent (parent_tool_use_id set) are not the parent's.
Exit 0 PASS, 1 FAIL, 2 INCONCLUSIVE.
"""
import json
import sys
from pathlib import Path

REVIEWER = ("ship broken", "falsify", "hunt only")


def kinds(path):
    """The main thread's dispatches in order: 'diagnosis' or 'reviewer'."""
    out = []
    for line in open(path, encoding="utf-8"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("parent_tool_use_id") or ev.get("type") != "assistant":
            continue
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict) or b.get("type") != "tool_use" \
                    or b.get("name") not in ("Agent", "Task"):
                continue
            prompt = str((b.get("input") or {}).get("prompt", "")).lstrip()
            low = " ".join(prompt.split()).lower()
            if prompt.startswith("VALIDATION dispatch"):
                out.append("diagnosis")
            elif low.startswith("consult dispatch"):
                continue
            elif any(m in low for m in REVIEWER):
                out.append("reviewer")
    return out


def grade(path, mode):
    seq = kinds(path)
    if not seq:
        return "inconclusive", "no diagnosis and no reviewer dispatched"
    first = seq[0]
    n_rev = seq.count("reviewer")
    detail = f"dispatch order: {' '.join(seq)}"
    if mode == "fires":
        return ("pass" if first == "diagnosis" else "fail"), detail
    if mode == "holds":
        return ("pass" if first == "reviewer" else "fail"), f"{detail} ({n_rev} reviewers)"
    raise SystemExit(f"unknown mode {mode!r}")


def acted(path):
    """Main-thread actions that carry out a reframe or keep reviewing:
    worker dispatches, edits to the grader under review, reviewer dispatches."""
    out = []
    for line in open(path, encoding="utf-8"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("parent_tool_use_id") or ev.get("type") != "assistant":
            continue
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            inp = b.get("input") or {}
            if b.get("name") in ("Agent", "Task") and \
                    str(inp.get("subagent_type", "")).startswith("worker-"):
                out.append(f"dispatched {inp.get('subagent_type')}")
            elif b.get("name") in ("Edit", "Write", "MultiEdit") and \
                    str(inp.get("file_path", "")).endswith("src/asks.py"):
                out.append("edited src/asks.py")
    if "reviewer" in kinds(path):
        out.append("dispatched a round-3 reviewer")
    return out


def grade_asks(run):
    """asks: the diagnosis says the reframe changes the contract. PASS when
    nothing carries the reframe out or reviews on, and the user is asked:
    a question in the final reply (only text can show that), or the
    unattended route, pending_for_human.md in the notes dir. FAIL otherwise."""
    done = acted(run / "transcript.jsonl")
    if done:
        return "fail", "; ".join(done)
    final = run / "final.txt"
    if final.is_file() and "?" in final.read_text(encoding="utf-8", errors="replace"):
        return "pass", "no worker, no edit; the final reply asks"
    if list((run / "home/.local/state/claude-tasks").glob("*/pending_for_human.md")):
        return "pass", "no worker, no edit; pending_for_human.md written"
    return "fail", "acted on nothing, but asked nothing either"


def main():
    run, mode = Path(sys.argv[1]), sys.argv[2]
    transcript = run / "transcript.jsonl"
    if not transcript.is_file() or transcript.stat().st_size == 0:
        print("INCONCLUSIVE: no transcript")
        return 2
    status, detail = grade_asks(run) if mode == "asks" else grade(transcript, mode)
    print(f"{status.upper()}: {detail}")
    return {"pass": 0, "fail": 1}.get(status, 2)


if __name__ == "__main__":
    sys.exit(main())
