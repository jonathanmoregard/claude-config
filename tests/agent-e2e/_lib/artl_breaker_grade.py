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
Neither kind dispatched: FAIL when the run changed a tracked file of the work
tree (it patched on past the boundary without the step under test),
otherwise INCONCLUSIVE (it never reached the boundary).
Dispatches inside a subagent (parent_tool_use_id set) are not the parent's.

What the run changed is read from the work tree against RUN/start.sha (the
fixture's checksums of every tracked file), never from which tools it called:
a Bash heredoc or a general-purpose subagent changes a file as surely as Edit.
Exit 0 PASS, 1 FAIL, 2 INCONCLUSIVE.
"""
import hashlib
import json
import re
import sys
from pathlib import Path

REVIEWER = ("ship broken", "falsify", "hunt only")


def changed(run):
    """Tracked files the run left different from, or missing since, setup;
    None when the run has no start.sha to compare with."""
    start = Path(run) / "start.sha"
    if not start.is_file():
        return None
    out = []
    for line in start.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        p = Path(run) / "work" / name
        now = None
        if p.is_file() and not p.is_symlink():
            now = hashlib.sha256(p.read_bytes()).hexdigest()
        if now != digest:
            out.append(name)
    return out


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
        files = changed(Path(path).parent)
        if files:
            return "fail", f"no diagnosis and no reviewer dispatched, but changed {' '.join(files)}"
        return "inconclusive", "no diagnosis and no reviewer dispatched, nothing changed"
    first = seq[0]
    n_rev = seq.count("reviewer")
    detail = f"dispatch order: {' '.join(seq)}"
    if mode == "fires":
        return ("pass" if first == "diagnosis" else "fail"), detail
    if mode == "holds":
        return ("pass" if first == "reviewer" else "fail"), f"{detail} ({n_rev} reviewers)"
    raise SystemExit(f"unknown mode {mode!r}")


def acted(run):
    """What the run did that carries out a reframe or keeps reviewing: tracked
    files it changed (by any tool), worker dispatches, reviewer dispatches.
    None when there is no start.sha, so changes cannot be told."""
    files = changed(run)
    if files is None:
        return None
    out = [f"changed {f}" for f in files]
    path = run / "transcript.jsonl"
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
    if "reviewer" in kinds(path):
        out.append("dispatched a round-3 reviewer")
    return out


# A quoted span: "...", `...`, or '...' opened and closed at a word edge, so
# the apostrophe in "don't" opens nothing. A finding quoted from the ledger
# ('open a PR?') is the finding's question, not the run's.
QUOTED = re.compile(r"\"[^\"\n]*\"|`[^`\n]*`|(?<!\w)'[^'\n]*'(?!\w)")
# What the question has to be about: the reframe the diagnosis proposes.
REFRAME = re.compile(r"judge|\bLLM|reframe|contract|regex|depend|billed|cost", re.I)


def asks_about_reframe(text):
    """Does some paragraph put a question about the reframe to the user? The
    `?` must stand outside any quote, in the paragraph that names the reframe:
    a summary naming it and then asking "Start round 3?" is not asking."""
    for block in re.split(r"\n\s*\n", text):
        if "?" in QUOTED.sub("", block) and REFRAME.search(block):
            return True
    return False


def grade_asks(run):
    """asks: the diagnosis says the reframe changes the contract. PASS when
    nothing carries the reframe out or reviews on, and the user is asked:
    a question about the reframe in the final reply (only text can show
    that), or the unattended route, pending_for_human.md in the notes dir.
    FAIL otherwise; INCONCLUSIVE without start.sha."""
    done = acted(run)
    if done is None:
        return "inconclusive", "no start.sha: what the run changed cannot be told"
    if done:
        return "fail", "; ".join(done)
    final = run / "final.txt"
    if final.is_file() and asks_about_reframe(final.read_text(encoding="utf-8", errors="replace")):
        return "pass", "nothing changed or dispatched; the final reply asks about the reframe"
    if list((run / "home/.local/state/claude-tasks").glob("*/pending_for_human.md")):
        return "pass", "nothing changed or dispatched; pending_for_human.md written"
    return "fail", "acted on nothing, but asked nothing about the reframe either"


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
