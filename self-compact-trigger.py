#!/usr/bin/env python3
"""Stop hook: self-compact threshold trigger.

At 30% of the DETECTED context window (~300k on a 1M model) this writes a durable
mission handoff and records a self-compact *request*. `scripts/idle-handoff.sh`
consumes the request and does the reset+resume with its existing guard chain (fork
ledger, per-cwd 24h budget, staleness DENY-when-uncheckable, live-cwd collision
abort, ralph collision).

WHY `Stop`
    Stop is the only hook event that recurs WITHOUT a human turn, and autonomy
    is the whole point: a trigger on UserPromptSubmit can only ever nudge,
    because it needs someone to type first. PreCompact is too late (it fires
    only if a compaction happens at all, and the 70% nudge means the user
    usually hands off instead). PostToolUse recurs but fires tens of times per
    turn for no extra signal. So: Stop, once per assistant turn.

HOW IT COMPOSES WITH ralph-loop's Stop HOOK
    ralph-loop registers its own Stop hook, which emits
    {"decision":"block","reason":<prompt>} to keep a loop running. Claude Code
    merges the outputs of all Stop hooks and a `block` wins. This hook therefore
    NEVER writes to stdout, NEVER emits a `decision`, and NEVER exits non-zero
    (exit 2 on Stop is itself a block). It can only add side effects on disk, so
    it cannot race, veto, or short-circuit ralph-loop's decision in either
    direction. When ralph IS looping, the fork is separately refused downstream
    by idle-handoff.sh's ralph-collision guard.

WHAT "RESET" MEANS HERE
    NOT /compact and NOT /clear. Standing decision, on measured grounds ($3-10
    cold cache-write per event against a 95.5% cache-hit rate), and anyway not
    mechanically possible: no tool emits slash commands, hooks inject text only,
    and there is no programmatic in-process context reset. Reset = the NEXT
    process is a fresh one seeded from the handoff. A live session's context is
    never truncated; this hook only makes the handoff exist while the cache is
    still warm, so the resume is cheap whenever it happens.

NO SWITCH — USER DECISION 2026-08-16
    This used to be gated on `components.self_compact` in the RSI config, off by
    default. That key is deleted and there is no replacement: no config key, no
    env var, no opt-in. Crossing the threshold writes a handoff, full stop.

    What bounds it is not a switch, it is the guard chain that was already there
    and is enumerated above — plus the loop bound below. A flag that ships `false`
    protects nothing: it makes the feature untested in the only configuration that
    matters, and it is the operator, not the code, who then has to be careful. If
    this is not safe to run unconditionally, the correct fix is to make it safe,
    not to hide it behind a key. Do not add one back.

    Note what firing does and does not cost. The handoff write is a haiku call
    (~$0.01-0.03, prefix cached). The expensive part is the opus-5 resume, and that
    is idle-handoff's pre-existing autofork: this trigger only makes a session
    fork-ELIGIBLE sooner than the 45-minute idle floor would. The per-run (3) and
    per-cwd-24h (5) fork budgets, the staleness DENY, and the live-cwd abort all
    still decide whether anything actually spawns.

LOOP BOUND (B6)
    Once per session per threshold crossing, persisted in
    ~/.claude/sessions/<sid>/state.json. A reading below the
    threshold re-arms; only an armed session can fire; MAX_FIRES_PER_SESSION caps
    pathological oscillation. State is written BEFORE the model call, and if the
    state file cannot be written the hook does not fire at all — an unbounded
    trigger is worse than a missing one.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import context_usage  # noqa: E402  (path shim must precede the import)
import inplace_reset  # noqa: E402  (same shim; ships beside this file)

# --- Fixed values, not settings ----------------------------------------------
# Fire at 30% of the DETECTED context window — ~300k tokens on a 1M model, ~60k on
# a 200k one. Deliberately aggressive relative to the 70% advisory nudge in
# context-baggage.py (both are live and independent): the point is to hand off
# while there is still ample runway, not at the ~92% auto-compact cliff.
#
# Hardcoded, and NOT readable from config. It was a config key; that key is gone
# along with the on/off switch it lived beside. Changing this number is a code
# change with a test run behind it, which is the correct amount of friction for a
# value that decides when a machine spends usage.
THRESHOLD = 0.30

# Ceiling on fires per session, for a session that oscillates across the
# threshold: each crossing re-arms (see LOOP BOUND above), so without a cap a
# session hovering at 30% could fire indefinitely. Three is enough for a long
# session that legitimately crosses, sheds context, and crosses again.
MAX_FIRES_PER_SESSION = 3
# Same rule reflect.sh validates, ALL of it: these ids become path components.
#
# The charset half was already here; the leading-dot rejection was not, so the
# claimed parity was not real. lib.sh:is_valid_sid rejects a leading dot for two
# reasons that both apply here: `.`, `..` and `...` are directory references that a
# charset check happily passes (basename(".") == "."), and hidden names make state
# files a human listing the dir will not see. `..` and `.hidden` therefore reached
# the sid became a DIRECTORY NAME under ~/.claude/sessions, so a sid of `..` or
# one containing a slash would escape the tree entirely — the validation matters
# more now than when it only prefixed a filename.
# Contained — the paths stay inside the state dir either way — but a validator that
# says it matches another one has to.
SID_RE = re.compile(r"^(?!\.)[A-Za-z0-9._-]{1,128}$")
# Retention for state/request records. ~590 sessions/week would otherwise
# accumulate two files each, forever.
STATE_RETENTION_DAYS = 30
# Hard ceiling on the hand-off to reflect.sh. It detaches (REFLECT_ASYNC=1) and
# returns in ~1s; this only exists so a pathological child cannot eat Claude
# Code's HARD, unconfigurable 60s hook cap.


# Truthy set, IDENTICAL to lib.sh:reflect_truthy — `1|true|yes|on`,
# case-insensitive, everything else false. This used to be
# `if os.environ.get("REFLECT_SUBPROCESS")`, i.e. truthy-if-non-empty, while
# reflect.sh:59 tested `== "1"`. So `REFLECT_SUBPROCESS=0` meant "not a
# subprocess" to reflect.sh and "subprocess, do nothing" here: the same lever
# silently DISABLED the trigger in one place and enabled the work in the other.
# One semantic, both sides.
_TRUTHY = {"1", "true", "yes", "on"}


def env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def claude_dir() -> Path:
    return Path.home() / ".claude"


def sessions_root() -> Path:
    """`~/.claude/sessions` — self-compact's own tree, one directory per session.

    DELIBERATELY NOT UNDER $REFLECT_ROOT. Self-compact used to live inside the
    session-reflect tree because it reused that subsystem to write its handoff.
    That reuse was the mistake: session-reflect exists to reflect on sessions
    that have ENDED, where an external resumed worker is the only way to see the
    context. Self-compact fires while the session is ALIVE and already holding
    that context. Sharing a root made two subsystems with opposite preconditions
    look like one.

    Layout, one uuid-keyed directory per session:

        ~/.claude/sessions/<sid>/
            cwd            which project this session belongs to. NOT in the
                           fork's write grant -- it decides where a resume runs,
                           so a resumed process must not be able to retarget the
                           next one.
            request.json   the pending self-compact request
            state.json     fire counters for this session
            handoff/
                mission.md the handoff, and the only thing in that directory,
                           ever. It IS writable by a resumed process, which is
                           why nothing else may sit beside it.

    CLAUDE_SESSIONS_DIR exists for test isolation only, exactly as REFLECT_ROOT
    does for session-reflect. It selects a ROOT, never a behaviour.
    """
    env = os.environ.get("CLAUDE_SESSIONS_DIR")
    return Path(env) if env else claude_dir() / "self-compact"


def session_dir(sid: str) -> Path:
    return sessions_root() / sid


def handoff_path(sid: str) -> Path:
    return session_dir(sid) / "handoff" / "mission.md"


def state_dir() -> Path:
    """Where cross-session bookkeeping lives (the log). Per-session state does
    not live here -- it lives in that session's own directory."""
    return sessions_root()


