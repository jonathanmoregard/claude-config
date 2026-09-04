#!/usr/bin/env python3
"""Reset a LIVE session in place: it writes its own handoff, clears, resumes.

THE SEQUENCE, and why it is in this order:

    1. ask the session to write its handoff to
       ~/.local/state/claude-self-compact/<sid>/handoff/mission.md — userspace,
       NOT under ~/.claude, because that whole tree is Claude Code's own config
       and state surface and the agent's working state is not the CLI's to own
    2. wait until that file appears AND is newer than the request
    3. wait for the turn that wrote it to END. The file lands at the session's
       Write TOOL CALL, and the turn runs on after it; text arriving mid-turn is
       queued by the TUI rather than parsed, so a `/clear` sent here is not a
       command at all
    4. /clear
    5. a pickup message naming only that file

Step 1 is the whole point. The session crossing the threshold is ALREADY holding
the context, so it writes the summary itself: no second model call, no
re-ingestion, no context window ceiling. The previous design handed this to
session-reflect's `mission` mode, which spawns `claude -p --resume <sid> --model
haiku`. That replays the entire session, so the prompt IS the session — and the
trigger fires at 300k tokens, against haiku's 200k window. Measured: a 382,543-token session returned HTTP 400
prompt_too_long after billing $0.23. It was guaranteed to fail for exactly the
sessions this exists to serve.

Step 2 is also the liveness probe, deliberately. Observing the session produce a
fresh file proves it was idle, that the channel reaches it, and that it acted —
BEFORE anything is destroyed. If the user was mid-keystroke, the instruction is
mangled, no fresh file appears, and we time out having cleared nothing.

That wait is bounded by the session going IDLE, not by a fixed duration. An
injected instruction cannot be read until the current turn ends, so a session
that is mid-tool-call when the threshold is crossed needs however long that call
takes, plus a turn. A flat deadline instead measures what the session happened
to be doing, and threw away a correct handoff for arriving 111 seconds late.

WHAT AN AGENT CANNOT DO, and why this is out of process: a session cannot clear
itself. No tool emits slash commands and hooks inject text only. But a session
is a process in a terminal, and kitty here runs with `allow_remote_control yes`.
Verified, not assumed: `kitten @ send-text` delivers into a window's stdin, `\\r`
arrives as Enter, and window-resolution-by-pid returns the right window and
refuses a pid matching none.

WHAT THIS IS NOT. It is not a sandbox, and it does not need to be. The successor
is THIS session continuing with less context: same process, same user, same
permissions, same everything it was already carrying a moment earlier. Clearing
hands nobody anything they did not already have, so there is deliberately no cwd
restriction, no reduced tool set, and no "treat your own notes as untrusted"
framing. An earlier revision had all three, ported from the unattended fork path
where the reader is a DIFFERENT process running with acceptEdits and nobody
watching. That comparison was simply the wrong baseline.

The guards that remain are about the MECHANISM working, not about containing the
session:
  * one window, matched on the session's own pid via process ancestry. Zero or
    more than one match is a refusal — the failure mode is typing into a
    DIFFERENT live session, which is someone else's problem, not this one's.
  * the pid is re-verified immediately before EVERY send. Between `ls` and
    `send-text` the session can exit and leave a shell owning that window, and
    the text would then be executed as commands.
  * nothing is cleared unless a FRESH handoff exists. Existence alone is not
    enough: a handoff from an earlier crossing would otherwise pass instantly
    and we would clear a session to resume it from obsolete state.
  * the clear is not sent until the session is out of its turn, because input
    arriving mid-turn is queued as ordinary text and never reaches the command
    parser.
  * the clear is confirmed before the pickup is sent, because `/clear` typed
    into a prompt that already holds text is swallowed rather than run.
  * one reset per session at a time, so two runs cannot interleave.

EVERY TERMINAL PATH REPORTS BACK, machine-readably, in <sid>/outcome.json
(write_outcome). The trigger disarms a session when it fires, and an abort here
destroys nothing — but with no machine channel the trigger could not tell an
abort from a success, so `armed:false` was final and the session never retried:
sid 6f11dbf0, 2026-09-04, fired once at 409k tokens, aborted still-mid-turn,
and self-compact was silently gone for the rest of the session. The log above
stays human-forensic (its wording changes freely); outcome.json is the one
record self-compact-trigger.py's bounded re-arm reads. It is a THIRD channel
beside the request marker and the handoff, and it must not disturb either:
success still renames request.json -> consumed.json, an abort still KEEPS the
marker for idle-handoff.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

CLAUDE_EXE_TAILS = ("claude", "claude-code", ".claude-wrapped")
# Same shape the trigger enforces: no leading dot, no slash, no `..`.
SID_RE = re.compile(r"^(?!\.)[A-Za-z0-9._-]{1,128}$")

# How long an IDLE session gets to write its handoff before we conclude it never
# will. The clock only runs while the session is idle: writing the handoff is one
# summarisation turn on a context it already holds, so an idle session that has
# not produced one in this long is not going to.
#
# MEASURED, do not turn this back into a total budget. As a flat 240s deadline
# this fired at 19:36:10 against a session that was mid-turn, gave up at
# 19:40:11, and the handoff landed at 19:42 — a correct handoff thrown away for
# being 111 seconds late. The instruction cannot be read until the current turn
# ends, so any fixed budget is really a bet on what the session happens to be
# doing when the threshold is crossed.
WRITE_IDLE_GRACE_SECONDS = 240
# Ceiling on the whole wait, so a session that never stops working cannot pin a
# waiter forever.
WRITE_WAIT_CAP_SECONDS = 900
# A clear ends the CONTEXT, not the WORK. Subagents this session dispatched keep
# running, and the clear interrupts every one of them mid-flight -- their
# transcripts end `[Request interrupted by user]`, which is the clear and not the
# operator. They are also hard to recover afterwards: `SendMessage` resolves a
# subagent transcript inside the CURRENT session's directory only, and a clear
# starts a new session id, so resuming one by id fails with "No transcript found"
# until the files are moved by hand.
#
# MEASURED 2026-08-20: three review agents were killed exactly this way, each
# carrying hundreds of KB of accumulated reading and reproduction work, none of
# it recorded anywhere the successor could find. So the reset now DRAINS before
# it clears.
INTERRUPTED_MARK = "[Request interrupted by user]"
# Ceiling on the drain. A subagent that never finishes must not pin a waiter
# forever -- but the reset REFUSES at the ceiling rather than clearing anyway,
# because clearing is what destroys the work. THREE HOURS, set by the operator
# 2026-08-20: a review or fix agent legitimately runs over an hour, and waiting
# out a slow one costs only time while cutting it short costs the work.
SUBAGENT_WAIT_CAP_SECONDS = 3 * 60 * 60
# Backstop only. A transcript neither finished nor appended to in this long is
# treated as a dead agent, so a previous clear's casualty cannot hold the drain
# open for the full ceiling. Deliberately generous against the longest observed
# gap: a single tool call can run 10-15 minutes without appending anything.
SUBAGENT_STALE_SECONDS = 30 * 60
SUBAGENT_POLL_SECONDS = 5
# THE HANDOFF LANDING IS NOT THE TURN ENDING, and the wait above stops at the
# landing. The file appears at the session's Write TOOL CALL; the turn runs on
# afterwards while the model finishes its closing message.
#
# MEASURED 2026-08-27, sid f90449b2: Write at 14:26:52, file on disk 14:26:54,
# `end_turn` at 14:27:03 — an 11-second tail. `/clear` went out at 14:26:55,
# inside it. Text arriving in a live turn is QUEUED by the TUI rather than
# parsed, so it was never a command: that session's transcript recorded
# `queue-operation enqueue` carrying the whole write-instruction with `/clear`
# stuck on the end (905 chars), then `remove`. The session id never changed, the
# confirmation below caught it, and the reset refused — correctly, but having
# spent a whole handoff turn for nothing.
#
# So the clear waits for the session to leave its turn. Bounded, because the
# instruction it is answering says "write the file and then stop": a tail longer
# than this is not the tail of that turn, and sending into it would only
# reproduce the failure.
CLEAR_IDLE_WAIT_SECONDS = 120
# Between the turn ending and `/clear`: the TUI has to finish rendering the last
# of the turn and settle on an empty prompt. Same reason as CLEAR_SETTLE_SECONDS
# below, at the other end of the command.
TURN_SETTLE_SECONDS = 1.5
# Gap between `/clear` and the pickup: the TUI has to process the command and
# return to an empty prompt.
CLEAR_SETTLE_SECONDS = 2.0
# How long to wait for the CLI's own evidence that /clear took effect.
CLEAR_CONFIRM_SECONDS = 30


def sessions_root() -> Path:
    env = os.environ.get("CLAUDE_SESSIONS_DIR")
    return Path(env) if env else Path.home() / ".local" / "state" / "claude-self-compact"


def handoff_path(sid: str) -> Path:
    return sessions_root() / sid / "handoff" / "mission.md"


def registry_session_id(spid: int) -> str | None:
    """The session id Claude Code currently associates with this pid, or None.

    Used ONLY to confirm, after the fact, that a `/clear` we sent took effect: a
    clear keeps the process and starts a NEW session, so the id changes. It is
    never consulted to decide whether to send anything.

    CHOSEN ON EVIDENCE over the obvious alternative. Counting transcript files
    looked equivalent and is not: transcripts are written lazily, so a session
    that had been prompted once still showed ZERO transcripts, and the next
    submission made one appear. Measured against a real session, `/clear` into a
    non-empty prompt: transcript count went 0 -> 1 while no clear had happened,
    and the registry id correctly stayed UNCHANGED.

    Note this is deliberately NOT the registry's `status` field. That is
    turn-state, not input-buffer state: a user who has typed but not submitted
    reads as idle, so gating a destructive send on it would license precisely
    the case it appears to guard. Reading `sessionId` after the fact carries no
    such hazard, and an absent or restructured registry degrades to "cannot
    confirm", which keeps the marker and sends nothing.
    """
    try:
        p = Path.home() / ".claude" / "sessions" / f"{spid}.json"
        return json.loads(p.read_text()).get("sessionId") or None
    except Exception:
        return None


def session_is_working(spid: int, sid: str) -> bool:
    """Whether the session is mid-turn. Used ONLY to extend patience.

    DELIBERATELY THE OPPOSITE POLARITY to the `status` caveat in
    registry_session_id. The hazard there is that a user who has typed but not
    submitted reads as idle, so gating a destructive send on `status` would
    license the very case it appears to guard. Here `status` can only make this
    wait LONGER; it never authorises a send. The clear stays gated on a fresh
    handoff and on its own confirmation, so a wrong answer here costs time and
    destroys nothing.

    CHOSEN ON EVIDENCE over watching the transcript grow. The transcript is
    appended at message boundaries, so it sits unchanged for the whole of a
    single long tool call — measured unchanged across 24s of an active turn, and
    a deep research call runs 10-15 minutes. Transcript mtime would therefore
    read "stalled" during exactly the long turns this needs to wait out.

    Any failure to read the registry degrades to False, i.e. to the plain idle
    timeout, which is the behaviour this replaced.
    """
    if not Path(f"/proc/{spid}").exists():
        return False
    try:
        reg = json.loads(
            (Path.home() / ".claude" / "sessions" / f"{spid}.json").read_text())
    except Exception:
        return False
    if reg.get("sessionId") != sid:
        return False
    return reg.get("status") == "busy"


def subagent_transcripts(sid: str) -> list[Path]:
    """Every subagent transcript belonging to this session.

    Globbed across all project directories rather than derived from the session's
    cwd. The session id is already unique, and reconstructing Claude Code's
    project-slug convention from a path is one more way to be quietly wrong.
    """
    out: list[Path] = []
    try:
        for proj in (Path.home() / ".claude" / "projects").iterdir():
            d = proj / sid / "subagents"
            if d.is_dir():
                out.extend(sorted(d.glob("agent-*.jsonl")))
    except Exception:
        pass
    return out


def _transcript_finished(path: Path) -> bool:
    """Whether this subagent has stopped, judged by its own last entry.

    Two terminal states: an assistant turn that ended normally, and an
    interruption -- an agent some earlier clear already killed must not pin this
    one forever.

    CHOSEN OVER mtime, for the reason session_is_working already documents: a
    transcript is appended at message boundaries, so it sits unchanged for the
    whole of one long tool call. Staleness survives only as the backstop in
    live_subagents(), never as the primary signal.

    An unreadable or half-written file counts as finished. Refusing to clear on a
    file we cannot parse would hand any corrupt transcript a permanent veto.
    """
    try:
        last = b""
        with path.open("rb") as fh:
            for line in fh:
                if line.strip():
                    last = line
        if not last:
            return False
        msg = (json.loads(last) or {}).get("message") or {}
    except Exception:
        return True
    if msg.get("role") == "assistant" and msg.get("stop_reason") == "end_turn":
        return True
    content = msg.get("content")
    if isinstance(content, str):
        return INTERRUPTED_MARK in content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and INTERRUPTED_MARK in str(block.get("text") or ""):
                return True
    return False


def live_subagents(sid: str) -> list[str]:
    """Ids of this session's subagents that still appear to be running."""
    live: list[str] = []
    now = time.time()
    for p in subagent_transcripts(sid):
        if _transcript_finished(p):
            continue
        try:
            if now - p.stat().st_mtime > SUBAGENT_STALE_SECONDS:
                continue
        except Exception:
            continue
        name = p.stem
        live.append(name[len("agent-"):] if name.startswith("agent-") else name)
    return live


