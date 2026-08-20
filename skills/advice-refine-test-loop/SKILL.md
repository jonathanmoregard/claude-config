---
name: advice-refine-test-loop
description: Use when shipping non-trivial changes that must land clean — the user says "look through carefully and fix what's off", "ask for advice and repeat", "keep refining until neat", or is going to bed. Runs four sequential phases (Opus + narrow specialists → Codex → adversarial → optional taste incl. Codex vision-taste 4b for UI diffs) with structured JSON verdicts, empirical repro, prefix-cached prompts, and a bounded consolidator sub-loop for LOWs (MEDIUMs surface but do not block).
---

# Advice-Refine-Test Loop

Single-pass review reliably misses fail-open paths, silently-swallowed exceptions, resource leaks, schema mismatches. Single-vendor review misses whatever that vendor's model is systematically blind to. Both-vendors-agree still misses what confirmation bias hides. Single-vendor VISUAL review misses geometric defects (radio-dot Y-centering, dot position, width alignment, selected-state color regressions) — the DiffSpot 2026 benchmark shows every frontier VLM misses 60%+ of true single-CSS-property defects, and pixel magnitude doesn't predict detection. Multi-vendor visual taste + a deterministic DOM-geometry oracle is the only combination that closes that gap.

The loop catches all four by staging the review:

- **Phase 1 (Opus + narrow specialists)** does broad correctness review plus targeted passes (secure defaults, footgun APIs) when the diff touches those areas.
- **Phase 2 (Codex/GPT-5.5)** cross-vendor review runs LAST so it only sees Opus-blessed state — preserves cross-vendor coverage while spending Codex's tighter ChatGPT Plus quota only on what Opus already cleared.
- **Phase 3 (adversarial)** is a single Opus dispatch framed as "assume the prior phases were wrong, find why" — catches the confirmation-bias class both prior phases share.
- **Phase 4 (taste)** applies to UI work only, after all correctness phases have ADVANCED. Split into 4a (Opus defect + taste critics, existing) and 4b (Codex vision-taste on regenerated shots, second vendor).

Every phase uses a **structured JSON verdict schema** (`verdict-schema.json`) so findings are parseable, not vibes. **The loop-bounds gate is the only authority on advancement; the verdict is one of its inputs and routes nothing.** Between phases, a **consolidator sub-loop** auto-fixes safe_auto LOW findings (severity × autofix_class AND-gate; see below).

## The Phases

```
PHASE 0:  Deterministic rules — scripts/deterministic-gate.sh. Ungated,
                               non-negotiable, re-run every round. No LLM
                               phase may overturn it (see "Phase 0")
PHASE 1:  Opus + specialists — loop rounds until the gate ends the phase
                               (see "Loop bounds")
PHASE 2:  Codex (GPT-5.5) — loop rounds until the gate ends the phase
                               (see "Loop bounds")
PHASE 3:  Adversarial — single Opus round, "the prior phases were wrong"
          framing (a gate `continue` resets to Phase 1; at most ONE reset
          per run)
PHASE 4a: Taste round (UI-only, ONCE) — Opus defect + taste critics
PHASE 4b: Codex vision-taste (UI-only, ONCE) — second-vendor VLM on the
          regenerated shots; combined with geometry-oracle output via
          union + confidence tiers (see "Phase 4b" section below)
```

Whenever the gate advances a phase, the **consolidator sub-loop** runs first if the phase's final round left any `severity=LOW AND autofix_class=safe_auto` finding (see "Consolidator" below).

Each Phase 1/2 round follows the same shape:

```
1. SELF-REVIEW: read each changed file top-to-bottom (not just diff)
2. VERIFY: tests + syntax + smoke green before inviting external eyes. On the
   phase's FIRST round, RECORD the SET OF PASSING TEST IDS from this run as the
   PHASE BASELINE; later rounds reuse it unchanged. `regressed_r` is defined
   against it
3. ADVISE: dispatch the phase's advisor (Opus in phase 1, Codex in phase 2) —
   exactly ONE advisor dispatch per round. From round 2 on, the prompt's "Prior
   rounds fixed:" list carries the entire phase's fix history, compressed per
   the prompt-shape cap
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
4.5. VALIDATE: dispatch a fresh Opus subagent (`Agent(subagent_type="advisor", ...)`) with ONLY the PIVOTAL findings JSON (see "Validate only PIVOTAL findings" below) + absolute paths of files referenced by those findings. `index` is positional within the array you SEND; the parent maps indices back to the full findings list itself. Prompt: "VALIDATION dispatch. For each finding in the input array, read the file at the given line. Emit a JSON array `[{index, verified, unverifiable_confirmed, note}]` where `index` is the 0-based position of the finding in the input array. `verified=true` iff the described condition is empirically present at that location, `false` iff it is not. `unverifiable_confirmed=true` when the finding was tagged `unverifiable: true` by the parent advisor AND you agree no empirical check applies (subjective — taste, 'could be simpler', style preferences). Report what you found. What happens to a finding afterwards is not your concern."

   **Drop rule, with the `unverifiable` exemption stated so it cannot be read as a collision.** Parent drops `findings[i]` iff `verified == false` AND `findings[i].unverifiable != true`. A finding tagged `unverifiable: true` is NEVER dropped on `verified == false`: no empirical check applies to it, so `verified == false` carries no information about it — it is the expected answer, not a refutation. Unverifiable findings continue to step 5 as-is (whether or not the validator confirmed the tag); repro is still attempted and often confirms nothing, which is fine.

   **A dropped finding is CLOSED, still counted, and still logged.** A drop is not a skip and not a deferral: it closes the finding, and it stays in `new_r`. It never reaches step 5, so it never reproduces. Append it to `findings_log` at step 7 with this round's `repro: false`, exactly like an unreproducible one — otherwise next round's dedupe cannot see it and re-flags it as new.

   **Failure-mode handling (degrade-to-pre-4.5 on any failure).** Step 4.5 is an optimization, not a correctness gate:
   - Dispatch the validator ONCE. Never retry it, never PAUSE the loop on it.
   - Anything other than a parseable JSON array carrying exactly one row per sent finding — quota-exhausted, malformed JSON, a short or unparseable array, no usable result at all — means: skip step 4.5 for this round entirely, log `"validator unavailable: <cause>"` in the round's commit message, and proceed to step 5 with ALL findings intact (no drops, no tags — parent behaves as if step 4.5 did not exist).
   - Never patch a partial array; discard it whole.

   **Validator input boundary — findings JSON + absolute paths ONLY.** Never add the advisor's reasoning, exploration trace, or diff context to the validator prompt. The narrower input is the mechanism, not the model choice.

   **Validate only PIVOTAL findings.** A finding the primary AND a specialist flagged INDEPENDENTLY at the same location is not pivotal — skip the validator, go straight to step 5. A singleton finding (one reviewer only) IS pivotal — validate it. This trims step 4.5 only; it NEVER skips step 5 repro.
5. REPRODUCE each BLOCKER/HIGH finding before fixing — advisors hallucinate.
   `new_r` and `repro_r` are both finalised at the END of this step
6. FIX by severity, re-verify after each. Read `regressed_r` off the round's
   FINAL re-verification, diffed against the PHASE BASELINE — run by the PARENT,
   on a settled tree, never from an agent's reported count. Every fix agent's
   brief carries the grep-before-report rule; see "Verification traps" below
7. COMMIT the round (per-round granularity, not a batch), then WRITE RUN
   STATE (`tasks/arftl-run.json` — counters, `baselines`, `findings_log`; see
   "Run state" below). Both, every round.
8. GATE: evaluate the LOOP BOUNDS gate (see below) — every round, whatever the
   verdict. The gate is the ONLY thing that decides what happens next. On
   `continue`, round r+1 opens at step 1 and re-advises at its own step 3; any
   other routing ends the phase here, with no further dispatch.
```

## Verification traps

Each of these has silently produced a WRONG counter — a green report over a red
suite, or a regression the gate could not see. They are cheap to avoid and
expensive to miss, because every number the gate reads comes through here.

**The parent runs the gate. An agent's count is never the gate.** Fix agents
report green on the files they chose to run, and the files they chose are the
ones they were already thinking about. Measured twice: a store agent reported
"1019 passed" from a partial run when the real figure was 1528; a later round had
all THREE fix agents report green while the full suite was red.

