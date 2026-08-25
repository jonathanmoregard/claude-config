#!/usr/bin/env python3
"""SessionStart(resume) — state the session was resumed, and what that implies.

WHY THIS EXISTS

A resumed session inherits the transcript but NOT the processes. On
2026-08-25 the same implementer subagent was lost twice to Claude Code
process exits. Both times it had already done the work and staged it; both
times it died before committing, and no completion record was written. The
recovering session's transcript said "agent is working", which was false,
and the truth was only on disk: `git status` showed three staged files that
the conversation had no record of.

Nothing covered that case. Of the SessionStart hooks registered at the time,
none used the `resume` matcher — two explicitly used `startup|clear|compact`.
So the one source that means "you are missing state" was the one source that
got no context injected.

WHAT IT DOES NOT DO

It does not enumerate what changed. Scanning every working directory here
would cost wall-clock on every resume, go stale between the scan and the
model reading it, and duplicate what `git status` reports accurately on
demand. The hook states the situation; the session does the looking.

CHANNEL

`hookSpecificOutput.additionalContext` on SessionStart — injected into model
context. Exit 2 does nothing on SessionStart, so every failure path here
exits 0: a broken notice must not be a broken session.
"""

import json
import os
import sys

NOTICE = """\
This session was RESUMED (SessionStart source=resume). It inherited a \
transcript, not a running process.

Facts about that boundary, established by incident on 2026-08-25:

- Background subagents do NOT survive the exit. A transcript saying an agent \
is running describes a process that no longer exists. Their work often DOES \
survive, as edited or `git add`-ed files with no commit and no completion \
record.
- Detached `systemd-run --user` units DO survive, and may have finished or \
failed while the session was gone. `systemctl --user list-units` and any \
sentinel log they were writing hold the result.
- Anything the transcript describes as "in flight", "running", or "pending" \
is a claim about the past, not the present.

On disk is the authoritative record of what landed; this conversation's \
memory of it is not. Before continuing prior work, or reporting its status, \
establish current state from the filesystem: `git status` and `git log` in \
the relevant working directories, any task or mission notes, and the state \
of long-running units. Re-dispatching an agent whose work is already staged \
duplicates it; reporting from transcript memory misreports it.

This notice is about resumed state only. It is not a task.\
"""


def main() -> int:
    # CLI-only, matching the other hooks in this repo: a non-interactive
    # entrypoint has no user to inform and no prior session to have lost.
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli") != "cli":
        return 0

    try:
        raw = sys.stdin.read()
    except OSError:
        return 0

    # Registered under matcher `resume`, but a matcher is configuration and
    # configuration drifts. Re-check the source so a widened matcher cannot
    # silently announce a resume on a fresh startup, which would be false.
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get("source") != "resume":
        return 0

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": NOTICE,
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Never let this hook take out a session it exists to help.
        sys.exit(0)
