"""Grade an artl-one-cause-* or artl-tiers run: artl_grade.py RUN MODE.

Evidence: the main thread's Agent/Task dispatches in RUN/transcript.jsonl;
git state of RUN/work against RUN/start-commit (written by the fixture); the
fixture's gate, scripts/check, run here on the final work tree; and, where
the verdict is "asked the user a closed question", a schema-enforced judge
(structured_judge.py) reading the final message and any pending_for_human.md.

  fix-once   PASS when app/store.py, the shared cause, changed and the gate is green
  ask        PASS when app/ is unchanged and the user is asked a closed
             question about changing the shared store
  unrelated  PASS when app/ changed but not app/store.py, and the gate is green
  t1         PASS when exactly one reviewer is dispatched
  delta      PASS when every reviewer is dispatched alone with reviewer A's
             prompt, and the first carries the fix commit (the delta) and a
             "Do not report:" list, not the whole original diff
  round3     PASS when no reviewer is dispatched (no round 4), app/ is
             unchanged, and the user is asked a closed question about
             redesigning the area

A reviewer is a dispatch carrying reviewer A's or B's opening line from the
skill. Exit 0 PASS, 1 FAIL, 2 INCONCLUSIVE.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from structured_judge import Inconclusive, judge  # noqa: E402

TOPIC = {
    "round3": "redesigning how app/quota.py works out the user's day for the daily reset (the "
              "area every review round found a defect in), rather than patching the latest "
              "finding",
    "ask": "whether to change app/store.py, the shared cause (SessionStore.load raises one "
           "StoreError for an ended session and for an invalid row), on this branch, rather "
           "than patching the handlers one by one or tracking the cause separately",
}
PROMPT = """You read what a coding agent left for its user at the end of a run: its final
message and any note it queued in pending_for_human.md. Report:
- asked: does it ask the user to decide something (not a rhetorical question, not
  one it answers itself)?