**Require every fix agent to grep before it reports.** Selecting test files by
reasoning about the diff is the failure above. Put this in the brief verbatim:

> Before reporting, grep `tests/` for every symbol you touched and run every file
> that matches. Do not select test files by reasoning about your diff.

That one command found the missed file instantly in the round where reasoning had
not. Note the trap is worst for a file whose location breaks the mental pattern —
the miss was `tests/test_x.py` while the agent was thinking in `tests/core/`.

**Never run the gate while an agent is still editing.** A run against a moving
tree produces phantom reds: a test that spawns subprocesses re-imports a module
another agent is mid-commit on. Confirm `git status --porcelain` is clean and all
agents have returned, THEN run. A phantom red costs a full re-run to disprove.

**A fix can void a prior round's test without failing it.** Watch for this
explicitly at step 6 — `regressed_r` counts only FAILING baseline tests, so a
test that still passes while no longer asserting anything is invisible to the
gate. Three real instances in one round: hardening tests whose fixtures stopped
reaching the guard they were written for and now passed trivially; a contract
inverted under a test still asserting the old contract; and a fix landing while
another agent was still reporting the defect. When a fix changes a parse order, a
return type, or a refusal, re-read the prior tests that cover it and confirm they
still fail for the right reason.

**Reading test ids out of a `-v` log.** Use `sed -E 's/ PASSED.*$//'`, never a
strict `^<id> PASSED` anchor: a parametrized id can contain SPACES
(`test_every_spelling[ 1 ]`) and the anchor drops it silently. Symptom is an
off-by-a-few between your extracted count and pytest's own summary — always
reconcile the two before computing `regressed_r`.

**A renamed test is not a regression.** A baseline id missing because the test was
deliberately renamed for a contract change is expected. Check the rename landed
with a successor that still covers the old behaviour before counting it.

**`pytest -q` can suppress the summary entirely.** Where `pyproject.toml` sets
`addopts = "-q"`, adding `-q` resolves to `-qq` and the run ends at `[100%]` with
NO count — which reads as success to anything parsing the tail. Always pass
`-o addopts=` (add `-v` when you need ids).

**Verify a cached check actually ran.** `nix flake check` prints
`running 0 flake checks` when derivations are already in the store, which looks
identical to a check that passed. Force it: `nix build --rebuild <check>`.

**Never name a real uncached model in a test.** One round did and pulled 15 GB
from the HF CDN. Stub the loader and assert on the arguments a resolution is made
with, never on whether a file appears.

**`arftl-run.json` is merged, never hand-written.** `baselines` holds thousands of
test ids that a careless rewrite destroys. Merge with `jq`, assert the counts
before AND after, and `diff` the `baselines` object across the write. Note
`stop_reasons` is an OBJECT keyed by phase — `+= [...]` fails on it.

## Phase 0: Deterministic rules (ungated, non-negotiable)

Every other phase of this loop is a model forming a judgement, and a model can
be argued out of a verdict — by a persuasive comment in the diff, by its own
rationalisation, or by a later phase's framing. Phases 1-3 already guard
against *correlated* error by varying vendor and stance. They do not guard
against the shared property that all three are persuadable.

Regex is not persuadable. That is the whole contribution of this phase, and
the reason it sits outside the gate rather than inside it.

```bash
skills/advice-refine-test-loop/scripts/deterministic-gate.sh --json
# exit 0 = clean · 1 = findings · 2 = the gate could not run
```

**When it runs.** Once before Phase 1, and again at every round's step 2
(VERIFY), against the round's current diff. A round whose fixes introduce a
Phase 0 match is not a passing round, whatever its advisor said.

**Routing — this phase has none, which is the point.** Phase 0 findings do not
enter `new_r`, do not carry an `autofix_class`, are never validated at step
4.5, and are never subject to step 5 repro. There is nothing for an advisor to
dispose of: the pattern either still matches or it does not. While any Phase 0
finding is outstanding, the run cannot advance a phase and cannot ship,
regardless of every verdict in the file.

**Exit 2 is a stop, not a pass.** "The scanner did not run" must never be
indistinguishable from "the scanner found nothing" — a missing `gitleaks`, an
unreadable range, a non-repo path. Exit 2 halts-and-pauses with
`pause_reason: deterministic-gate-unavailable`. A gate that fails open is not
a gate.

**The only escape is an inline waiver**, `arftl-allow: <rule-id>`, on the
matched line or the line directly above it. Waivers live in the diff, so they
are reviewable, greppable and attributable to a commit. There is deliberately
no config file and no global suppression: both are the mechanism by which a
rules layer quietly stops covering anything. A waiver an agent adds to make
the gate pass is itself a visible diff line for Phases 1-3 to review — which is
the correct place for that judgement, since "is this waiver justified" is
exactly the kind of question regex must not try to answer.

**Two named blind spots, both deliberate, neither silent.**

1. *Prose files* (`.md`, `.txt`, `.rst`, `.adoc`) are not pattern-scanned — a
   plan that says "then run `git push --force`" is a sentence, not a push.
   Override with `--include-docs`. gitleaks still covers them; a secret in a
   document is still a secret.
2. *The gate script and its fixture* are not scanned. Both state rules as
   literal text — the fixture exists precisely to hold one example of every
   pattern — so scanning either makes the gate flag its own rule table on
   every edit. Changes to those two files get human review instead.

Measured against this repo's own history, the first draft of these rules
produced 197 findings and zero true ones: `trap 'rm -rf "$TMP"' EXIT` after a
`mktemp -d` is correct code, and `kill … 2>/dev/null || true` is ordinary
cleanup rather than a verification forced green. Tightening the patterns cut
that to 77; skipping prose took it to 0, with the fixture suite still firing
all 11 rule classes. Both numbers matter — the second one alone would have
been precision bought entirely with coverage.

**Rules stay narrow on purpose.** Each one reports something true by pattern
match — destructive shell, force-push and history rewrites, unscoped SQL
mutation, curl-piped-to-shell, disabled TLS verification and host-key checking,
world-writable modes, swallowed exceptions, verification forced green
(`|| true`, `--exit-zero`, `continue-on-error`), suppression markers, widened
permission surface — plus gitleaks for secrets by content. Nothing here needs
to understand intent. Resist adding clever rules: the false-positive rate is
what decides whether anyone still reads the output, and a rules layer people
route around has negative value over none.

## Loop bounds (materiality stop)

Phase 1 and Phase 2 loop until this gate ends them. Nothing else ends them: an
advisor that keeps surfacing fresh MEDIUM/LOW findings would otherwise loop for
hours. This skill's own trigger includes "the user is going to bed", so the
failure mode is an unattended overnight run draining both quota buckets with
nothing to show.

**The gate measures MATERIALITY, not headcount.** A raw BLOCKER+HIGH count is
a bad proxy for progress: a round that finds two *new, reproducible* defects on
an 80-commit diff is the loop working, and stopping to ask about it wastes the
user's attention on a question with an obvious answer. What actually
distinguishes progress from churn is whether each round surfaces defects that
are (a) NEW, (b) SURVIVE REPRODUCTION, and (c) cost less fresh breakage than
they buy. Count that, not findings. **Benefit alone is not the signal — a
round's fixes can break more than the round found.**

**Capture the PHASE BASELINE ONCE, at the phase's FIRST step-2 verification,
before any fix lands.** It is a SET OF TEST IDENTITIES — the full name of every
test that PASSED in that run — written to `baselines[phase]` and never rewritten
while the phase runs. If the runner does not name passing tests by default,
re-run it once with the flag that makes it (`-v`, `--verbose`,
`--reporter=json`). Never record a pass COUNT in its place: a count cannot say
which tests existed, and `regressed_r` is uncomputable without that.

**When NO invocation of the runner yields test identities**, do not substitute a
count and do not assume zero regressions. Write `baselines[<phase>]: null` and
PAUSE before the phase's first advisor dispatch, via the Phase 2 PAUSE handler
with `pause_reason: baseline-unavailable`. On the human's (a) continue, the
phase runs with `regressed_r` forced to `0` and that degradation stated —
`"regressed_r UNMEASURED: no test identities available"` — in the resume line,
in every round commit message for that phase, and in the Exit report. An
unmeasured `regressed_r` is never evidence of no damage, so a `converged` or
`unreproducible` stop for that phase carries the same note.

