# Phase 4 — UI taste review (reference)

**Read this file in full, or not at all.** Phase 4 is ONE wave with four inputs
and a combiner that only means anything while holding all of them; a reader who
stops after the first reviewer lane runs that lane alone and reports a taste
pass. A truncated read produces a coherent-looking result with no signal that
most of the phase is missing — which is indistinguishable from "one critic was
the plan", and is the failure this warning exists to prevent.

Section names quoted below without a path ("Elicit risk, not policy", "Loop
bounds", the step-4 `jq` cross-validation) live in `../SKILL.md`, which the
parent already has in context when it reaches this file.

Lane labels `4a` (the two Opus critics) and `4b` (the Codex vision pass) are
retained — the ledger and the composition table use them. They name lanes, not
an order. Nothing in Phase 4 runs after `4a`. The run-state `phase` value is
plain `4` for the whole wave: a per-lane resume point would let a resumed run
re-enter "at 4b" with 4a already done, which is the serial relay this
restructure removed, rebuilt by the recovery path.

## Why this shape

### Why a second vendor at all

Single-vendor VISUAL review misses geometric defects — radio-dot Y-centering,
dot position, width alignment, selected-state color regressions. The DiffSpot
2026 benchmark (Tencent WeChat AI, arXiv 2605.29615) shows every frontier VLM
catches ≤40.7% of true single-CSS-property defects, and pixel magnitude does not
predict detection (r = −0.08).

UI-Lens (CVPR 2026, Xiang et al., Zhejiang University) shows the same pattern for
boundary defects, and shows it worse than the round numbers suggest. Best-model
F1 on the camera-ready benchmark: **22.19%** text overflow, **33.75%** container
overlap, **11.44%** sequential consistency. Beware the stale figures in
circulation — the CVF HTML abstract still carries an earlier run (20.36 / 31.21 /
10.61 over 10 models); the camera-ready PDF (22.19 / 33.75 / 11.44 over 9 models)
is authoritative. Cite the PDF. Any framing that rounds these toward "about half"
overstates model performance by roughly 2x and is why this pipeline exists.

Vendors also review their own output worse than a stranger's: cross-model review
beats same-model by 6.3–11.5 pp in both directions (Greptile, 2026-07-21). Two
vendors on the same shots is a coverage mechanism, not a courtesy.

Multi-vendor visual taste plus a deterministic layer is the only combination that
closes the gap. Neither half substitutes for the other.

### Why one blind wave and not a relay

Every reviewer in Phase 4 sees byte-identical shots and none of them ever sees
another's output. Not "sees it late", not "sees only the labels" — never.

- **Quoting a prior judgment into a VLM reviewer's context moves its verdict by
  19–26 pp** across seven VLM judges (arXiv 2608.07920, Aug 2026). A label-only
  control — the reviewer is told a peer already judged the artifact, without the
  peer's content — moves it −0.17 pp. It is the CONTENT entering context that
  does the damage, so "just mention the other vendor flagged something here" is
  not a safe middle ground; it is the whole of the effect.
- **Independence is already nearly exhausted.** Nine frontier judges across seven
  model families carry about **two effective votes**; the panel lands 8–22 pp
  below the independent-voting ideal, and better aggregation recovers at most 11%
  of that gap (Apple, arXiv 2605.29800). A step that couples reviewers spends
  capital this pipeline does not have.
- **A fixed defect is unobservable to a later reviewer.** This one is arithmetic,
  not empirics: a vendor cannot vote on a defect that no longer renders. Fixing
  between reviewers therefore yields zero agreement signal on precisely the
  subset where a second opinion was wanted — which is what made the combiner's
  "2+ reviewers agree" tier structurally dead in the fix-then-re-review ordering
  this file used to describe.

Cascade architectures are not a counter-example. Cascades win on COST through
complementary errors, and they escalate the *same unmodified input*. That is
orthogonal to fix-then-re-review and is not a defence of it.

The one real cost of the blind wave is that fixes now land AFTER every vendor has
looked. "Stage 4 — post-fix regression gate" is the mitigation, and it is not
optional.

### Why the deterministic layer goes first

Deterministic capture-and-diff catches 100% of CSS changes. The best frontier VLM
names 40.7% of them. A VLM is a triage and explanation layer over a deterministic
diff, never a replacement for one — DiffSpot's own construction puts a pixel
"grounding gate" upstream of the VLM for exactly this reason.

So the deterministic diff runs before any VLM sees a pixel, and its output scopes
them. The previous shape had the one non-hallucinating component downstream, as
the arbiter of claims the hallucinating components had already made. That is
backwards: it spent the ground truth on adjudicating noise instead of on
preventing it.

## The phase at a glance

```
STAGE 0  Deterministic pre-pass   capture → diff vs baseline → changed-region
                                  manifest; freeze + hash the shots
STAGE 1  Blind parallel wave      lane 4a-A (Opus defect critic)
         ONE tool-use block       lane 4a-B (Opus taste critic)
         no lane sees another     lane 4b   (Codex vision-taste)
                                  lane ORACLE (deterministic geometry assertions)
STAGE 2  Combiner                 ground truth → drop rule → convergence tiers
STAGE 3  Bounded fix pass         exactly ONE, then stop
STAGE 4  Post-fix regression gate re-diff vs the ORIGINAL baseline; VLM pass on
                                  newly-changed regions only
```

Phases 1-3 catch correctness. UI work also needs a TASTE pass — misalignments,
generic-AI aesthetic, house-style drift. Phase 4 runs ONCE after Phases 1-3 all
ADVANCE, not woven into the earlier phases.

The bounded-pass discipline is adapted from `pbakaus/impeccable`'s critique
pattern (two independent assessments → synthesis → bounded fix cap; verified via
github.com/pbakaus/impeccable 2026). Impeccable's own rule: **"Verify in bounded
passes, not a loop... Open-ended self-QA burns the user's money."** Cap: ONE fix
round after the critique. No more.

### When it fires

- Diff touches HTML templates / view partials / CSS / rendered-view components
  (see `~/.claude/skills/visual-verify/SKILL.md` glob list)
- AND Phases 1-3 all ADVANCED — each exited on a gate stop routed `advance` or
  `advance-with-findings-surfaced`. A `halt-and-pause` stop never reaches Phase
  4; the run is paused, not advanced.
- AND regenerated screenshots exist for the touched surfaces.

Skip for pure backend / infra / docs work.

**Neither deterministic component is a trigger condition.** A geometry oracle
(e.g. `tests/e2e/tests/component-geometry-oracle.spec.ts`, whose violations are
retrievable as JSON — but NOT from disk; see "Getting the violations out") and a
screenshot-diff harness are inputs the wave uses when the project has them. When
the project has neither, the VLM lanes still run — see "Degrading when a
deterministic component is absent", which is the DEFAULT path today, not an edge
case.

## Stage 0 — deterministic pre-pass

Runs to completion before a single lane dispatches. Nothing here is a judgment,
so nothing here can anchor a reviewer.

1. **Capture.** Regenerate shots for every touched surface. Keep the pre-change
   render as the ORIGINAL baseline for the whole phase — Stage 4 diffs against
   it, not against the post-critique render, and a baseline overwritten mid-phase
   destroys the only reflow check the pipeline has.
2. **Diff.** Run the project's deterministic screenshot diff, baseline vs
   current, and record per surface a **changed-region manifest**: the bounding
   boxes that actually differ. This is ground truth about WHERE the diff landed;
   it says nothing about whether the change is good.
3. **Freeze and hash.** Record a `sha256` per shot before dispatch. "Every
   reviewer saw byte-identical shots" is then checkable in the run log rather
   than asserted — and a re-render that sneaks in mid-wave (a stray dev-server
   rebuild, a flaky font load) shows up as a hash mismatch instead of as
   inexplicable reviewer disagreement.

The changed-region manifest goes into the VLM prompts as SCOPE — the regions to
review. It never goes in as consequence. Telling a reviewer that findings
outside the manifest get discarded both violates "Elicit risk, not policy" and
destroys the drop rate as a measurement, because a reviewer that knows the filter
optimizes against it.

Never substitute a VLM's opinion of which regions changed for the diff's. That
would launder a 40.7% guess into the one input the combiner treats as ground
truth, and every downstream guarantee reads off that input.

### Integration traps — measured, and every one of them fails quietly

**These are facts about ONE integration, not properties of the pipeline.**
Measured 2026-08-27 against kablong `feat/design-review-round3-2026-08-02`, the
only project wired to this skill. They are here rather than in a footnote because
each one produces a run that looks like it worked, and kablong-side details drift
— when a run behaves oddly, re-check these against the repo rather than trusting
this list.

- **`vv-shots.sh` exits `rc=1` on complete success.** `login` and `not-found` are
  intentionally-captured non-200 surfaces, and the driver propagates the last
  non-zero per-shot exit as its own (`RC=$ec` … `exit $RC`). **Never read its exit
  code as a success signal — count the PNGs it wrote.** Same measurement, related
  bug: the underlying screenshot CLI synthesizes `status=401` for any URL matching
  `\b(login|sign[-_]?in|oauth\d*|magic)\b`, so a deliberately captured login page
  is ALWAYS mislabeled as an auth wall. Neither is a capture failure, and both
  look exactly like one.
- **The server needs `--features test-endpoints,dev-playground`.**
  `test-endpoints` ALONE is not enough: the oracle's `/dev/components/*` pages sit
  behind the separate `dev-playground` feature. A server built with only
  `test-endpoints` serves the app perfectly and 404s every oracle page, which
  reads as a broken oracle rather than a wrong build. `vv-shots.sh`'s own header
  comment shows only `test-endpoints` — that comment is the trap.
- **`PORT=3737` must be set explicitly.** `main.rs` defaults to `3000`, while
  `playwright.config.ts` and `vv-shots.sh` both target `3737`. Nothing reconciles
  them; the mismatch surfaces as connection-refused on every shot.
- **`pg_tmp` and `openssl` exist only inside `nix develop`.** Driving the capture
  or the oracle from a bare shell fails on a missing binary, not on anything
  visual.

## Stage 1 — the blind parallel wave

All four lanes dispatch in ONE tool-use block. No lane receives another lane's
output, ever — the same rule the Phase 1 specialists follow, for the same reason
("Give the specialists a peek at the primary's findings before they report" in
`../SKILL.md`).

**The three VLM lanes and the oracle take different inputs, and conflating them
writes a spec nothing can execute.** The VLM lanes read the frozen shots and are
scoped to the changed-region manifest. The oracle reads neither: it drives a live
browser against the project's component pages and asserts on the DOM, so it takes
no shot and cannot be scoped to a bounding box (see "What scoping the oracle can
and cannot mean"). "Byte-identical shots" is a guarantee about the reviewers who
consume shots; it is not a claim about the oracle.

Merge lane outputs only after all lanes have returned.

### Lane 4a-A — defect critic

Fresh Opus dispatch with the `visual-verify` 4-step rubric (zoom →
compare-vs-known-good-in-repo → walk checklist → severity-tagged findings).
Anti-slop guardrails from `docs/reviews/component-sota-2026-07-31.md` (if present
in project).

### Lane 4a-B — taste critic

Fresh Opus dispatch with a house-style rubric — pulls from the project's design
spec + brand workbook + prior founder taste decisions (e.g.
`docs/brand/klaffat-design-spec.md` §Locked variants + `docs/reviews/*` picks).
Explicit anti-generic-AI list (see below).

Both 4a lanes use the SAME model + fixed prompt so scores are internally
comparable (per SOTA research: cross-model taste-scoring agreement is ~zero; fix
the model to compare).

**Comparability is bought with correlated blind spots.** Different role labels on
the same model do NOT make two independent reviewers — persona-differentiated
same-model agents share the most errors. 4a-A and 4a-B agreeing means "worth
looking at first", never "independently confirmed", and the combiner counts the
two of them as ONE vendor. Lane 4b and the oracle are what actually decorrelate;
do not let a strong A+B agreement talk you into skipping either.

### Lane 4b — Codex vision-taste

Founder observation: primary Claude visual review misses geometric defects the
founder catches on first look — radio-dot Y-centering, dot position, width
alignment, selected-state color regressions. The benchmark evidence for that
generalizing is in "Why a second vendor at all". Codex is the second vendor
because it has vision support and a wired-in quota here; see the "Gemini future
upgrade" note below.

Codex CLI supports image input via `--image` (verified: v0.24.0+ scripted;
v0.115.0 Mar 2026 for full-resolution, no thumbnail compression). Requires a
vision-capable model (`gpt-5-codex` or newer). Approx 1-3k vision tokens per
image per turn.

```bash
# One invocation per surface (component/page). Multiple shots per surface go
# as a comma-separated list — Codex sees them all in the same turn.
SHOTS="/abs/path/to/component-radio.png,/abs/path/to/component-radio-selected.png"
PROMPT="$(cat <<'EOF'
You are a design defect reviewer working from screenshots. Bias toward
reporting; a downstream combiner suppresses false positives.

Review ONLY these regions of the images — they are the regions this change
touched:
<changed-region manifest for this surface, verbatim from Stage 0>

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

printf '%s\n' "$PROMPT" > /tmp/vision-prompt-<surface>.txt

~/.claude/skills/advice-refine-test-loop/scripts/codex-advise.sh \
  --prompt-file /tmp/vision-prompt-<surface>.txt \
  --model gpt-5-codex \
  --images "$SHOTS" \
  --out /tmp/codex-vision-taste-<surface>.json
# exit 0 = verdict produced · 2 = the script could not run
#      3 = quota · 4 = timeout/connectivity · 5 = no parseable verdict
```

**The prompt says nothing about the other lanes.** It does not say a Claude
review already ran, does not say one is running alongside, and does not say which
vendor arbitrates. The first is now false, and all three are the content-cue
class that moves a VLM judge 19–26 pp.

The wrapper is the same one Phase 2 uses, and it owns the shared plumbing:
read-only sandbox, no `--dangerously-*` anything, the 300s cap,
`--output-schema` pinned to `verdict-schema.json`, and exactly one retry per
failure class. What is 4b-specific stays here — the model, the images, the
prompt, the severity floor and the LOW filter below. `--images` takes ABSOLUTE
paths and the script rejects anything else, since the combiner attributes each
finding to its shot by that exact path.

Repeat per surface. Store outputs under a run-scoped dir (e.g.
`/tmp/design-review-YYYY-MM-DD/vision-taste/`). Do not batch every surface
into one invocation — Codex vision recall drops sharply past ~4 images per
turn, per DiffSpot Hard-tier data. Per-surface invocations are themselves
independent and dispatch together; they share no state.

The schema the wrapper pins returns exactly ONE verdict object per invocation.
Attribute each finding to its shot via its absolute `file` path — the schema's
`file` is an absolute path in every phase, 4b included; never ask for or expect a
per-image object or a per-shot findings array.

**Severity floor, applied by the parent AFTER parsing — never in the prompt.**
Any returned finding whose `rationale` prefix names a fixed rule or a known
drift-back is raised to HIGH if the advisor rated it lower. The advisor is told
which rules to check; the parent decides what a violation is worth.

**LOW filter, applied by the parent AFTER the floor — never in the prompt.**
Drop 4b findings still rated LOW once the floor has been applied; the geometry
oracle and 4a cover polish. Never put this in the prompt: a "do not report LOW"
instruction pushes borderline LOWs up to MEDIUM instead, uncorrectably.

**Failure modes** follow the same PAUSE rules as Phase 2, and the wrapper has
already spent the one sanctioned retry by the time it exits non-zero. Never
re-invoke it hoping for a better roll:

- Exit 3 — quota exhausted, including on the lighter fallback tier (which still
  has vision on Plus quota).
- Exit 4 — timeout or connectivity, including after the one 30s retry.
- Exit 5 — no parseable verdict, including after the one schema-reminder re-ask.
- Exit 2 — the script could not run at all. Not a review result, and never a
  clean pass.

All five route to the Phase 2 PAUSE handler: interactive prompts the user,
unattended writes `pending_for_human.md` and exits `pending-human`.

Never silently skip lane 4b. The whole point of the second vendor is coverage of
what 4a misses; a skipped 4b is worse than a skipped Phase 2 because the
drift-back class of defects survives the rest of the pipeline whole.

### Lane ORACLE — deterministic geometry assertions

The project's geometry oracle (e.g.
`tests/e2e/tests/component-geometry-oracle.spec.ts`) runs in the same block as
the three VLM lanes. It is deterministic, so being in the wave cannot anchor it
and it cannot anchor anyone — its output goes to the parent-side combiner and
into no prompt.

Its verdict is not a vote. **An oracle violation is fixed on the oracle's
authority, with no VLM vote required, and no VLM's silence counts against it** —
the DOM says the pixels are wrong, and 60%+ of true CSS defects go unnamed by
every frontier VLM, so silence is the expected reading rather than a
contradiction.

What the oracle no longer does is arbitrate singleton claims on demand. Extending
an assertion mid-phase to check a fresh VLM claim would re-serialize the wave and
turn ground truth into a debugging aid for one reviewer's guess. When a singleton
claim looks numerically checkable, write the assertion as follow-up work so the
NEXT run has it; do not re-run the wave to consult it.

#### Getting the violations out — the JSON is NOT on disk

**Measured 2026-08-27 against kablong `feat/design-review-round3-2026-08-02`.
The reference oracle does not write machine-readable output anywhere the
combiner can glob for it.** This is the single most likely way to wire the
combiner up wrong, and the failure is silent.

What it actually emits:

- **stdout** — ordinary Playwright reporter output, plus one HUMAN-readable
  `console.log` per component (`[geometry-oracle] btn: 3 violations` followed by
  a summary capped at 30 lines, or `[geometry-oracle] btn: clean`).
- **the JSON** — only via a `test.info().attach()` call naming
  `<slug>-violations.json` and passing the payload as `body`. A `body`
  attachment lives in the report, and Playwright **never writes it to disk**:
  forcing violations produced **zero** `*-violations.json` files under
  `test-results/`.

So the ONLY retrieval path is the JSON reporter plus a base64 decode of the
attachment `body`:

```bash
npx playwright test component-geometry-oracle --reporter=json > /tmp/oracle.json
# Recursive descent, because the reporter's suite nesting varies by version.
jq -r '[.. | objects | select(has("name") and has("body"))
        | select(.name | endswith("-violations.json")) | .body] | .[]' \
  /tmp/oracle.json | base64 -d | jq -s 'add'
```

The decoded schema is the one the combiner wants — `component`, `variant`,
`element`, `selector`, `property`, `actual`, `expected`, `delta`, `note`.

Two consequences, both fail-open if missed:

- **A combiner that globs for `*-violations.json`, or that parses stdout, gets
  NOTHING and reads it as "oracle clean."** That is the fail-open this skill
  exists to name, wearing the costume of ground truth. The success signal is a
  successfully DECODED attachment; an empty glob is never evidence of a clean
  run.
- **The oracle hard-fails on any violation** (`expect(violations).toEqual([])`),
  so a run that has something to report is a FAILING Playwright run. Non-zero
  exit is the expected shape when the oracle is doing its job, and the exit code
  alone cannot distinguish "violations found" from "the oracle could not run".
  Parse the report; never branch on the exit code.

#### What scoping the oracle can and cannot mean

**Measured the same day, same branch: the oracle has component granularity and
nothing finer, and no changed-region awareness at all.**

The driver loops a hardcoded 5-entry `COMPONENTS` array (`radiogroup`, `btn`,
`combobox`, `dialog`, `timegrid`) emitting one `test()` per slug, so
`-g "geometry oracle: timegrid"` runs exactly one test (verified: "Running 1
test"). That grep on the slug is the ONLY scoping axis that exists. There is:

- **no git or changed-file awareness** — the oracle does not know what the diff
  touched and cannot find out;
- **no env-var scoping hook** — nothing to pass a region, a selector or a bounds
  list to;
- **no sub-component selection** — the 28 `variant:` fixtures are hardcoded
  inside the assert functions and are not individually selectable.

So "scope the oracle to changed regions" cannot mean what it sounds like.
Mapping changed regions to component slugs happens OUTSIDE the oracle, in the
parent, before dispatch — and below slug granularity there is no scoping at all.
Do not write a step that implies the oracle can be pointed at a bounding box; it
cannot, and a spec that says otherwise sends an implementer looking for a flag
that does not exist.

None of this costs much: running all five slugs is deterministic and cheap, which
is why Stage 4 re-runs the oracle in full rather than scoping it. Slug mapping is
an optimisation, never a correctness requirement.

### Severity

**Every lane emits verdict-schema.json, same as every other phase — no separate
vocabulary.** Each lane returns one verdict object, parsed by the same `jq`
cross-validation in step 4. There is no `blocker` / `should-fix` / `nit` enum;
those words are not schema-parseable and must not appear in a critic prompt or a
critic's output.

Critic prompts carry the schema's intrinsic-risk definitions unchanged — blast
radius, reversibility, silence, recoverability, read visually (how much of the
surface is wrong, how obvious it is, whether it survives a reload). Nothing about
what a grade triggers, and no rule of the form "a violation of X is BLOCKER"; see
"Elicit risk, not policy".

**The taste tiers are a parent-side severity FLOOR, applied AFTER parsing —
never in the prompt**, same mechanism as lane 4b's:

- Rationale names a house-style rule the founder has explicitly ruled on before
  → floor `BLOCKER`.
- Rationale names broken layout or unreadable content → floor `HIGH`.
- Everything else keeps the critic's grade.

Findings whose confirmation is a taste call carry `unverifiable: true`, per the
schema.

### Anti-generic-AI-aesthetic guardrails

From Anthropic `frontend-design` skill + practitioner blogs (design-bootcamp,
aidesigner.ai 2026):

- Generic-blue CTA (`#3B82F6` shadcn/Tailwind default)
- Purple → blue gradient CTAs or backgrounds
- Glassmorphism (frosted-glass `backdrop-filter` on cards)
- Stock illustration (isometric-purple person-with-laptop)
- Emoji-in-heading substituting for icon
- Card grid where a list would do
- "Coming soon" placeholder that isn't
- Everything-center-aligned (no left-alignment for prose)

### When Anthropic frontend-design skill is available

Anthropic ships an official `frontend-design` skill (`npx skills add
https://github.com/anthropics/skills --skill frontend-design`). If installed in
the project, its rubric replaces lane 4a-B — it's the maintained upstream version
of the taste-critic prompt.

## Stage 2 — combiner

Four inputs to merge, all produced without any of them seeing the others:

1. **Changed-region manifest** — Stage 0's deterministic diff. Ground truth about
   WHERE the change landed. Consumed as a filter, not as findings.
2. **Geometry-oracle findings** — deterministic pixel/color assertions.
   **Always take** — this is ground truth, not a taste opinion.
3. **Opus findings** — lanes 4a-A + 4a-B. Same model, so they count as ONE vendor.
4. **Codex vision-taste findings** — the per-surface JSON blobs from lane 4b.

Bucket findings by `(surface, element_selector_if_any, rule)`. `surface` is the
invocation label the shots were dispatched under — the `<surface>` in the output
filename, and the same string the ledger's `surface` column carries. The
finding's absolute `file` path identifies which shot within that surface; it is
never the bucket key. For each bucket, in this order:

| Convergence | Confidence | Action |
|---|---|---|
| Geometry-oracle violation (any) | GROUND TRUTH | Always auto-fix. Not a vote; the DOM says the pixels are wrong, and no VLM needs to have seen it. |
| VLM finding in a region the diff reports as **never changed** | HALLUCINATION | **Discard, and log it to the vendor's drop rate.** Not deferred, not surfaced, not fixed — the diff says nothing there moved. See "The drop rule" below. |
| VLM finding contradicted by a green oracle assertion on the SAME rule | HALLUCINATION | Discard, and log to the drop rate. Ground truth checked exactly this and found it clean. |
| 2+ independent vendors agree (Opus lane + Codex lane; or oracle + either) | HIGH | Auto-fix without further verification. 4a-A + 4a-B is ONE vendor and does not reach this tier. |
| Singleton vendor finding in a changed region, reproducible against the artifact by a check a third party could re-run | LOW | Fix, and record the check in the ledger row. "Reproducible" means the pixels or the DOM show it — not that the reviewer sounded sure. |
| Singleton vendor finding in a changed region, subjective (gestalt, "does this look right") | LOW | **Surface to the founder as an A/B variant fan-out. Do NOT auto-fix. Do NOT auto-block.** There is no in-phase arbiter for a taste call, and inventing one is how a hallucinated defect gets shipped as a fix. |
| Singleton vendor finding with no artifact and no reproducible check | LOW | Log to ledger with disposition `singleton-unverifiable`. Do not auto-fix. Do not auto-block. |

The drop rows come BEFORE the convergence rows on purpose: two vendors agreeing
about a region that never changed is two hallucinations, not a HIGH-confidence
finding. Filter on ground truth first, count votes second.

### The drop rule

**Any VLM finding whose region the deterministic diff reports as unchanged is
discarded as a hallucination, and the per-vendor drop RATE is logged for the
run.** VLMs invent defects — UI-Lens documents the mechanism directly
("misinterpreting intentional spacing ... as an abnormal blank space") — and a
region the diff says never changed cannot have been broken by this diff.

The rate is the production analogue of DiffSpot's 500 no-diff control pairs: the
frequency with which a vendor reports a defect where nothing moved. Two honest
caveats, both of which must travel with the number:

- **It is a calibration metric, not a scoreboard.** It measures a vendor's
  tendency to invent under this pipeline's prompts and this project's surfaces.
  It is not a benchmark result, it does not rank vendors in general, and a run
  or two of it ranks nothing at all.
- **It is not directly comparable to DiffSpot's control** in absolute terms,
  because Stage 0 hands the reviewer the changed regions as scope. What it
  measures is out-of-scope invention under a stated scope. Compare it across
  vendors in this pipeline and across runs over time; do not quote it against a
  published control figure.

Log the rate even when it is zero, and log the denominator with it. A bare "0
dropped" over four findings says nothing.

### The two ship-gating constraints

Both are stated here and in `../SKILL.md`, deliberately duplicated because they
gate the ship rather than the phase. The two copies must stay identical.

- **Never singleton-VLM = auto-block.** A single Codex or Opus visual claim
  without geometry-oracle backing does not stop the ship; it goes to the ledger
  and (if reproducible) the founder.
- **Sigil requires reviewer confirmation.** The sigil-ready state needs
  geometry-oracle green + lane 4b run to completion + all HIGH-tier findings
  resolved. Never sigil on a partial or half-run 4b. "Run to completion" means a
  parseable verdict for every surface; a lane that exited non-zero is a PAUSE,
  and concurrent dispatch does not make an incomplete lane complete. When the
  project has no oracle, "geometry-oracle green" cannot be shown and must not be
  claimed — the Exit report states that precondition as unmet, not satisfied.

## Stage 3 — bounded fix pass

Max ONE fix pass after the wave:

1. Fix every geometry-oracle violation. Ground truth, no vote needed.
2. Fix everything the Opus lane and the Codex lane agree on at `BLOCKER` or
   `HIGH`.
3. Fix singleton `BLOCKER`s that reproduce against the artifact by a re-runnable
   check. A singleton that is a taste call does not qualify no matter how it was
   graded — it goes to the founder.
4. Re-render affected shots.
5. Founder-taste calls (singleton subjective findings) go to the founder as A/B/C
   variants via the founder-decision loop, NOT auto-fixed.

If a second taste round is genuinely needed, that's a signal the design isn't
converging → escalate to founder with variant fan-out (per SOTA research
2026-07-30), don't loop more times.

## Stage 4 — post-fix regression gate

The blind wave's one real cost is that every vendor looked BEFORE the fixes
landed. This stage is what covers it, and skipping it converts that cost into an
unreviewed diff.

Fixes inject defects at rates that justify the re-check: **14.8–24.4% of
post-release fixes are incorrect**, and **48.7% of bug-fixing changes break
regression testing on the first run**. Stated honestly: those are general
software-engineering figures. No UI-specific defect-injection-by-fix rate exists.
CSS is plausibly worse, because a rule can reflow content well outside the
element it edited — but that is inference from how layout works, not a measured
result, and must not be quoted as one.

1. **Re-shoot** every surface the fix pass touched.
2. **Re-diff against the ORIGINAL baseline** — the Stage 0 pre-change render, not
   the post-critique render. Diffing against the intermediate render is what
   makes reflow contamination invisible: a fix that moved pixels in a component
   nobody edited shows up only against the original.
3. **Compute the newly-changed regions**: regions in the Stage 4 manifest that
   were not in the Stage 0 manifest. That set IS the fix's collateral footprint.
   An empty set is the expected, good outcome and gets recorded as such.
4. **Re-run the oracle in full.** It is deterministic and cheap; there is no
   reason to scope it.
5. **Run the VLM pass ONLY on the newly-changed regions.** Same lanes, same
   blindness rule, same drop rule, scoped to that set. If the set is empty, no
   VLM runs and the gate passes on the oracle alone — record that this is what
   happened.

**The gate is a gate, not round two.** Its fix authority is limited to oracle
violations and to reverting or repairing the contamination it just detected.
Anything else it turns up goes to the ledger and the founder. Without this bound
the "ONE fix pass" cap is fiction: each gate finds something, each fix triggers
another gate, and the phase becomes the open-ended self-QA loop impeccable's rule
exists to forbid.

## Degrading when a deterministic component is absent

Stage 0's diff harness and the geometry oracle are two SEPARATE components, and a
project can be missing either or both. **Measured 2026-08-27 on kablong, the one
project using this skill: it has neither on `origin/main`.** The oracle exists
only on the unmerged branch `feat/design-review-round3-2026-08-02`, and there is
no project-authored deterministic screenshot-diff harness anywhere in the repo
(every `toHaveScreenshot` hit is inside Playwright's own `node_modules`). The
paths below are therefore the DEFAULT path today, not an edge case.

A spec that silently assumes infrastructure nobody has is a gate that fails open
— the exact defect class this skill exists to catch. So absence is never silent:
each missing component is recorded in the run state, in every ledger row for the
run, and in the Exit report, in the words below.

### When the screenshot-diff harness is absent

- **What runs:** everything else. All three VLM lanes dispatch as normal on the
  same frozen, hashed shots, and the oracle dispatches as normal against the live
  DOM. The VLM lanes are given the touched surfaces as scope instead of a region
  manifest; the oracle's scope is unaffected, having never been region-based.
- **Guarantee lost — the drop rule.** With no manifest there is no ground truth
  about where the change landed, so nothing is dropped and no hallucination is
  filtered. The ledger's drop-rate cell for the run is `n/a`, **never `0`** — the
  two look identical in a table and mean opposite things.
- **Guarantee lost — Stage 4's reflow check.** Steps 2, 3 and 5 of the regression
  gate cannot run. Re-shoot and re-run the oracle anyway; that is the whole of
  the gate that survives, and it checks only what the oracle asserts.
- **Must NOT be claimed:** that hallucinations were filtered; that the fix pass
  was checked for reflow contamination; any per-vendor precision or drop-rate
  figure; that a finding was "confirmed to be in a changed region". The Exit
  report says: *the pipeline ran without a deterministic change-region layer.*
- **Do NOT substitute a VLM** for the missing diff. Asking a model which regions
  changed replaces a 100% measurement with a 40.7% guess, and every guarantee
  above reads off that input.

### When the geometry oracle is absent

- **What runs:** the three VLM lanes, concurrently, exactly as specified. The
  combiner runs with the VLM inputs instead of all four. **Cross-vendor agreement
  auto-fixes with or without an oracle** — that row of the table is unchanged.
- **Guarantee lost — the GROUND TRUTH row is empty.** Nothing auto-fixes on
  deterministic authority, and no VLM claim can be contradicted by a green
  assertion, so the "contradicted by a green oracle" drop row never fires. The
  changed-region drop row still does; it reads the diff, not the oracle.
- **Guarantee lost — no arbiter for singletons.** Every singleton goes to the
  founder as an A/B or to the ledger, never to an auto-fix. This is the existing
  behaviour and it is unchanged.
- **Must NOT be claimed:** "geometry-oracle green"; that two-vendor agreement was
  confirmed against geometry; that a singleton was discarded because the oracle
  disagreed. The sigil may still be reached on the remaining conditions — 4b
  complete, all HIGH resolved — but the Exit report states: *no deterministic
  ground-truth layer was available; the oracle precondition is unmet, not
  satisfied.* Recording it as `n/a` and moving on silently is the fail-open
  reading this rule forbids.

### Absent, clean, and unretrieved are THREE states with ONE observable

"The combiner received no oracle findings" is produced by three different
situations, and only one of them is good:

1. **Absent** — the project has no oracle. Precondition UNMET; say so.
2. **Clean** — the oracle ran and asserted no violations. Genuinely green.
3. **Unretrieved** — the oracle ran, found violations, and the parent never got
   them, because it globbed for `*-violations.json` or read stdout instead of
   decoding the report attachment (see "Getting the violations out"). Ground
   truth existed and was thrown away.

State 3 is the dangerous one: it reports as state 2 while behaving like state 1,
and the sigil's "geometry-oracle green" precondition then passes on a run that
never read the oracle. **Distinguish them positively, never by absence of
output.** A green oracle is one whose report was successfully parsed and
contained zero violations — record the parse, not the silence. A run that cannot
show which of the three states it was in must state the oracle precondition as
UNMET, exactly as in state 1.

### When both are absent

Both sets of losses apply simultaneously. What remains is a three-lane blind VLM
wave with no ground truth anywhere in it, whose strongest claim is "two vendors
agreed" — a claim the benchmark numbers at the top of this file put well short of
verification. Run it, report it as exactly that, and treat building either
component as the highest-value follow-up the pipeline has.

## Per-model disposition ledger

Every Phase 4 run appends to `docs/reviews/multi-model-taste-ledger.md`
(project-local). When the project has no `docs/reviews/`, create it — the ledger
is what makes per-model precision measurable, and a run that skips it silently
loses that history. One row per bucket, so per-model precision can be measured
over time (sortie's pattern):

```
| run-id | date | surface | rule | opus | codex | oracle | disposition | outcome |
|--------|------|---------|------|------|-------|--------|-------------|---------|
| r4-... | 2026-08-03 | c-radiogroup | dot-y-center | HIT | HIT | HIT | auto-fix (2+) | fixed |
| r4-... | 2026-08-03 | c-dialog --danger | red-title | HIT | MISS | n/a | founder-A/B (singleton, subjective) | founder-A/B |
| r4-... | 2026-08-03 | combobox | chevron-present | MISS | HIT | HIT | auto-fix (2+) | fixed |
```

Columns:
- `run-id` — links the row to the round (e.g. `r4-2026-08-03`)
- `opus` / `codex` — `HIT` / `MISS` / `n/a` (n/a = surface not in that reviewer's dispatch)
- `oracle` — `HIT` / `MISS` / `n/a` (n/a = oracle can't check this rule, or the
  project has none — say which)
- `disposition` — one of `auto-fix (2+)`, `auto-fix (oracle)`,
  `dropped-unchanged-region`, `founder-A/B`, `singleton-unverifiable`
- `outcome` — `fixed` / `deferred` / `founder-picked-<variant>` / `discarded-as-hallucination` — filled retrospectively after the fix loop

Alongside the per-bucket rows, record the run's per-vendor **drop rate** with its
denominator (or `n/a` when Stage 0's diff was unavailable).

After ~20 runs the ledger surfaces per-model precision; use it to weight
reviewers, not before. Until then, treat Opus and Codex as equal-weight peers;
the oracle, when present, is ground truth rather than a peer.

## Gemini — future upgrade, blocked on API access

DiffSpot ranks Gemini (2.5 Pro / 3.x Pro) as the strongest visual perceiver
(47.2% acc, 40.7% recall — best in class). A claim repeated elsewhere, that
UI-Lens ranks Gemini 2.5 Pro top-2 on F1 with the highest recall of any tested
model (81.4%), is UNVERIFIED — a 2026-08-27 source check could not locate that
figure in the camera-ready paper, and the per-model results table could not be
retrieved at all. It is neither confirmed nor refuted. Do not restate it as
fact, and do not let it decide anything.

If Gemini API access becomes available in this shop, add it as a FOURTH LANE in
the same blind wave, using the same invocation shape and the same combiner rules
— Gemini's reported high-recall / low-precision profile is what a miss-catching
reviewer wants, and the drop rule plus the oracle already absorb the extra false
positives. Adding it as a later stage instead would re-serialize the wave and
reintroduce the anchoring gap the wave exists to avoid. Do NOT add Gemini until
an API key is actually wired: adding a dependency the loop cannot invoke would
silently degrade to skipping the lane and calling the pipeline done. The wired
key is the gate, not a date.

## Anti-patterns (UI phase)

These belong to Phase 4 and are enforced only once this file is loaded. The
ship-gating ones — singleton-VLM and sigil — are ALSO stated in `../SKILL.md`,
deliberately duplicated, because they gate the ship rather than the phase.

| Mistake | Why bad |
|---|---|
| **Skip lane 4b when the diff touches component templates or CSS** | **Red flag.** The drift-back class of bugs (dot-above-text, selected-state regression, red-title-on-non-danger, combobox-narrower-than-control) survives Phases 1-3 and the Opus lanes — every frontier VLM misses 60%+ of true CSS defects (DiffSpot 2026), and a single vendor's blind spot repeats across rounds. A second-vendor visual reviewer plus the geometry oracle is the only pipeline shape that catches them. Skipping 4b on a UI-touching diff ships the exact regression class the founder has already flagged multiple times. |
| Run the lanes one after another, or let one lane's findings into another's prompt | Quoting a prior judgment into a VLM reviewer's context moves its verdict 19–26 pp (arXiv 2608.07920); a label-only control moves it −0.17 pp, so it is the content that does it. Serial ordering also destroys the agreement signal outright: a defect fixed between lanes no longer renders, so the later lane cannot vote on it. |
| Fix anything between lanes "since we're waiting anyway" | Same failure, worse: the later lanes then review a different artifact than the earlier ones, and every convergence tier in the combiner silently becomes meaningless. Fixes land in Stage 3, after all lanes have returned. |
| Run a VLM lane before Stage 0's diff, or scope it by a VLM's guess at what changed | The deterministic diff catches 100% of CSS changes; the best VLM names 40.7%. A VLM-produced region manifest launders a guess into the input the combiner treats as ground truth, and the drop rule then filters against noise. |
| Put the drop rule (or any consequence) in a reviewer's prompt | "Elicit risk, not policy". A reviewer told that out-of-region findings are discarded optimizes against the filter, which destroys the drop rate as a calibration measurement — the one number that tells you how much a vendor invents. |
| Auto-fix a singleton VLM finding that is a taste call | Session-constraint violation. Singleton visual claims can be hallucinations (UI-Lens: VLMs invent defects, e.g. flagging intentional spacing). There is no in-phase arbiter for a subjective claim; surface it to the founder as A/B, don't auto-fix. |
| Auto-block on a singleton VLM finding | Session-constraint violation. `Never singleton-VLM = auto-block.` Bug the ledger, not the ship gate. Only geometry-oracle findings or 2+ vendor convergence gate the ship. |
| Sigil on a partial lane 4b | Session-constraint violation. `Sigil requires reviewer confirmation.` The sigil-ready state needs: oracle green + 4b run to completion + all HIGH resolved. A skipped or half-run 4b is not sigil-ready, and a concurrent dispatch does not make an incomplete lane complete. |
| Treat 4a-A + 4a-B agreement as independent confirmation | Same model with two role labels shares its blind spots. A+B agreement is "look here first", and the combiner counts them as one vendor. Independence comes from lane 4b's second vendor and the oracle. |
| Diff Stage 4 against the post-critique render instead of the ORIGINAL baseline | That is exactly the comparison that hides reflow contamination: a fix that moved pixels outside the element it edited is invisible against a baseline captured after the fix. |
| Let the Stage 4 gate open a new fix round | The ONE-fix-pass cap becomes fiction, and the phase turns into the open-ended self-QA loop impeccable's rule forbids. The gate fixes oracle violations and its own contamination; everything else escalates. |
| Record a missing oracle or missing diff harness as a passing check | The fail-open failure this skill exists to name. `n/a` and `0` look identical in a ledger and mean opposite things; an unmet precondition is not a satisfied one. State the absence in the Exit report. |
| Read an empty `*-violations.json` glob, or silent stdout, as an oracle pass | Measured: the reference oracle attaches that JSON with a `body` and Playwright never writes it to disk, so the glob is ALWAYS empty — on clean runs and on violating ones alike. The success signal is a decoded attachment from `--reporter=json`; see "Getting the violations out". A combiner wired to the glob reports ground-truth-clean forever. |
| Read `vv-shots.sh`'s exit code as the capture result | Measured: it exits `rc=1` on complete success, because `login` and `not-found` are intentional non-200 surfaces and it propagates the last non-zero per-shot exit. Count the PNGs. Treating rc as the signal aborts a healthy capture; the inverse — assuming rc=0 means every shot landed — is the same bug pointed the other way. |
| Add Gemini vision without an API key wired | Would silently degrade to skipping the lane and calling the pipeline complete. Note as future upgrade in the skill; do not add as a dispatch step until the API dependency is real. |
