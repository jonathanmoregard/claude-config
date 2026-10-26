---
name: hardening-flaky-ci
description: Use when a CI check fails on a PR but the same check passes on main, when a re-run turns a red check green, when a failure looks like a timeout, a slow runner, a race, a port clash or a network blip, or when reaching for "just re-run it" / "unrelated failure" / "known flake".
---

# Hardening Flaky CI

## Overview

A re-run that goes green fixes this run. It leaves the flake armed for the next
person. **Re-running is triage, not the fix.** The fix is making that failure
mode unable to recur, and it ships in the PR that hit it.

## The Rule

```
YOU HIT THE FLAKE. YOU HARDEN IT. IN THIS PR.
```

Boy-scout scope: the smallest change that closes this failure mode. Not a test
refactor, not a CI overhaul.

## Steps

1. **Read the actual failure output.** Never classify from the check name.
2. **Decide flake vs real bug.** Real until proven otherwise:
   - Does the same check pass on the base branch's own recent runs?
   - Does the failure mention the code this PR changed?
   - Is the assertion about *timing/environment* rather than *behaviour*?
   - Are runtimes wildly off normal for that job?

   Real bug → stop; fix the bug, this skill does not apply.
3. **Re-run to unblock**, in parallel with step 4. Never as the last action.
4. **Harden**, then push it to the same PR. Pick the cheapest fix that holds:

   | Failure shape | Cheapest hardening |
   |---|---|
   | Timeout / budget exceeded | Retry the *non-answer*, keep the budget. Never widen a limit that production also runs. |
   | Cold-start cost inside the measured path | Warm once, assert on the warm call |
   | Sleep-based wait | Wait on the condition, with a generous ceiling |
   | Port / temp-path / fixture clash | Randomise or namespace per run |
   | Network or third-party dependency | Stub it, or retry with backoff |
   | Ordering dependence | Make the assertion order-independent |
5. **Make the failure name itself.** After hardening, the message on a genuine
   failure must distinguish "the system answered wrongly" from "the system never
   answered". If it cannot, harden further.
6. **Record the diagnosis in the commit message**, not in the test file.

## Do Not

- Disable, skip, or `continue-on-error` the check
- Widen a production-shared timeout to make a test pass
- Retry the whole job as the hardening
- Retry unconditionally — retry only the specific non-answer, so a real
  regression still fails on the first attempt
- Defer to a follow-up issue or a separate PR
- Ship hardening you have not run at least once

## Red Flags — STOP

- "Unrelated failure, re-running"
- "Known flake, ignore it"
- "Passes locally"
- "I'll open a ticket for the flake"
- "Not my PR's problem"
- Merging on a green re-run with no diff to the test

**All of these mean: harden it now, in this PR.**

## Rationalization Table

| Excuse | Reality |
|---|---|
| "It's not my change that broke it" | You are the one holding the evidence and a live branch. Nobody better-placed will come along. |
| "Hardening belongs with the code that owns it" | Ownership decides review, not timing. Land it here; mention it in the PR body. |
| "The re-run passed, so it's fine" | A green re-run is proof the failure is nondeterministic. That is the reason to fix it, not to close it. |
| "Widening the timeout is the obvious fix" | It moves the threshold, keeps the coupling, and diverges test from production. |
| "Too small to bother" | Cost of hardening is minutes now, and an unbounded number of confused re-runs later. |
| "It'll slow the PR down" | A retry on a non-answer costs nothing when nothing is wrong. |