def state_path(sid: str) -> Path:
    return session_dir(sid) / "state.json"


def marker_path_for(sid: str) -> Path:
    return session_dir(sid) / "request.json"


def log(msg: str) -> None:
    """Best-effort audit line. Never raises; never touches stdout."""
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "trigger.log", "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n")
    except Exception:
        pass


def load_state(sid: str) -> dict:
    path = state_path(sid)
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            data.setdefault("armed", True)
            data.setdefault("fires", 0)
            data.setdefault("crossings", 0)
            return data
    except Exception:
        pass
    # No state yet: a fresh session is armed, so the first crossing fires.
    return {"session_id": sid, "armed": True, "fires": 0, "crossings": 0}


def write_json_atomic(path: Path, payload: dict) -> bool:
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
        with open(tmp, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        return False


def gc_state(retention_days: int = STATE_RETENTION_DAYS) -> None:
    """Prune old session directories. Called on fire only — not on every Stop.

    The unit of retention is now the session DIRECTORY, because that is the unit
    of isolation. Age is taken from the newest thing inside it, so a session
    whose handoff was updated by a resume is not collected on the strength of a
    stale state.json written when it first fired.
    """
    cutoff = time.time() - retention_days * 86400
    try:
        entries = list(sessions_root().iterdir())
    except Exception:
        return
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            newest = entry.stat().st_mtime
            for child in entry.rglob("*"):
                try:
                    newest = max(newest, child.stat().st_mtime)
                except Exception:
                    pass
            if newest < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except Exception:
            pass


def spawn_inplace_reset(sid: str, instructions: str = "") -> str:
    """Reset THIS session where it stands, if its terminal can be driven.

    Detached, because it has to outlive the hook: it waits for the handoff to
    land before it clears anything, and the hook must return in well under the
    60s cap.

    This is tried FIRST and the fork path is the fallback, not the reverse. A
    live session resetting itself in place keeps the permissions the user
    already granted interactively and starts no second process, so none of the
    unattended-fork surface applies. When the terminal cannot be driven -- no
    remote control, no unique window, no handoff -- this refuses, logs why, and
    leaves the request marker in place for idle-handoff to pick up once the
    session exits.
    """
    spid = inplace_reset.session_pid()
    if not spid:
        return "no_session_pid"
    handoff = handoff_path(sid)
    script = claude_dir() / "inplace_reset.py"
    if not script.exists():
        return f"missing:{script}"
    argv = [sys.executable, str(script), "--sid", sid, "--session-pid", str(spid),
            "--handoff", str(handoff)]
    if instructions:
        argv += ["--instructions", instructions]
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,   # survive the hook's exit
        )
    except Exception as exc:  # noqa: BLE001
        return f"spawn_error:{type(exc).__name__}"
    return f"queued(pid_target={spid})"