def wait_for_turn_to_end(spid: int, sid: str, deadline_seconds: float | None = None) -> bool:
    """Block until the session is out of its turn. False if it never got there.

    A separate function from the handoff wait because it answers a different
    question. That one asks "has the session produced the file yet", and breaks
    at the Write TOOL CALL. This one asks "is the TUI able to read a COMMAND
    yet", and only the end of the turn answers it — see CLEAR_IDLE_WAIT_SECONDS
    for the transcript this was measured against.

    Returning False is a refusal, not a fallback: the caller must not clear. The
    one thing that must NOT happen at the ceiling is sending anyway, which is
    precisely the failure this exists to stop.
    """
    limit = CLEAR_IDLE_WAIT_SECONDS if deadline_seconds is None else deadline_seconds
    deadline = time.time() + limit
    while session_is_working(spid, sid):
        if time.time() >= deadline:
            return False
        time.sleep(1)
    return True


def log(msg: str) -> None:
    try:
        d = sessions_root()
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(d / "inplace-reset.log", "a") as f:
            f.write(f"{stamp} {msg}\n")
    except Exception:
        pass


def write_outcome(sid: str, status: str, reason: str) -> None:
    """Machine-readable terminal record: <sessions_root>/<sid>/outcome.json.

    THE TRIGGER'S RE-ARM CHANNEL. self-compact-trigger.py consumes an
    `aborted` record (bounded, once per record) to re-arm a session it
    disarmed at fire time; an `ok` record ends that crossing's retry story.
    The log above is deliberately NOT that channel: it is a human forensic
    record whose wording changes freely, and a trigger parsing prose would
    break on the first reword.

    Best-effort, like log(): an unwritable outcome must never turn a completed
    reset into a failure, nor block an abort path that destroyed nothing.
    Atomic (tmp + rename, mirroring the trigger's write_json_atomic — copied,
    not imported: the trigger imports this module, so importing back would be
    a cycle, and both files are deliberately self-contained), so the trigger
    can never read a half-written record.
    """
    tmp: Path | None = None
    try:
        d = sessions_root() / sid
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".outcome.json.tmp{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump({"status": status, "reason": reason,
                       "at": int(time.time())}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, d / "outcome.json")
    except Exception:
        if tmp is not None:
            try:
                os.unlink(tmp)
            # tmp cleanup inside a best-effort write whose contract
            # (docstring above) is that it never fails a completed reset
            # nor blocks an abort path.
            except Exception:
                pass  # arftl-allow: swallowed-exception


