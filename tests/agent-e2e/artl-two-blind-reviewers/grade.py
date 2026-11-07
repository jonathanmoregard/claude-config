"""Grade one close-out-review run by its reviewer DISPATCH, not its prose.

Reads transcript.jsonl (stream-json) and reports, in order:

  status            pass | one-reviewer | leaked | no-hunt | nosub
  n                 reviewer dispatches from the main session
  block             all reviewers in ONE assistant message (one tool-use
                    block). Reported, NOT required -- see below
  hunt              some reviewer prompt carries a named hunt class
  classes           the class markers seen, for the summary line

What is graded is the ANCHORING CHANNEL, not the mechanism that closes it.
The skill prefers one tool-use block because a reviewer dispatched there
cannot have seen the other's findings -- they do not exist yet. Measured, that
instruction is not followed: across two batches, ten runs, every single one
dispatched the second reviewer in the NEXT message, and every single one
carried no content from the first. So `block` is reported for the record and
`leaked` is the failure: a later prompt that quotes, summarises or reacts to
the earlier reviewer's output is anchored, and that is the thing the wave
exists to prevent.
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

    # Only a prompt that was NOT co-dispatched can be anchored, so the leak
    # check runs over the prompts after the first wave.
    later = " ".join(prompts[1:]).lower() if not parallel else ""
    leaked = any(
        m in later
        for m in ("reviewer a", "prior review", "previous review",
                  "already found", "first reviewer", "round 1",
                  "the other reviewer")
    )

    if n == 0:
        status = "nosub"
    elif n < 2:
        status = "one-reviewer"
    elif leaked:
        status = "leaked"
    elif not seen:
        status = "no-hunt"
    else:
        status = "pass"

    block = "one-block" if parallel else "serial"
    print(f"{status} {n} {block} {str(bool(seen)).lower()} {','.join(seen) or '-'}")


if __name__ == "__main__":
    main()