**OPEN finding — the one definition. Every other mention of `new_r` defers here
and must not restate it.** A BLOCKER or HIGH finding is OPEN from the moment it
is first reported until it CLOSES, and exactly two things close it:

- a fix is applied AND its repro passes afterwards (`fixed: true`); or
- the round produced no demonstration of it (`repro: false`) — either its step-5
  repro did not demonstrate the defect, or it was dropped at step 4.5 and never
  reached step 5.

**A step-4.5 drop CLOSES the finding** — not a skip, not a deferral. It records
`repro: false`, it stays counted in `new_r`, and an `unreproducible` stop prints
it verbatim to the human.

A failed fix does not close a finding. Neither does the advisor going quiet
about it. A closed finding reported again REOPENS iff this round's step-5 repro
demonstrates it again; a relapse is then OPEN and counted exactly like a first
report. Without that demonstration it stays closed — advisor staleness, feeding
no counter.

**No verdict value advances a phase while any finding is OPEN — no verdict value
advances a phase at all.** Advancement belongs to the gate below and to nothing
else; the verdict is one of its inputs. Every finding still OPEN at step 8 was
demonstrated at step 5 of this round, so it is counted in BOTH `new_r` and
`repro_r`. Both advancing stops therefore entail zero OPEN findings:
`converged` requires `new_r == 0` and `unreproducible` requires `repro_r == 0`.
A finding that reproduced and was not fixed is OPEN, so the gate can only
`continue` or halt on it.

**Track per round**, appended to `rounds[phase]` in the run state at step 7:

- `new_r` — the round's BLOCKER+HIGH workload. Candidates are deduped at step 4;
  the count is FINALISED at the END of step 5, once step-4.5 drops AND step-5
  repro outcomes are both known. It is: this round's deduped BLOCKER+HIGH
  candidates, PLUS every finding an earlier round of this phase left open,
  deduped against the candidates so none is counted twice. A step-4.5 drop
  closes a finding but never removes it from `new_r`. A candidate matching an
  already-CLOSED finding counts only when step 5 demonstrates it again (a
  relapse); a re-report that does not reproduce is staleness and is excluded.
  Identity is by MEANING (see below); rewording and shifted line numbers never
  make a finding new.
- `repro_r` — of those `new_r`, how many EMPIRICALLY REPRODUCED at step 5.
  Carried-open findings and re-reports of closed findings are re-checked at
  step 5 like any other. A candidate dropped at step 4.5 never reaches step 5
  and therefore never reproduces.
  Whether a fix later succeeded is a DIFFERENT fact, recorded as `fixed` in
  `findings_log`; it never changes `repro_r`. Invariant: `0 <= repro_r <= new_r`.
- `regressed_r` — how many tests IN THE PHASE BASELINE SET are FAILING at this
  round's LAST verification run (step 6 when a fix was attempted, else the
  step-2 run). A test absent from that set can NEVER be regressed: that covers
  every test added during the phase — including the failing repro tests step 5
  mandates — and every test already failing when the phase began. Measured once,
  at the end of the round. Breakage introduced and repaired within the round is
  not damage and counts 0; breakage carried over from an earlier round of the
  same phase still counts, every round, until it is repaired.

`new_r` and `repro_r` are both FINALISED at the end of step 5, `regressed_r` at
the round's last verification run. None of the three is revised afterwards.

`n` is the number of rounds recorded in `rounds[phase]` for the current phase,
including the round being gated. A Phase 3 reset re-enters Phase 1 WITHOUT
resetting `n` or `baselines["1"]` — `rounds["1"]` keeps accumulating, so the
`ceiling` backstop covers the run's whole Phase 1 cost rather than handing it a
fresh budget.

**Dedupe `new_r` by MEANING, not by `(file, line, claim)`.** The tuple rule is
insufficient: a reworded repeat at a shifted line passes it and counts as NEW,
which inflates the continue signal that the gate below depends on.

**Dedupe assigns IDENTITY, never exclusion.** A candidate that matches a prior
takes that prior's `findings_log` id and still goes to step 5. Whether it counts
in `new_r` is decided at the end of step 5, per the `new_r` definition above.
Never drop a candidate from step 5 because dedupe matched it.

Two methods, in preference order:

1. **Equivalence-judge subagent (preferred). ONE dispatch per round, not one
   per pair.** Pairwise dispatch is O(candidates × priors) — 6 new against 30
   priors is 180 subagent calls, which costs more than the round it is
   bounding. Instead dispatch a single fresh read-only subagent per round with
   both lists numbered:

   ```
   PRIOR (this run, already reported):
     [0] <file> :: <rationale>
     [1] ...
   CANDIDATES (this round):
     [0] <file> :: <rationale>
     [1] ...

   For each CANDIDATE, answer ONE yes/no question against each PRIOR: "do
   these describe the same underlying defect?" No similarity scores, no
   numeric thresholds. Emit ONLY a JSON array
   [{"candidate": <int>, "match": <prior index or null>}], one row per
   candidate, in candidate order. If a candidate matches more than one prior,
   return the lowest matching prior index. If unsure, return null.
   ```

   **Default to "not equivalent" (`match: null`) when ambiguous** — the failure
   direction is then one extra round, never a missed defect. If the prior list
   exceeds 60 entries, split by CANDIDATE into batches of 10 candidates against
   the full prior list; never split the prior list, or a candidate can miss its
   own match.

   **Fallback trigger — the only one.** Dispatch the judge ONCE. Fall back to
   method 2 for this round iff it returns quota-exhausted, returns no usable
   result, or emits anything that is not a parseable JSON array carrying exactly
   one row per candidate. Do not retry, do not PAUSE; a partial or malformed
   array is not patched, it is discarded whole. Log
   `"dedupe fallback: <cause>"` in the round's commit message. On a batched
   dispatch, one failed batch falls back for that batch only.
2. **Character-trigram Jaccard ≥ 0.8 within the same category (fallback).**
   Cheap, no dispatch. Pinned exactly, so two runs over identical traces
   compute identical `new_r`:
   - **Category** = `basename(file)`. Two findings are compared only when their
     `file` basenames are equal. Different basename ⇒ not equivalent, no
     further test.
   - **Text** = the `rationale` string ALONE. Not the file path, not the line,
     not the severity. Normalise in this order: lowercase → replace every
     character that is not `[a-z0-9]` with a single space → collapse runs of
     spaces to one → trim.
   - **Trigrams** = the SET of distinct 3-character substrings of the
     normalised string (character trigrams, not word trigrams — rationales are
     ≤240 chars and word trigrams are too sparse to be stable at that length).
   - **Score** = `|A ∩ B| / |A ∪ B|`. Equivalent iff `≥ 0.8`.
   - Applies from the FIRST prior finding onward. There is no minimum-corpus
     floor: a verbatim repeat must never count as new, least of all in the
     early rounds the gate is most sensitive to.

**Gate, evaluated at step 8 of EVERY round, including rounds that emitted
`PASS`.** Ordered cascade — first match wins, exactly one branch fires:

```bash
# B0. Accounting invariant. A violation means the round's bookkeeping is wrong
#     and no counter downstream can be trusted.
if (( repro_r > new_r || new_r < 0 || repro_r < 0 || regressed_r < 0 )); then
  stop="accounting-error"

# B1. DAMAGE. Tests green at phase start are red now, and this round did not
#     reproduce more than that costs.
elif (( regressed_r > 0 && repro_r <= regressed_r )); then
  stop="no-marginal-gain"

# B2. Nothing carried open, nothing proposed, nothing broken: the good exit.
elif (( new_r == 0 && regressed_r == 0 )); then
  stop="converged"

# B3. Phantoms. Findings arrive but do not survive reproduction. Fires on
#     round 1 too — a first round of pure hallucinations is not convergence.
elif (( repro_r == 0 )); then
  stop="unreproducible"

# B4. COST backstop only. Explicitly NOT a convergence signal — a fixed round
#     ceiling cannot judge whether a round was worth running.
elif (( n >= 8 )); then
  stop="ceiling"

else
  continue_round=1
fi
```

