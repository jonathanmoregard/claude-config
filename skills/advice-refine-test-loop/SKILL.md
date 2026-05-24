---
name: advice-refine-test-loop
description: Use when shipping non-trivial changes that must land clean — the user says "look through carefully and fix what's off", "ask for advice and repeat", "keep refining until neat", or is going to bed. Pairs an Opus advisor with empirical verification over multiple rounds to catch latent bugs single-pass review misses.
---

# Advice-Refine-Test Loop

Single-pass review reliably misses fail-open paths, silently-swallowed exceptions, resource leaks, schema mismatches. The loop catches them by alternating fresh Opus reviewer → empirical verification → fix → re-review.

## The Round

```
1. SELF-REVIEW: read each changed file top-to-bottom (not just diff)
2. VERIFY: tests + syntax + smoke green before inviting external eyes
3. ADVISE: Agent(subagent_type="advisor", prompt=...)  // read-only Opus reviewer; see ~/.claude/agents/advisor.md. NEW SESSION REQUIRED after editing the advisor agent file — Claude Code loads agent definitions at session start. If `advisor` is unknown in the current session, restart before continuing the loop; do NOT fall back to `general-purpose` (loses both invariants: whitelist + bypassPermissions).
4. REPRODUCE each blocker/high claim before fixing — advisors hallucinate
5. FIX by severity, re-verify after each
6. COMMIT the round (per-round granularity, not a batch)
7. RE-ADVISE with a fresh prompt that lists what prior rounds fixed
8. STOP on explicit green-light ("SAFE TO SHIP" / "no blockers")
```

## Advisor Prompt Shape

Every round's prompt must contain:

- **Project goal** (1–2 sentences)
- **Prior rounds fixed:** bulleted list (prevents re-flagging resolved issues)
- **Verified since last round:** what you empirically confirmed
- **Re-read fresh:** absolute file paths (don't trust advisor memory)
- **Seeded hypotheses:** your own doubts, phrased as "verify X"
- **Format:** severity-ranked (BLOCKER / HIGH / MEDIUM / LOW), file:line citations
- **Closer:** one-line verdict `SAFE TO SHIP` or `BLOCKS SHIPPING`
- **Length cap:** ≤500 words

## Reproduce Before Fixing

```
IF advisor says "X is a bug":
  Write 10-line repro. Run it. Confirm behavior matches claim.
IF advisor says "file Y contains Z":
  Grep for Z. Don't fix what isn't there.
IF claim is a race / state issue:
  Force the state, verify the failure mode.
```

Discard unreproducible claims. Note them so next round's "prior rounds fixed" can include "verified non-issue: <claim>".

## Anti-Patterns

| Mistake | Why bad |
|---|---|
| Trust advisor without reproduction | Advisors confident even when wrong |
| Expand scope per advisor suggestion | "Could be cleaner" is not a blocker |
| Same prompt every round | Advisor re-flags fixed issues, wastes findings |
| Batch all rounds into one commit | Loses bisectable history |
| Skip re-verification after each fix | Regressions hide in the batch |
| Stop after round 1 because it "felt thorough" | Most valuable finding often lands in round 3 |

## Exit

Final commit or chat message: round count + categories of bug caught (fail-open / resource leak / schema mismatch / …) + final verdict. Trains judgment for when future changes deserve the loop.
