---
name: advice-refine-test-loop
description: Use when shipping non-trivial changes that must land clean — the user says "look through carefully and fix what's off", "ask for advice and repeat", "keep refining until neat", or is going to bed. Runs four sequential phases (Opus + narrow specialists → Codex → adversarial → optional taste incl. Codex vision-taste 4b for UI diffs) with structured JSON verdicts, empirical repro, prefix-cached prompts, and a bounded consolidator sub-loop for LOWs (MEDIUMs surface but do not block).
---

# Advice-Refine-Test Loop

Single-pass review reliably misses fail-open paths, silently-swallowed exceptions, resource leaks, schema mismatches. Single-vendor review misses whatever that vendor's model is systematically blind to. Both-vendors-agree still misses what confirmation bias hides. Single-vendor VISUAL review misses geometric defects (radio-dot Y-centering, dot position, width alignment, selected-state color regressions) — the DiffSpot 2026 benchmark shows every frontier VLM misses 60%+ of true single-CSS-property defects, and pixel magnitude doesn't predict detection. Multi-vendor visual taste + a deterministic DOM-geometry oracle is the only combination that closes that gap.

The loop catches all four by staging the review:

- **Phase 1 (Opus + narrow specialists)** does broad correctness review plus targeted passes (secure defaults, footgun APIs) when the diff touches those areas.
- **Phase 2 (Codex/GPT-5.5)** cross-vendor review runs LAST so it only sees Opus-blessed state — preserves cross-vendor coverage while spending Codex's tighter ChatGPT Plus quota only on what Opus already cleared.
- **Phase 3 (adversarial)** is a single Opus dispatch framed as "assume the prior PASS was wrong, find why" — catches the confirmation-bias class both prior phases share.
- **Phase 4 (taste)** applies to UI work only, after all correctness phases pass. Split into 4a (Opus defect + taste critics, existing) and 4b (Codex vision-taste on regenerated shots, second vendor).

Every phase uses a **structured JSON verdict schema** (`verdict-schema.json`) so stop conditions are parseable, not vibes. Between phases, a **consolidator sub-loop** auto-fixes safe_auto LOW findings (severity × autofix_class AND-gate; see below).

## The Phases

```
PHASE 1:  Opus + specialists — loop rounds until verdict PASS
PHASE 2:  Codex (GPT-5.5) — loop rounds until verdict PASS
PHASE 3:  Adversarial — single Opus round with "prove PASS wrong" framing
          (findings loop back to Phase 1)
PHASE 4a: Taste round (UI-only, ONCE) — Opus defect + taste critics
PHASE 4b: Codex vision-taste (UI-only, ONCE) — second-vendor VLM on the
          regenerated shots; combined with geometry-oracle output via
          union + confidence tiers (see "Phase 4b" section below)
```

At end of each phase's PASS, if the final round emitted `PASS_WITH_FIXES`, the **consolidator sub-loop** runs before advancing (see "Consolidator" below).

Each Phase 1/2 round follows the same shape:

```
1. SELF-REVIEW: read each changed file top-to-bottom (not just diff)
2. VERIFY: tests + syntax + smoke green before inviting external eyes
3. ADVISE: dispatch the phase's advisor (Opus in phase 1, Codex in phase 2)
4. PARSE + CROSS-VALIDATE verdict: `jq -r '.verdict'` on advisor output; if not parseable JSON, the round FAILED. Then **cross-validate verdict against findings** (the JSON schema enum can't enforce this on its own — `codex --output-schema` and jq both accept incoherent combinations):

   ```bash
   verdict=$(jq -r '.verdict' <<<"$out")
   blockers=$(jq '[.findings[] | select(.severity=="BLOCKER" or .severity=="HIGH")] | length' <<<"$out")
   findings_count=$(jq '.findings | length' <<<"$out")
   bad=0

   # FIRST: verdict must be one of the three enum values. This gate catches
   # null (missing .verdict), typos, and any string Opus might emit that
   # advisor.md's prompt-only enforcement failed to constrain.
   [[ "$verdict" =~ ^(PASS|PASS_WITH_FIXES|BLOCKS)$ ]] || bad=1

   # THEN cross-validate coherence between verdict and findings.
   if [[ "$verdict" == "PASS" && "$findings_count" -gt 0 ]]; then bad=1; fi
   if [[ "$verdict" == "PASS_WITH_FIXES" && "$blockers" -gt 0 ]]; then bad=1; fi
   if [[ "$verdict" == "BLOCKS" && "$blockers" -eq 0 ]]; then bad=1; fi
   ```

   Any of: unparseable JSON, missing `.verdict`, or verdict/findings mismatch → the round FAILED. Re-dispatch once with an explicit "output valid JSON matching verdict-schema.json AND ensure verdict matches findings (PASS = empty findings; PASS_WITH_FIXES = no BLOCKER/HIGH; BLOCKS = ≥1 BLOCKER/HIGH)" reminder.

   Second failure PAUSEs the loop (interactive: prompt user; unattended: append to `pending_for_human.md` and exit `pending-human`). NEVER treat unparseable output or a mismatched verdict as advance-worthy — an incoherent advisor response has produced zero trustworthy coverage.
4.5. VALIDATE: dispatch a fresh Opus subagent (`Agent(subagent_type="advisor", ...)`) with ONLY the findings JSON + absolute paths of files referenced by findings. Prompt: "For each finding in the input array, read the file at the given line. Emit a JSON array `[{index, verified, unverifiable_confirmed, note}]` where `index` is the 0-based position of the finding in the input array. `verified=true` iff the described condition is empirically present at that location. `unverifiable_confirmed=true` when the finding was tagged `unverifiable: true` by the parent advisor AND you agree no empirical check applies (subjective — taste, 'could be simpler', style preferences). `verified=false` = parent drops `findings[index]` from the report; `unverifiable_confirmed=true` is informational (parent already knows the tag; you're confirming it can't be checked) and the finding is kept as-is." Parent drops `findings[i]` where the validator returned `verified: false` for that index before proceeding to step 5. Unverifiable findings (whether or not the validator confirmed) continue to step 5 as-is — repro is still attempted; often it will confirm nothing, which is fine.

   **Failure-mode handling (degrade-to-pre-4.5 on any failure).** Step 4.5 is an optimization, not a correctness gate — do NOT retry, do NOT PAUSE the loop:
   - Wrap the validator dispatch with `timeout 180s` (bounded — validator is a cheap read-only pass).
   - If the validator returns quota-exhausted, times out, produces malformed JSON, or emits a verdict-missing / unparseable result on the FIRST try: skip step 4.5 for this round entirely, log `"validator unavailable: <cause>"` in the round's commit message, and proceed to step 5 with ALL findings intact (no drops, no tags — parent behaves as if step 4.5 did not exist).
   - This is safe by construction: the worst case is repro effort in step 5 on findings that might have been hallucinations; the benefit is no PAUSE storm from an ancillary check. Phase 2's PAUSE ladder exists because Codex is the cross-vendor coverage lane — irreplaceable. Step 4.5's validator is one advisor of many; skipping it once is cheaper than pausing the loop.
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
  "findings": [{"severity": "…", "autofix_class": "…", "file": "…", "line": N, "rationale": "…"}]
}
```

**Severity × autofix_class are orthogonal axes.** `severity` is urgency (how bad if this ships); `autofix_class` is apply-authority (can this be applied without a human call). Conflating them ships wrong fixes: a `severity=LOW` rename that ripples across 40 files is NOT `safe_auto` — it's `gated_auto` (mechanically fine but wants human ack) or `manual` (needs human implementation). Consolidator gates on the AND of both axes (see below); severity alone is not license to auto-apply. `advisory` findings are surface-only and never auto-fixed regardless of severity.