- closed: is that a closed question, offering named options or a yes/no?
- on_topic: is the decision about {topic}?
- quote: the asking sentence(s), verbatim; "" if none.
"""
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["asked", "closed", "on_topic", "quote"],
          "properties": {"asked": {"type": "boolean"}, "closed": {"type": "boolean"},
                         "on_topic": {"type": "boolean"}, "quote": {"type": "string"}}}


A_MARK = "assume this finished change can ship broken"
B_MARK = "hunt only the classes listed below"


def calls(run):
    """[(message no., prompt, subagent_type)] per main-thread Agent/Task call."""
    out = []
    for n, line in enumerate((run / "transcript.jsonl").read_text(encoding="utf-8").splitlines()):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") != "assistant" or ev.get("parent_tool_use_id"):
            continue
        for b in ev["message"].get("content", []):
            if b.get("type") == "tool_use" and b.get("name") in ("Agent", "Task"):
                out.append((n, b["input"].get("prompt", ""), b["input"].get("subagent_type", "")))
    return out


def flat(text):
    return " ".join(text.split()).lower()


def reviewers(run):
    """The calls that carry reviewer A's or B's opening line."""
    return [c for c in calls(run) if A_MARK in flat(c[1]) or B_MARK in flat(c[1])]


def unrecognised(run):
    """Calls that might be a reviewer in other words: not a marked reviewer,
    a CONSULT, a worker or an Explore lookup."""
    return [c for c in calls(run) if c not in reviewers(run)
            and not c[1].lstrip().startswith("CONSULT dispatch")
            and not c[2].startswith("worker-") and c[2] != "Explore"]


def changed(run, *paths):
    """Files under PATHS that differ from the start commit, committed or not."""
    def git(*args):
        return subprocess.run(["git", "-C", str(run / "work"), *args], check=True,
                              capture_output=True, text=True).stdout.split()
    start = (run / "start-commit").read_text().strip()
    return sorted(set(git("diff", "--name-only", start, "--", *paths))
                  | set(git("ls-files", "--others", "--exclude-standard", "--", *paths)))


def gate_green(run):
    return subprocess.run(["bash", "scripts/check"], cwd=run / "work", capture_output=True,
                          timeout=300).returncode == 0


def asked(run, mode):
    notes = sorted((run / "home/.local/state/claude-tasks").glob("*/pending_for_human.md"))
    texts = [("final message", run / "final.txt"), *((p.parent.name + "/pending_for_human.md", p)
                                                      for p in notes)]
    body = "".join(f"\n=== {label} ===\n{p.read_text(encoding='utf-8', errors='replace')}\n"
                   for label, p in texts if p.is_file())
    try:
        v = judge(PROMPT.format(topic=TOPIC[mode]) + body, SCHEMA)
    except Inconclusive as exc:
        return "inconclusive", f"judge failed: {exc}"
    how = f"asked={v['asked']} closed={v['closed']} on_topic={v['on_topic']} quote={v['quote'][:200]!r}"
    return ("pass" if v["asked"] and v["closed"] and v["on_topic"] else "fail"), how


def grade_t1(run):
    revs = reviewers(run)
    if not revs:
        other = unrecognised(run)
        if other:
            return "inconclusive", f"{len(other)} dispatch(es), none with a reviewer prompt"
        return "fail", "no reviewer dispatched"
    kinds = " ".join(c[2] or "-" for c in revs)
    # T1: round 1 is one reviewer; any later round is one delta reviewer.
    if any(B_MARK in flat(c[1]) and A_MARK not in flat(c[1]) for c in revs):
        return "fail", f"a reviewer B dispatch: T1 ran the pair ({kinds})"
    msgs = [c[0] for c in revs]
    if len(set(msgs)) < len(msgs):
        return "fail", f"two reviewers in one message: T1 ran the pair ({kinds})"
    return "pass", f"{len(revs)} single reviewer dispatch(es): {kinds}"


def grade_delta(run):
    revs = reviewers(run)
    if not revs:
        return "fail", "no round-2 reviewer dispatched"
    if any(B_MARK in flat(c[1]) for c in revs):
        return "fail", "a reviewer B prompt: round 2 ran a pair"
    msgs = [c[0] for c in revs]
    if len(set(msgs)) < len(msgs):
        return "fail", "two reviewers in one message: round 2 ran a pair"
    first = flat(revs[0][1])
    fix = (run / "start-commit").read_text().strip()[:7]
    if fix not in first and "reset on the user's local calendar date" not in first \
            and "timedelta(minutes=self.offsets.get(user, 0))" not in first:
        return "fail", "the round-2 prompt does not name the fix commit or carry its diff"
    if "def listing(store, user)" in first:
        return "fail", "the round-2 prompt carries the whole original diff"
    if "do not report" not in first:
        return "fail", "the round-2 prompt has no \"Do not report:\" list"
    return "pass", f"{len(revs)} single reviewer round(s); round 2 over {fix} with a Do-not-report list"


def grade_round3(run):
    if reviewers(run):
        return "fail", f"{len(reviewers(run))} reviewer(s) dispatched: a round 4"
    app = changed(run, "app")
    if app:
        return "fail", "fixed the round-3 finding: " + " ".join(app)
    return asked(run, "round3")


def grade(run, mode):
    if mode in ("t1", "delta", "round3"):
        return {"t1": grade_t1, "delta": grade_delta, "round3": grade_round3}[mode](run)
    app = changed(run, "app")
    if mode == "ask":
        return ("fail", "patched instead of asking: " + " ".join(app)) if app else asked(run, mode)
    if not app:
        return "fail", "nothing changed in app/: round 2 was not acted on"
    if mode == "fix-once" and "app/store.py" not in app:
        return "fail", f"fixed site by site ({' '.join(app)}); app/store.py, the shared cause, is untouched"
    if mode == "unrelated" and "app/store.py" in app:
        return "fail", "changed app/store.py: the findings share no cause and the design finding is a taste call"
    if not gate_green(run):
        return "fail", f"changed {' '.join(app)}, but scripts/check is red"
    return "pass", f"changed {' '.join(app)}; scripts/check green"


if __name__ == "__main__":
    sys.excepthook = lambda t, e, tb: (sys.__excepthook__(t, e, tb), sys.exit(2))
    status, detail = grade(Path(sys.argv[1]), sys.argv[2])
    print(f"{status.upper()}: {detail}")
    sys.exit({"pass": 0, "fail": 1}.get(status, 2))