Reaching B2 guarantees `regressed_r == 0` when `new_r == 0` (B1 already took
the `new_r == 0 && regressed_r > 0` case), and reaching B3 guarantees
`new_r > 0`. The cascade is exhaustive over every integer triple.

In prose: **keep going while each round reproduces more new defects than the
phase has broken and left broken.** Round count is irrelevant while that holds —
round 7 finding a real data-loss bug is worth more than stopping at round 3 for
tidiness.

### Stop routing — single source of truth

Every stop reason maps to EXACTLY ONE routing. This table is the ONLY thing in
this file that routes. No verdict value, and no other section, may state or
imply a routing; where one appears to, this table wins.

| Stop | Routing | Parent action |
|---|---|---|
| `converged` | advance | Advance to the next phase. Nothing to surface. |
| `unreproducible` | advance-with-findings-surfaced | Advance. List every unreproduced finding verbatim in the phase's exit commit message and in the Exit report. |
| `no-marginal-gain` | halt-and-pause | Do NOT advance, do NOT run another round. The phase's unrepaired breakage costs at least as much as this round bought, so continuing and advancing are both wrong. PAUSE handler with `pause_reason: no-marginal-gain` and every regressed test named. |
| `ceiling` | halt-and-pause | Do NOT advance. Cost backstop, never read as convergence. PAUSE handler with `pause_reason: ceiling`. |
| `accounting-error` | halt-and-pause | Do NOT advance. PAUSE handler with `pause_reason: accounting-error` and the offending `(new_r, repro_r, regressed_r)` triple. |
| none (`continue_round=1`) | continue | Start round r+1. |

In Phase 3 only, `continue` means: reset to Phase 1 with the findings
pre-seeded, not another Phase 3 round. Capped at ONE reset per run; a second
Phase 3 `continue` halts-and-pauses with `pause_reason: phase3-reset-cap`.

**Every halt-and-pause stop routes to the EXISTING Phase 2 PAUSE handler** —
do not invent a second escalation path:

- **Interactive:** print `"Phase <P> stopped: <reason> after <n> rounds
  (new/repro/regressed per round: <trace>). Options: (a) continue more rounds anyway,
  (b) ship with the remaining findings surfaced, (c) abort."` Wait for the
  user, then apply that option's run-state edit under "Run state" before acting
  on it. (a), (b) and (c) are the same three options in both modes.
- **Unattended:** append to `pending_for_human.md` in the CLAUDE.md format,
  set run-state `status: pending-human` + `pause_reason`, exit
  `pending-human`. Do NOT advance to the next phase and do NOT ship — a
  non-converging phase has not established the coverage the next phase
  assumes.

**Gated phases: 1, 2 and 3.** Every round of all three ends at this gate, and
no phase among them starts, repeats, or ends any other way. Not covered
(bounded by their own caps, and neither loops nor advances on a verdict):
consolidator sub-loop (3 rounds), Phase 4a (one critique + one fix pass),
Phase 4b (one pass per surface).

**Phase 0 is not gated and never routes through this table.** It is a
precondition, not a round: it produces no verdict, has no counters, and cannot
`continue`. An outstanding Phase 0 finding blocks advancement from wherever the
run currently is; `deterministic-gate-unavailable` (exit 2) is a halt-and-pause
through the existing PAUSE handler. See "Phase 0".

## Run state

Loop state currently lives only in the parent's context, carried by the
"Prior rounds fixed:" prompt delta. A compact, crash, or restart mid-Phase-2
therefore loses which phase and round were live and the entire fix history —
and the natural recovery is to restart Phase 1, re-burning both quotas on
work already done. Persist it.

**Write `tasks/arftl-run.json` (repo-local, alongside the `tasks/mission.md`
that CLAUDE.md already mandates) at every round commit — step 7, same step,
no exceptions:**

```json
{
  "status": "running | pending-human | complete",
  "phase": "1 | 2 | 3 | 4a | 4b",
  "round": 3,
  "rounds": {"1": [{"new": 5, "repro": 5, "regressed": 0},
                   {"new": 2, "repro": 2, "regressed": 0},
                   {"new": 0, "repro": 0, "regressed": 0}],
             "2": [{"new": 1, "repro": 1, "regressed": 0}]},
  "baselines": {"1": ["session::drops_line", "cli::exit_code_3"],
                "2": ["session::drops_line", "cli::exit_code_3",
                      "ingest::errors_sink"]},
  "findings_log": {
    "1": [{"id": "p1-f3",
           "file": "/abs/path/session.rs", "line": 88,
           "category": "session.rs",
           "rationale": "<verbatim advisor rationale, <=240 chars>",
           "history": [{"round": 1, "repro": true,  "fixed": true},
                       {"round": 3, "repro": true,  "fixed": false}]}]},
  "dedupe_status": "live | unavailable",
  "stop_reasons": {"1": "converged", "2": "unreproducible"},
  "phase3_resets": 0,
  "round_commits": ["a1b2c3d", "e4f5g6h"],
  "fixed_summary": ["<one line per fixed finding, phase-tagged>"],
  "pause_reason": null,
  "last_update": "<ISO-8601>"
}
```

`fixed_summary` is the durable form of the "Prior rounds fixed:" list — it is
what makes resume cheap rather than a restart. `stop_reasons` records how each
completed phase exited (the stop-routing table); the Exit report reads it from
here, so a compact never loses it.

`baselines[phase]` is the phase's set of passing test IDs, written once at the
phase's first round commit and never rewritten while the phase runs. `null`
means the runner could not name them — see "Loop bounds".

`findings_log` is what makes semantic dedupe survive a compact, and it is what
makes OPEN computable after a resume. **It is APPEND-ONLY, per finding.** At
step 7: append one entry for each BLOCKER+HIGH finding not already logged, and
append EXACTLY ONE `history` row — never a second row bearing the same `round` —
to the matching entry for every logged finding this round touched. Appending a
row EXTENDS the record and is not a rewrite; what is never rewritten is a fact
already written — an entry's `id`, `file`, `category` and `rationale`, and any
`history` row once appended.

- `rationale` verbatim and `category` are the dedupe inputs. Without them
  `new_r` inflates, because every finding looks unseen.
- `history` is what decides whether a finding is still OPEN: **AT MOST ONE row
  per finding per round**, carrying that round's `repro` outcome and whether a
  fix was applied and verified. The OPEN definition reads both fields, so record
  both on every row. A resume computes OPEN by replaying an entry's rows in round
  order and applying the OPEN definition to the last one. If a replay finds two
  or more rows sharing a `round`, it never picks one and never reads array order:
  collapse them into a single row — `repro` true if ANY of them is true, `fixed`
  true only if ALL of them are true — and announce
  `"findings_log anomaly: <id> round <r>"` in the resume line and in every
  subsequent round commit message. That collapse is order-independent and never
  closes a finding that any of the rows leaves open.

**On skill start, before Phase 1:** if `tasks/arftl-run.json` exists with
`status: running`, do NOT start over. Read it, announce `"Resuming ARFTL at
phase <P> round <r> (<n> prior rounds, <k> findings fixed)"`, and re-enter at
the recorded phase with `fixed_summary` seeding the prompt's "Prior rounds
fixed:" list. `status: complete` is a finished prior run: archive or delete it
and start fresh.

`status: pending-human` means a human owes a decision — surface `pause_reason`
and stop; never resume past a PAUSE on your own. The decision is recorded by
editing this file, and these three edits are the persisted form of the PAUSE
handler's (a)/(b)/(c):

- **(a) continue** — set `status: running`, `pause_reason: null`. Next resume
  re-enters at the recorded phase and round and runs another round.
- **(b) ship with findings surfaced** — set `status: running`,
  `pause_reason: null`, and write the pause reason into `stop_reasons[<phase>]`.
  Next resume advances to the next phase without running another round.
- **(c) abort** — set `status: complete`. Next resume archives the file and
  starts fresh.

**A resume that cannot reconstruct `findings_log` must SAY SO, not pretend.**
If the file lacks `findings_log`, or its entries for the current phase are
missing or truncated:

1. Set `dedupe_status: "unavailable"`.
2. Announce `"ARFTL resumed WITHOUT dedupe history — new_r is inflated and an
   unreproducible stop may be an artifact"` in the resume line, in every
   subsequent round commit message, and in the Exit report.