def main() -> int:
    # Fork-chain / recursion breaks. A reflection subprocess carries
    # REFLECT_SUBPROCESS=1; every headless run (including our own autofork)
    # reports a non-cli entrypoint. Either one firing this trigger would create
    # a self-feeding chain of mission writes.
    if env_truthy("REFLECT_SUBPROCESS"):
        return 0
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli") != "cli":
        return 0

    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0

    sid = data.get("session_id") or ""
    transcript = data.get("transcript_path") or ""
    cwd = data.get("cwd") or ""
    if not SID_RE.match(sid) or not transcript or not os.path.exists(transcript):
        return 0
    if not cwd or not os.path.isdir(cwd):
        return 0

    occ = context_usage.occupancy(transcript)
    if occ is None:
        return 0
    if occ.estimated:
        # No recorded usage: firing an expensive path off a byte-count guess is
        # not acceptable.
        return 0
    if not occ.window_known:
        # The DENOMINATOR is a guess: this model family is in neither of
        # context_usage.py's prefix tables, so `window` is the 200k base as an
        # assumption. A 1M session measured against it reads ~5x high and would
        # fire at ~6% real occupancy — spending an opus-5 resume on a session
        # with 940k of runway left. Refuse, and say so loudly enough that the
        # table gets updated rather than silently drifting.
        log(f"sid={sid} REFUSING to fire: context window is UNKNOWN for "
            f"model={occ.model!r} (window_source={occ.window_source}, assuming "
            f"{occ.window} would read ~{occ.fill_pct:.2f} fill). Add the family "
            f"to context_usage.py ONE_M_MODEL_PREFIXES or "
            f"KNOWN_BASE_MODEL_PREFIXES.")
        return 0

    state = load_state(sid)

    if occ.fill_pct < THRESHOLD:
        if not state.get("armed"):
            state["armed"] = True
            state["rearmed_at"] = int(time.time())
            write_json_atomic(state_path(sid), state)
        return 0

    if not state.get("armed"):
        return 0  # already fired for this crossing
    if state.get("fires", 0) >= MAX_FIRES_PER_SESSION:
        if not state.get("capped"):
            state["capped"] = True
            write_json_atomic(state_path(sid), state)
            log(f"sid={sid} capped at {MAX_FIRES_PER_SESSION} fires this session; not firing")
        return 0

    now = int(time.time())
    state["armed"] = False
    state["fires"] = state.get("fires", 0) + 1
    state["crossings"] = state.get("crossings", 0) + 1
    state["last_fired_at"] = now
    state["last_fill_pct"] = occ.fill_pct
    state["window"] = occ.window
    state["threshold"] = THRESHOLD

    # Persist the bound BEFORE spending anything. If this write fails we do not
    # fire: an unbounded trigger is worse than a missing one.
    if not write_json_atomic(state_path(sid), state):
        log(f"sid={sid} could not persist state under {state_dir()}; refusing to fire")
        return 0

    request = {
        "session_id": sid,
        "cwd": cwd,
        "transcript_path": transcript,
        "tokens": occ.total_tokens,
        "window": occ.window,
        "fill_pct": occ.fill_pct,
        "threshold": THRESHOLD,
        "model": occ.model,
        "crossing": state["crossings"],
        "requested_at_epoch": now,
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }
    # THE MARKER IS THE ONLY THING idle-handoff READS. Its write status used to be
    # discarded: the fire state persisted (so the session was `armed: false` and would
    # not retry this crossing), the FIRE line below logged a success, and no marker
    # existed — so idle-handoff never widened the idle gate and the context resume
    # silently degraded to the 45-minute idle floor, indistinguishable from the feature
    # working. Counted in the state file as well as logged: one failed write is a bad
    # afternoon, a rising count is a broken state dir.
    marker_path = marker_path_for(sid)
    marker_written = write_json_atomic(marker_path, request)
    if not marker_written:
        state["request_write_failures"] = state.get("request_write_failures", 0) + 1
        state["last_request_write_failed_at"] = now
        # Best effort: if THIS write fails too the state dir is gone, and the log line
        # below is the whole record.
        write_json_atomic(state_path(sid), state)
        log(f"sid={sid} ERROR could not write the request marker {marker_path} "
            f"(failures={state['request_write_failures']}); the handoff will still be "
            f"written, but idle-handoff has nothing to widen its idle gate with, so the "
            f"resume degrades to the 45-minute idle floor")

    # NO SEPARATE WRITER. The session that crossed the threshold is holding the
    # context, so it writes its own handoff; nothing re-reads the transcript.
    # This used to call session-reflect's `mission` mode, which spawns
    # `claude -p --resume <sid> --model haiku`. That replays the whole session,
    # so the prompt IS the session -- and the trigger fires at 30% of the
    # detected window, ~300k on a 1M model, against haiku's 200k. Measured: a
    # 382,543-token session returned HTTP 400 prompt_too_long after billing
    # $0.23. The write was guaranteed to fail for exactly the sessions this
    # feature exists to serve.
    reset = spawn_inplace_reset(sid)
    log(f"sid={sid} FIRE crossing={state['crossings']} "
        f"tokens={occ.total_tokens}/{occ.window} "
        f"fill={occ.fill_pct:.3f} threshold={THRESHOLD} cwd={cwd} "
        f"marker={'ok' if marker_written else 'FAILED'} inplace={reset}")
    gc_state()
    return 0


