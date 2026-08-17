---
name: self-compact
description: Use when the user wants to reset this session's context on demand and keep going — "/self-compact", "self compact", "hand this off", "context is getting full, start over", "carry on in a new session", "reset my context and keep going", "clear and resume". The session writes its own handoff from the context it already holds, clears itself, and resumes from that handoff alone. Optional free text after the command steers what gets picked up first.
---

# self-compact

Reset this session's context on demand, without losing the thread and without
you pasting anything.

## How it works

An agent cannot clear **itself**: no tool emits slash commands, and hooks only
inject text. But a session is a process in a terminal, and a terminal with remote
control can be typed into. So the reset is done *to* the session by a small
detached helper, in this order:

1. the session is asked to write its handoff — **from the context it is already
   holding**, so nothing re-reads anything;
2. the helper waits for that file to appear and be newer than the request;
3. `/clear`;
4. the clear is **confirmed** before anything else happens;
5. a pickup message naming only the handoff.

Same process, same pane, same permissions — fresh context.

Step 1 is why there is no second model call and no context-window ceiling. An
earlier design had a separate worker resume the whole session into a small model
to summarise it, which cannot work: the trigger fires at a size larger than that
model's entire window.

Step 4 matters more than it looks. `kitten` returning success proves bytes were
delivered, not that the TUI acted. If the prompt already holds typed text,
`/clear` is appended to it and submitted as an ordinary prompt — no clear
happens. Measured. So the helper confirms a new session actually began before it
sends the pickup or consumes the request; if it did not, it stops and keeps
everything.

If the terminal cannot be driven, **nothing is cleared**. The request is left for
the existing fork path, which picks it up once the session exits. Say which of
the two happened; do not imply a reset that did not occur.

## Steps

1. **Fire it.** Everything after the command name is the operator's
   instructions — pass it through verbatim, quoted, or omit it entirely.

   ```bash
   python3 ~/.claude/self-compact-trigger.py --manual "<the user's text>"
   ```

   It prints the session id, the measured context fill, the marker path and an
   `in-place:` line. A non-zero exit is a real failure — report it and stop.

2. **Read the `in-place:` line and say which future they are in.**

   - `queued(...)` — this session will write its handoff, clear, and come back
     working. Tell them not to type until it does: text in the prompt is exactly
     what stops a clear from landing.
   - anything else — no reset is coming, and the reason is in
     `~/.claude/self-compact/inplace-reset.log`. Common ones: `KITTY_LISTEN_ON`
     unset (no remote control), `no kitty window has pid N` or `matched N
     windows` (the window could not be uniquely identified, so it refuses rather
     than typing into someone else's session), no fresh handoff appeared, the
     clear was not confirmed, or another reset is already running for this
     session.

3. **Do nothing else.** Do not start work, do not run a tick, do not "prepare".
   The next thing that should happen here is the write, then the clear.

## Notes

- **The threshold does not apply to a manual request.** The automatic path fires
  at 300,000 tokens and re-measures that claim before acting. A person asking to
  hand off asserts nothing about occupancy, so it is honoured at any size.
- **It refuses rather than guesses.** The window is matched on the session's own
  pid via process ancestry, and re-verified immediately before every send —
  between resolving and sending, a session can exit and leave a shell owning that
  window, where the text would be executed as commands.
- **It will not clear a session with nothing to resume from.** A fresh handoff
  must exist first; a stale one from an earlier crossing does not count.
- **The successor is the same session, not a sandbox.** Same process, same
  permissions, same everything it was already carrying — just less context. That
  is the point, not a caveat: clearing hands nobody anything they did not have a
  moment earlier. So there is no cwd restriction, no reduced tool set, and the
  handoff is not treated as untrusted input — it is the session's own notes to
  itself.

## Testing

`tests/smoke.sh` stubs `kitten`, so it covers the refusal paths only. The
mechanism — paste arriving as one prompt, the session being able to write the
file, the clear landing, the resume working from the handoff alone — is covered
by `tests/live-kitty.sh`, which drives a real kitty on a private virtual display
against a real session. Run it with `REFLECT_LIVE_TEST=1`; it bills.