3. Treat every finding as new (`new_r` counts all of them) for the rest of the
   phase.
4. Never report a `converged` or `unreproducible` stop for that phase without
   the degradation note attached. `unreproducible` needs it most: an inflated
   `new_r` over an unchanged `repro_r` drives straight at it, and it ADVANCES —
   so its exit commit message must also say the stop may be an artifact of the
   missing log.

Silently proceeding as if dedupe were live is the failure this rule exists to
prevent.

Write `status: complete` at Exit. The file is state, not a report — the Exit
commit message remains the human-facing artifact.

## Verdict schema

Both lanes emit JSON matching `~/.claude/skills/advice-refine-test-loop/verdict-schema.json`:

```json
{
  "verdict": "PASS" | "PASS_WITH_FIXES" | "BLOCKS",
  "verdict_reason": "<one-line>",
  "findings": [{"severity": "…", "autofix_class": "…", "file": "…", "line": N, "rationale": "…", "unverifiable": true | false | null}]
}
```

**Severity × autofix_class are orthogonal axes.** `severity` is intrinsic risk (blast radius, reversibility, silence, recoverability — a property of the defect); `autofix_class` is apply-authority (can this be applied without a human call). Conflating them ships wrong fixes: a `severity=LOW` rename that ripples across 40 files is NOT `safe_auto` — it's `gated_auto` (mechanically fine but wants human ack) or `manual` (needs human implementation). Consolidator gates on the AND of both axes (see below); severity alone is not license to auto-apply. `advisory` findings are surface-only and never auto-fixed regardless of severity.

**The verdict has exactly two jobs: it carries the findings, and its coherence
with them is cross-validated at step 4** (an unparseable or incoherent response
is a FAILED ROUND). It decides nothing. No verdict value advances a phase, ends
a round loop, or triggers the consolidator — the gate does all three.

- **PASS** = ZERO findings (empty `findings` array).
- **PASS_WITH_FIXES** = at least one MEDIUM or LOW finding, no BLOCKER/HIGH.
- **BLOCKS** = ≥1 BLOCKER or HIGH.

MEDIUMs never enter `new_r` and never block; they surface in the round's commit
message for the human to triage.

The advisor gets ONE part of this and no more: the COHERENCE rule — which verdict string goes with which findings array (`PASS` = empty; `PASS_WITH_FIXES` = no BLOCKER/HIGH; `BLOCKS` = ≥1 BLOCKER/HIGH). Never what a verdict costs, triggers, or buys. `PASS` with a non-empty findings array is unreachable and must never be emitted; LOW-only remainders are `PASS_WITH_FIXES`. `verdict-schema.json` is written to that boundary: its `verdict` description carries the coherence rule only, its `severity` description defines each level by blast radius, reversibility, silence and recoverability, and its `autofix_class` description defines each value by reversibility, how far the edit reaches, and whether human judgement is required. Keep it that way — the schema text ships to the Phase 2 advisor verbatim via `--output-schema`.

Codex enforces the schema via `codex exec --output-schema ~/.claude/skills/advice-refine-test-loop/verdict-schema.json …`. Opus advisor enforces via prompt instruction (advisor.md system prompt requires JSON output).

## Phase 1: Opus + narrow specialists

**Primary dispatch:** `Agent(subagent_type="advisor", prompt=...)` — read-only Opus reviewer; see ~/.claude/agents/advisor.md. NEW SESSION REQUIRED after editing the advisor agent file — Claude Code loads agent definitions at session start. If `advisor` is unknown in the current session, restart before continuing the loop; do NOT fall back to `general-purpose` (loses both invariants: whitelist + bypassPermissions).

**Narrow-specialist sidecars (parallel with primary):** dispatch alongside the general advisor when the diff touches areas the specialist targets. Broad-review LLMs miss things narrow specialists surface reliably (per Gooen RoastMyPost pattern: many specialists > one broad critic).

| Specialist | Trigger — dispatch when diff touches | How to dispatch |
|---|---|---|
| `insecure-defaults` skill | secrets loading, auth code, env-var-driven config, module defaults, feature flags | Fresh Opus subagent via `Agent(subagent_type="advisor", prompt="Invoke the insecure-defaults skill on <paths>. If Skill tool is unavailable in this session (advisor.md was recently edited without a session restart), fall back to applying the skill's checklist manually from your training. Emit findings in verdict-schema.json format.")` |
| `sharp-edges` skill | new public API surface, config schema, CLI flag design, cryptographic function ergonomics | Same shape as above |