def _ppid(pid: int):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except Exception:
        return None
    return None


def _is_claude(pid: int) -> bool:
    try:
        exe = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
    except Exception:
        exe = ""
    if exe.endswith(" (deleted)"):
        exe = exe[: -len(" (deleted)")]
    if exe in CLAUDE_EXE_TAILS:
        return True
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv0 = f.read().split(b"\0")[0].decode(errors="replace")
    except Exception:
        argv0 = ""
    return os.path.basename(argv0) in CLAUDE_EXE_TAILS


def session_pid(start: int | None = None):
    """The claude process this hook runs under, by ancestry — not by title or
    cwd, since two sessions can share a cwd and titles are cosmetic."""
    pid = start if start is not None else os.getpid()
    for _ in range(24):
        if _is_claude(pid):
            return pid
        nxt = _ppid(pid)
        if not nxt or nxt == pid or nxt == 1:
            return None
        pid = nxt
    return None


def kitty_socket(spid: int | None) -> str | None:
    s = os.environ.get("KITTY_LISTEN_ON")
    if s:
        return s
    if spid is None:
        return None
    try:
        with open(f"/proc/{spid}/environ", "rb") as f:
            for entry in f.read().split(b"\0"):
                if entry.startswith(b"KITTY_LISTEN_ON="):
                    return entry.split(b"=", 1)[1].decode(errors="replace")
    except Exception:
        return None
    return None


