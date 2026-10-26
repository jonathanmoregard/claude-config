---
name: scheduling-follow-through
description: Use when about to end a turn while something is still running — a background agent, CI build, long test suite, nix build, VM gate, download, deploy, or research call — or when finishing depends on state that changes without you. Also use when tempted to write "I'll report when it lands" or "running in the background", or when about to re-run `gh pr checks` / `gh run view` / `sleep N; <status>` to see whether CI or a gate has finished yet.
---

# Scheduling Follow-Through

Handing control back with a live process and no scheduled return is dropping the work. The user did not ask for a status report; they asked for the thing to be done.

**Core rule: never end a turn on an open loop without a scheduled return.**

## The two failures this prevents

1. **Abandonment.** You end the turn, the process finishes, nobody acts. The user has to notice and re-prompt.
2. **Loss.** A `/clear`, restart, or crash kills an in-flight background agent. Its result is gone and the work is re-run from scratch.

## When it fires

You are about to say any of these:

- "running in the background, I'll report when it lands"
- "waiting on CI"
- "this takes ~15 minutes"
- "let me know when you want me to continue"
- anything ending in a live process and no scheduled return

## What to do instead

Schedule a durable one-shot BEFORE ending the turn:

```
CronCreate(cron: "<M> <H> <DoM> <Mon> *", recurring: false, durable: true, prompt: <self-contained>)
```

- **`durable: true`** — survives `/clear`, restart, crash. Session-only jobs die with the process, which is the exact case you are protecting against.
- **Delay matched to the process**, not to a default. An 8-minute CI run gets one ~10-minute check, not six 90-second ones. Add slack; firing early wastes the wake.
- **Off-minute** (not :00 or :30).
- **Waits have a blocking primitive — use it instead of re-checking.** `~/.claude/scripts/wait-for pr <PR> [-R owner/repo]` (CI settled and GitHub's lazy mergeability computed), `wait-for unit <name> [--user] [--since now]` (a deploy or oneshot run finished), `wait-for run <ID>`; or gh's own `gh pr checks <PR> --watch --fail-fast`. Run it with Bash `run_in_background: true`: one call, and you are re-invoked when it settles, with a one-line summary and an exit code (0 fine · 1 failed · 2 conflict/closed · 3 checks unreadable · 124 timeout). Never `sleep N; gh pr checks|gh pr view|systemctl status` in the foreground — bash-guard denies it. Add the durable cron on top only when the wait must survive a `/clear`.
- `ScheduleWakeup` is `/loop`-only and does not survive a clear — do not reach for it here.

### The scheduled prompt must be self-contained

Written for an agent with no memory of this turn:

- what was running, and where its output lands
- where durable state lives (`tasks/mission.md`, a run-state file, a PR number)
- what to do for each outcome — success, failure, and **never arrived**
- what NOT to do without the user

If the result may be lost, say so and include how to re-run it.

## Definition of done

**As close to merged and pushed as possible without user intervention.**

Work through, in order — do not stop early because a step "feels like" the user's:

1. changes committed
2. gates green (tests, lint, build, VM)
3. branch pushed
4. PR opened, body written
5. CI watched to green, failures fixed, re-pushed
6. durable state updated (`mission.md`) so a fresh session can resume
7. handed over with exactly what remains and why

Stop only at gestures that are genuinely the user's: clicking merge, credentials, interactive logins, physical-world actions, and anything the harness denies by design.

## Rationalizations

| Excuse | Reality |
|---|---|
| "The harness re-invokes me when the agent finishes" | True until a `/clear`. Durable cron survives; the notification does not. |
| "I'll just wait in this turn" | You cannot. The turn ends when you stop producing tool calls. |
| "The user will tell me when it's done" | That is the work you were asked to do. |
| "It's only a few minutes" | Then schedule a few minutes. Cost is one tool call. |
| "I don't know how long it takes" | Estimate, add slack, schedule. A late wake beats no wake. |
| "Reporting status IS the deliverable" | Only if the user asked for a status report. |
| "Merging is the user's call, so I'm done at PR-open" | Merging is theirs. Green CI is not. Drive to the last non-user step. |

## Red flags

- A turn ending with a live process and no `CronCreate`
- `durable: false` on anything that must survive a clear
- A scheduled prompt that says "continue" without saying what, where, or what-if-missing
- Stopping at "PR opened" while CI is still running
- Polling every 60s for something that takes 20 minutes

## Testing status

RED baseline is observational, from a 2026-08-15 session: ~20 turns ended with
"running in the background, I'll report when it lands", and one `/clear` killed
an in-flight background agent whose result was lost. GREEN verification against
a fresh agent under time pressure is NOT yet run — treat the rationalization
table as provisional until it is.
