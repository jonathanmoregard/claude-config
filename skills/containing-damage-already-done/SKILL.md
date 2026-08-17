---
name: containing-damage-already-done
description: Use when an action has already caused real harm outside the sandbox — a third party blocked, banned, rate-limited, throttled, CAPTCHA-challenged or 403'd you; a deploy went out broken or to the wrong target; credentials, keys or user data leaked; a destructive operation (delete, drop, force-push, overwrite, mass update) hit real state; spend, tokens or quota blew past budget; or trust with a user or external party was breached. Also use when tempted to retry harder or work around a block.
---

# Containing Damage Already Done

The harm has landed. Your next actions decide whether it stays this size.

## Contain

1. **Halt every related process now.** Background jobs, loops, schedulers,
   sibling agents — anything touching the resource. Verify they are actually
   dead in the process list and scheduler state; assume nothing exited because
   you asked it to.
2. **Preserve state and evidence before any cleanup.** Snapshot logs, the exact
   failing command, requests and responses. All job state stays resumable;
   never restart from zero for a "clean run".
3. **Stop touching the resource.** A single probe, on a schedule, or none.
   Repeated probing is a second incident.

## Forbidden

However well-reasoned it feels:

- Workarounds, evasion, retry-harder
- Changing User-Agent, identity, account, credentials or fingerprint
- Proxying, mirroring or routing around a block
- Solving or bypassing a challenge
- Raising a limit, weakening a check or adding an override to proceed

A block is an answer, not an obstacle.

## Diagnose

- **Get an independent second opinion** (separate agent or model) before
  concluding root cause. The actor that caused the incident is the least
  reliable witness.
- Measure the **aggregate** effect, not per-action rate. Concurrency,
  cheap-looking probes and single expensive calls sum into the number that
  caused this.
- Count **every** client that touched the resource — diagnostics, one-offs,
  side work, other processes. None of it is exempt.
- Look upstream for a **correctness bug**. A silent data bug manufactures load
  and blast radius by forcing a repair pass.

## Recover

- Recovery is **one retry scheduled for tomorrow**. No escalating ladder, no
  "quick check" in between.
- Create a TickTick task as the backup channel so the retry survives the
  scheduler not firing.
- Log the blocker with the **exact command a human runs** to resume.
- Resume only on **explicit human approval**. A schedule firing is not approval.

## Make the fix bind

Defaults, comments and warnings do not bind; judgement fails under debug
pressure. Any constraint you add must be an invariant at the single chokepoint
every client passes through, enforced globally across processes, with no
per-caller override.

## Quick reference

| Situation | First move |
|---|---|
| Blocked, banned, rate-limited | Halt all clients, one scheduled probe, retry tomorrow |
| Bad or wrong-target deploy | Freeze rollout, preserve artifacts and logs, human approves rollback |
| Leaked credential or data | Revoke, then scope the leak; never delete evidence |
| Destructive op hit real state | Stop, inventory what changed, no repair write without approval |
| Budget or quota blown | Stop spending; do not raise the ceiling to finish |
| Trust breached | Report to the human before any further action |

## Red flags

- "Just one more request to confirm"
- "This probe is tiny, it doesn't count"
- "That limit applies to the main job, not to this"
- "I'll lower the delay only while debugging"
- "I'll change the User-Agent and see what happens"
- "I already know what caused it"
- "Restarting from scratch is cleaner"
- "The schedule fired, so I'm cleared to resume"

All of these mean: stop and get human approval.

**Limit:** this protocol is the response to the incident class, not prevention
of it. Only a globally enforced invariant at the chokepoint stops the next one.
