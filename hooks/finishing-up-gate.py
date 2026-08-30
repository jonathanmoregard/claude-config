#!/usr/bin/env python3
"""Stop hook: refuse to end a turn on unfinished, unmeasured work.

The guarantee half of the definition-of-done system. The `finishing-up` skill
holds the procedure; this holds the fact that the procedure runs. A skill fires
on the model's judgement, which is the mechanism that has been failing — it
works right up until the model is context-starved and rushing, which is exactly
when it is needed.

Cost model: does nothing unless finishing-up-arm.sh armed this session, so an
ordinary question / research / read session pays one file-existence check. Only
a session that actually edited a repo ever runs dod-check.

Modes (CLAUDE_DOD_MODE, default "active"):
    active   block  (the default — enforcement is the point)
    shadow   log what it would have blocked; never block

Stand-down guards, in order. Every one of these ENDS the turn rather than
blocking, because a gate that can never be satisfied protects nothing and just
hangs the session:
  * not armed
  * background work still in flight (`background_tasks`) — the turn is pausing
    on a live CI watch, not finishing
  * a durable follow-up already scheduled (`session_crons`) — that IS the
    follow-through
  * this session's own block budget spent

Protocol note: Stop hooks use TOP-LEVEL `decision`/`reason`, not
`hookSpecificOutput`. Putting it in the wrong place is the quickest way to
write a hook that silently does nothing.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import time

STATE_DIR = pathlib.Path(
    os.environ.get(
        "CLAUDE_DOD_STATE_DIR", pathlib.Path.home() / ".local/state/claude-dod"
    )
)
# Overridable so a harness can point at a copy. ~/.claude is a working checkout
# that other agents share; a branch switch under a running session deletes every
# file not present on the target branch, and a hook whose predicate path can
# vanish measures the branch as much as it measures the work. Observed: the
# first headless run of the harness died with "can't open file" mid-session and
# the turn ended UNBLOCKED. Missing still fails closed here (run_dod returns an
# error, which counts as a problem); this only drops the fixed-path assumption.
DOD = pathlib.Path(
    os.environ.get(
        "CLAUDE_DOD_CHECK", pathlib.Path.home() / ".claude/scripts/dod-check.py"
    )
)
# ACTIVE by default. The ask was "programmatically enforced, every time, no
# exceptions"; a default of shadow means anyone who does not set an env var gets
# nothing enforced, which is how the last two attempts at this died. Shadow is
# now the deliberate opt-out (CLAUDE_DOD_MODE=shadow), not the resting state.
MODE = os.environ.get("CLAUDE_DOD_MODE", "active").strip().lower()

# Own budget, deliberately below the harness's default cap of 8 so this gate
# stands down on its own terms rather than being overridden.
MAX_BLOCKS = int(os.environ.get("CLAUDE_DOD_MAX_BLOCKS", "6"))

# A session that ends while still blocked never reaches the clean pass that
# deletes its armfile, so the state dir grows without bound. 14 days is chosen
# to be uninteresting: no real session lives that long, and reaping is
# SELF-HEALING anyway — the arm hook rewrites the file on the very next edit, so
# the worst case for a reap that is somehow too eager is one unguarded turn in a
# session that has done nothing for a fortnight.
ARM_MAX_AGE_DAYS = float(os.environ.get("CLAUDE_DOD_ARM_MAX_AGE_DAYS", "14"))
DOD_TIMEOUT = 60


def log(session, payload):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / "gate.log").open("a") as fh:
            fh.write(
                json.dumps({"ts": time.time(), "session": session, **payload}) + "\n"
            )
    except OSError:
        pass


def allow():
    sys.exit(0)


def reap(session):
    """Delete arm/blocks state left behind by sessions that ended blocked.

    Runs only for a session that is ALREADY armed, so an ordinary read-only
    session still pays a single file-existence check and never a directory
    scan. Touches nothing but `*.arm` and `*.blocks` — gate.log is the audit
    trail and outlives everything here.
    """
    if ARM_MAX_AGE_DAYS <= 0:
        return
    cutoff = time.time() - ARM_MAX_AGE_DAYS * 86400
    removed = []
    try:
        entries = list(STATE_DIR.iterdir())
    except OSError:
        return
    for path in entries:
        if path.suffix not in (".arm", ".blocks"):
            continue
        if path.stem == session:  # never reap the session doing the reaping
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed.append(path.name)
        except OSError:
            continue
    if removed:
        log(session, {"action": "reap",
                      "why": f"{len(removed)} file(s) older than "
                             f"{ARM_MAX_AGE_DAYS}d",
                      "files": removed[:20]})


def block(reason, session, why, guards=None):
    # `guards` records what the stand-down fields actually looked like on this
    # payload. In a headless run the agent booked a durable CronCreate follow-up
    # and was then blocked twice more anyway, and the log could not say whether
    # `session_crons` arrived empty or never arrived at all — the field is not in
    # the public hook docs, so both are live possibilities. Shadow mode is read by
    # hand before this is ever promoted; a log that cannot distinguish "the guard
    # is broken" from "the guard correctly saw nothing" cannot support that call.
    log(session, {
        "action": "block" if MODE == "active" else "shadow-block",
        "why": why,
        **({"guards": guards} if guards else {}),
    })
    if MODE != "active":
        # Shadow: surface it, never fault the turn.
        print(json.dumps({"systemMessage": f"[dod shadow] would block: {why}"}))
        sys.exit(0)
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def run_dod(repo):
    try:
        p = subprocess.run(
            [sys.executable, str(DOD), "--repo", repo, "--json"],
            capture_output=True,
            text=True,
            timeout=DOD_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"dod-check did not run: {type(exc).__name__}"
    try:
        return json.loads(p.stdout), None
    except ValueError:
        return None, (p.stderr or "dod-check emitted unparseable JSON")[:200]


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        allow()

    session = payload.get("session_id") or ""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", session):
        allow()

    armfile = STATE_DIR / f"{session}.arm"
    if not armfile.exists():
        allow()  # nothing was edited — the overwhelmingly common case

    reap(session)

    # A turn pausing for live background work is not a turn that finished.
    # Faulting it would fight the very CI watch the skill was told to start.
    if payload.get("background_tasks"):
        log(session, {"action": "allow", "why": "background work in flight"})
        allow()

    # A durable follow-up is already booked.
    if payload.get("session_crons"):
        log(session, {"action": "allow", "why": "follow-up already scheduled"})
        allow()

    # Another loop already owns this session's stop condition. Two Stop hooks
    # pushing "keep going" at one turn is how a human's explicit instruction
    # gets out-voted: on 2026-08-27 a user said "write the handoff and then
    # stop, do no other work", ralph blocked to continue the mission, and this
    # gate piled on with the same push. The agent held six turns and then broke
    # the instruction. One driver per turn. The loop that was armed on purpose
    # wins; this gate is the general-purpose backstop and yields to it.
    # Path agreed with scripts/ralph-loop-state.sh, which is the single source
    # of truth and says so. Python cannot source bash, so the two agree via the
    # env var rather than by copying the literal — that file was written after a
    # migration moved the writer and left four readers behind.
    ralph_dir = pathlib.Path(
        os.environ.get(
            "CLAUDE_RALPH_STATE_DIR",
            pathlib.Path.home() / ".local/state/claude-ralph-loop",
        )
    )
    if (ralph_dir / f"{session}.md").exists():
        log(session, {"action": "allow", "why": "a ralph loop owns this session"})
        allow()

    countfile = STATE_DIR / f"{session}.blocks"
    try:
        blocks = int(countfile.read_text().strip() or 0)
    except (OSError, ValueError):
        blocks = 0
    if blocks >= MAX_BLOCKS:
        log(session, {"action": "allow", "why": f"block budget spent ({blocks})"})
        allow()

    try:
        repos = [r for r in armfile.read_text().splitlines() if r.strip()]
    except OSError:
        allow()

    problems = []
    for repo in repos:
        result, err = run_dod(repo)
        if err:
            problems.append(f"{repo}: {err} — treat as NOT done")
            continue
        if not result.get("applicable") or result.get("done"):
            continue
        lines = [f"  [{b['code']}] {b['detail']}" for b in result.get("blockers", [])]
        problems.append(f"{repo}:\n" + "\n".join(lines))

    if not problems:
        for f in (armfile, countfile):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        allow()

    try:
        countfile.write_text(str(blocks + 1))
    except OSError:
        pass

    body = "\n".join(problems)
    reason = (
        "Work in this session is not finished. `dod-check` reports:\n\n"
        f"{body}\n\n"
        "Invoke the `finishing-up` skill and work these blockers. Do not end "
        "the turn by reporting status — drive to green and mergeable.\n\n"
        f"Re-check with: python3 {DOD} --repo <repo>\n"
        "That exact command, not a bare `dod-check` — it is not on PATH. "
        "Reading the conditions yourself and concluding you are done is the "
        "self-attestation this gate exists to replace.\n\n"
        "If a check is still running, either start `gh pr checks --watch` as a "
        "background task or schedule a durable CronCreate follow-up; either one "
        "satisfies this gate. The follow-up REPORTS — it must not merge, and "
        "must not push anything Jonathan has not already seen. Merging is his "
        "click, and being blocked by this gate is not authorisation to take "
        "it.\n\n"
        "If you are genuinely blocked on Jonathan — a credential, an "
        "interactive login, a taste call, or his merge click — say so "
        "explicitly, with the clickable PR URL and what you actually measured. "
        f"This gate stands down after {MAX_BLOCKS} blocks regardless."
    )
    block(
        reason,
        session,
        body.replace("\n", " | ")[:400],
        guards={
            k: ("absent" if k not in payload else len(payload.get(k) or []))
            for k in ("background_tasks", "session_crons")
        },
    )


if __name__ == "__main__":
    main()
