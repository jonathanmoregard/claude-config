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

import fcntl
import json
import math
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
    # NaN and the infinities parse fine and then defeat every comparison that
    # follows: `nan < floor` is False and `nan > ceiling` is False, so a NaN
    # clamps to nothing at all. MEASURED 2026-08-30, both halves:
    # ARM_MAX_AGE_DAYS=nan made `st_mtime >= nan` False for every file, and ONE
    # sweep unlinked six other sessions' .arm and .blocks — disarming their
    # gates, and silently, because an unarmed session's Stop hook is the one
    # turn-ending path that deliberately writes no log line. TOTAL_BUDGET=nan
    # cleared both the floor and the slot ceiling and ran 32s inside a 20s slot.
    if isinstance(val, float) and not math.isfinite(val):
        return default, f"{name}={raw!r} is not a finite number; using {default}"
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
# Path agreed with scripts/ralph-loop-state.sh via the env var, never by copying
# the literal — that file is the single source of truth and says so.
RALPH_DIR = pathlib.Path(
    os.environ.get("CLAUDE_RALPH_STATE_DIR",
                   pathlib.Path.home() / ".local/state/claude-ralph-loop"))

ARM_MAX_AGE_DAYS, _ARM_WARN = _num_env(
    "CLAUDE_DOD_ARM_MAX_AGE_DAYS", 14.0, float)
if ARM_MAX_AGE_DAYS != 0 and ARM_MAX_AGE_DAYS < 1:
    _ARM_WARN = f"CLAUDE_DOD_ARM_MAX_AGE_DAYS={ARM_MAX_AGE_DAYS} below floor 1; using 1"
    ARM_MAX_AGE_DAYS = 1.0

# Set by ralph-loop-stop.sh when it calls us as it ends a loop. It has already
# decided the loop is over and deleted its own state, so the ownership check
# must not make us stand down, and the block budget must not silence us: that
# call is the LAST thing that measures this work.
HANDOVER = os.environ.get("CLAUDE_DOD_HANDOVER") == "1"
DOD_TIMEOUT = 60

# TOTAL wall clock for this hook, bounded BELOW the timeout it is registered
# with in settings.json (60s today). DOD_TIMEOUT above is spent PER ARMED REPO
# inside main()'s loop, so the hook could legally run 60s x N while the harness
# kills it at 60s — which puts its own fail-closed TimeoutExpired branch ABOVE
# the point the kill lands. MEASURED 2026-08-29: two armed repos with a 45s
# dod-check ran 90.08s, and a SIGTERM at 60s left EMPTY stdout, no gate.log
# line and no .blocks file — the turn ended unguarded and silent. With one repo
# and a 61s check the gate's own timeout fires at 60.12s and loses that race by
# 120ms, so the fail-closed path only ever worked when nothing enforced the
# registered timeout. A budget that outlives its harness slot is not a budget.
# Below this there is no point starting another dod-check: it cannot finish, and
# a dod-check killed at 1s reports the same "did not run" as a real timeout
# while costing a second of the budget the remaining repos need.
MIN_SLICE = 3.0

_SELF = pathlib.Path(__file__).name

_EXIT_FIELDS = {
    "QUESTION": {"repo", "outcome", "question"},
    "BLOCKED": {"repo", "outcome", "blocker", "needed"},
    "PRESERVE": {"repo", "outcome", "reason", "owner", "next"},
    "ABANDON": {"repo", "outcome", "reason", "remaining_work"},
}