**advisor.md now includes `Skill` in its tool whitelist** — safe because Skill invocation is metadata-only (loads instructions into the subagent's context); any tool the loaded skill prescribes that is not in the whitelist still fails, preserving the read-only invariant. NEW SESSION REQUIRED after editing advisor.md; specialist dispatches in a stale session fall back to manual checklist application (still useful, marginally lower recall).

Dispatch specialists in the SAME tool-use block as the primary Opus advisor — parallel, independent, read-only, **NO shared state**. No specialist sees the primary's findings, another specialist's findings, or any hint of what the others are looking at, until all have reported. One shared cue before they report collapses them into one reviewer. Merge afterwards by semantic-equivalence dedupe (same rule as `new_r`), not by `(file, line)`; specialist findings inherit the primary's phase counter but are tagged in commit messages (`[specialist:insecure-defaults]`). If BOTH the primary and a specialist independently flag the same location, that convergence is the confidence signal: skip step 4.5's validator for it and reproduce it first.

Skip specialists when the diff is clearly out-of-scope (pure prose docs, VM test fixture changes, cosmetic renames). Rule of thumb: if you'd forward the diff to a security reviewer at your org, dispatch the specialist.

**Usage-limit fallback (primary + specialists):** if any dispatch returns with "usage limit" in the result (Fable/default org-monthly quota exhausted), re-dispatch ONCE with `model="claude-opus-5"` — Opus is a separate quota bucket and typically completes. Do this only on limit-hit; other failures (crash, timeout, refusal) get diagnosed, not fallback-masked. Never re-dispatch a third time — if Opus also 429s, the round exits with a note in the round's commit message.

Loop rounds until the "Loop bounds" gate ends the phase, then apply its routing. For the merged primary+specialist round, `new_r` is counted over the semantically DEDUPED union (a defect both reviewers flag is one finding, not two), and `regressed_r` is counted once for the round, not per reviewer. The primary and every specialist report into the SAME round; their findings merge before step 4's cross-validation and before the gate.

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

**All four failure modes (quota-exhausted / timeout / connectivity / verdict-missing) route to the SAME PAUSE handler on second failure** — the loop stops, unattended runs write to `pending_for_human.md` and exit `pending-human`, interactive runs prompt the user. Never fall through silently. A DISPATCH FAILURE never advances Phase 2; only a gate stop can, per the stop-routing table in "Loop bounds".

**Timeout:** wrap the codex exec call with a wall-clock cap (`timeout 300s codex exec ...`). A model hang or long stall past 5 minutes is treated as connectivity failure → retry once after 30s → if second try also stalls, PAUSE per above.

**Connectivity failure:** if codex exits with network error, retry once after 30s. On second failure, PAUSE per above.

**Verdict-missing:** if `jq -r '.verdict'` prints `null` or errors, treat the round as failed and re-run once with an explicit "your last output was not valid JSON — emit exactly the verdict-schema.json shape" reminder. On second verdict-missing, PAUSE per above (do NOT silently advance — a Codex round that can't emit a parseable verdict has produced zero coverage).

**Budget expectation:** Phase 2 should usually complete in 1-2 rounds because Opus already caught the bulk. If Codex demands ≥3 rounds, that IS the signal — Codex saw something Opus missed, and the extra rounds are the reason Phase 2 exists. Do not read a flat blocker COUNT as churn: two new reproducible defects in round 4 is the loop working, and on a large diff the count can sit flat for rounds while every round finds something real. The distinction that matters is novelty, reproducibility, and marginal gain, which is what the "Loop bounds" gate measures. Codex re-flagging findings that no longer reproduce is the actual churn signal, and the gate stops on that.

### Why Codex still runs last, and what that costs

**The ordering is a QUOTA decision. Do not defend it as a quality one.** Second-vendor review of another vendor's output can net-regress the work rather than improve it, and the damage travels through one channel: APPLYING the second vendor's feedback. Close that channel, and keep the order.

Required, non-negotiable:

- Every Codex finding clears parent-side reproduction (step 5) before ANY edit. Confidence is not evidence; a Codex BLOCKER that will not reproduce is discarded, not downgraded into a smaller fix.
- Codex never edits. `codex exec`, read-only sandbox, no `--dangerously-*` flags. The parent writes every fix.
- Do not soften a Codex finding into a "safe tidy-up" when repro fails. That is the regression path.

**Still run it.** Codex rounds after clean Opus rounds reliably surface defect classes Opus rounds miss whole — in particular **inference-by-absence**, where a reviewer treats a coverage gap (no test, no branch, no error path) as evidence that the case is handled. Hunt that class explicitly in Phase 2.

Loop rounds until the "Loop bounds" gate ends the phase. Which stops advance and which PAUSE is decided by the stop-routing table in "Loop bounds" — read it there, not here.

## Phase 3: Adversarial round (single Opus dispatch)

After Phase 2 ADVANCES (a gate stop routed `advance` or
`advance-with-findings-surfaced`).

**Phase 3 is ONE round of the same round shape and evaluates the SAME gate at
its step 8** — steps 1-8, repro included. It has exactly one difference, already
stated in the stop-routing table: `continue` means reset to Phase 1 with the
findings pre-seeded, not another Phase 3 round. Increment `phase3_resets`; on a
second `continue`, PAUSE with `pause_reason: phase3-reset-cap`. Every other
routing applies the table unchanged — `converged` and `unreproducible` advance
to Phase 4, `no-marginal-gain` / `ceiling` / `accounting-error` halt-and-pause.

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

**Budget:** exactly 1 dispatch per pass. Never loop adversarial itself; the only loop is Phase 3 → Phase 1, and the gate's `continue` is the only thing that starts it. A second reset is the "split the diff" signal — PAUSE instead.

## Advisor Prompt Shape

Order the prompt for maximum implicit prefix caching (both Anthropic + OpenAI cache stable prefixes automatically; putting varying content last lets rounds 2+ pay only for the delta):

**Stable prefix (identical every round for a given diff):**
- **Project goal** (1–2 sentences)
- **Re-read fresh:** absolute file paths (don't trust advisor memory)
- **Output shape:** emit JSON matching `~/.claude/skills/advice-refine-test-loop/verdict-schema.json`. No prose outside JSON. (Codex enforces via `--output-schema`; Opus advisor.md system prompt enforces.)
- For each finding, emit both `severity` (intrinsic risk) and `autofix_class` (apply-authority — `safe_auto` / `gated_auto` / `manual` / `advisory`). These are orthogonal; do not conflate. Tag findings whose confirmation requires human judgment (taste, style, "could be simpler") as `unverifiable: true`.

**Variable delta (grows each round):**
- **Prior rounds fixed:** bulleted list — prevents re-flagging resolved issues. Covers every prior round in the current phase plus every prior phase's exit-state summary, compressed per the cap below. Never DROP history; compress it.
- **Verified since last round:** what you empirically confirmed (repros run, tests re-run, docs read).
- **Seeded hypotheses:** your own doubts, phrased as "verify X". Advisors follow leads; give them the ones you have.

**Length caps — two separate budgets, so a long run can satisfy both.**

- Everything EXCEPT the "Prior rounds fixed:" list: ≤500 words. Rationale
  strings ≤200 chars each. Advisor's job is triage, not exposition.
- "Prior rounds fixed:" list: ≤30 lines, budgeted on its own. Under 30 entries,
  emit all of them verbatim. At 30 or more, keep the 20 most recent verbatim
  and replace everything older with one summary line per prior round:
  `round <r>: <k> fixed — <id>:<basename>, <id>:<basename>, …`, where `<id>` is
  the entry's `findings_log` id and `<basename>` is `basename(file)`.
  Compression drops the RATIONALE, never the IDENTITY: every older entry keeps
  its own `<id>:<basename>` pair, so a re-flag of a compressed entry is still
  matchable back to its `findings_log` row and recognisable as staleness rather
  than a new finding. The pair counts across all summary lines plus the verbatim
  entries must equal the total fixed so far.

Truncating the fixed-list instead of compressing it makes the advisor re-flag
resolved issues, which the gate then has to disambiguate from real re-flags.
Never silently drop an entry.

### Elicit risk, not policy

**The advisor-facing prompt states what a severity MEANS. It never states what a severity TRIGGERS.** Severity is a property of the defect — blast radius, reversibility, whether the failure is silent, whether the data is unrecoverable. It is not a property of the loop's response to it. Naming the response moves the label the advisor picks, in a direction that differs per advisor, so it cannot be corrected for downstream. Every consequence — next round, consolidator, auto-fix, ship gate, PAUSE — lives in parent-side code and in the parent-side sections of this file. None of it goes in a prompt.

Strip from every advisor prompt (primary, specialist, adversarial, 4a critics, 4b vision): what happens on BLOCKS, what the consolidator does, what auto-fixes, what blocks the ship, how much a round costs, what quota is left, whether the user is asleep, and any rule of the form "a violation of X is HIGH".

**DON'T** — consequence stated, severity contaminated:
```
Rate each finding. BLOCKER or HIGH forces another full round and burns
Codex quota; LOW gets auto-fixed by the consolidator without review.
Violations of the radio-dot rule are HIGH.
```

**DO** — intrinsic risk only:
```
Rate each finding by the risk of the defect itself:
BLOCKER = wide blast radius, irreversible or silent, data unrecoverable.
HIGH    = real failure in normal operation, recoverable but not silently.
MEDIUM  = latent trap or narrow-condition failure, loud when it fires.
LOW     = contained, obvious, trivially reversible.
Rate on those properties alone. Do not consider what happens next.
```

Where the loop needs a severity FLOOR for a known rule class (Phase 4a's taste tiers, Phase 4b's fixed visual rules and drift-backs), the parent applies the floor in code after parsing. Give the advisor the rule to check, not the grade to assign.

One lever is deliberately kept and is NOT severity policy: telling a reviewer to bias toward REPORTING when a downstream arbiter suppresses false positives (Phase 4b). That moves the reporting threshold, not the severity label. Do not extend it into "and mark them HIGH".

## Reproduce Before Fixing

```
IF advisor says "X is a bug":
  Write 10-line repro. Run it. Confirm behavior matches claim.
IF advisor says "file Y contains Z":
  Grep for Z. Don't fix what isn't there.
IF claim is a race / state issue:
  Force the state, verify the failure mode.
```

Discard unreproducible claims — do NOT fix them. Discard means "do not act on", never "delete the record". Record every one: next round's "prior rounds fixed" carries `verified non-issue: <claim>`, `findings_log` gets that round's `repro: false`, and an `unreproducible` stop lists them verbatim in the exit commit message.

**A weak repro is worse than no repro** — it manufactures a false approval that the rest of the loop then trusts. A repro counts toward `repro_r` iff, at step 5 and BEFORE any fix, it FAILS against the current code and exercises the exact path the finding claims. A repro that fails for an unrelated reason is discarded and the finding goes back to unreproduced.

**`repro_r` is decided at step 5 and never revised.** It counts findings whose bad behaviour was empirically DEMONSTRATED, full stop — whether the later fix succeeded is a separate fact, recorded as `fixed` in `findings_log`. A finding that reproduced and is STILL BROKEN at the end of the round counts 1: that is precisely the case the loop must keep working on, and discounting it would advance the phase past a live defect. A fix counts as successful (`fixed: true`) only when the repro passes after it.

## Consolidator sub-loop (LOW remainder)

**Trigger: the gate advanced the phase, AND the phase's final round left at least one `severity=LOW AND autofix_class=safe_auto` finding.** Run it after the gate's advance decision and before the phase transition. No verdict value triggers it, and it never changes the gate's routing. **Consolidator auto-fixes `severity=LOW AND autofix_class=safe_auto` only.** LOWs with `autofix_class` in `{gated_auto, manual, advisory}` surface in the phase's exit commit message for the human — auto-fixing a rippling rename or a design call is exactly the class of thing that turns a nit into a merge conflict. MEDIUMs (any autofix_class) also surface for human triage; auto-fixing them without reproduction is risky (they're "latent bug or future trap", not nits).

Parent MUST filter the JSON findings to `severity=LOW AND autofix_class=safe_auto` before dispatching (`jq '[.findings[] | select(.severity=="LOW" and .autofix_class=="safe_auto")]'`). If nothing passes the AND filter, skip the consolidator entirely and go straight to the exit-commit-message surface step — the remaining LOWs go to the human alongside the MEDIUMs.

**Confirm before fixing — every finding, no exceptions.** The consolidator does NOT get a weaker gate than step 5 just because its findings are smaller. Unverified edits break working code. The gate is lighter in FORM, not in force: read the file at the stated location and confirm the described condition is actually there. No repro script needed — these are nits. Anything that cannot be confirmed against the real file is SKIPPED with a reason, never fixed on the advisor's word.

Dispatch: `Agent(subagent_type="general-purpose", prompt=...)` with the following prompt shape (does NOT use the `advisor` type — consolidator needs Edit access):

```
For EACH finding below, in order:
1. CONFIRM: read the file at the given line. Does the described condition
   actually exist there? If NO, or if the file/line does not exist, or if
   the code has already changed — SKIP it and report
   [skipped: unconfirmed — <what you actually found>]. Do NOT fix it.
   Do NOT go hunting for a nearby thing it might have meant.
2. FIX only what you confirmed. One commit per finding, per-file granularity.

After ALL fixes: re-verify (run the same test/lint/smoke the main loop uses).
Report per-finding: [fixed | skipped: <reason>]. A confirm miss is a normal
outcome, not a failure — report it plainly.

Findings:
<JSON array containing ONLY findings where severity=LOW AND autofix_class=safe_auto
 — parent MUST filter out MEDIUMs and any LOW whose autofix_class is
 gated_auto/manual/advisory before dispatching>
```

**Bounded to 3 rounds** — if 3 consolidator rounds don't clear the LOW list, exit with what's left in a commit message. Don't infinite-loop trivial fixes.

**Consolidator per-finding skip is a first-class outcome** — a nit that turns out to require a design call ("rename this variable" that would ripple through 40 files) gets skipped with a reason, not force-fixed. Same for a confirm miss: `skipped: unconfirmed` is a correct result, and a run where several LOWs come back unconfirmed is information about the advisor, not a consolidator failure.

**Error handling** — if the consolidator subagent crashes, exceeds quota, or produces partial commits mid-batch:
- Parent inspects `git status` on return. Uncommitted mutations from a crashed consolidator are `git stash push -u -m "consolidator-crash-<phase>-<round>"`ed (never `git reset --hard` — preserve evidence).
- Partial commits (some findings fixed, some not) stay in place — subsequent rounds see them as prior-round work and skip the corresponding findings.
- After 3 crash-outs, exit the sub-loop and note "consolidator unavailable: <cause>" in the phase's exit commit message. Remaining LOWs get logged to `pending_for_human.md` if the run is unattended.

After consolidator exits (clean or bounded), complete the phase transition the gate already authorised, using the post-consolidator state as the new baseline.

## Anti-Patterns

| Mistake | Why bad |
|---|---|
| Trust advisor without reproduction | Advisors confident even when wrong |
| Expand scope per advisor suggestion | "Could be cleaner" is not a blocker |
| Same prompt every round | Advisor re-flags fixed issues, wastes findings |
| Batch all rounds into one commit | Loses bisectable history |
| Skip re-verification after each fix | Regressions hide in the batch |
| Stop after round 1 because it "felt thorough" | Most valuable finding often lands in round 3. Stop on the "Loop bounds" gate, not on a feeling — the gate is what makes "keep going" safe to say. |
| Keep looping a phase because the advisor "still has notes" | The bound is marginal gain, not exhaustion. Notes that don't reproduce aren't work. Stop when a round stops reproducing more NEW defects than its fixes break — not when the count stops shrinking. |
| Stop a phase because the blocker COUNT went flat | Count is a bad proxy. A round that finds two new reproducible defects on a large diff is the loop working, and stopping to ask about it spends the user's attention on a question with an obvious answer. Judge novelty, reproducibility, and regressions caused. |
| Count only what a round FOUND, ignoring what its fixes BROKE | Benefit alone is not progress. Fixes regress previously-passing tests, and a loop that never counts `regressed_r` keeps iterating while it destroys working code — reported acceptance rises the whole time. Continue only while `repro_r > regressed_r`. |
| Treat the `n >= 8` ceiling as "the phase converged" | It is a COST backstop. A fixed round ceiling carries no information about whether the last round was worth running. `ceiling` routes to the PAUSE handler; `converged` is a different stop with a different meaning. |
| Dedupe `new_r` by `(file, line, claim)` | A reworded repeat at a shifted line then counts as NEW and inflates the continue signal the gate depends on. Dedupe by MEANING — equivalence-judge subagent, or trigram Jaccard ≥0.8 as fallback. |
| PAUSE and ask the human on a stop the loop can rule on itself | `converged` and `unreproducible` advance without asking, with the reason surfaced in the exit commit message. Asking a human to adjudicate unreproducible claims is the wrong use of their attention. |
| Advance (or run another round) on a `no-marginal-gain` stop | It means the loop is DAMAGING. Advancing ships the damage; continuing feeds it. halt-and-pause. Only the stop-routing table decides this — do not infer routing from a stop's name. |
| Read `converged` as "the round found nothing" | `converged` requires `new_r == 0` — nothing proposed beyond stale re-reports that did not reproduce, AND nothing left open by an earlier round of the phase, per the OPEN definition. A round that proposes findings none of which reproduce is `unreproducible`, and it must surface those findings, not exit silently. |
| Freeze `new_r` before step 5 knows what reproduced | A relapse — closed in an earlier round, demonstrated again now — is then deduped out of the count and `converged` can fire over a live defect. Dedupe assigns identity at step 4; the count is finalised at the end of step 5. |
| Reset to Phase 1 a second time from Phase 3 | Capped at one reset. A diff still REPRODUCING fresh BLOCKER/HIGH after a full 1→2→3 pass wants splitting, not another lap through both vendors. |
| Skip writing `tasks/arftl-run.json` because "the loop is nearly done" | Nearly-done is when a compact is most likely and the fix history is most expensive to lose. Write it at every round commit, same step. |
| Restart at Phase 1 after a compact or crash | Check `tasks/arftl-run.json` first. Re-running phases the gate already advanced re-burns both quotas to reproduce a result you already have on disk. |
| Resume without `findings_log` and keep reporting `new_r` as if it were live | Dedupe runs off that log. Missing it silently inflates `new_r` — the loop looks healthy while the mechanism is off. Set `dedupe_status: unavailable` and say so in every round commit and the Exit report. |
| Skip Phase 2 (Codex) because "Opus already said PASS" | Single-vendor blindness is the whole reason Phase 2 exists. If Codex is out of quota, PAUSE and tell the user — don't ship on a half-run loop. |
| Run Phase 2 in parallel with Phase 1 to "save time" | Wastes Codex quota on drafts Opus will reject. The ordering is a QUOTA argument and only a quota argument — do not claim second-vendor-last reviews better; running a second vendor over another vendor's output can net-regress it. Keep the order, and keep the reproduce gate that makes it safe (see "Why Codex still runs last"). |
| Interleave phases (Opus round, Codex round, Opus round, …) | Same failure mode — Codex burns quota reviewing states you're about to change. Complete Phase 1 first, THEN start Phase 2. |
| Drop Phase 1's "prior rounds fixed" list when starting Phase 2 | Codex will re-flag everything Opus already caught, wasting its budget. Carry the fix history forward into Phase 2's first prompt — compressed per the prompt-shape cap when it is long, never dropped and never truncated. |
| Skip narrow specialists because "general advisor will catch it" | Empirically wrong (per Gooen RoastMyPost). Narrow specialists surface a distinct class of issues (secure defaults, footgun APIs) at higher precision than broad review. Cost is 1-2 extra parallel subagent calls per Phase 1 round. |
| Skip Phase 3 (adversarial) because "both vendors already passed" | The whole point of adversarial is confirmation bias — both vendors can agree on a wrong answer. One extra dispatch per invocation. Cheap. |
| Loop adversarial itself when it emits findings | Adversarial is one-shot confirmation, not another main loop. Findings do not route; only the gate does, and in Phase 3 its `continue` means back to Phase 1. If that keeps firing across resets, split the diff instead. |
| Skip verdict JSON validation and trust prose | Prose verdicts drift ("looks fine", "no major issues", "SAFE-ish"). The schema is what makes findings parseable; if the output doesn't match it, the round FAILED — don't guess intent. |
| Advance, or end a round loop, on a verdict value | The gate is the only authority on advancement. A `PASS` over a finding that reproduced and was not fixed ships a live defect. The verdict carries findings and is cross-validated at step 4; that is all it does. |
| Put "Prior rounds fixed:" at the START of the prompt | Kills prefix caching. Stable content first (goal, re-read paths, output shape), variable delta last. |
| **Skip Phase 4b when the diff touches component templates or CSS** | **Red flag.** The drift-back class of bugs (dot-above-text, selected-state regression, red-title-on-non-danger, combobox-narrower-than-control) survives Phases 1-4a — every frontier VLM misses 60%+ of true CSS defects (DiffSpot 2026), and a single vendor's blind spot repeats across rounds. A second-vendor visual reviewer (Codex vision-taste) plus the geometry oracle is the only pipeline shape that catches them. Skipping 4b on a UI-touching diff ships the exact regression class the founder has already flagged multiple times. |
| Auto-fix a singleton VLM finding without oracle backing | Session-constraint violation. Singleton visual claims can be hallucinations (UI-Lens: VLMs invent defects, e.g. flagging intentional spacing). Verify against the geometry oracle first; if the oracle can't arbitrate, surface to the founder as A/B, don't auto-fix. |
| Auto-block on a singleton VLM finding | Session-constraint violation. `Never singleton-VLM = auto-block.` Bug the ledger, not the ship gate. Only geometry-oracle findings or 2+ reviewer convergence gate the ship. |
| Sigil on a partial Phase 4b | Session-constraint violation. `Sigil requires reviewer confirmation.` The sigil-ready state needs: oracle green + 4b run to completion + all HIGH resolved. A skipped or half-run 4b is not sigil-ready. |
| Add Gemini vision without an API key wired | Would silently degrade to skipping phase 4c and calling the pipeline complete. Note as future upgrade in the skill; do not add as a dispatch step until the API dependency is real. |
| Auto-fix based on severity alone | Severity is intrinsic risk, not apply-authority. A LOW rename that ripples 40 files is not `safe_auto` — it's `gated_auto` or `manual`. Consolidator MUST AND `severity=LOW` with `autofix_class=safe_auto`; anything else surfaces for the human. |
| Skip step 4.5 (validate) because "advisor is trustworthy" | Advisors hallucinate file paths, line numbers, and stale-state claims (they read pre-fix state). Validator is cheap (small prompt, file re-read) and catches the class before it becomes a wasted repro round in step 5. Exactly TWO skips are sanctioned and neither is this one: per-finding, when the primary and a specialist converged independently on the location; wholesale for the round, when the validator itself fails (quota / malformed JSON / no usable result) and the round degrades to pre-4.5 behaviour. Any other skip is the anti-pattern. Neither sanctioned skip ever skips step 5. |
| Tell the advisor what a severity TRIGGERS ("HIGH forces another round", "LOW gets auto-fixed", "a violation of this rule is HIGH") | Consequence text moves the severity the advisor picks, by a different amount and a different direction per advisor, so it cannot be corrected downstream. Describe severity by blast radius / reversibility / silence / recoverability. Consequences live in parent-side code. |
| Let the consolidator fix a LOW without confirming it against the file | The weakest findings would then get the weakest gate, and unverified edits break working code. Read the stated location, confirm the condition is there, `skipped: unconfirmed` otherwise. |
| Give the specialists a peek at the primary's findings before they report | One shared cue collapses independent reviewers into one. Same block, no shared state, merge only after all have returned. |
| Treat Phase 4a's A+B agreement as independent confirmation | Same model with two role labels shares its blind spots. A+B agreement is "look here first". Independence comes from Phase 4b's second vendor and the geometry oracle. |

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

Founder observation: primary Claude visual review misses geometric defects the founder catches on first look — radio-dot Y-centering, dot position, width alignment, selected-state color regressions. The DiffSpot 2026 benchmark (Tencent WeChat AI, arXiv 2605.29615) confirms this generalizes: every frontier VLM catches ≤40.7% of true single-CSS-property defects, and pixel magnitude does not predict detection (r = −0.08). UI-Lens (CVPR 2026) shows the same pattern for boundary defects (overflow, cropping, overlap) at coin-flip accuracy. A second-vendor VLM + a deterministic DOM-geometry oracle is the only combination that closes the gap; single-vendor visual review is not sufficient regardless of which single vendor.

Phase 4b runs a scripted Codex vision-taste pass on the shots regenerated by Phase-R, then merges its findings with the Opus-taste output from 4a and the geometry-oracle output via union + confidence tiers. Codex is the second vendor because it has vision support and a wired-in quota here; see the "Gemini future upgrade" note below.

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
| 4.5 (in-round) | Anthropic (Opus, validator) | Re-verify PIVOTAL (singleton) findings against actual file state; findings + paths only, no advisor context; array-index correlation `[{index, verified, unverifiable_confirmed, note}]`; parent drops `findings[i]` iff `verified=false` AND the finding is not tagged `unverifiable: true`; a drop CLOSES that finding with `repro: false` and keeps it in `new_r`; `unverifiable_confirmed` informational | Feeds into step 5 (reproduce) | Per-finding when two reviewers independently converged on the location (go straight to step 5). Wholesale on any validator failure (quota / malformed JSON / no usable result) — degrade to pre-4.5 behavior, never retry, never PAUSE |
| 2 | OpenAI (Codex / GPT-5.5) | Cross-vendor correctness on Opus-clean state | Phase 1 fix history carried in prompt | Never; PAUSE on quota, don't silently skip |
| 3 | Anthropic (Opus, adversarial) | "Prior phases were wrong — find why" | Same gate at step 8; a `continue` resets to Phase 1 | Never; one shot, cheap |
| 4a | Anthropic (Opus x2 — defect + taste critics) | UI defect + house-style critique | Own phase output only | Non-UI diffs |
| 4b | OpenAI (Codex vision-taste) on regenerated shots | Second-vendor VLM catch-what-Opus-missed | Union w/ geometry oracle (ground truth) + Phase 4a Opus taste, per `docs/reviews/multi-model-taste-ledger.md` protocol | Non-UI diffs; NEVER skip on UI-touching diffs — drift-back class survives 1-4a |
| 4c (future) | Google (Gemini 2.5 Pro / 3.x) | Third-vendor VLM, high-recall miss catcher | Same combiner slot as 4b | No API key wired; not this round |

## Exit

Final commit or chat message: round count + categories of bug caught (fail-open / resource leak / schema mismatch / taste-defect / anti-slop / …) + how the run ended. Trains judgment for when future changes deserve the loop.

Also state, so the run is greppable and a stopped run is never mistaken for a clean one:

- per-phase round counts and `rounds` — the convergence trace: new vs reproduced
  vs regressed per round, which is what shows whether each round bought more
  than it broke
- the stop reason each phase exited on (`converged` / `unreproducible` /
  `no-marginal-gain` / `ceiling` / `accounting-error`) and its routing per the
  stop-routing table, plus every unreproduced finding verbatim on an
  `unreproducible` exit and every regressed test on a `no-marginal-gain` pause
- accepted commit SHA + whether the worktree is clean
- how the run ended: `complete` / `pending-human`, **plus the `pause_reason`** when it stopped on a bound
- whether `regressed_r` was measured or forced to 0 for want of a baseline
- `dedupe_status`, and — when it is `unavailable` — that `new_r` was inflated
  for the affected phases
- anything left in `pending_for_human.md`

Then set `tasks/arftl-run.json` to `status: complete`. A run that exits `pending-human` keeps `status: pending-human` and its `pause_reason` — that file is how the next session knows a human decision is outstanding.
