# Phases 4a + 4b — UI taste review (reference)

**Read this file in full, or not at all.** Phase 4b's combiner takes Phase 4a's
two assessments as an input and buckets by a `surface` label 4a assigns; a
reader holding only half of this file cannot bucket anything. A truncated read
produces a coherent-looking taste pass with no signal that half the phase is
missing — which is indistinguishable from "4a-only was the plan", and is the
failure this warning exists to prevent.

Section names quoted below without a path ("Elicit risk, not policy", "Loop
bounds", the step-4 `jq` cross-validation) live in `../SKILL.md`, which the
parent already has in context when it reaches this file.

**Why a second vendor at all.** Single-vendor VISUAL review misses geometric
defects — radio-dot Y-centering, dot position, width alignment, selected-state
color regressions. The DiffSpot 2026 benchmark (Tencent WeChat AI, arXiv
2605.29615) shows every frontier VLM catches ≤40.7% of true single-CSS-property
defects, and pixel magnitude does not predict detection (r = −0.08). UI-Lens
(CVPR 2026) shows the same pattern for boundary defects (overflow, cropping,
overlap) at coin-flip accuracy. Multi-vendor visual taste plus a deterministic
DOM-geometry oracle is the only combination that closes that gap.

## Phase 4a: Taste round (UI-work only; run ONCE after Phases 1-3 all ADVANCE)

Phases 1-3 catch correctness. UI-work also needs a TASTE pass — misalignments, generic-AI aesthetic, house-style drift. Runs ONCE after Phase 3 advances, not woven into the earlier phases.

Adapted from `pbakaus/impeccable`'s critique pattern (two independent assessments → synthesis → bounded fix cap; verified via github.com/pbakaus/impeccable 2026). Impeccable's own rule: **"Verify in bounded passes, not a loop... Open-ended self-QA burns the user's money."** Cap: 1 fix round after taste critique. No more.

### When it fires

- Diff touches HTML templates / view partials / CSS / rendered-view components (see `~/.claude/skills/visual-verify/SKILL.md` glob list)
- AND Phases 1-3 all ADVANCED — each exited on a gate stop routed `advance` or
  `advance-with-findings-surfaced`. A `halt-and-pause` stop never reaches Phase
  4; the run is paused, not advanced.
- AND regenerated screenshots exist for the touched surfaces

Skip for pure backend / infra / docs work.

### Two-assessment protocol

Two INDEPENDENT critic passes on the same regenerated shots. Sources kept distinct so they don't converge on the same blindspot:

- **Assessment A — defect critic**: fresh Opus dispatch with the `visual-verify` 4-step rubric (zoom → compare-vs-known-good-in-repo → walk checklist → severity-tagged findings). Anti-slop guardrails from `docs/reviews/component-sota-2026-07-31.md` (if present in project).
- **Assessment B — taste critic**: fresh Opus dispatch with a house-style rubric — pulls from the project's design spec + brand workbook + prior founder taste decisions (e.g. `docs/brand/klaffat-design-spec.md` §Locked variants + `docs/reviews/*` picks). Explicit anti-generic-AI list (see below).

Both use the SAME model + fixed prompt so scores are internally comparable (per SOTA research: cross-model taste-scoring agreement is ~zero; fix the model to compare).

**Comparability is bought with correlated blind spots.** Different role labels on the same model do NOT make two independent reviewers — persona-differentiated same-model agents share the most errors. A+B agreement here means "worth looking at first", never "independently confirmed". Phase 4b's second vendor and the geometry oracle are what actually decorrelate; do not let a strong A+B agreement talk you into skipping either.

### Synthesis

Parent takes both reports. Findings that BOTH assessments flag = high-confidence; investigate first. Findings unique to A xor B = flag for founder decision, don't auto-fix.

### Severity

**4a emits verdict-schema.json, same as every other phase — no separate
vocabulary.** Both critics return one verdict object each, parsed by the same
`jq` cross-validation in step 4. There is no `blocker` / `should-fix` / `nit`
enum; those words are not schema-parseable and must not appear in a critic
prompt or a critic's output.

The critic prompt carries the schema's intrinsic-risk definitions unchanged —
blast radius, reversibility, silence, recoverability, read visually (how much of
the surface is wrong, how obvious it is, whether it survives a reload). Nothing
about what a grade triggers, and no rule of the form "a violation of X is
BLOCKER"; see "Elicit risk, not policy".

**The taste tiers are a parent-side severity FLOOR, applied AFTER parsing —
never in the prompt**, same mechanism as Phase 4b's:

- Rationale names a house-style rule the founder has explicitly ruled on before
  → floor `BLOCKER`.
- Rationale names broken layout or unreadable content → floor `HIGH`.
- Everything else keeps the critic's grade.

Findings whose confirmation is a taste call carry `unverifiable: true`, per the
schema.

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
1. Fix everything both critics agree on at `BLOCKER` or `HIGH`
2. Fix single-critic `BLOCKER`s if reproducible against the artifact
3. Re-render affected shots
4. Founder-taste calls (A-xor-B findings) go to the founder as A/B/C variants via the founder-decision loop, NOT auto-fixed

If a second taste round is genuinely needed, that's a signal the design isn't converging → escalate to founder with variant fan-out (per SOTA research 2026-07-30), don't loop more times.

### When Anthropic frontend-design skill is available

Anthropic ships an official `frontend-design` skill (`npx skills add https://github.com/anthropics/skills --skill frontend-design`). If installed in the project, its rubric replaces Assessment B — it's the maintained upstream version of the taste-critic prompt.

## Phase 4b: Codex vision-taste (UI-work only; run ONCE after Phase 4a)

Founder observation: primary Claude visual review misses geometric defects the founder catches on first look — radio-dot Y-centering, dot position, width alignment, selected-state color regressions. The benchmark evidence for that generalizing is in "Why a second vendor at all" above: a second-vendor VLM + a deterministic DOM-geometry oracle is the only combination that closes the gap; single-vendor visual review is not sufficient regardless of which single vendor.

Phase 4b runs a scripted Codex vision-taste pass on the shots regenerated for the touched surfaces, then merges its findings with the Opus-taste output from 4a and the geometry-oracle output via union + confidence tiers. Codex is the second vendor because it has vision support and a wired-in quota here; see the "Gemini future upgrade" note below.

### When it fires

- Diff touches HTML templates / view partials / CSS / rendered-view components (same trigger as 4a)
- AND Phase 4a completed
- AND regenerated shots exist for the touched surfaces

A geometry oracle (e.g. `tests/e2e/tests/component-geometry-oracle.spec.ts`
emitting JSON violations) is NOT a trigger condition. When the project has none,
run the Codex vision-taste pass anyway; the combiner then runs with two inputs
instead of three. **Two-vendor agreement auto-fixes with or without an oracle**
— that row of the combiner table is unchanged. What the missing oracle removes
is the arbiter for SINGLETON findings: with no oracle to extend, every singleton
goes to the founder as an A/B or to the ledger, never to an auto-fix.

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

FIXED visual rules — check every one on every image:
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

Emit ONE JSON object for this whole call, matching the verdict-schema.json
used elsewhere in this pipeline. Not one object per image — one object,
whose `findings` array covers every image you were given (severity,
autofix_class, file, line, rationale, unverifiable — all per schema).
Identify the image PER FINDING: `file` = that image's
ABSOLUTE path, copied exactly as given to you, `line: 0` (schema requires line;
visual findings are whole-image).
Encode region + rule INSIDE rationale as a prefix: `[region: <where>]
[rule: <fixed-rule# or drift-back#>] <what breaks>`. Example: `[region:
top-left option in radiogroup] [rule: drift-back#1] radio dot renders above
text, not to the left`. Keep total rationale ≤200 chars.

If nothing to flag on ANY image in this call, emit verdict PASS with an
empty findings array. Do not emit per-image empty arrays — the schema
permits exactly one findings array per call.
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

`--output-schema` returns exactly ONE verdict object per invocation. Attribute
each finding to its shot via its absolute `file` path — the schema's `file` is
an absolute path in every phase, 4b included; never ask for or expect a
per-image object or a per-shot findings array.

**Severity floor, applied by the parent AFTER parsing — never in the prompt.**
Any returned finding whose `rationale` prefix names a fixed rule or a known
drift-back is raised to HIGH if the advisor rated it lower. The advisor is told
which rules to check; the parent decides what a violation is worth.

**LOW filter, applied by the parent AFTER the floor — never in the prompt.**
Drop 4b findings still rated LOW once the floor has been applied; the geometry
oracle and 4a cover polish. Never put this in the prompt: a "do not report LOW"
instruction pushes borderline LOWs up to MEDIUM instead, uncorrectably.

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

Bucket findings by `(surface, element_selector_if_any, rule)`. `surface` is the
invocation label the shots were dispatched under — the `<surface>` in the output
filename, and the same string the ledger's `surface` column carries. The
finding's absolute `file` path identifies which shot within that surface; it is
never the bucket key. For each bucket:

| Convergence | Confidence | Action |
|---|---|---|
| Geometry-oracle finding (any) | GROUND TRUTH | Always auto-fix. Not a vote; the DOM says the pixels are wrong. |
| 2+ independent reviewers agree (e.g. Opus + Codex; or oracle + either) | HIGH | Auto-fix without further verification. |
| Singleton VLM finding (Opus xor Codex, no oracle backing) | LOW | Verify-then-fix. Arbiter: **the geometry oracle**. If the oracle can be extended to numerically check the claim, extend it and re-run; if the oracle confirms, fix; if the oracle contradicts, discard as a hallucinated defect (VLMs invent defects — UI-Lens explicitly documents this: "misinterpreting intentional spacing ... as an abnormal blank space"). If the claim is genuinely subjective (gestalt, "does this look right") and the oracle can't arbitrate, **surface to the founder as an A/B variant fan-out, do NOT auto-fix**. |
| Singleton VLM finding with no artifact and no reproducible check | LOW | Log to ledger with disposition `singleton-unverifiable`. Do not auto-fix. Do not auto-block. |

**Session constraint (verbatim, load-bearing)**: Never singleton-VLM = auto-block. A single Codex or Opus visual claim without oracle backing does not stop the ship; it goes to the ledger and (if reproducible) the founder.

**Sigil constraint (verbatim, load-bearing)**: Sigil requires reviewer confirmation. The final sigil-ready state must show at minimum the geometry-oracle green + Phase 4b run to completion + all HIGH-tier findings resolved. Do not sigil on a partial 4b.

### Per-model disposition ledger

Every 4b run appends to `docs/reviews/multi-model-taste-ledger.md` (project-local).
When the project has no `docs/reviews/`, create it — the ledger is what makes
per-model precision measurable, and a run that skips it silently loses that
history. One row per bucket, so per-model precision can be measured over time
(sortie's pattern):

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

### Gemini — future upgrade, blocked on API access

DiffSpot ranks Gemini (2.5 Pro / 3.x Pro) as the strongest visual perceiver (47.2% acc, 40.7% recall — best in class); UI-Lens ranks Gemini 2.5 Pro top-2 on F1 with the highest recall (81.4%) of any tested model. If Gemini API access becomes available in this shop, add as **phase 4c** using the same invocation shape and combiner rules — Gemini's high-recall / low-precision profile is exactly what a second miss-catching reviewer wants, and the arbiter+oracle mechanics already absorb the extra false positives. Do NOT add Gemini until an API key is actually wired: adding a dependency the loop cannot invoke would silently degrade to skipping 4c and calling the pipeline done. The wired key is the gate, not a date.

## Anti-patterns (UI phases)

These belong to 4a/4b and are enforced only once this file is loaded. The
ship-gating ones — singleton-VLM and sigil — are ALSO stated in `../SKILL.md`,
deliberately duplicated, because they gate the ship rather than the phase.

| Mistake | Why bad |
|---|---|
| **Skip Phase 4b when the diff touches component templates or CSS** | **Red flag.** The drift-back class of bugs (dot-above-text, selected-state regression, red-title-on-non-danger, combobox-narrower-than-control) survives Phases 1-4a — every frontier VLM misses 60%+ of true CSS defects (DiffSpot 2026), and a single vendor's blind spot repeats across rounds. A second-vendor visual reviewer (Codex vision-taste) plus the geometry oracle is the only pipeline shape that catches them. Skipping 4b on a UI-touching diff ships the exact regression class the founder has already flagged multiple times. |
| Auto-fix a singleton VLM finding without oracle backing | Session-constraint violation. Singleton visual claims can be hallucinations (UI-Lens: VLMs invent defects, e.g. flagging intentional spacing). Verify against the geometry oracle first; if the oracle can't arbitrate, surface to the founder as A/B, don't auto-fix. |
| Auto-block on a singleton VLM finding | Session-constraint violation. `Never singleton-VLM = auto-block.` Bug the ledger, not the ship gate. Only geometry-oracle findings or 2+ reviewer convergence gate the ship. |
| Sigil on a partial Phase 4b | Session-constraint violation. `Sigil requires reviewer confirmation.` The sigil-ready state needs: oracle green + 4b run to completion + all HIGH resolved. A skipped or half-run 4b is not sigil-ready. |
| Add Gemini vision without an API key wired | Would silently degrade to skipping phase 4c and calling the pipeline complete. Note as future upgrade in the skill; do not add as a dispatch step until the API dependency is real. |
| Treat Phase 4a's A+B agreement as independent confirmation | Same model with two role labels shares its blind spots. A+B agreement is "look here first". Independence comes from Phase 4b's second vendor and the geometry oracle. |