def parse_exit_envelope(raw):
    """Parse one visible, runtime-scoped explanation of unfinished work."""
    if not isinstance(raw, str):
        return None, "last_assistant_message is not a string"
    lines = [line.rstrip() for line in raw.splitlines() if line.strip()]
    if len(lines) < 3:
        return None, "missing a visible explanation and closing DOD-EXIT envelope"
    marker = lines[-2]
    prefix = "DOD-EXIT: "
    if not marker.startswith(prefix):
        return None, "the penultimate non-empty line is not DOD-EXIT"
    try:
        data = json.loads(marker[len(prefix):])
    except (TypeError, ValueError):
        return None, "DOD-EXIT is not valid one-line JSON"
    if not isinstance(data, dict) or set(data) != {"outcomes"}:
        return None, "DOD-EXIT must contain only an outcomes array"
    records = data["outcomes"]
    if not isinstance(records, list) or not records:
        return None, "DOD-EXIT outcomes must be a non-empty array"

    seen = set()
    kinds = []
    for index, record in enumerate(records):
        where = f"DOD-EXIT outcome {index + 1}"
        if not isinstance(record, dict):
            return None, f"{where} is not an object"
        kind = record.get("outcome")
        if kind not in _EXIT_FIELDS:
            return None, f"{where} has unknown outcome {kind!r}"
        if set(record) != _EXIT_FIELDS[kind]:
            return None, f"{where} does not have the exact {kind} fields"
        for key, value in record.items():
            if not isinstance(value, str) or not value.strip():
                return None, f"{where}.{key} must be a non-empty string"
        repo = record["repo"]
        if not pathlib.Path(repo).is_absolute():
            return None, f"{where}.repo must be an absolute path"
        if repo in seen:
            return None, f"DOD-EXIT repeats repository {repo}"
        seen.add(repo)
        if kind == "QUESTION" and not record["question"].rstrip().endswith("?"):
            return None, f"{where}.question must end with ?"
        kinds.append(kind)

    expected_state = (
        "QUESTION" if "QUESTION" in kinds
        else "BLOCKED" if "BLOCKED" in kinds
        else "DONE"
    )
    if lines[-1] != expected_state:
        return None, f"DOD-EXIT requires final state marker {expected_state}"
    return records, None


def validate_exit_coverage(records, blocked_repos):
    explained = {record["repo"] for record in records}
    blocked = set(blocked_repos)
    missing = sorted(blocked - explained)
    extra = sorted(explained - blocked)
    if not missing and not extra:
        return None
    parts = []
    if missing:
        parts.append("missing blocked repo(s): " + ", ".join(missing))
    if extra:
        parts.append("repo(s) not currently blocked: " + ", ".join(extra))
    return "; ".join(parts)


def _registered_slot():
    """The timeout the harness will actually enforce on this hook.

    The budget below is meaningless unless it stays UNDER the registered
    timeout, and two numbers that must agree with nothing keeping them agreeing
    is the footgun three independent reviewers flagged in one round. MEASURED
    2026-08-30: CLAUDE_DOD_TOTAL_BUDGET=90 — a plausible operator value once a
    session arms two repos — ran 80.34s inside a 60s slot and was killed with
    EMPTY stdout, no gate.log line and no countfile, which is precisely the
    fail-open the budget was added to remove. Nothing warned, because the only
    budget message the hook could emit was the floor one.

    So the hook reads its own registration rather than trusting whoever set the
    env var to have also read settings.json. Every failure path falls back to
    60s: Claude Code's default for a hook with no `timeout` key, and the value
    registered here today.
    """
    path = pathlib.Path(os.environ.get(
        "CLAUDE_DOD_SETTINGS", pathlib.Path.home() / ".claude/settings.json"))
    try:
        data = json.loads(path.read_text())
        found = [h.get("timeout", 60)
                 for group in data.get("hooks", {}).get("Stop", [])
                 for h in (group.get("hooks") or [])
                 if _SELF in str(h.get("command", ""))]
    except (OSError, ValueError, AttributeError, TypeError):
        return 60.0, "could not read this hook's registered timeout; assuming 60s"
    if not found:
        # Not registered as a Stop hook in that file at all: a test harness, a
        # manual run, or a different settings file. 60s is the harness default.
        return 60.0, None
    try:
        return float(min(found)), None
    except (TypeError, ValueError):
        return 60.0, "the registered timeout is not a number; assuming 60s"


