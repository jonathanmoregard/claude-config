---
name: self-compact
description: Use when the user wants to hand this session off to a fresh one on demand — "/self-compact", "self compact", "hand this off", "context is getting full, start over", "carry on in a new session", "reset my context and keep going", "fork this and continue". Writes a durable mission handoff for the CURRENT session and spawns a restricted process to carry the work forward. Optional free text after the command steers what the successor picks up first.
---

# self-compact

Hand the current session off to a fresh process, on demand, without waiting for
the automatic threshold and without any copy-paste.

## What this actually does, and what it cannot do

There is **no programmatic in-process context reset.** No tool emits slash
commands, hooks inject text only, and nothing can reach into a running session
and clear it. So "self-compact" does not shrink *this* session.

What it does is spawn a **new process**, seeded from a durable handoff, which
carries the work forward in a fresh context. Your current pane keeps existing at
whatever context it is at — it simply stops being where the work happens.

Say this plainly to the user. Do not imply their context was reset.

## Steps

1. **Fire the request.** Everything after the command name is the operator's
   instructions; pass it through verbatim, quoted. Omit the argument entirely if
   they gave none.

   ```bash
   python3 ~/.claude/self-compact-trigger.py --manual "<the user's text>"
   ```

   This writes the request marker and starts the handoff write. It prints the
   session id, the measured context fill, the marker path, and the mission
   result. A non-zero exit is a real failure — report it and stop; do not
   continue to step 2 and let the user think a fork is coming.

2. **Wait for the handoff to land.** The mission write is detached and takes a
   few seconds. Poll for it rather than sleeping a fixed amount:

   ```bash
   for i in $(seq 1 60); do
     [ -f "$REFLECT_ROOT/sessions/<sid>/handoff/mission.md" ] && break
     sleep 1
   done
   ```

   `REFLECT_ROOT` defaults to `~/.claude/reflections` when unset. If the handoff
   never appears, say so — a fork with nothing to resume from is worse than no
   fork.

3. **Run one tick.** The 5-minute timer would get to it on its own; running it
   now is what makes this on-demand.

   ```bash
   bash ~/.claude/scripts/idle-handoff.sh
   ```

4. **Report the outcome from the log, not from the exit code.** The script exits
   0 whether or not it forked. Read the last summary line of
   `$REFLECT_ROOT/idle-handoff-logs/handoff.log`:

   - `forked=1` — the successor is running. Tell the user its cwd, and tell them
     to stop working in this pane; the fork edits the same tree with edits
     auto-accepted, and two of you in one working tree is a race.
   - `forked=0` — say which counter is non-zero and what it means. Common ones:
     `git_stale` (the tree moved since the handoff was written), `no_next` (the
     mission has no actionable Next items), `fork_budget` (3 per run, 5 per cwd
     per 24h), `control_plane_cwd` (refuses to fork with `~/.claude` as its
     workspace), `ralph` (a ralph loop is armed here), `deny_unproven` (the CLI's
     permission rail could not be proven, so it failed closed).

## Notes

- **The threshold does not apply here.** The automatic path fires at 30% and
  re-measures that claim before acting. A manual request asserts nothing about
  occupancy, so it is honoured at any fill. Spend is still bounded downstream, at
  3 forks per run and 5 per cwd per 24h.
- **The live-cwd guard does not apply here either.** It refuses forks into a tree
  someone is working in, which is correct for the idle trigger and wrong for this
  one — the session asking to be handed off is, necessarily, live.
- **The successor is restricted.** It runs with no ambient settings source, no
  MCP servers, a fixed tool set with no shell, and a write grant limited to the
  project plus its own handoff directory. It reads the mission from outside every
  repo, and treats the mission body as untrusted data.
- **Operator instructions are not a permission escalation.** They steer which
  Next items matter and in what order. Every refusal rule in the fork's prompt
  still applies.
