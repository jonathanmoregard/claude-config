---
name: advice-refine-test-loop
description: Use when shipping non-trivial changes that must land clean — the user says "look through carefully and fix what's off", "ask for advice and repeat", "keep refining until neat", or is going to bed. Runs four sequential phases (Opus + narrow specialists → Codex → adversarial → optional taste) with structured JSON verdicts, empirical repro, prefix-cached prompts, and a bounded consolidator sub-loop for LOW-only remainders.
---

# Advice-Refine-Test Loop

Single-pass review reliably misses fail-open paths, silently-swallowed exceptions, resource leaks, schema mismatches. Single-vendor review misses whatever that vendor's model is systematically blind to. Both-vendors-agree still misses what confirmation bias hides.

The loop catches all three by staging the review:

- **Phase 1 (Opus + narrow specialists)** does broad correctness review plus targeted passes (secure defaults, footgun APIs) when the diff touches those areas.
- **Phase 2 (Codex/GPT-5.5)** cross-vendor review runs LAST so it only sees Opus-blessed state — preserves cross-vendor coverage while spending Codex's tighter ChatGPT Plus quota only on what Opus already cleared.
- **Phase 3 (adversarial)** is a single Opus dispatch framed as "assume the prior PASS was wrong, find why" — catches the confirmation-bias class both prior phases share.
- **Phase 4 (taste)** applies to UI work only, after all correctness phases pass.

Every phase uses a **structured JSON verdict schema** (`verdict-schema.json`) so stop conditions are parseable, not vibes. Between phases, a **consolidator sub-loop** auto-fixes LOW findings without spending another advisor round.

## The Phases

```
PHASE 1: Opus + specialists — loop rounds until verdict PASS
PHASE 2: Codex (GPT-5.5) — loop rounds until verdict PASS
PHASE 3: Adversarial — single Opus round with "prove PASS wrong" framing
         (findings loop back to Phase 1)
PHASE 4: Taste round (UI-only, ONCE) — see "Taste round" section below
```

At end of each phase's PASS, if the final round emitted `PASS_WITH_FIXES`, the **consolidator sub-loop** runs before advancing (see "Consolidator" below).

Each Phase 1/2 round follows the same shape:

```
1. SELF-REVIEW: read each changed file top-to-bottom (not just diff)
2. VERIFY: tests + syntax + smoke green before inviting external eyes
3. ADVISE: dispatch the phase's advisor (Opus in phase 1, Codex in phase 2)
4. PARSE verdict: `jq -r '.verdict'` on advisor output. If not parseable JSON,
   the round FAILED — re-dispatch once with an explicit "output valid JSON
   matching verdict-schema.json" reminder. Second failure exits the phase.
5. REPRODUCE each BLOCKER/HIGH finding before fixing — advisors hallucinate
6. FIX by severity, re-verify after each
7. COMMIT the round (per-round granularity, not a batch)
8. RE-ADVISE with a fresh prompt whose "Prior rounds fixed:" list carries
   the entire phase's fix history
9. On verdict `PASS` or `PASS_WITH_FIXES` → exit round loop; on `BLOCKS` →
   next round
```

## Verdict schema

Both lanes emit JSON matching `~/.claude/skills/advice-refine-test-loop/verdict-schema.json`:

```json
{
  "verdict": "PASS" | "PASS_WITH_FIXES" | "BLOCKS",
  "verdict_reason": "<one-line>",
  "findings": [{"severity": "…", "file": "…", "line": N, "rationale": "…"}]
}
```

- **PASS** = no findings above LOW; ship after consolidator handles any LOWs.
- **PASS_WITH_FIXES** = only LOW findings remain; consolidator auto-fixes.
- **BLOCKS** = ≥1 BLOCKER or HIGH; next round required.

Codex enforces the schema via `codex exec --output-schema ~/.claude/skills/advice-refine-test-loop/verdict-schema.json …`. Opus advisor enforces via prompt instruction (advisor.md system prompt requires JSON output).

## Phase 1: Opus + narrow specialists

**Primary dispatch:** `Agent(subagent_type="advisor", prompt=...)` — read-only Opus reviewer; see ~/.claude/agents/advisor.md. NEW SESSION REQUIRED after editing the advisor agent file — Claude Code loads agent definitions at session start. If `advisor` is unknown in the current session, restart before continuing the loop; do NOT fall back to `general-purpose` (loses both invariants: whitelist + bypassPermissions).

**Narrow-specialist sidecars (parallel with primary):** dispatch alongside the general advisor when the diff touches areas the specialist targets. Broad-review LLMs miss things narrow specialists surface reliably (per Gooen RoastMyPost pattern: many specialists > one broad critic).

| Specialist | Trigger — dispatch when diff touches | How to dispatch |
|---|---|---|
| `insecure-defaults` skill | secrets loading, auth code, env-var-driven config, module defaults, feature flags | Fresh Opus subagent via `Agent(subagent_type="advisor", prompt="Invoke the insecure-defaults skill on <paths>. Emit findings in verdict-schema.json format.")` |
| `sharp-edges` skill | new public API surface, config schema, CLI flag design, cryptographic function ergonomics | Same shape as above |