- **PASS** = ZERO findings (empty `findings` array). Advance immediately, no consolidator needed.
- **PASS_WITH_FIXES** = at least one MEDIUM or LOW finding, but no BLOCKER/HIGH. Consolidator runs before phase advance (auto-fixes `severity=LOW AND autofix_class=safe_auto` only; other LOWs surface alongside MEDIUMs in the round's commit message for the human to triage but do NOT block advance). If a MEDIUM ought to block, it should have been a HIGH — trust the advisor's severity.
- **BLOCKS** = ≥1 BLOCKER or HIGH; next round required.

The advisor MUST NOT emit `PASS` with a non-empty findings array — that combination is unreachable in the state machine. LOW-only remainders are `PASS_WITH_FIXES`.

Codex enforces the schema via `codex exec --output-schema ~/.claude/skills/advice-refine-test-loop/verdict-schema.json …`. Opus advisor enforces via prompt instruction (advisor.md system prompt requires JSON output).

## Phase 1: Opus + narrow specialists

**Primary dispatch:** `Agent(subagent_type="advisor", prompt=...)` — read-only Opus reviewer; see ~/.claude/agents/advisor.md. NEW SESSION REQUIRED after editing the advisor agent file — Claude Code loads agent definitions at session start. If `advisor` is unknown in the current session, restart before continuing the loop; do NOT fall back to `general-purpose` (loses both invariants: whitelist + bypassPermissions).

**Narrow-specialist sidecars (parallel with primary):** dispatch alongside the general advisor when the diff touches areas the specialist targets. Broad-review LLMs miss things narrow specialists surface reliably (per Gooen RoastMyPost pattern: many specialists > one broad critic).

| Specialist | Trigger — dispatch when diff touches | How to dispatch |
|---|---|---|
| `insecure-defaults` skill | secrets loading, auth code, env-var-driven config, module defaults, feature flags | Fresh Opus subagent via `Agent(subagent_type="advisor", prompt="Invoke the insecure-defaults skill on <paths>. If Skill tool is unavailable in this session (advisor.md was recently edited without a session restart), fall back to applying the skill's checklist manually from your training. Emit findings in verdict-schema.json format.")` |
| `sharp-edges` skill | new public API surface, config schema, CLI flag design, cryptographic function ergonomics | Same shape as above |

**advisor.md now includes `Skill` in its tool whitelist** — safe because Skill invocation is metadata-only (loads instructions into the subagent's context); any tool the loaded skill prescribes that is not in the whitelist still fails, preserving the read-only invariant. NEW SESSION REQUIRED after editing advisor.md; specialist dispatches in a stale session fall back to manual checklist application (still useful, marginally lower recall).

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

**Usage-limit fallback:** if the call returns "usage limit" / "quota exceeded" / HTTP 429, re-run ONCE with `-m gpt-5.4` (lighter tier still on Plus quota). If that also fails, **PAUSE the loop and route to the user**:

- **Interactive session:** print "Phase 2 blocked: Codex quota exhausted at <ts>. Options: (a) wait <T> for reset then resume, (b) ship consciously without cross-vendor coverage, (c) abort." Wait for user decision.
- **Unattended session** (loop triggered by "going to bed" / cron / no live human): append to `pending_for_human.md` per the CLAUDE.md format (`## <date>: Codex Phase 2 quota-exhausted / Why blocked / Steps for human / Everything else done`). Do NOT advance and do NOT ship. Exit the whole skill with status `pending-human`.

**All four failure modes (quota-exhausted / timeout / connectivity / verdict-missing) route to the SAME PAUSE handler on second failure** — the loop stops, unattended runs write to `pending_for_human.md` and exit `pending-human`, interactive runs prompt the user. Never fall through silently. Phase 2 has only two ways to reach Phase 3: an emitted `PASS` or `PASS_WITH_FIXES` verdict. Everything else PAUSEs.

**Timeout:** wrap the codex exec call with a wall-clock cap (`timeout 300s codex exec ...`). A model hang or long stall past 5 minutes is treated as connectivity failure → retry once after 30s → if second try also stalls, PAUSE per above.

**Connectivity failure:** if codex exits with network error, retry once after 30s. On second failure, PAUSE per above.

**Verdict-missing:** if `jq -r '.verdict'` prints `null` or errors, treat the round as failed and re-run once with an explicit "your last output was not valid JSON — emit exactly the verdict-schema.json shape" reminder. On second verdict-missing, PAUSE per above (do NOT silently advance — a Codex round that can't emit a parseable verdict has produced zero coverage).

**Budget expectation:** Phase 2 should usually complete in 1-2 rounds because Opus already caught the bulk. If Codex demands ≥3 rounds, that IS the signal — Codex saw something Opus missed, and the extra rounds are the reason Phase 2 exists.

Loop rounds until Codex emits `PASS` (or `PASS_WITH_FIXES`; consolidator handles). Then move to Phase 3.

## Phase 3: Adversarial round (single Opus dispatch)

After Phase 2 PASS. One shot; the verdict determines advance:

- `PASS` or `PASS_WITH_FIXES` (findings ≤ MEDIUM) → advance to Phase 4. MEDIUMs and LOWs surface in the exit commit message; consolidator handles LOWs; MEDIUMs are surfaced for the human but do NOT trigger a Phase 1 reset (the whole point of "PASS_WITH_FIXES advances" from earlier phases applies here too — a MEDIUM adversarial finding that would have been BLOCKER-worthy should have been emitted as HIGH by the adversarial prompt).
- `BLOCKS` (any BLOCKER or HIGH) → loop resets to Phase 1 with the new findings pre-seeded so both vendors re-review the fix.

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
- For each finding, emit both `severity` (urgency) and `autofix_class` (apply-authority — `safe_auto` / `gated_auto` / `manual` / `advisory`). These are orthogonal; do not conflate. Tag findings whose confirmation requires human judgment (taste, style, "could be simpler") as `unverifiable: true` — Stage 4.5 keeps them but marks them.

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

## Consolidator sub-loop (PASS_WITH_FIXES remainder)

When a phase's final round emits `PASS_WITH_FIXES` (MEDIUM and/or LOW findings remain, no BLOCKER/HIGH), run the consolidator BEFORE advancing to the next phase. **Consolidator auto-fixes `severity=LOW AND autofix_class=safe_auto` only.** LOWs with `autofix_class` in `{gated_auto, manual, advisory}` surface in the phase's exit commit message for the human — auto-fixing a rippling rename or a design call is exactly the class of thing that turns a nit into a merge conflict. MEDIUMs (any autofix_class) also surface for human triage; auto-fixing them without reproduction is risky (they're "latent bug or future trap", not nits).

Parent MUST filter the JSON findings to `severity=LOW AND autofix_class=safe_auto` before dispatching (`jq '[.findings[] | select(.severity=="LOW" and .autofix_class=="safe_auto")]'`). If nothing passes the AND filter, skip the consolidator entirely and go straight to the exit-commit-message surface step — the remaining LOWs go to the human alongside the MEDIUMs.

Dispatch: `Agent(subagent_type="general-purpose", prompt=...)` with the following prompt shape (does NOT use the `advisor` type — consolidator needs Edit access):

```
Fix these LOW findings in the listed files. One commit per finding, per-file
granularity. After ALL fixes: re-verify (run the same test/lint/smoke the
main loop uses). Report per-finding: [fixed | skipped: <reason>].

Findings:
<JSON array containing ONLY findings where severity=LOW AND autofix_class=safe_auto
 — parent MUST filter out MEDIUMs and any LOW whose autofix_class is
 gated_auto/manual/advisory before dispatching>
```

**Bounded to 3 rounds** — if 3 consolidator rounds don't clear the LOW list, exit with what's left in a commit message. Don't infinite-loop trivial fixes; the point of PASS_WITH_FIXES is fast handling, not exhaustive polish.

**Consolidator per-finding skip is a first-class outcome** — a nit that turns out to require a design call ("rename this variable" that would ripple through 40 files) gets skipped with a reason, not force-fixed.

**Error handling** — if the consolidator subagent crashes, exceeds quota, or produces partial commits mid-batch:
- Parent inspects `git status` on return. Uncommitted mutations from a crashed consolidator are `git stash push -u -m "consolidator-crash-<phase>-<round>"`ed (never `git reset --hard` — preserve evidence).
- Partial commits (some findings fixed, some not) stay in place — subsequent rounds see them as prior-round work and skip the corresponding findings.
- After 3 crash-outs or a total exceeding the round budget, exit the sub-loop and note "consolidator unavailable: <cause>" in the phase's exit commit message. Remaining LOWs get logged to `pending_for_human.md` if the run is unattended.

After consolidator exits (clean or bounded), advance to the next phase using the post-consolidator state as the new baseline.

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
| **Skip Phase 4b when the diff touches component templates or CSS** | **Red flag.** The drift-back class of bugs (dot-above-text, selected-state regression, red-title-on-non-danger, combobox-narrower-than-control) survives Phases 1-4a — every frontier VLM misses 60%+ of true CSS defects (DiffSpot 2026), and a single vendor's blind spot repeats across rounds. A second-vendor visual reviewer (Codex vision-taste) plus the geometry oracle is the only pipeline shape that catches them. Skipping 4b on a UI-touching diff ships the exact regression class the founder has already flagged multiple times. |
| Auto-fix a singleton VLM finding without oracle backing | Session-constraint violation. Singleton visual claims can be hallucinations (UI-Lens: VLMs invent defects, e.g. flagging intentional spacing). Verify against the geometry oracle first; if the oracle can't arbitrate, surface to the founder as A/B, don't auto-fix. |
| Auto-block on a singleton VLM finding | Session-constraint violation. `Never singleton-VLM = auto-block.` Bug the ledger, not the ship gate. Only geometry-oracle findings or 2+ reviewer convergence gate the ship. |
| Sigil on a partial Phase 4b | Session-constraint violation. `Sigil requires reviewer confirmation.` The sigil-ready state needs: oracle green + 4b run to completion + all HIGH resolved. A skipped or half-run 4b is not sigil-ready. |
| Add Gemini vision without an API key wired | Would silently degrade to skipping phase 4c and calling the pipeline complete. Note as future upgrade in the skill; do not add as a dispatch step until the API dependency is real. |
| Auto-fix based on severity alone | Severity is urgency, not apply-authority. A LOW rename that ripples 40 files is not `safe_auto` — it's `gated_auto` or `manual`. Consolidator MUST AND `severity=LOW` with `autofix_class=safe_auto`; anything else surfaces for the human. |
| Skip step 4.5 (validate) because "advisor is trustworthy" | Advisors hallucinate file paths, line numbers, and stale-state claims (they read pre-fix state). Validator is cheap (small prompt, file re-read) and catches the class before it becomes a wasted repro round in step 5. |

## Phase 4a: Taste round (UI-work only; run ONCE after Phases 1-3 all PASS)

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

## Phase 4b: Codex vision-taste (UI-work only; run ONCE after Phase 4a)

Founder observation: primary Claude visual review misses geometric defects the founder catches on first look — radio-dot Y-centering, dot position, width alignment, selected-state color regressions. The DiffSpot 2026 benchmark (Tencent WeChat AI, arXiv 2605.29615) confirms this generalizes: every frontier VLM catches ≤40.7% of true single-CSS-property defects, and pixel magnitude does not predict detection (r = −0.08). UI-Lens (CVPR 2026) shows the same pattern for boundary defects (overflow, cropping, overlap) at coin-flip accuracy. A second-vendor VLM + a deterministic DOM-geometry oracle is the only combination that closes the gap; single-vendor visual review is not sufficient regardless of which single vendor.

Phase 4b runs a scripted Codex vision-taste pass on the shots regenerated by Phase-R, then merges its findings with the Opus-taste output from 4a and the geometry-oracle output via union + confidence tiers. Codex is the second vendor because it has vision support and a wired-in quota here; see the "Gemini future upgrade" note below.

### When it fires

- Diff touches HTML templates / view partials / CSS / rendered-view components (same trigger as 4a)
- AND Phase 4a completed
- AND regenerated shots exist for the touched surfaces
- AND the project has a geometry oracle (e.g. `tests/e2e/tests/component-geometry-oracle.spec.ts`) emitting JSON violations — if absent, run Codex vision-taste alone and treat its findings as singleton (no ground-truth arbiter)

Skip for pure backend / infra / docs work.

### Codex vision invocation

Codex CLI supports image input via `--image` (verified: v0.24.0+ scripted; v0.115.0 Mar 2026 for full-resolution, no thumbnail compression). Requires a vision-capable model (`gpt-5-codex` or newer). Approx 1-3k vision tokens per image per turn.

```bash
# One invocation per surface (component/page). Multiple shots per surface go
# as a comma-separated list — Codex sees them all in the same turn.
SHOTS="/abs/path/to/component-radio.png,/abs/path/to/component-radio-selected.png"
PROMPT="$(cat <<'EOF'
You are a design defect reviewer. You are the SECOND VENDOR in a multi-model
taste pipeline — the primary Claude review already ran; your job is to catch
what it missed. Bias toward reporting; a downstream arbiter suppresses false
positives.

FIXED visual rules (session constraints — a violation is a HIGH or BLOCKER):
- Radio dot renders LEFT of text, ALWAYS (never dot-above, never dot-right).
- Radio dot Y-center aligns with the option box's Y-center (or first-line
  center on wrapped labels — never middle of the wrapped block).
- Selected radio state = dark rail-blue background + white text. Light-fill
  selected states are a regression.
- Dialog title uses --strike red ONLY for --danger variant. Reopen, Confirm,
  and other non-danger dialogs use --ink.
- Combobox has no chevron glyph (native <select> chrome only, if any).
- Combobox dropdown <li> items fill the full width of the combobox control.
- Timegrid busy overlay width = column width; cell width = column width.

KNOWN drift-backs (regressed at least once before — flag on sight):
- Radio dot renders ABOVE text instead of LEFT (segmented-pill / --block
  variants prone to this).
- Selected radio reverts to light fill instead of dark rail-blue + white.
- Dialog title turns red on non-danger dialogs.
- Combobox items narrower than the control.
- Timegrid busy overlay narrower than the column.

For EACH image, emit a JSON object matching the verdict-schema.json used
elsewhere in this pipeline. Findings must include:
  - severity: BLOCKER | HIGH | MEDIUM
  - file: image basename
  - screen_region: rough bbox or region name (e.g. "top-left option in
    radiogroup", "dialog title")
  - rule: which fixed-rule or drift-back number the finding violates
  - rationale: ≤200 chars

If nothing to flag on an image, emit an empty findings array for that shot.
Do not report LOW / nit — the geometry oracle and 4a already cover polish.
EOF
)"

codex exec --skip-git-repo-check \
  -m gpt-5-codex \
  --image "$SHOTS" \
  --output-schema ~/.claude/skills/advice-refine-test-loop/verdict-schema.json \
  "$PROMPT" > /tmp/codex-vision-taste-<surface>.json
```

Repeat per surface. Store outputs under a run-scoped dir (e.g.
`/tmp/design-review-YYYY-MM-DD/vision-taste/`). Do not batch every surface
into one invocation — Codex vision recall drops sharply past ~4 images per
turn, per DiffSpot Hard-tier data.

**Failure modes** follow the same PAUSE rules as Phase 2:

- Quota exhausted → retry once with `-m gpt-5.4` (lighter tier still has
  vision on Plus quota); on second failure, PAUSE per Phase 2 rules
  (interactive: prompt; unattended: `pending_for_human.md`).
- Timeout (wrap with `timeout 300s`) → retry once after 30s → PAUSE.
- Unparseable JSON → re-run once with an explicit schema reminder → PAUSE.

Never silently skip 4b. The whole point of the second vendor is coverage of
what 4a misses; a skipped 4b is worse than a skipped Phase 2 because the
drift-back class of defects survives phases 1-4a.

### Combiner algorithm

Three inputs to merge:

1. **Geometry-oracle findings** — JSON from `tests/e2e/tests/component-geometry-oracle.spec.ts` (or project's equivalent). Deterministic pixel/color assertions. **Always take** — this is ground truth, not a taste opinion.
2. **Opus advisor findings** — output from Phase 4a Assessments A + B.
3. **Codex vision-taste findings** — the per-surface JSON blobs written above.

Bucket findings by `(surface, element_selector_if_any, rule)`. For each bucket:

| Convergence | Confidence | Action |
|---|---|---|
| Geometry-oracle finding (any) | GROUND TRUTH | Always auto-fix. Not a vote; the DOM says the pixels are wrong. |
| 2+ independent reviewers agree (e.g. Opus + Codex; or oracle + either) | HIGH | Auto-fix without further verification. |
| Singleton VLM finding (Opus xor Codex, no oracle backing) | LOW | Verify-then-fix. Arbiter: **the geometry oracle**. If the oracle can be extended to numerically check the claim, extend it and re-run; if the oracle confirms, fix; if the oracle contradicts, discard as a hallucinated defect (VLMs invent defects — UI-Lens explicitly documents this: "misinterpreting intentional spacing ... as an abnormal blank space"). If the claim is genuinely subjective (gestalt, "does this look right") and the oracle can't arbitrate, **surface to the founder as an A/B variant fan-out, do NOT auto-fix**. |
| Singleton VLM finding with no artifact and no reproducible check | LOW | Log to ledger with disposition `singleton-unverifiable`. Do not auto-fix. Do not auto-block. |

**Session constraint (verbatim, load-bearing)**: Never singleton-VLM = auto-block. A single Codex or Opus visual claim without oracle backing does not stop the ship; it goes to the ledger and (if reproducible) the founder.

**Sigil constraint (verbatim, load-bearing)**: Sigil requires reviewer confirmation. The final sigil-ready state must show at minimum the geometry-oracle green + Phase 4b run to completion + all HIGH-tier findings resolved. Do not sigil on a partial 4b.

### Per-model disposition ledger

Every 4b run appends to `docs/reviews/multi-model-taste-ledger.md` (project-local). One row per bucket, so per-model precision can be measured over time (sortie's pattern):

```
| run-id | date | surface | rule | opus | codex | oracle | disposition | outcome |
|--------|------|---------|------|------|-------|--------|-------------|---------|
| r4-... | 2026-08-03 | c-radiogroup | dot-y-center | HIT | HIT | HIT | auto-fix (2+) | fixed |
| r4-... | 2026-08-03 | c-dialog --danger | red-title | HIT | MISS | n/a | verify-then-fix (singleton-Opus, oracle can't check) | founder-A/B |
| r4-... | 2026-08-03 | combobox | chevron-present | MISS | HIT | HIT | auto-fix (2+) | fixed |
```

Columns:
- `run-id` — links the row to the round (e.g. `r4-2026-08-03`)
- `opus` / `codex` — `HIT` / `MISS` / `n/a` (n/a = surface not in that reviewer's dispatch)
- `oracle` — `HIT` / `MISS` / `n/a` (n/a = oracle can't check this rule)
- `disposition` — one of `auto-fix (2+)`, `auto-fix (oracle)`, `verify-then-fix (…)`, `singleton-unverifiable`, `founder-A/B`
- `outcome` — `fixed` / `deferred` / `founder-picked-<variant>` / `discarded-as-hallucination` — filled retrospectively after the fix loop

After ~20 runs the ledger surfaces per-model precision; use it to weight reviewers, not before. Until then, treat Opus and Codex as equal-weight peers; the oracle is always arbiter.

### Gemini — future upgrade, not this round

DiffSpot ranks Gemini (2.5 Pro / 3.x Pro) as the strongest visual perceiver (47.2% acc, 40.7% recall — best in class); UI-Lens ranks Gemini 2.5 Pro top-2 on F1 with the highest recall (81.4%) of any tested model. If Gemini API access becomes available in this shop, add as **phase 4c** using the same invocation shape and combiner rules — Gemini's high-recall / low-precision profile is exactly what a second miss-catching reviewer wants, and the arbiter+oracle mechanics already absorb the extra false positives. Do NOT add Gemini this round: no API key is wired, and adding a dependency the loop can't invoke would silently degrade to skipping 4c and calling it done.

## Composition table (skill-facing summary)

| Phase | Vendor | Reviewer role | Combines with | Skip when |
|---|---|---|---|---|
| 1 | Anthropic (Opus + specialists) | Broad correctness + narrow security/API passes | Own phase output only | Never; foundation phase |
| 4.5 (in-round) | Anthropic (Opus, validator) | Re-verify findings against actual file state; array-index correlation `[{index, verified, unverifiable_confirmed, note}]`; parent drops `findings[i]` on `verified=false`; `unverifiable_confirmed` informational | Feeds into step 5 (reproduce) | On any validator failure (timeout/quota/malformed JSON) skip this round and degrade to pre-4.5 behavior — never retry, never PAUSE |
| 2 | OpenAI (Codex / GPT-5.5) | Cross-vendor correctness on Opus-clean state | Phase 1 fix history carried in prompt | Never; PAUSE on quota, don't silently skip |
| 3 | Anthropic (Opus, adversarial) | "Prior PASS was wrong — find why" | Findings loop back to Phase 1 | Never; one shot, cheap |
| 4a | Anthropic (Opus x2 — defect + taste critics) | UI defect + house-style critique | Own phase output only | Non-UI diffs |
| 4b | OpenAI (Codex vision-taste) on regenerated shots | Second-vendor VLM catch-what-Opus-missed | Union w/ geometry oracle (ground truth) + Phase 4a Opus taste, per `docs/reviews/multi-model-taste-ledger.md` protocol | Non-UI diffs; NEVER skip on UI-touching diffs — drift-back class survives 1-4a |
| 4c (future) | Google (Gemini 2.5 Pro / 3.x) | Third-vendor VLM, high-recall miss catcher | Same combiner slot as 4b | No API key wired; not this round |

## Exit

Final commit or chat message: round count + categories of bug caught (fail-open / resource leak / schema mismatch / taste-defect / anti-slop / …) + final verdict. Trains judgment for when future changes deserve the loop.