HARNESS_SLOT, _SLOT_WARN = _registered_slot()
# Margin for interpreter start-up, the stdin read, the reap sweep and writing
# the block itself. Measured: the default budget lands its block at 45.06s in a
# 60s slot, so 5s is comfortable rather than tight.
SLOT_MARGIN = 5.0
_CEILING = max(MIN_SLICE, HARNESS_SLOT - SLOT_MARGIN)
# The floor is MIN_SLICE, not something smaller: a budget below one slice can
# never measure ANY repository. Reproduced with CLAUDE_DOD_TOTAL_BUDGET=1, which
# clamped to the old 2.0 floor and returned "NOT MEASURED" for a repo whose
# dod-check was never even spawned — fail-closed, but on a reason no amount of
# work could clear, and after MAX_BLOCKS such turns the gate stands down anyway.
TOTAL_BUDGET, _BUDGET_WARN = _num_env(
    "CLAUDE_DOD_TOTAL_BUDGET", min(45.0, _CEILING), float, floor=MIN_SLICE)
if TOTAL_BUDGET > _CEILING:
    _BUDGET_WARN = (
        f"CLAUDE_DOD_TOTAL_BUDGET={TOTAL_BUDGET:g} outlives the "
        f"{HARNESS_SLOT:g}s slot this hook is registered with; using "
        f"{_CEILING:g}. A budget that outlives its slot is not a budget — the "
        "harness kills the hook with nothing on stdout and nothing in this log.")
    TOTAL_BUDGET = _CEILING


def _guard_shape(payload, key):
    """What a stand-down field actually looked like, without trusting it.

    This used to be `len(payload.get(k) or [])` inline. It raises TypeError on
    any truthy scalar — a bool, an int, a float — and it is evaluated as an
    ARGUMENT to block(), so it fired BEFORE block() logged anything and before
    it printed the decision. MEASURED 2026-08-30 with `background_tasks: true`:
    exit 1, EMPTY stdout, no decision line in gate.log, and a block-budget slot
    spent on a turn that guarded nothing.

    It was unreachable on master, where a truthy scalar stood the gate down by
    bare truthiness at the guard itself. Hardening _standdown() to demand a real
    list is what made the latent crash reachable — the line was never touched.
    The suite missed it because its two malformed shapes, a string and a dict,
    are the only bad shapes that HAVE a len().
    """
    if key not in payload:
        return "absent"
    val = payload.get(key)
    if isinstance(val, list):
        return len(val)
    return f"not-a-list:{type(val).__name__}"


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
        # Nor a session with a LIVE loop. A long loop can outlive the reap age
        # while still working, and taking its armfile away means its eventual
        # handover finds nothing to check and allows — turning a housekeeping
        # sweep into a silent un-gating of the longest-running sessions.
        try:
            if (RALPH_DIR / f"{path.stem}.md").exists():
                continue
        except OSError:
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


