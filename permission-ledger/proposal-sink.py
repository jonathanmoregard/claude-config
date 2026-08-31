#!/usr/bin/env python3
"""Persist proposal blocks emitted by the nightly evaluator on stdout.

Stage 3 of the ledger used to hand the model
`--allowedTools 'Write(~/.claude/proposals/permissions/*)'` and let it write its
own files. That grant is dead by construction in headless mode — path-scoped
Write(...) grants never register under `--print`, and every path under
`~/.claude/` is additionally refused by Claude Code's built-in sensitive-path
gate (both probed 2026-08-01, see tests/base.nix in nixos-config). The first
night the digest actually held groups (2026-08-03, 5 groups) the agent wrote
zero files and said so in the log.

So the evaluator now runs READ-ONLY and emits each proposal as a delimited
stdout block; this sink parses those blocks and writes the files. Proposer
emits, pipeline persists — the same shape rsiProposalSink uses in
home/jonathan-linux.nix, which is the one path proven to work here.

The model's output is downstream of untrusted material (tool inputs sampled
from real sessions land in the digest it reads), so every cap below is a
security boundary, not tidiness: basename-only filenames matching the exact
shape the prompt mandates, no overwrite of an existing proposal, 64 KiB per
body, 5 proposals per run, 2 MiB of stdin.

Writes are atomic (2026-08-31): O_EXCL reserves the name race-safely, the body
lands in a same-directory temp file and arrives by rename(2). A crashed nightly
run therefore cannot leave a truncated proposal that triage reads as evidence
and every later run skips as "already there".

Usage:
    proposal-sink.py <dest-dir> < raw-agent-output

Exit codes:
    0  clean run
    2  anomaly (rejected block, missing END marker, truncation) — the caller
       preserves the raw output for forensics; a bad-model night must stay
       distinguishable from a quiet good one.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

MAX_PROPOSALS = 5           # the prompt caps the agent at 5; enforce it here too
MAX_BODY_BYTES = 64 * 1024
MAX_STDIN_BYTES = 2 * 1024 * 1024
MAX_PASSTHROUGH_LINES = 60  # agent prose outside blocks, echoed for the log

HEADER = re.compile(r"^===PROPOSAL: (.*?)===$")
END = "===END PROPOSAL==="
# Exactly the filename shape section 5 of prompt.md mandates:
# YYYY-MM-DD-<slug>.md, slug lowercase kebab. Rejects traversal, absolute
# paths, dotfiles and squat-bait like CLAUDE.md as a side effect.
NAME_OK = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]{0,100}\.md$")


def write_proposal_atomically(final, body):
    """Create `final` with `body`, or leave the filesystem exactly as it was.

    Two properties, both load-bearing, and the original only had the first:

      * create-or-skip, race-safe. The O_EXCL open reserves the name against a
        concurrent run in one syscall — no exists()/write TOCTOU window.
        Returns False when the name is already taken.
      * crash-safe. The body goes to a temp file in the SAME directory and
        arrives at `final` by rename(2), so a crash, a kill, or ENOSPC part-way
        through can never leave a truncated proposal at the destination for
        triage to read as evidence. The original wrote the body straight into
        the O_EXCL'd file, so an interrupted run left an empty or half-written
        proposal that every later run then skipped as "already there".

    Raises whatever the write raised, after removing both the reservation and
    the temp file — the caller reports it as an anomaly.
    """
    # 0600 because proposals quote tool inputs sampled from real sessions.
    try:
        fd = os.open(final, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    os.close(fd)
    tmp = None
    try:
        tfd, tmp = tempfile.mkstemp(dir=os.path.dirname(final) or ".",
                                    prefix=".sink-tmp-")  # mkstemp: mode 0600
        with os.fdopen(tfd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
        tmp = None
    except BaseException:
        for path in (tmp, final):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        raise
    return True


def main():
    if len(sys.argv) != 2:
        print("sink: usage: proposal-sink.py <dest-dir>", file=sys.stderr)
        return 1

    anomalies = 0
    dest = Path(sys.argv[1])
    # mode applies to the leaf only (parents get umask) — fine, the parent
    # chain lives under ~/.claude.
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Cron sets no LANG; a locale-dependent stdin encoding must not crash the
    # sink on non-ASCII prose.
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    text = sys.stdin.read(MAX_STDIN_BYTES)
    if sys.stdin.read(1):
        print("sink: WARNING stdin exceeded 2MiB cap; trailing output dropped")
        anomalies += 1

    # Line state machine, not a multiline regex: a non-greedy regex body glues
    # two proposals together whenever the model drops one END marker, and then
    # loses both to the size cap. Here a missing END rejects only its own block.
    blocks = []
    passthrough = []
    name = None
    buf = []
    for ln in text.split("\n"):
        m = HEADER.match(ln)
        if m:
            if name is not None:
                print("sink: rejected %r (missing END marker)" % name)
                anomalies += 1
            name = m.group(1).strip()
            buf = []
            continue
        if ln == END:
            if name is not None:
                blocks.append((name, "\n".join(buf) + "\n"))
                name = None
            continue
        if name is not None:
            buf.append(ln)
        elif ln.strip():
            # Prose outside any block: the agent's per-proposal summary lines
            # and its termination line. Keep them in the log — that is what
            # makes a "nothing to propose" night readable after the fact.
            passthrough.append(ln.strip())
    if name is not None:
        print("sink: rejected %r (missing END marker)" % name)
        anomalies += 1

    written = 0
    for fname, body_s in blocks:
        if written >= MAX_PROPOSALS:
            print("sink: cap of %d proposals reached, ignoring remainder" % MAX_PROPOSALS)
            anomalies += 1
            break
        body = body_s.encode("utf-8")
        if not NAME_OK.match(fname):
            print("sink: rejected filename %r" % fname)
            anomalies += 1
            continue
        if len(body) > MAX_BODY_BYTES:
            print("sink: rejected oversized body for %s" % fname)
            anomalies += 1
            continue
        # Atomic create-or-skip AND crash-safe body write — see the helper.
        try:
            created = write_proposal_atomically(str(dest / fname), body)
        except (OSError, ValueError) as exc:
            print("sink: FAILED to write %s (%s); nothing left at the "
                  "destination" % (fname, exc))
            anomalies += 1
            continue
        if not created:
            print("sink: skipped existing %s" % fname)
            continue
        written += 1
        print("sink: wrote %s" % fname)

    for ln in passthrough[:MAX_PASSTHROUGH_LINES]:
        print("agent: %s" % ln)
    if len(passthrough) > MAX_PASSTHROUGH_LINES:
        print("agent: (%d further line(s) suppressed)"
              % (len(passthrough) - MAX_PASSTHROUGH_LINES))

    print("sink: done, %d proposal(s) written" % written)
    if anomalies:
        print("sink: %d anomaly(ies) — raw output worth keeping" % anomalies)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