Dispatch specialists in the SAME tool-use block as the primary Opus advisor (parallel — independent read-only reviews, no shared state). Merge findings by (file, line) dedupe; specialist findings inherit the primary's phase counter but are tagged in commit messages (`[specialist:insecure-defaults]`). If BOTH the primary and a specialist flag the same location, treat as higher-confidence — reproduce first.

Skip specialists when the diff is clearly out-of-scope (pure prose docs, VM test fixture changes, cosmetic renames). Rule of thumb: if you'd forward the diff to a security reviewer at your org, dispatch the specialist.

**Usage-limit fallback (primary + specialists):** if any dispatch returns with "usage limit" in the result (Fable/default org-monthly quota exhausted), re-dispatch ONCE with `model="claude-opus-4-7"` — Opus is a separate quota bucket and typically completes. Do this only on limit-hit; other failures (crash, timeout, refusal) get diagnosed, not fallback-masked. Never re-dispatch a third time — if Opus also 429s, the round exits with a note in the round's commit message.

Loop rounds until primary AND all dispatched specialists return `PASS` (or `PASS_WITH_FIXES`; consolidator handles). Then move to Phase 2.

## Phase 2: Codex (GPT-5.5) advisor

Dispatch:

```bash
codex exec --skip-git-repo-check \
  --output-schema ~/.claude/skills/advice-refine-test-loop/verdict-schema.json \
  "<PROMPT>"
```

`--output-schema` forces valid JSON matching the shared verdict schema — no regex parsing on prose. Codex CLI runs GPT-5.5 (per ~/.codex/config.toml default, ChatGPT Plus auth). Sandbox is read-only by default; do NOT pass `--dangerously-*` flags. The prompt template is the same "Advisor Prompt Shape" used in Phase 1, but with an updated "Prior rounds fixed:" list covering the entire Phase 1 history (primary + specialists) so Codex doesn't re-flag what was already caught.

Parse: `verdict=$(codex exec … | jq -r '.verdict')`. Values: `PASS`, `PASS_WITH_FIXES`, `BLOCKS`.

**Usage-limit fallback:** if the call returns "usage limit" / "quota exceeded" / HTTP 429, re-run ONCE with `-m gpt-5.4` (lighter tier still on Plus quota). If that also fails, exit the phase with a note in the round's commit message — do NOT ship as "SAFE" without Codex verdict; the whole point of Phase 2 is cross-vendor coverage. Pause and let the user decide (wait for quota reset, or ship consciously without Codex).

**Connectivity failure:** if codex exits with network error, retry once after 30s. On second failure, pause the phase (same as quota-exhausted).

**Verdict-missing:** if `jq -r '.verdict'` prints `null` or errors, treat the round as failed and re-run once with an explicit "your last output was not valid JSON — emit exactly the verdict-schema.json shape" reminder. Second failure exits the phase.

**Budget expectation:** Phase 2 should usually complete in 1-2 rounds because Opus already caught the bulk. If Codex demands ≥3 rounds, that IS the signal — Codex saw something Opus missed, and the extra rounds are the reason Phase 2 exists.

Loop rounds until Codex emits `PASS` (or `PASS_WITH_FIXES`; consolidator handles). Then move to Phase 3.

## Phase 3: Adversarial round (single Opus dispatch)

After Phase 2 PASS. One shot; if it emits any findings above LOW, the finding is treated as a genuine BLOCKER and the loop resets to Phase 1 (so both vendors re-review the fix). If it emits PASS or PASS_WITH_FIXES, advance.

Dispatch: same `Agent(subagent_type="advisor", …)` as Phase 1, but the prompt is framed adversarially:

```
Assume both prior phases were wrong. Something ships broken.
Given the state of these files, what did they miss?

- Read <files> top-to-bottom.
- Do NOT re-verify what prior rounds already fixed (listed below); look for
  what they OVERLOOKED.
- Specifically hunt: fail-open paths, silent-drop interactions, resource
  leaks under contention, schema mismatches across process boundaries,
  time-of-check-to-time-of-use, framework defaults nobody remembered.
- If nothing new, emit verdict PASS with empty findings.
```

**Budget:** exactly 1 dispatch. If findings emerge → back to Phase 1 with the new findings pre-seeded. Do not loop adversarial itself — the loop lives at the phase-1-to-phase-3 level.

**Why one shot:** confidence-check on the loop, not another full loop. If adversarial keeps finding real things across resets, the diff is too big — split it.

## Advisor Prompt Shape

Order the prompt for maximum implicit prefix caching (both Anthropic + OpenAI cache stable prefixes automatically; putting varying content last lets rounds 2+ pay only for the delta):