def _kitten(socket: str, *args: str, timeout: int = 15):
    return subprocess.run(["kitten", "@", "--to", socket, *args],
                          capture_output=True, text=True, timeout=timeout)


def resolve_window(socket: str, spid: int):
    """The window id whose foreground process IS this session. Unique or nothing."""
    try:
        r = _kitten(socket, "ls")
    except Exception as e:
        return None, f"kitten @ ls failed: {e}"
    if r.returncode != 0:
        return None, f"kitten @ ls rc={r.returncode}: {r.stderr.strip()[:200]}"
    try:
        data = json.loads(r.stdout)
    except Exception as e:
        return None, f"kitten @ ls did not return JSON: {e}"
    hits = set()
    for osw in data:
        for tab in osw.get("tabs", []):
            for w in tab.get("windows", []):
                for fg in w.get("foreground_processes") or []:
                    if fg.get("pid") == spid:
                        hits.add(w.get("id"))
    if not hits:
        return None, f"no kitty window has pid {spid} in the foreground"
    if len(hits) > 1:
        return None, f"pid {spid} matched {len(hits)} windows ({sorted(hits)}); refusing to guess"
    return hits.pop(), None


CONTROL_CHARS = {c for c in range(0x20) if c not in (0x09,)} | {0x7F}


def sanitise(text: str) -> str:
    """Strip control bytes that would steer the terminal rather than fill it.

    An ESC in the payload can leave paste mode or drive the TUI; a stray CR
    submits early. Newlines are kept because the payload is delivered as a
    BRACKETED PASTE, where they are literal text. idle-handoff already refuses
    control characters in a cwd for the same reason.
    """
    return "".join(ch for ch in text if ord(ch) not in CONTROL_CHARS or ch == "\n")