def run_dod(repo, timeout=DOD_TIMEOUT):
    try:
        p = subprocess.run(
            [sys.executable, str(DOD), "--repo", repo, "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
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
    # The four paths below all end the turn without checking anything, because
    # without a usable session id there is no armfile to look up and therefore
    # nothing this gate can measure. That is unavoidable — but it was also
    # SILENT, which made "the harness sent us something we could not read"
    # indistinguishable from "this session had no work to check". Every other
    # allow in this file logs; a full sweep of the exit paths found these four
    # were the last that did not.
    def allow_unidentified(why):
        log("(unidentified)", {"action": "allow", "why": why})
        allow()

    try:
        payload = json.load(sys.stdin)
    except ValueError:
        allow_unidentified("stdin was not valid JSON; NOTHING WAS CHECKED")

    if not isinstance(payload, dict):
        allow_unidentified(
            f"payload was {type(payload).__name__}, not an object; "
            "NOTHING WAS CHECKED")
    session = payload.get("session_id") or ""
    if not isinstance(session, str):
        allow_unidentified(
            f"session_id was {type(session).__name__}, not a string; "
            "NOTHING WAS CHECKED")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", session):
        allow_unidentified(
            "session_id is not a plain token, so it cannot become a filename; "
            "NOTHING WAS CHECKED")

    armfile = STATE_DIR / f"{session}.arm"
    if not armfile.exists():
        allow()  # nothing was edited — the overwhelmingly common case

    # Report any config value that could not be read, now that logging works.
    # These are parsed at import, where a crash would end the turn unguarded, so
    # they degrade to defaults there and are surfaced here instead of silently.
    for warn in (_MODE_UNKNOWN and
                 f"CLAUDE_DOD_MODE={_MODE_UNKNOWN!r} unrecognised; "
                 "failing closed to active",
                 _MAX_BLOCKS_WARN, _ARM_WARN, _BUDGET_WARN,
                 _SLOT_WARN):
        if warn:
            log(session, {"action": "warn", "why": warn})

    if HANDOVER:
        # In the audit trail on purpose. This flag is the loop's internal call
        # protocol, but it lives on the same env namespace as the operator
        # knobs, and a stray persistent export would skip BOTH the ownership
        # yield and the block budget on every turn — reviving the two-drivers
        # failure the yield exists to prevent. Nothing here can authenticate the
        # caller, so being noticeable is the whole mitigation.
        log(session, {"action": "note",
                      "why": "CLAUDE_DOD_HANDOVER=1 — ownership check and "
                             "block budget skipped for this call"})

    reap(session)

    # Both Claude Code and Codex set this boolean after a Stop hook has already
    # continued the current turn. It authenticates which Stop chain the final
    # assistant text belongs to; it does NOT establish that the agent resolved
    # or explained the unfinished state. A valid DOD-EXIT is checked against the
    # repositories that remain blocked below. Keep Ralph handover exempt: that
    # is the loop's final delivery measurement, not an ordinary recursive Stop.
    stop_hook_active = payload.get("stop_hook_active")
    exit_records = None
    exit_error = None
    if stop_hook_active is True and not HANDOVER:
        exit_records, exit_error = parse_exit_envelope(
            payload.get("last_assistant_message"))
    if stop_hook_active is not None and not isinstance(stop_hook_active, bool):
        log(session, {"action": "warn",
                      "why": "stop_hook_active has unexpected shape "
                             f"{type(stop_hook_active).__name__}; ignoring it"})

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
    #
    # THERE IS NO HANDOVER MARKER, AND THAT IS THE DESIGN.
    #
    # There used to be one: ralph wrote `<session>.handover` before unlinking
    # its state file, and this gate stood down on a fresh marker, consuming it.
    # Four consecutive review rounds each found a different ordering it did not
    # cover, and the review loop stopped with `self-inflicted-churn` — its own
    # rule for "the design is wrong and more rounds will not converge on it".
    #
    # It was a LEASE (Gray & Cheriton 1989) with a 300s term. A lease is safe
    # only when the protected resource validates a fencing token (Kleppmann,
    # "How to do distributed locking", 2016). The resource here is the Claude
    # Code runtime aggregating two hooks' stdout: it validates nothing and
    # cannot be made to. So no lease built here could have been safe, and
    # adding a fencing token would not have helped — nothing downstream could
    # enforce it. The TTL was worse than arbitrary: a TTL reclaims a claim from
    # a process that died without cleaning up, which is the CROSS-HOST
    # mitigation used where kill(pid,0) is meaningless from another kernel.
    # This is one machine. The mitigation was inherited without the constraint.
    #
    # Nothing replaces it, because the requirement was weaker than the
    # mechanism. MEASURED 2026-08-29: two Stop hooks emitting an identical
    # `decision: block` are tolerated — 22 blocking responses across 11
    # firings, session ended cleanly. So on the one turn where this gate and
    # ralph's inline call can both fire, the cost is a duplicated block
    # message. The marker existed to prevent exactly that, while its own
    # failure — an orphan silently allowing unfinished work — was strictly
    # worse than what it prevented.
    #
    # The state file survives because its lifetime matches its owner: one
    # writer, alive exactly as long as the loop. The marker churned because its
    # lifetime was a sub-turn window read by racing processes. That is the
    # general lesson, and the reason this is a plain read and not a lock — a
    # stale read in either direction is harmless.
    #
    # (A stable per-firing id, `prompt_id`, IS available and identical across
    # hooks, so an O_EXCL claim keyed on it would be implementable if duplicate
    # blocks ever stopped being tolerated. Not needed today; noted so the next
    # person does not have to re-measure it.)
    if not HANDOVER and (RALPH_DIR / f"{session}.md").exists():
        log(session, {"action": "allow", "why": "a ralph loop owns this session"})
        allow()

    countfile = STATE_DIR / f"{session}.blocks"
    try:
        blocks = int(countfile.read_text().strip() or 0)
    except (OSError, ValueError):
        blocks = 0
    # A syntactically valid exit still needs objective per-repo coverage below,
    # even when the ordinary retry budget is spent. Invalid or absent exits use
    # the existing bounded stand-down and cannot make the hook loop forever.
    if blocks >= MAX_BLOCKS and not HANDOVER and exit_records is None:
        log(session, {"action": "allow", "why": f"block budget spent ({blocks})"})
        allow()
    if blocks >= MAX_BLOCKS and HANDOVER:
        # The budget exists to stop the gate arguing with a session turn after
        # turn. A handover is not another turn in that argument: it happens
        # once, when a loop ends, and it is the last chance anything measures
        # this work. Honouring the budget here let a loop that had already
        # spent it end its final turn unmeasured — silently, with the loop
        # state already deleted so nothing would revisit it.
        log(session, {"action": "warn",
                      "why": f"budget spent ({blocks}) but this is the loop's "
                             "handover; checking anyway"})

    try:
        # A SET of repositories, order preserved — not a tally of edits. The arm
        # hook appends after a check-then-append with no lock, and SUBAGENTS
        # SHARE THE PARENT'S session_id, so one repo can legitimately arrive
        # twice; measuring it twice spends the budget the tail of the list
        # needs. finishing-up-arm.sh now locks the armfile, which closes the
        # race at its source — this is the belt, and it also covers an armfile
        # hand-edited, concatenated or restored from a backup.
        seen = set()
        repos = []
        for line in armfile.read_text().splitlines():
            line = line.strip()
            if line and line not in seen:
                seen.add(line)
                repos.append(line)
    except (OSError, ValueError) as exc:
        # ValueError covers UnicodeDecodeError: a corrupt or non-UTF8 armfile
        # raised straight past the OSError-only guard and crashed the hook,
        # ending the turn unguarded. The countfile read one block up already
        # caught ValueError; this one simply omitted it.
        # Armed, but the list of repos is unreadable. Allowing is the only
        # option — there is nothing to check against — but doing it silently
        # would be indistinguishable from a clean pass, which is the exact
        # confusion this tool exists to remove.
        # Armed, and the record of WHAT to check is corrupt. Allowing here
        # ends a turn nobody measured, which is the definition of the failure
        # this gate exists to prevent — the fact that the corruption is ours
        # rather than the repo's does not make the work finished. Block, name
        # it, and let the budget bound the damage if it persists.
        block(f"This session is armed, but its record of which repositories to "
              f"check could not be read ({exc}), so NOTHING was verified. Do "
              f"not treat that as finished: run `python3 {DOD} --repo <repo>` "
              f"for the repositories you touched and report what it says.",
              session, why=f"armfile unreadable, NOTHING WAS CHECKED: {exc}")

    # Spend the budget in a ROTATED order. Fixed armfile order starves the
    # same tail every turn: with a slow first repository, the last one is
    # reported NOT MEASURED turn after turn and has no path to ever being read —
    # the block text tells the agent to re-run it by hand, which is the closing
    # ceremony this system exists to remove. `blocks` counts the turns that has
    # already happened for, so it is exactly the offset that gives every
    # repository its turn at the front.
    if repos:
        start = blocks % len(repos)
        repos = repos[start:] + repos[:start]

    # An armfile that exists but lists NOTHING is armed-with-no-record, which
    # is the unreadable-armfile case wearing a different hat — and it already
    # blocks. MEASURED 2026-08-30: under a write-size limit the arm hook leaves
    # a 0-byte armfile behind, and the gate read it as zero repositories, took
    # the clean-pass branch, deleted the file and ended the turn with no output
    # and no log line.
    if not repos:
        block(f"This session is armed, but its record of which repositories to "
              f"check is EMPTY, so NOTHING was verified. That is not a clean "
              f"pass — the arm hook writes that file, and an empty one means it "
              f"failed. Run `python3 {DOD} --repo <repo>` for the repositories "
              f"you touched and report what it says.",
              session, why="armfile empty, NOTHING WAS CHECKED")

    problems = []
    problem_repos = []
    # What dod-check could not ESTABLISH, as opposed to what it found wrong.
    # These never block — blocking on them would hang every offline session —
    # but they must not live only in gate.log, which no agent ever reads.
    unmeasured = []
    # One budget for the whole hook, not one per repo. The repo that the budget
    # runs out on is NOT MEASURED, which is a problem like any other — so the
    # hook blocks, in its own slot, instead of being killed mid-loop with
    # nothing on stdout and nothing in the log.
    deadline = time.monotonic() + TOTAL_BUDGET
    for repo in repos:
        remaining = deadline - time.monotonic()
        if remaining < MIN_SLICE:
            problem_repos.append(repo)
            problems.append(
                f"{repo}: NOT MEASURED — this gate's own {TOTAL_BUDGET:g}s "
                "budget was spent before this repository was reached, so its "
                "state is unknown.\n"
                "  That is not a green result. Re-run "
                f"`python3 {DOD} --repo {repo}` yourself, or arm fewer "
                "repositories per session."
            )
            continue
        result, err = run_dod(repo, timeout=min(DOD_TIMEOUT, remaining))
        if err:
            problem_repos.append(repo)
            problems.append(f"{repo}: {err} — treat as NOT done")
            continue
        # dod-check reports a failed measurement IN BAND: exit 3 sets `error`
        # and leaves `blockers` empty, because its own render() refuses to print
        # a verdict it could not measure. Reading only `blockers` turned that
        # into a block whose body was just the repo path — the partial-
        # measurement presentation dod-check deliberately avoids, reproduced one
        # layer up. Flagged independently by two reviewers.
        if result.get("error"):
            problem_repos.append(repo)
            problems.append(
                f"{repo}: COULD NOT MEASURE — {result['error']}\n"
                "  PR and check state were NOT read. This is not a green "
                "result; fix the tooling before believing anything here."
            )
            continue
        # dod-check demotes several FAILED MEASUREMENTS to warnings while
        # still reporting done=true — "unresolved review threads not counted",
        # "could not list PRs for this branch", a stale fetch. This gate reads
        # only error/done/blockers, so "could not count" and "counted zero"
        # were identical to it. Blocking on them would hang every offline
        # session, so they are LOGGED instead: the audit trail is where "green
        # as far as I got" has to remain visible.
        warns = result.get("warnings")
        if isinstance(warns, list) and warns:
            log(session, {"action": "warn",
                          "why": f"{repo}: dod-check could not establish: "
                                 + " | ".join(str(w)[:120] for w in warns[:5])})
            unmeasured.append(f"{repo}: " + " | ".join(
                str(w)[:160] for w in warns[:5]))
        # `applicable` must be an explicit False, and a skip is never silent.
        # Two failures met here. A dod-check emitting `{}` passes the dict check
        # in run_dod, and `not {}.get("applicable")` is True — so a malformed
        # payload read as "the definition of done does not apply here", the repo
        # was skipped with no log line, and the armfile was unlinked and the
        # turn allowed. The same silence hid a MISSING GIT, which answers n/a
        # for every repository on the machine: measured end to end, the only
        # variable being whether git was on PATH, and the turn went from
        # blocked-and-named to allowed-and-silent.
        if result.get("applicable") is False:
            log(session, {"action": "note",
                          "why": f"{repo}: dod-check says the definition of done "
                                 "does not apply here — "
                                 f"{result.get('reason') or 'no reason given'}"})
            continue
        if "applicable" not in result:
            problem_repos.append(repo)
            problems.append(
                f"{repo}: dod-check emitted no `applicable` field, so its "
                "verdict could not be read — treat as NOT done")
            continue
        if result.get("done"):
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
        problem_repos.append(repo)
        problems.append(f"{repo}:\n" + "\n".join(lines))

    if not problems:
        # THE ARMFILE SURVIVES A CLEAN PASS. It used to be unlinked here, and
        # that is how a clean instance DISARMED a session its sibling was still
        # blocking.
        #
        # Two gate instances run on the loop's final turn — ralph's inline
        # handover call and the standalone Stop entry — over the same unlocked
        # state. MEASURED 2026-08-30: with both running the REAL predicate
        # against the same healthy repo and only a flaky transport between them,
        # 20 of 40 concurrent pairs reached DIFFERENT verdicts, and EVERY
        # divergence disarmed the session. Once the armfile is gone the gate
        # returns at `if not armfile.exists(): allow()` — before any logging
        # exists — so every later turn ends allowed, silent, and unrecorded.
        # The disarm event itself writes no log line either.
        #
        # Deleting nothing here removes the whole class without inventing a
        # protocol between the two instances. That was the alternative, and it
        # is the shape the ADOPT step already deleted once: a coordination
        # mechanism whose correctness nothing downstream can enforce. The
        # runtime still validates nothing, and `prompt_id` would make it
        # implementable, not safe.
        #
        # What replaces the cleanup: the armfile is reaped by AGE (see reap()),
        # and the arm hook re-touches its mtime on every edit, so a session that
        # goes quiet is collected on the ordinary schedule. The cost is that a
        # clean armed session re-measures on each later turn — which is this
        # gate's job, at ~200ms per clean repo — and the benefit is that no
        # concurrent verdict can ever leave a session unguarded.
        #
        # The COUNTFILE still goes: it is this session's block budget, a clean
        # pass is exactly when it should reset, and losing that race costs at
        # most one block slot rather than the whole gate.
        try:
            countfile.unlink(missing_ok=True)
        except OSError as exc:
            # Not swallowed. A budget that cannot be reset means the NEXT block
            # in this session starts from a stale count and the stand-down
            # arrives early — quietly, on a session that had just passed clean.
            # Small, but this whole file exists because "could not" and "did
            # not" must not print the same way.
            log(session, {"action": "warn",
                          "why": f"clean pass, but the block budget could not be "
                                 f"reset ({exc}); the next block in this session "
                                 "starts from a stale count"})
        if unmeasured:
            # The work is done as far as anything could tell — but something was
            # NOT established, and until now that fact reached only gate.log, a
            # file no agent reads. Blocking on it would hang every offline
            # session, which is why this is a systemMessage and not a decision:
            # the turn ends, and the agent is told what nobody measured.
            # Reproduced 2026-08-30: a token missing scopes makes the
            # review-thread read fail PERMANENTLY, so "no unresolved threads"
            # and "could not find out" printed the same green verdict forever.
            print(json.dumps({"systemMessage":
                              "[dod] the turn is not blocked, but dod-check "
                              "could not establish: " + " ; ".join(unmeasured)
                              + " — say so rather than reporting a clean sheet."}))
        allow()

    if exit_records is not None and not HANDOVER:
        exit_error = validate_exit_coverage(exit_records, problem_repos)
        if exit_error is None:
            try:
                countfile.unlink(missing_ok=True)
            except OSError as exc:
                exit_error = f"block budget could not be reset ({exc})"
            else:
                log(session, {
                    "action": "allow",
                    "why": "explicit terminal explanation for current Stop chain",
                    "outcomes": sorted({r["outcome"] for r in exit_records}),
                    "repos": sorted(r["repo"] for r in exit_records),
                })
                allow()

    if blocks >= MAX_BLOCKS and not HANDOVER and not (
            exit_error and exit_error.startswith("block budget could not be reset")):
        log(session, {"action": "allow", "why": f"block budget spent ({blocks})"})
        allow()

    if stop_hook_active is True and not HANDOVER and exit_error:
        problems.append(f"DOD-EXIT rejected: {exit_error}")

    # Read-modify-write under an exclusive lock. Both instances read the same
    # `blocks` earlier and both wrote read+1: MEASURED, two blocks 29ms apart
    # consumed ONE budget slot. The lock is held for a file read and a file
    # write, so it cannot meaningfully delay either instance — this is not the
    # gate body being serialised, which would put two full measurements inside
    # one harness slot.
    try:
        with open(countfile, "a+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass  # no locking on this filesystem: the old lost-update risk
            fh.seek(0)
            try:
                current = int((fh.read() or "0").strip() or 0)
            except ValueError:
                current = blocks
            fh.seek(0)
            fh.truncate()
            fh.write(str(current + 1))
    except OSError as exc:
        # Swallowing this froze two things at once: the block budget never
        # advances, so the MAX_BLOCKS stand-down the reason text promises
        # "regardless" never arrives and the session is blocked forever; and the
        # rotation offset is derived from the same counter, so the tail of the
        # armfile is re-starved every turn. Reaching here means STATE_DIR is
        # readable but not writable — plausible, and previously undetectable.
        log(session, {"action": "warn",
                      "why": f"could not record this block ({exc}); the budget "
                             "cannot advance, so this gate will keep blocking "
                             "and the rotation offset is frozen"})

    body = "\n".join(problems)
    reason = (
        "Work in this session is not finished. `dod-check` reports:\n\n"
        f"{body}\n\n"
        "Invoke the `finishing-up` skill and choose the correct terminal "
        "outcome before acting on checker codes:\n"
        "- Ship: deliver this work upstream, then work its blockers to green "
        "and mergeable.\n"
        "- Preserve: retain it only when Jonathan explicitly wants it kept; "
        "report the exact branch, worktree, commit, and measured blockers.\n"
        "- Abandon: discard it only after Jonathan explicitly approves "
        "deletion; resolve the exact targets first.\n"
        "Never push or open a PR solely to clear this gate. Never delete work "
        "without explicit user approval. Act now when the outcome is already "
        "authorized; otherwise ask one focused outcome question.\n\n"
        f"Re-check with: python3 {DOD} --repo <repo>\n"
        "That exact command, not a bare `dod-check` — it is not on PATH. "
        "Reading the conditions yourself and concluding you are done is the "
        "self-attestation this gate exists to replace.\n\n"
        "If an external check is still running, create one durable reporting "
        "follow-up, then report its URL/state and what you observed. That "
        "follow-up must only report: it must not merge or push unseen work, "
        "and it does not clear this session gate.\n\n"
        "For a credential, interactive login, outcome choice, taste call, or "
        "merge click, state once what you measured and the human action needed. "
        "Include a PR URL only when one exists. Do not rerun unchanged checks "
        "or repeat the same question.\n\n"
        "To end the current Stop chain without shipping, first explain the "
        "outcome to Jonathan, then end with one-line JSON and its state marker:\n"
        "DOD-EXIT: {\"outcomes\":[<one exact record per blocked repo>]}\n"
        "Question uses {repo,outcome,question} + QUESTION; Blocked uses "
        "{repo,outcome,blocker,needed} + BLOCKED; Preserve uses "
        "{repo,outcome,reason,owner,next} + DONE; Abandon uses "
        "{repo,outcome,reason,remaining_work} + DONE. Values are strings; repo "
        "is the absolute path above. Abandon does not authorize deletion. The "
        "exit applies only to this current Stop chain; the next user turn is "
        "measured again. "
        f"This gate stands down after {MAX_BLOCKS} blocks regardless."
    )
    block(
        reason,
        session,
        body.replace("\n", " | ")[:400],
        guards={k: _guard_shape(payload, k)
                for k in ("background_tasks", "session_crons")},
    )


if __name__ == "__main__":
    main()