**Stable prefix (identical every round for a given diff):**
- **Project goal** (1–2 sentences)
- **Re-read fresh:** absolute file paths (don't trust advisor memory)
- **Output shape:** emit JSON matching `~/.claude/skills/advice-refine-test-loop/verdict-schema.json`. No prose outside JSON. (Codex enforces via `--output-schema`; Opus advisor.md system prompt enforces.)

**Variable delta (grows each round):**
- **Prior rounds fixed:** bulleted list — prevents re-flagging resolved issues. Include EVERY prior round in the current phase, plus every prior phase's PASS-time state summary.
- **Verified since last round:** what you empirically confirmed (repros run, tests re-run, docs read).
- **Seeded hypotheses:** your own doubts, phrased as "verify X". Advisors follow leads; give them the ones you have.

**Length cap:** ≤500 words total. Rationale strings ≤200 chars each. Advisor's job is triage, not exposition.

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

## Consolidator sub-loop (LOW-only remainder)

When a phase's final round emits `PASS_WITH_FIXES` (only LOW findings remain), run the consolidator BEFORE advancing to the next phase. LOW findings are nits and small ergonomics — not worth another full advisor round, but worth fixing before shipping.

Dispatch: `Agent(subagent_type="general-purpose", prompt=...)` with the following prompt shape (does NOT use the `advisor` type — consolidator needs Edit access):

```
Fix these LOW findings in the listed files. One commit per finding, per-file
granularity. After ALL fixes: re-verify (run the same test/lint/smoke the
main loop uses). Report per-finding: [fixed | skipped: <reason>].

Findings:
<JSON dump of the phase's LOW findings from the final advisor verdict>
```

**Bounded to 3 rounds** — if 3 consolidator rounds don't clear the LOW list, exit with what's left in a commit message. Don't infinite-loop trivial fixes; the point of PASS_WITH_FIXES is fast handling, not exhaustive polish.

**Consolidator per-finding skip is a first-class outcome** — a nit that turns out to require a design call ("rename this variable" that would ripple through 40 files) gets skipped with a reason, not force-fixed.

After consolidator exits, advance to the next phase using the post-consolidator state as the new baseline.

## Anti-Patterns

| Mistake | Why bad |
|---|---|
| Trust advisor without reproduction | Advisors confident even when wrong |
| Expand scope per advisor suggestion | "Could be cleaner" is not a blocker |
| Same prompt every round | Advisor re-flags fixed issues, wastes findings |
| Batch all rounds into one commit | Loses bisectable history |
| Skip re-verification after each fix | Regressions hide in the batch |
| Stop after round 1 because it "felt thorough" | Most valuable finding often lands in round 3 |
| Skip Phase 2 (Codex) because "Opus already said PASS" | Single-vendor blindness is the whole reason Phase 2 exists. If Codex is out of quota, PAUSE and tell the user — don't ship as PASS on a half-run loop. |
| Run Phase 2 in parallel with Phase 1 to "save time" | Wastes Codex quota on drafts Opus will reject. Sequential ordering is deliberate: Codex only sees Opus-clean state so its rounds are cheap. |
| Interleave phases (Opus round, Codex round, Opus round, …) | Same failure mode — Codex burns quota reviewing states you're about to change. Complete Phase 1 first, THEN start Phase 2. |
| Drop Phase 1's "prior rounds fixed" list when starting Phase 2 | Codex will re-flag everything Opus already caught, wasting its budget. Carry the full fix history forward into Phase 2's first prompt. |
| Skip narrow specialists because "general advisor will catch it" | Empirically wrong (per Gooen RoastMyPost). Narrow specialists surface a distinct class of issues (secure defaults, footgun APIs) at higher precision than broad review. Cost is 1-2 extra parallel subagent calls per Phase 1 round. |
| Skip Phase 3 (adversarial) because "both vendors already passed" | The whole point of adversarial is confirmation bias — both vendors can agree on a wrong answer. One extra dispatch per invocation. Cheap. |
| Loop adversarial itself when it emits findings | Adversarial is one-shot confirmation, not another main loop. Findings → back to Phase 1. If adversarial fires blocker after blocker across resets, split the diff instead. |
| Skip verdict JSON validation and trust prose | Prose verdicts drift ("looks fine", "no major issues", "SAFE-ish"). JSON schema is the stop condition; if it's missing, the round FAILED — don't guess intent. |
| Put "Prior rounds fixed:" at the START of the prompt | Kills prefix caching. Stable content first (goal, re-read paths, output shape), variable delta last. |

## Phase 4: Taste round (UI-work only; run ONCE after Phases 1-3 all PASS)

Phases 1-3 catch correctness. UI-work also needs a TASTE pass — misalignments, generic-AI aesthetic, house-style drift. Runs ONCE after Phase 3 emits PASS, not woven into the earlier phases.

Adapted from `pbakaus/impeccable`'s critique pattern (two independent assessments → synthesis → bounded fix cap; verified via github.com/pbakaus/impeccable 2026). Impeccable's own rule: **"Verify in bounded passes, not a loop... Open-ended self-QA burns the user's money."** Cap: 1 fix round after taste critique. No more.

### When it fires

- Diff touches HTML templates / view partials / CSS / rendered-view components (see `~/.claude/skills/visual-verify/SKILL.md` glob list)
- AND Phases 1-3 all landed `PASS`
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
