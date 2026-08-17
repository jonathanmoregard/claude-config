#!/usr/bin/env python3
"""Reset a LIVE session in place: it writes its own handoff, clears, resumes.

THE SEQUENCE, and why it is in this order:

    1. ask the session to write its handoff to ~/.claude/self-compact/<sid>/handoff/mission.md
    2. wait until that file appears AND is newer than the request
    3. /clear
    4. a pickup message naming only that file

Step 1 is the whole point. The session crossing the threshold is ALREADY holding
the context, so it writes the summary itself: no second model call, no
re-ingestion, no context window ceiling. The previous design handed this to
session-reflect's `mission` mode, which spawns `claude -p --resume <sid> --model
haiku`. That replays the entire session, so the prompt IS the session — and the
trigger fires at 30% of the detected window, ~300k on a 1M model, against
haiku's 200k. Measured: a 382,543-token session returned HTTP 400
prompt_too_long after billing $0.23. It was guaranteed to fail for exactly the
sessions this exists to serve.

Step 2 is also the liveness probe, deliberately. Observing the session produce a
fresh file proves it was idle, that the channel reaches it, and that it acted —
BEFORE anything is destroyed. If the user was mid-keystroke, the instruction is
mangled, no fresh file appears, and we time out having cleared nothing.

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
  * the clear is confirmed before the pickup is sent, because `/clear` typed
    into a prompt that already holds text is swallowed rather than run.
  * one reset per session at a time, so two runs cannot interleave.
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

# The session gets this long to write its own handoff. It is one summarisation
# turn on a context it already holds, so this is generous.
WRITE_WAIT_SECONDS = 240
# Gap between `/clear` and the pickup: the TUI has to process the command and
# return to an empty prompt.
CLEAR_SETTLE_SECONDS = 2.0
# How long to wait for the CLI's own evidence that /clear took effect.
CLEAR_CONFIRM_SECONDS = 30


def sessions_root() -> Path:
    env = os.environ.get("CLAUDE_SESSIONS_DIR")
    return Path(env) if env else Path.home() / ".claude" / "self-compact"


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


def log(msg: str) -> None:
    try:
        d = sessions_root()
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(d / "inplace-reset.log", "a") as f:
            f.write(f"{stamp} {msg}\n")
    except Exception:
        pass


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
        log(f"REFUSING: sid {sid!r} is not a valid session id; it becomes a path "
            "component under the sessions root and `..` or a slash would escape it")
        return 1
    instructions = args.get("instructions", "")
    spid_arg = args.get("session-pid", "")

    spid = int(spid_arg) if spid_arg.isdigit() else session_pid()
    if not spid:
        log(f"sid={sid} REFUSING: could not resolve the session pid")
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
        return 1

    wid, why = resolve_window(socket, spid)
    if wid is None:
        log(f"sid={sid} REFUSING: {why}")
        return 1

    target = handoff_path(sid)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log(f"sid={sid} REFUSING: cannot create {target.parent} ({e})")
        return 1
    # FRESHNESS BASELINE. Existence is not enough: a handoff from an earlier
    # crossing would pass instantly and we would clear the session to resume it
    # from obsolete state.
    before = target.stat().st_mtime if target.exists() else 0.0
    requested_at = time.time()

    err = send_text(socket, wid, spid, write_instruction(str(target), instructions), submit=True)
    if err:
        log(f"sid={sid} REFUSING: could not ask the session to write its handoff ({err})")
        return 1

    deadline = requested_at + WRITE_WAIT_SECONDS
    while time.time() < deadline:
        try:
            if target.exists() and target.stat().st_mtime > before and target.stat().st_size > 0:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        log(f"sid={sid} NOT CLEARING: no fresh handoff at {target} within "
            f"{WRITE_WAIT_SECONDS}s. The session may have been busy or the user may have "
            "been typing. Nothing was destroyed.")
        return 1

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
        return 1
    time.sleep(CLEAR_SETTLE_SECONDS)
    err = send_text(socket, wid, spid, pickup_message(str(target), instructions), submit=True)
    if err:
        log(f"sid={sid} HALF-DONE: cleared window {wid} but the pickup did not send "
            f"({err}). The session is cleared and idle; the handoff is at {target}.")
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
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        log(f"in-place reset crashed: {e}")
        sys.exit(1)