def send_text(socket: str, wid: int, spid: int, text: str, submit: bool = False) -> str | None:
    """Send, but only after re-confirming this window is still the session.

    Without the re-check this is a TOCTOU with someone's terminal as the target:
    if the session exits after `ls`, the window's foreground becomes a shell and
    every line we send is executed as a command — including any free text the
    operator passed to /self-compact.

    EVERY PAYLOAD GOES AS A BRACKETED PASTE. In a TUI a newline is Enter, so a
    multi-paragraph instruction sent raw is not one prompt — it is submitted
    line by line, blank lines included. Observed live: the session received the
    instruction as a stream of fragments and died mid-way, leaving a waiter
    blocked on a handoff that could never arrive. Bracketed paste makes the
    newlines literal; `submit=True` then sends a single Enter afterwards.
    """
    now, why = resolve_window(socket, spid)
    if now is None:
        return f"window vanished before send: {why}"
    if now != wid:
        return f"window changed from {wid} to {now} before send"
    try:
        r = _kitten(socket, "send-text", "--match", f"id:{wid}",
                    "--bracketed-paste", "enable", "--", sanitise(text))
        if r.returncode != 0:
            return f"send-text rc={r.returncode}: {r.stderr.strip()[:160]}"
        if submit:
            time.sleep(0.3)
            r = _kitten(socket, "send-text", "--match", f"id:{wid}", "--", "\r")
            if r.returncode != 0:
                return f"submit rc={r.returncode}: {r.stderr.strip()[:160]}"
    except Exception as e:
        return f"send-text raised: {e}"
    return None


