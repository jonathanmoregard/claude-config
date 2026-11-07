"""Grade one close-out-review run by its reviewer DISPATCH, not its prose.

Reads transcript.jsonl (stream-json) and reports, in order:

  status            pass | one-reviewer | serial | no-hunt | nosub
  n                 reviewer dispatches from the main session
  parallel          two or more reviewers in ONE assistant message (one
                    tool-use block) -- which is what makes them blind to each
                    other: a reviewer dispatched in the same block cannot have
                    seen the other's findings, because they do not exist yet
  hunt              some reviewer prompt carries a named hunt class
  classes           the class markers seen, for the summary line

A reviewer dispatched in a LATER message is graded `serial` even when the
prompt differs, because the whole point of the wave is that neither reviewer
is anchored on the other's output.
"""
import json
import sys

# Markers from references/hunt-classes.md. The prompt-level marker catches a
# verbatim paste; the class names catch a dispatcher that quoted the Hunt lines
# without the preamble.
HUNT_MARKERS = [
    "hunt only the classes",
    "fail-open",
    "swallowed error",
    "resource leak",
    "check-then-act",
    "schema mismatch",
    "stated intent",
    "parse, don't validate",
    "primitive where a type belongs",
    "misuse resistance",
    "homeless domain concept",
    "abstraction-level",
    "readymade",
    "standards conformance",
    "weak assertion",
]

# A dispatch counts as a reviewer when it asks for review and not for edits.
REVIEW_MARKERS = ["falsify", "review", "finding", "defect", "hunt", "ship broken"]


def events(path):
    for line in open(path, encoding="utf-8"):
        try:
            yield json.loads(line)
        except ValueError:
            continue


def reviewer_waves(path):
    """[[prompt, ...], ...] -- one list per assistant message that dispatched
    reviewers, main session only."""
    waves = []
    for ev in events(path):
        if ev.get("type") != "assistant" or ev.get("parent_tool_use_id"):
            continue
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        wave = []
        for b in content:
            if b.get("type") != "tool_use" or b.get("name") != "Agent":
                continue
            inp = b.get("input") or {}
            prompt = str(inp.get("prompt", ""))
            low = prompt.lower()
            # A CONSULT dispatch is advice, not a review round.
            if low.startswith("consult dispatch"):
                continue
            if any(m in low for m in REVIEW_MARKERS):
                wave.append(prompt)
        if wave:
            waves.append(wave)
    return waves


def main():
    waves = reviewer_waves(sys.argv[1])
    prompts = [p for w in waves for p in w]
    n = len(prompts)
    seen = sorted({m for p in prompts for m in HUNT_MARKERS if m in p.lower()})
    parallel = any(len(w) >= 2 for w in waves)

    if n == 0:
        status = "nosub"
    elif n < 2:
        status = "one-reviewer"
    elif not parallel:
        # Both serial shapes fail, but they fail differently and the summary
        # line should say which: a later prompt that quotes the earlier
        # reviewer leaked, while one that does not was merely anchorable.
        later = " ".join(prompts[1:]).lower()
        leaked = any(
            m in later
            for m in ("reviewer a", "prior review", "previous review",
                      "already found", "first reviewer", "round 1")
        )
        status = "serial-leaked" if leaked else "serial"
    elif not seen:
        status = "no-hunt"
    else:
        status = "pass"

    print(f"{status} {n} {str(parallel).lower()} {str(bool(seen)).lower()} {','.join(seen) or '-'}")


if __name__ == "__main__":
    main()
