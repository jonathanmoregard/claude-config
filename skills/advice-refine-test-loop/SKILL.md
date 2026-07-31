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
   - USAGE-LIMIT FALLBACK: if the dispatch returns with "usage limit" in the result (Fable/default org-monthly quota exhausted), re-dispatch ONCE with `model="claude-opus-4-7"` — Opus is a separate quota bucket and typically completes. Do this only on limit-hit; other failures (crash, timeout, refusal) get diagnosed, not fallback-masked. Never re-dispatch a third time — if Opus also 429s, the round exits with a note in the round's commit message.
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

## Taste round (for UI-work only; run AFTER correctness lands SAFE TO SHIP)

The loop above catches correctness. UI-work also needs a TASTE pass — misalignments, generic-AI aesthetic, house-style drift. Runs ONCE after the last correctness round exits SAFE TO SHIP, not woven in.

Adapted from `pbakaus/impeccable`'s critique pattern (two independent assessments → synthesis → bounded fix cap; verified via github.com/pbakaus/impeccable 2026). Impeccable's own rule: **"Verify in bounded passes, not a loop... Open-ended self-QA burns the user's money."** Cap: 1 fix round after taste critique. No more.

### When it fires

- Diff touches HTML templates / view partials / CSS / rendered-view components (see `~/.claude/skills/visual-verify/SKILL.md` glob list)
- AND correctness round(s) landed `SAFE TO SHIP`
- AND regenerated screenshots exist for the touched surfaces

Skip for pure backend / infra / docs work.

### Two-assessment protocol

Two INDEPENDENT critic passes on the same regenerated shots. Sources kept distinct so they don't converge on the same blindspot:

- **Assessment A — defect critic**: fresh Opus dispatch with the `visual-verify` 4-step rubric (zoom → compare-vs-known-good-in-repo → walk checklist → severity-tagged findings). Anti-slop guardrails from `docs/reviews/component-sota-2026-07-31.md` (if present in project).
- **Assessment B — taste critic**: fresh Opus dispatch with a house-style rubric — pulls from the project's design spec + brand workbook + prior founder taste decisions (e.g. `docs/brand/klaffat-design-spec.md` §Locked variants + `docs/reviews/*` picks). Explicit anti-generic-AI list (see below).

Both use the SAME model + fixed prompt so scores are internally comparable (per SOTA research: cross-model taste-scoring agreement is ~zero; fix the model to compare).

### Synthesis

Parent takes both reports. Findings that BOTH assessments flag = high-confidence; investigate first. Findings unique to A xor B = flag for founder decision, don't auto-fix.

### Severity

Same vocabulary as correctness rounds + a taste tier:
- **blocker**: broken layout / unreadable / house-style violation the founder has explicitly ruled on before
- **should-fix**: obvious visual defect that the founder would catch in review
- **nit**: subjective polish; skip unless trivial

### Anti-generic-AI-aesthetic guardrails

From Anthropic `frontend-design` skill + practitioner blogs (design-bootcamp, aidesigner.ai 2026):

- Generic-blue CTA (`#3B82F6` shadcn/Tailwind default)
- Purple → blue gradient CTAs or backgrounds
- Glassmorphism (frosted-glass `backdrop-filter` on cards)
- Stock illustration (isometric-purple person-with-laptop)
- Emoji-in-heading substituting for icon
- Card grid where a list would do
- "Coming soon" placeholder that isn't
- Everything-center-aligned (no left-alignment for prose)

### Bounded fix cap

Max ONE fix pass after taste critique:
1. Fix everything both critics agree on (blocker + should-fix)
2. Fix single-critic blockers if reproducible against the artifact
3. Re-render affected shots
4. Founder-taste calls (A-xor-B findings) go to the founder as A/B/C variants via the founder-decision loop, NOT auto-fixed

If a second taste round is genuinely needed, that's a signal the design isn't converging → escalate to founder with variant fan-out (per SOTA research 2026-07-30), don't loop more times.

### When Anthropic frontend-design skill is available

Anthropic ships an official `frontend-design` skill (`npx skills add https://github.com/anthropics/skills --skill frontend-design`). If installed in the project, its rubric replaces Assessment B — it's the maintained upstream version of the taste-critic prompt.

## Exit

Final commit or chat message: round count + categories of bug caught (fail-open / resource leak / schema mismatch / taste-defect / anti-slop / …) + final verdict. Trains judgment for when future changes deserve the loop.