def write_instruction(target: str, instructions: str) -> str:
    ops = ""
    if instructions:
        ops = ("\nThe operator asked for this reset and said to focus on this, so let it "
               f"shape what you put under ## Next: {instructions}\n")
    return (
        "Your context is about to be cleared to make room, and you are the only thing "
        "that still has it. Write the handoff now, from what you already know — do not "
        "re-read the transcript or go looking, just write down where things stand.\n\n"
        f"Write it to exactly this path: {target}\n\n"
        "Use these sections, and make them concrete enough that you could continue from "
        "this file alone, with nothing else in context:\n"
        "  ## Done — what is finished, and the facts that were established\n"
        "  ## Next — the specific next actions, most important first\n"
        "  ## Blocked — anything stuck, and on what\n"
        "  ## Open questions — what is genuinely undecided\n"
        "  ## State on disk — branch, commits, files touched, what is verified\n"
        f"{ops}\n"
        "Write the file and then stop. Do no other work, and do not ask anything — "
        "nobody is going to answer before the clear."
    )


def pickup_message(handoff: str, instructions: str) -> str:
    ops = ""
    if instructions:
        ops = f"You also asked yourself to focus on this first: {instructions}\n\n"
    return (
        f"{ops}Your context was just cleared to make room. You are the same session, "
        f"carrying on. Read {handoff} — you wrote it yourself a moment ago, so that "
        "the work would survive the clear.\n\n"
        "Pick up the top item under ## Next and keep going."
    )


