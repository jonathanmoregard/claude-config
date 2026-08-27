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
  * a session-scoped wake already scheduled (`session_crons`) — that IS the
    follow-through. NOTE, measured 2026-08-27: a `durable: true` CronCreate
    does NOT appear here. The field is "session-scoped cron tasks that will
    wake THIS session", and a durable cron by definition outlives it. Confirmed
    end to end: the agent booked one, verified it with CronList, and the next
    two Stop payloads still carried `session_crons: []`. The guard is right;
    the block text used to promise otherwise, which is what actually broke.
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
_MODE_RAW = os.environ.get("CLAUDE_DOD_MODE", "active").strip().lower()
# An unrecognised value FAILS CLOSED to active. It used to fall through to
# shadow, so `CLAUDE_DOD_MODE=activ` — one typo — silently disabled enforcement
# for good, which is precisely the class of failure this gate exists to catch.
# Shadow must be asked for by its own name.
MODE = _MODE_RAW if _MODE_RAW in ("active", "shadow") else "active"
_MODE_UNKNOWN = None if _MODE_RAW in ("active", "shadow") else _MODE_RAW

# Own budget, deliberately below the harness's default cap of 8 so this gate
# stands down on its own terms rather than being overridden.
def _num_env(name, default, cast, floor=None):
    """Parse a numeric env var without ever crashing the hook.

    These are read at import. An uncaught ValueError there exits non-zero with
    no output, and the harness ends the turn UNGUARDED — a fail-open reachable
    by one stray character in a shell profile. Bad values fall back to the
    default and are reported later, once logging is possible.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default, None
    try:
        val = cast(raw)
    except (TypeError, ValueError):
        return default, f"{name}={raw!r} is not a number; using {default}"
    if floor is not None and val < floor:
        return floor, f"{name}={val} below floor {floor}; using {floor}"
    return val, None


MAX_BLOCKS, _MAX_BLOCKS_WARN = _num_env("CLAUDE_DOD_MAX_BLOCKS", 6, int, floor=1)

# A session that ends while still blocked never reaches the clean pass that
# deletes its armfile, so the state dir grows without bound. 14 days is chosen
# to be uninteresting: no real session lives that long, and reaping is
# SELF-HEALING anyway — the arm hook rewrites the file on the very next edit, so
# the worst case for a reap that is somehow too eager is one unguarded turn in a
# session that has done nothing for a fortnight.
# Floor of 1 day: a tiny positive value (0.001) would reap every other live
# session's state and silently disarm their gates. 0 still disables reaping.
ARM_MAX_AGE_DAYS, _ARM_WARN = _num_env(
    "CLAUDE_DOD_ARM_MAX_AGE_DAYS", 14.0, float)
if ARM_MAX_AGE_DAYS != 0 and ARM_MAX_AGE_DAYS < 1:
    _ARM_WARN = f"CLAUDE_DOD_ARM_MAX_AGE_DAYS={ARM_MAX_AGE_DAYS} below floor 1; using 1"
    ARM_MAX_AGE_DAYS = 1.0

# Set by ralph-loop-stop.sh when it calls us as it ends a loop. It has already
# decided the loop is over and deleted its own state, so the ownership and
# handover checks must not make us stand down — that call IS the handover.
HANDOVER = os.environ.get("CLAUDE_DOD_HANDOVER") == "1"
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
        parsed = json.loads(p.stdout)
    except ValueError:
        return None, (p.stderr or "dod-check emitted unparseable JSON")[:200]
    if not isinstance(parsed, dict):
        # A list, string or number here would make every result.get() below
        # raise AttributeError, crash the hook, and end the turn unguarded.
        return None, f"dod-check emitted {type(parsed).__name__}, expected an object"
    return parsed, None


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        allow()

    if not isinstance(payload, dict):
        allow()  # not a hook payload we understand; nothing to key state on
    session = payload.get("session_id") or ""
    if not isinstance(session, str):
        allow()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", session):
        allow()

    armfile = STATE_DIR / f"{session}.arm"
    if not armfile.exists():
        allow()  # nothing was edited — the overwhelmingly common case

    # Report any config value that could not be read, now that logging works.
    # These are parsed at import, where a crash would end the turn unguarded, so
    # they degrade to defaults there and are surfaced here instead of silently.
    for warn in (_MODE_UNKNOWN and
                 f"CLAUDE_DOD_MODE={_MODE_UNKNOWN!r} unrecognised; "
                 "failing closed to active",
                 _MAX_BLOCKS_WARN, _ARM_WARN):
        if warn:
            log(session, {"action": "warn", "why": warn})

    reap(session)

    def _standdown(field):
        """True only for a NON-EMPTY LIST — the documented shape.

        These two fields were trusted by truthiness, so any truthy value stood
        the gate down: `{"session_crons": {"oops": 1}}` and
        `{"background_tasks": "x"}` both silently disabled enforcement while
        logging a clean `allow`. Reproduced. Both fields are undocumented and
        were read out of the installed binary, so their shape is precisely the
        thing not to assume. A shape we do not recognise means we have NOT
        established that work is in flight, and unestablished never means done.
        """
        val = payload.get(field)
        if isinstance(val, list):
            return len(val) > 0
        if val is not None:
            log(session, {"action": "warn",
                          "why": f"{field} has unexpected shape "
                                 f"{type(val).__name__}; ignoring it"})
        return False

    # A turn pausing for live background work is not a turn that finished.
    # Faulting it would fight the very CI watch the skill was told to start.
    if _standdown("background_tasks"):
        log(session, {"action": "allow", "why": "background work in flight"})
        allow()

    # A durable follow-up is already booked.
    if _standdown("session_crons"):
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
    if not HANDOVER:
        if (ralph_dir / f"{session}.md").exists():
            # Consume any handover marker on the way out. Without this the
            # marker ORPHANS on the common path, not a rare one: ralph writes
            # it only inside end_loop, after a transcript poll that can take
            # ~3s, so this invocation almost always reads the state file first,
            # stands down, and leaves the marker behind. The NEXT Stop within
            # the staleness window then consumes it and allows a turn with
            # unfinished work — reproduced, and found independently by two
            # reviewers. A live loop makes any marker meaningless anyway.
            stale_marker = ralph_dir / f"{session}.handover"
            if stale_marker.exists():
                try:
                    stale_marker.unlink()
                except OSError as exc:
                    log(session, {"action": "warn",
                                  "why": f"could not clear stale marker: {exc}"})
            log(session, {"action": "allow", "why": "a ralph loop owns this session"})
            allow()

        # The handover marker closes the race on the turn a loop ENDS. Stop
        # hooks run concurrently, so this invocation and ralph's own call to us
        # overlap; without the marker both could evaluate and the turn got two
        # identical blocks.
        #
        # Airtight by WRITE ORDERING, not by timing. ralph does: write marker,
        # THEN unlink the state file, THEN call us with CLAUDE_DOD_HANDOVER=1.
        # So any invocation that finds the state file gone is reading after the
        # unlink, which is after the marker was written — it cannot miss it.
        # Consumed on read, so it never affects a later turn.
        marker = ralph_dir / f"{session}.handover"
        if marker.exists():
            try:
                fresh = (time.time() - marker.stat().st_mtime) < 300
            except OSError:
                fresh = False
            consumed = True
            try:
                marker.unlink()
            except OSError as exc:
                # A marker we cannot consume will silence this session again on
                # every later turn until it ages out, so standing down on it now
                # compounds a fault we already know about. Log it and DO NOT
                # stand down: one duplicate block is recoverable, a string of
                # silently unguarded turns is not.
                consumed = False
                log(session, {"action": "warn",
                              "why": f"could not consume handover marker, NOT "
                                     f"standing down on it: {exc}"})
            if fresh and consumed:
                log(session, {"action": "allow",
                              "why": "ralph handed this turn to its own gate call"})
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
    except (OSError, ValueError) as exc:
        # ValueError covers UnicodeDecodeError: a corrupt or non-UTF8 armfile
        # raised straight past the OSError-only guard and crashed the hook,
        # ending the turn unguarded. The countfile read one block up already
        # caught ValueError; this one simply omitted it.
        # Armed, but the list of repos is unreadable. Allowing is the only
        # option — there is nothing to check against — but doing it silently
        # would be indistinguishable from a clean pass, which is the exact
        # confusion this tool exists to remove.
        log(session, {"action": "allow",
                      "why": f"armfile unreadable, NOTHING WAS CHECKED: {exc}"})
        allow()

    problems = []
    for repo in repos:
        result, err = run_dod(repo)
        if err:
            problems.append(f"{repo}: {err} — treat as NOT done")
            continue
        # dod-check reports a failed measurement IN BAND: exit 3 sets `error`
        # and leaves `blockers` empty, because its own render() refuses to print
        # a verdict it could not measure. Reading only `blockers` turned that
        # into a block whose body was just the repo path — the partial-
        # measurement presentation dod-check deliberately avoids, reproduced one
        # layer up. Flagged independently by two reviewers.
        if result.get("error"):
            problems.append(
                f"{repo}: COULD NOT MEASURE — {result['error']}\n"
                "  PR and check state were NOT read. This is not a green "
                "result; fix the tooling before believing anything here."
            )
            continue
        if not result.get("applicable") or result.get("done"):
            continue
        raw_blockers = result.get("blockers")
        if not isinstance(raw_blockers, list):
            raw_blockers = []
        lines = []
        for b in raw_blockers:
            # .get with defaults, not indexing: last round validated the top
            # level of dod-check's JSON, and a malformed ITEM crashed the hook
            # by exactly the same mechanism one level down.
            if isinstance(b, dict):
                lines.append(f"  [{b.get('code', '?')}] {b.get('detail', '(no detail)')}")
            else:
                lines.append(f"  [MALFORMED] {str(b)[:120]}")
        if not lines:
            lines = ["  (dod-check reported not-done but listed no blockers)"]
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
        "If a check is still running: book a durable CronCreate follow-up "
        "because it is the right thing to leave for Jonathan — but it will NOT "
        "clear this gate, and pretending otherwise wasted three turns when it "
        "was measured (2026-08-27). A durable cron outlives the session, so it "
        "is not session-scoped and never appears in `session_crons`, which is "
        "one of the two fields this gate stands down for; the other is "
        "`background_tasks`. So: book the follow-up, then say plainly that you "
        "are blocked on the check — the URL, and what you actually observed — "
        "and let this gate stand down at its budget. Do not re-run hoping to "
        "clear it. The follow-up REPORTS — it must not merge, and must not "
        "push anything Jonathan has not already seen. Merging is his click, "
        "and being blocked by this gate is not authorisation to take it.\n\n"
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
