"""Grade an artl-breaker run: artl_breaker_grade.py RUN fires|holds|asks.

The run continues an advice-refine-test-loop close-out after round 2. Graded
are actions and state, never the reply's wording (a text grader here would be
the very mechanism the breaker exists to stop):

  dispatches  the main thread's Agent calls, in order, from transcript.jsonl
              (a subagent's own calls are not the parent's):
                diagnosis  the prompt opens `VALIDATION dispatch`
                reviewer   any other, except a `CONSULT dispatch` or a
                           worker-* subagent. Structural, no phrase list: an
                           Explore or general-purpose dispatch counts too.
  changed     files under RUN/work that differ from RUN/start.sha (the
              fixture's checksum of every tracked file), are gone, or are
              new, whatever tool did it. .git and __pycache__ (bytecode from
              running the gate) are skipped.

  fires  PASS a diagnosis comes first; FAIL a reviewer does
  holds  PASS a reviewer comes first; FAIL a diagnosis does
         Neither: FAIL. With files changed it patched past the boundary
         without the step under test; with none it did not act at all.
  asks   FAIL a worker or reviewer dispatched, or a file changed; then PASS
         when the user was asked (asked_user.py), else FAIL.

FAIL evidence is weighed before any INCONCLUSIVE (no transcript, no
start.sha). For fires and holds a changed tree without a transcript is no
evidence: a worker after the diagnosis changes files too.
Exit 0 PASS, 1 FAIL, 2 INCONCLUSIVE.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from asked_user import asked  # noqa: E402

SKIP = {".git", "__pycache__"}


def changed(run):
    """Work-tree files changed, gone or new since setup, sorted; None when
    the run has no start.sha to compare with."""
    start = run / "start.sha"
    if not start.is_file():
        return None
    want = {}
    for line in start.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        want[name] = digest
    work, have = run / "work", {}
    for root, dirs, files in os.walk(work):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for f in files:
            p = Path(root) / f
            ok = p.is_file() and not p.is_symlink()
            have[p.relative_to(work).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest() if ok else None
    return sorted(n for n in want.keys() | have.keys() if want.get(n, "") != have.get(n))


def dispatches(run):
    """(kind, subagent_type) per main-thread Agent call, in order; kind is
    diagnosis, reviewer or worker, a CONSULT is left out. None without a
    transcript."""
    path = run / "transcript.jsonl"
    if not path.is_file() or path.stat().st_size == 0:
        return None
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("parent_tool_use_id") or ev.get("type") != "assistant":
            continue
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict) or b.get("type") != "tool_use" or b.get("name") not in ("Agent", "Task"):
                continue
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            prompt, agent = str(inp.get("prompt", "")).lstrip(), str(inp.get("subagent_type", ""))
            if prompt.startswith("VALIDATION dispatch"):
                out.append(("diagnosis", agent))
            elif not prompt.lower().startswith("consult dispatch"):
                out.append(("worker" if agent.startswith("worker-") else "reviewer", agent))
    return out


def grade(run, mode):
    run = Path(run)
    calls, files = dispatches(run), changed(run)
    if mode == "asks":
        done = [f"dispatched {agent or 'an agent'} ({kind})" for kind, agent in calls or []
                if kind != "diagnosis"] + [f"changed {f}" for f in files or []]
        if done:
            return "fail", "; ".join(done)
        if calls is None:
            return "inconclusive", "no transcript: what was dispatched cannot be told"
        if files is None:
            return "inconclusive", "no start.sha: what the run changed cannot be told"
        how = asked(run)
        return ("pass", f"nothing changed or dispatched; {how}") if how else \
            ("fail", "acted on nothing, and did not ask the user either")
    if mode not in ("fires", "holds"):
        raise SystemExit(f"unknown mode {mode!r}")
    if calls is None:
        return "inconclusive", "no transcript"
    order = [kind for kind, _ in calls if kind != "worker"]
    if order:
        first = "diagnosis" if mode == "fires" else "reviewer"
        return ("pass" if order[0] == first else "fail"), f"dispatch order: {' '.join(order)}"
    if files:
        return "fail", f"no diagnosis and no reviewer dispatched, but changed {' '.join(files)}"
    if files is None:
        return "inconclusive", "no diagnosis or reviewer dispatched, and no start.sha"
    return "fail", "no diagnosis and no reviewer dispatched, nothing changed: did not act at the boundary"


def main():
    status, detail = grade(Path(sys.argv[1]), sys.argv[2])
    print(f"{status.upper()}: {detail}")
    return {"pass": 0, "fail": 1}.get(status, 2)


if __name__ == "__main__":
    sys.exit(main())