def main(argv: list[str]) -> int:
    args = {}
    it = iter(argv)
    for a in it:
        if a.startswith("--"):
            args[a[2:]] = next(it, "")
    sid = args.get("sid", "")
    if not SID_RE.match(sid):
        # No outcome record on this path: the sid IS the path component the
        # record would be filed under, so an invalid one has no valid session
        # dir to write into. The trigger validated the sid before spawning us,
        # so nothing upstream is waiting on an outcome it will never get.
        log(f"REFUSING: sid {sid!r} is not a valid session id; it becomes a path "
            "component under the sessions root and `..` or a slash would escape it")
        return 1
    instructions = args.get("instructions", "")
    spid_arg = args.get("session-pid", "")

    spid = int(spid_arg) if spid_arg.isdigit() else session_pid()
    if not spid:
        log(f"sid={sid} REFUSING: could not resolve the session pid")
        write_outcome(sid, "aborted", "no_session_pid")
        return 1

    # ONE RESET PER SESSION AT A TIME. `/self-compact` run twice, or a manual run
    # racing the automatic fire, otherwise starts two waiters on the same window:
    # both would wait for a handoff, both would send `/clear`, and the second
    # would type its pickup into a session the first had already resumed. The
    # lock is held for the whole sequence and released when the process exits,
    # including on a crash, because the fd dies with it.
    try:
        lock_dir = sessions_root() / sid
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(lock_dir / "reset.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log(f"sid={sid} REFUSING: another in-place reset is already running for this "
            "session. Two waiters on one window would interleave their clear and pickup.")
        # A re-arm+refire off this record while the holder is live is refused
        # by this same lock, which is correct and harmless — and the trigger's
        # retry spacing bounds it anyway. The holder overwrites this record
        # with its own terminal outcome when it finishes.
        write_outcome(sid, "aborted", "lock_held")
        return 1

    # cwd is read for the log only. There is deliberately no cwd restriction: the
    # successor is this same session continuing, so anywhere it was already
    # working is somewhere it may keep working.
    try:
        cwd = os.readlink(f"/proc/{spid}/cwd")
    except Exception:
        cwd = "<unknown>"

    socket = kitty_socket(spid)
    if not socket:
        log(f"sid={sid} no in-place reset: KITTY_LISTEN_ON unset for pid {spid}; this "
            "terminal has no remote control")
        write_outcome(sid, "aborted", "no_socket")
        return 1

    wid, why = resolve_window(socket, spid)
    if wid is None:
        log(f"sid={sid} REFUSING: {why}")
        write_outcome(sid, "aborted", "window_unresolved")
        return 1

    # DRAIN BEFORE CLEARING, and before the handoff is even requested -- so the
    # handoff describes settled state rather than work that lands seconds later
    # with nobody left to receive it.
    live = live_subagents(sid)
    if live:
        log(f"sid={sid} waiting for {len(live)} running subagent(s) before clearing: "
            f"{', '.join(live)}")
        drain_deadline = time.time() + SUBAGENT_WAIT_CAP_SECONDS
        while time.time() < drain_deadline:
            live = live_subagents(sid)
            if not live:
                break
            time.sleep(SUBAGENT_POLL_SECONDS)
        if live:
            log(f"sid={sid} NOT CLEARING: {len(live)} subagent(s) still running after the "
                f"{SUBAGENT_WAIT_CAP_SECONDS // 3600}h drain ceiling ({', '.join(live)}). "
                "Clearing now would interrupt them mid-flight and orphan their transcripts. "
                "Nothing was destroyed; re-run once they finish.")
            write_outcome(sid, "aborted", "subagent_drain_ceiling")
            return 1
        log(f"sid={sid} subagents drained; proceeding")

    target = handoff_path(sid)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log(f"sid={sid} REFUSING: cannot create {target.parent} ({e})")
        write_outcome(sid, "aborted", "handoff_dir_unwritable")
        return 1
    # FRESHNESS BASELINE. Existence is not enough: a handoff from an earlier
    # crossing would pass instantly and we would clear the session to resume it
    # from obsolete state.
    before = target.stat().st_mtime if target.exists() else 0.0
    requested_at = time.time()

    err = send_text(socket, wid, spid, write_instruction(str(target), instructions), submit=True)
    if err:
        log(f"sid={sid} REFUSING: could not ask the session to write its handoff ({err})")
        write_outcome(sid, "aborted", "handoff_request_send_failed")
        return 1

    # Wait for as long as the session is demonstrably still working, and only
    # start the give-up clock once it goes idle without having written.
    cap = requested_at + WRITE_WAIT_CAP_SECONDS
    idle_since = requested_at
    wrote = False
    while time.time() < cap:
        try:
            if target.exists() and target.stat().st_mtime > before and target.stat().st_size > 0:
                wrote = True
                break
        except Exception:
            pass
        if session_is_working(spid, sid):
            idle_since = time.time()
        elif time.time() - idle_since > WRITE_IDLE_GRACE_SECONDS:
            log(f"sid={sid} NOT CLEARING: no fresh handoff at {target}, and the session "
                f"has been idle for {WRITE_IDLE_GRACE_SECONDS}s without writing one. The "
                "user may have been typing when the instruction arrived. Nothing was "
                "destroyed.")
            write_outcome(sid, "aborted", "no_fresh_handoff_idle")
            return 1
        time.sleep(1)
    if not wrote:
        log(f"sid={sid} NOT CLEARING: no fresh handoff at {target} within the "
            f"{WRITE_WAIT_CAP_SECONDS}s ceiling; the session was still working. Nothing "
            "was destroyed.")
        write_outcome(sid, "aborted", "write_wait_ceiling")
        return 1

    # OUT OF THE TURN BEFORE THE CLEAR. The loop above breaks the moment the file
    # exists, which is the Write tool call, not the end of the turn — see
    # CLEAR_IDLE_WAIT_SECONDS. `/clear` sent into a live turn is queued as
    # ordinary text and never reaches the command parser.
    #
    # `session_is_working` degrading to False on an unreadable registry is the
    # right failure here as well: it puts us back to sending immediately, which
    # is what this did before, and the confirmation below still refuses if the
    # clear did not land.
    if not wait_for_turn_to_end(spid, sid):
        log(f"sid={sid} NOT CLEARING: the handoff at {target} is written, but the session "
            f"was still mid-turn {CLEAR_IDLE_WAIT_SECONDS}s after it landed. A `/clear` sent "
            "into a live turn is queued as ordinary text instead of being run, so it would "
            "be spent for nothing. Nothing was destroyed; the handoff is on disk and the "
            "marker is kept.")
        # The dominant abort shape (7 of ~21 in the corpus, 2026-08-16..09-04).
        # The trigger's re-arm answers it with a FRESH handoff on a later Stop —
        # a turn boundary — which is why CLEAR_IDLE_WAIT_SECONDS stays at 120s
        # instead of stretching over a handoff that only goes staler.
        write_outcome(sid, "aborted", "mid_turn_timeout")
        return 1
    time.sleep(TURN_SETTLE_SECONDS)

    # Evidence that the clear ACTUALLY landed, not merely that bytes arrived.
    # `kitten` returning 0 says the text was delivered to the window; it says
    # nothing about whether the TUI acted on it. If the user had typed something
    # first, `/clear` appends to their buffer and submits their text instead --
    # no clear happens, and without this check we would send the pickup into a
    # session that still holds its old context and then consume the marker,
    # logging success. `/clear` starts a NEW transcript, so the CLI's own
    # bookkeeping is the confirmation.
    id_before = registry_session_id(spid)
    err = send_text(socket, wid, spid, "/clear", submit=True)
    if err:
        log(f"sid={sid} NOT CLEARING: {err}. The handoff is written at {target}.")
        write_outcome(sid, "aborted", "clear_send_failed")
        return 1
    cleared = False
    deadline = time.time() + CLEAR_CONFIRM_SECONDS
    while time.time() < deadline:
        now_id = registry_session_id(spid)
        if id_before is not None and now_id is not None and now_id != id_before:
            cleared = True
            break
        time.sleep(0.5)
    if not cleared:
        log(f"sid={sid} NOT CONFIRMED: /clear was delivered to window {wid} but the session "
            f"id did not change within {CLEAR_CONFIRM_SECONDS}s, so the session was NOT "
            "cleared. Measured cause: a prompt that already holds typed text swallows the "
            "command instead of running it. Leaving the request marker in place and sending "
            f"no pickup, so nothing acts on a session that still has its old context. The "
            f"handoff is written at {target}.")
        write_outcome(sid, "aborted", "clear_not_confirmed")
        return 1
    time.sleep(CLEAR_SETTLE_SECONDS)
    err = send_text(socket, wid, spid, pickup_message(str(target), instructions), submit=True)
    if err:
        log(f"sid={sid} HALF-DONE: cleared window {wid} but the pickup did not send "
            f"({err}). The session is cleared and idle; the handoff is at {target}.")
        # `aborted` even though the clear landed: the reset did not deliver a
        # resume, so it did not succeed. The re-arm channel cannot loop on it —
        # the clear started a NEW session id, and this sid emits no more Stops.
        write_outcome(sid, "aborted", "pickup_send_failed")
        return 1

    # Consume the marker: the in-place reset IS the resume, so the fork path must
    # not also act on this session.
    try:
        marker = sessions_root() / sid / "request.json"
        if marker.exists():
            marker.rename(marker.with_name("consumed.json"))
    except Exception as e:
        log(f"sid={sid} WARNING: reset done but the marker could not be consumed ({e}); "
            "idle-handoff may also act on this session once it exits")

    log(f"sid={sid} in-place reset OK: window={wid} pid={spid} cwd={cwd} handoff={target} "
        f"instructions={'yes' if instructions else 'no'}")
    write_outcome(sid, "ok", "reset_complete")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        log(f"in-place reset crashed: {e}")
        # A crash is a terminal path too; without a record the trigger reads it
        # as "reset still running" forever. The sid comes from argv because the
        # crash may predate main()'s parse — validated, since it is about to
        # become a path component (same rule main() applies).
        crash_sid = ""
        for _i, _a in enumerate(sys.argv[1:-1]):
            if _a == "--sid":
                crash_sid = sys.argv[1:][_i + 1]
        if SID_RE.match(crash_sid):
            write_outcome(crash_sid, "aborted", "crash")
        sys.exit(1)
