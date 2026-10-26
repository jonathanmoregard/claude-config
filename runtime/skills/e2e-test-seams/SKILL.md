---
name: e2e-test-seams
description: Use when writing or reviewing end-to-end / black-box / integration tests against a running server and a needed state or event is hard to reach — a background/scheduled job, an emailed or signed token, an external provider's response, a rare domain state — and you're tempted to add a test-only HTTP route, `/test/...` endpoint, or "inject this state" flag to force it.
---

# e2e Test Seams

## Overview

To test a hard-to-reach state end-to-end, put the seam in the **adapters and
configuration** — never in the domain surface. Drive the *real* user flow; mock
what's *external*; use a *config knob* for time and background work. A state-injecting
test-only route ("mint this token", "own this record", "tick the job now") is a smell:
it tests the backdoor instead of the app, and it hides a missing product flow.

**If a state genuinely can't be reached by any real flow, that's usually a signal the
product flow itself is missing — build it (or flag it), don't backdoor around it.**

## When to use

- You're about to add `POST /test/...`, a `?force=` param, or a `#[cfg(test)]`-gated
  route that creates/mutates domain state so a test can observe it.
- A test needs: a background/cron job to have run, an emailed/signed token, a rare
  state (expired, over-limit, already-owned), or an external API's response.
- Reviewing tests and you see a backdoor route or "seed this row via HTTP" call.

Not for: pure unit tests (call the function directly), or read-only test/dev
*observation* routes (`/dev/last-mail`, health, a stats readout) — those are fine.

## The three legitimate seams (reach for these, in order)

| Hard-to-reach thing | Seam | Not this |
|---|---|---|
| State an external service returns (OAuth, payment, calendar, webhook) | **Mock the adapter** at its boundary (a fake provider server / injected client) and drive the real route | `/test/inject-oauth-result` |
| A **time or background** process (scheduler, deadline sweep, TTL expiry, retry) | **Config/env seam**: shrink the interval / shift the clock so the *real* job runs, then poll for its effect | `/test/run-job-now` |
| Domain state a real user creates (a vote, an owned record, a redemption) | **Do the real flow** in the test; if there's no route to create it, that's a **product gap** — build the real route | `/test/issue-token`, `/test/set-owner` |

## Why state-injecting test routes are a trap

1. **Security.** Their prod-safety rests on one load-bearing guard (a feature flag or
   `cfg!`). A refactor drops it → the backdoor is live in production (mint tokens,
   forge ownership, force jobs). Even correctly gated, it's attack surface that has to
   be reasoned about forever.
2. **They hide missing features.** The reason you *need* to inject the state is often
   that the real flow to produce it was never built. The backdoor papers over the gap
   and the gap ships.
3. **They test the wrong thing.** A test that sets up state through a path no user can
   take proves the backdoor works, not that the feature does.

## Example: a deadline / scheduled job

```ts
// ❌ Backdoor: a prod-reachable route that runs the job on demand
await request.post('/test/run-sweep');           // forces the scheduler
await expect(page.getByText('Closed')).toBeVisible();

// ✅ Config seam: let the REAL scheduler run fast, then poll for its effect
//    (webServer env)  SWEEP_INTERVAL_SECS=1
await createItemWithDeadline(page, nearFuture());     // real create flow
await expect.poll(() => statusOf(page), { timeout: 90_000 })
  .toBe('Closed');                                    // the genuine job did it
```

The same move applies to tokens (drive the real "send me a link" flow + read the
mocked mailbox) and to provider results (point the app at a mock server via a URL
override, return the shape you need).

## Red flags — stop and re-seam

- "I'll just add a `/test/...` route to create/force this."  → which of the three
  seams fits? If none, you've found a missing product flow.
- "It's gated behind a test feature, so it's safe."  → gating is load-bearing and
  drifts; prefer a seam that can't exist in prod at all.
- "There's no way for a user to reach this state."  → then either the feature is
  incomplete (build it) or the state is impossible (delete the test).
- "The real job is slow/time-based, so I have to trigger it."  → shrink the interval
  or move the clock; run the real job.

## Common mistakes

- Keeping a backdoor "just for tests" alongside a real flow — now you maintain and
  secure two paths, and tests exercise the fake one.
- Faking time with a test route instead of an injectable clock / short interval.
- Adding a state-injector when the real fix is a 20-line product route the users
  actually need anyway.
