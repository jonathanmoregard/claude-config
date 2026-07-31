---
name: advice-refine-test-loop
description: Use when shipping non-trivial changes that must land clean — the user says "look through carefully and fix what's off", "ask for advice and repeat", "keep refining until neat", or is going to bed. Runs three sequential phases (Opus loop → Codex loop → optional taste pass) with empirical verification each round to catch latent bugs single-pass review misses.
---

# Advice-Refine-Test Loop

Single-pass review reliably misses fail-open paths, silently-swallowed exceptions, resource leaks, schema mismatches. Single-vendor review misses whatever that vendor's model is systematically blind to. The loop catches both via three sequential phases: Opus rounds until PASS → Codex rounds until PASS → optional taste pass. Codex runs LAST (not in parallel) so it only sees Opus-blessed state — this preserves the cross-vendor coverage while spending Codex's tighter budget (ChatGPT Plus quota) only on what Opus already cleared.

## The Phases

```
PHASE 1: Opus rounds — loop until Opus says SAFE TO SHIP
PHASE 2: Codex rounds — loop until Codex says SAFE TO SHIP
PHASE 3: Taste round (UI-only, ONCE) — see "Taste round" section below
```

Each Opus/Codex round follows the same shape:

```
1. SELF-REVIEW: read each changed file top-to-bottom (not just diff)
2. VERIFY: tests + syntax + smoke green before inviting external eyes
3. ADVISE: dispatch the phase's advisor (Opus in phase 1, Codex in phase 2)
4. REPRODUCE each blocker/high claim before fixing — advisors hallucinate
5. FIX by severity, re-verify after each
6. COMMIT the round (per-round granularity, not a batch)
7. RE-ADVISE with a fresh prompt that lists what prior rounds fixed
8. STOP the phase on explicit green-light ("SAFE TO SHIP" / "no blockers")
```

## Phase 1: Opus advisor

Dispatch: `Agent(subagent_type="advisor", prompt=...)` — read-only Opus reviewer; see ~/.claude/agents/advisor.md. NEW SESSION REQUIRED after editing the advisor agent file — Claude Code loads agent definitions at session start. If `advisor` is unknown in the current session, restart before continuing the loop; do NOT fall back to `general-purpose` (loses both invariants: whitelist + bypassPermissions).

**Usage-limit fallback:** if the dispatch returns with "usage limit" in the result (Fable/default org-monthly quota exhausted), re-dispatch ONCE with `model="claude-opus-4-7"` — Opus is a separate quota bucket and typically completes. Do this only on limit-hit; other failures (crash, timeout, refusal) get diagnosed, not fallback-masked. Never re-dispatch a third time — if Opus also 429s, the round exits with a note in the round's commit message.

Loop rounds until Opus emits `SAFE TO SHIP`. Then move to Phase 2.

## Phase 2: Codex (GPT-5.5) advisor

Dispatch: `mcp__safe-bash__exec("codex exec --skip-git-repo-check <PROMPT>")` — Codex CLI runs GPT-5.5 (per ~/.codex/config.toml default, ChatGPT Plus auth). Sandbox is read-only by default; do NOT pass `--dangerously-*` flags. The prompt template is the same "Advisor Prompt Shape" used in Phase 1, but with an updated "Prior rounds fixed:" list covering the entire Phase 1 history so Codex doesn't re-flag what Opus already caught.

**Usage-limit fallback:** if the call returns "usage limit" / "quota exceeded" / HTTP 429, re-run ONCE with `-m gpt-5.4` (lighter tier still on Plus quota). If that also fails, exit the phase with a note in the round's commit message — do NOT ship as "SAFE" without Codex verdict; the whole point of Phase 2 is cross-vendor coverage. Instead: pause and let the user decide (wait for quota reset, or ship consciously without Codex).

**Connectivity failure:** if codex exits with network error, retry once after 30s. On second failure, pause the phase (same as quota-exhausted).

**Read-verdict rule:** parse the last line of Codex output. Expected shape: `SAFE TO SHIP` or `BLOCKS SHIPPING: <reason>`. If the output has no verdict line, treat the round as failed (didn't complete the prompt shape) and re-run with an explicit "end with a single-line verdict" reminder.

**Budget expectation:** Phase 2 should usually complete in 1-2 rounds because Opus already caught the bulk. If Codex demands ≥3 rounds, that IS the signal — Codex saw something Opus missed, and the extra rounds are the reason Phase 2 exists.

Loop rounds until Codex emits `SAFE TO SHIP`. Then move to Phase 3 (if UI work) or exit.

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
| Skip Phase 2 (Codex) because "Opus already said SAFE TO SHIP" | Single-vendor blindness is the whole reason Phase 2 exists. If Codex is out of quota, PAUSE and tell the user — don't ship as "SAFE" on a half-run loop. |
| Run Phase 2 in parallel with Phase 1 to "save time" | Wastes Codex quota on drafts Opus will reject. Sequential ordering is deliberate: Codex only sees Opus-clean state so its rounds are cheap. |
| Interleave phases (Opus round, Codex round, Opus round, …) | Same failure mode — Codex burns quota reviewing states you're about to change. Complete Phase 1 first, THEN start Phase 2. |
| Drop Phase 1's "prior rounds fixed" list when starting Phase 2 | Codex will re-flag everything Opus already caught, wasting its budget. Carry the full fix history forward into Phase 2's first prompt. |

## Phase 3: Taste round (UI-work only; run ONCE after Phases 1 + 2 both PASS)

Phases 1 and 2 catch correctness. UI-work also needs a TASTE pass — misalignments, generic-AI aesthetic, house-style drift. Runs ONCE after Phase 2 emits SAFE TO SHIP, not woven into the earlier phases.

Adapted from `pbakaus/impeccable`'s critique pattern (two independent assessments → synthesis → bounded fix cap; verified via github.com/pbakaus/impeccable 2026). Impeccable's own rule: **"Verify in bounded passes, not a loop... Open-ended self-QA burns the user's money."** Cap: 1 fix round after taste critique. No more.

### When it fires

- Diff touches HTML templates / view partials / CSS / rendered-view components (see `~/.claude/skills/visual-verify/SKILL.md` glob list)
- AND both Phase 1 (Opus) and Phase 2 (Codex) landed `SAFE TO SHIP`
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

Same vocabulary as Phases 1-2 + a taste tier:
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