def manual(instructions: str) -> int:
    """`--manual [instructions]` — an explicit human handoff, via /self-compact.

    This is a SECOND ENTRY POINT, not a switch on the first. Nothing it does
    changes how the Stop-hook path behaves, and no configuration selects between
    them: the hook fires on a measured crossing, a person runs this by typing.

    Two gates the automatic path applies are deliberately absent here, because
    both exist to police a CLAIM that a manual request does not make:

      * the >= THRESHOLD test. The marker's authority in the automatic path is
        "this session measured above the threshold", which is re-measured
        downstream. A human asking to hand off asserts nothing about occupancy,
        and "you are only at 12%" is not a reason to refuse them.
      * MAX_FIRES_PER_SESSION. That cap bounds an automatic path that could
        otherwise re-fire unattended. A person typing the command is the bound.
        Real spend is still bounded downstream where it is actually incurred:
        3 forks per run and 5 per cwd per 24h in idle-handoff.

    Unlike the hook, this writes to stdout and returns a non-zero status on
    failure — it is run by a person who needs to know it did not work.
    """
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not SID_RE.match(sid):
        print("self-compact: CLAUDE_CODE_SESSION_ID is unset or malformed; "
              "cannot identify this session.", file=sys.stderr)
        return 2

    cwd = os.getcwd()
    if not os.path.isdir(cwd):
        print(f"self-compact: cwd {cwd!r} is not a directory.", file=sys.stderr)
        return 2

    # Resolve this session's transcript by sid rather than by "newest file",
    # so a second session in the same project cannot be handed off by mistake.
    matches = sorted((claude_dir() / "projects").glob(f"*/{sid}.jsonl"))
    if not matches:
        print(f"self-compact: no transcript found for session {sid}.", file=sys.stderr)
        return 2
    transcript = str(matches[0])

    now = int(time.time())
    state = load_state(sid)
    state["manual_requests"] = state.get("manual_requests", 0) + 1
    state["last_manual_at"] = now
    write_json_atomic(state_path(sid), state)

    occ = context_usage.occupancy(transcript)
    request = {
        "session_id": sid,
        "cwd": cwd,
        "transcript_path": transcript,
        "manual": True,
        "instructions": instructions,
        "requested_at_epoch": now,
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }
    if occ is not None:
        request.update({"tokens": occ.total_tokens, "window": occ.window,
                        "fill_pct": occ.fill_pct, "model": occ.model})

    marker_path = marker_path_for(sid)
    if not write_json_atomic(marker_path, request):
        print(f"self-compact: could not write the request marker at {marker_path}; "
              "nothing will pick this handoff up.", file=sys.stderr)
        return 1

    reset = spawn_inplace_reset(sid, instructions)

    fill = f"{occ.fill_pct:.1%}" if occ is not None else "unmeasured"
    log(f"sid={sid} MANUAL request via /self-compact fill={fill} cwd={cwd} "
        f"instructions={'yes' if instructions else 'no'} inplace={reset}")
    print(f"self-compact: handoff requested for session {sid} (context {fill}).")
    print(f"  cwd:      {cwd}")
    print(f"  marker:   {marker_path}")
    print(f"  in-place: {reset}")
    if reset.startswith("queued"):
        print("This session will clear itself shortly and resume from the handoff.")
    else:
        print("This terminal cannot be driven, so nothing will clear this session; "
              "the handoff will be picked up after you exit it.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--manual":
        # Errors here are reported, not swallowed: a person is reading.
        sys.exit(manual(" ".join(sys.argv[2:]).strip()))
    # A Stop hook that exits non-zero BLOCKS the stop and feeds stderr back to
    # the model. Nothing in here is worth doing that for.
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
