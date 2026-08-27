---
name: advice-refine-test-loop
description: Use when shipping non-trivial changes that must land clean — the user says "look through carefully and fix what's off", "ask for advice and repeat", "keep refining until neat", or is going to bed. Also use to resume an interrupted run recorded in arftl-run.json. Runs four sequential phases (Opus + narrow specialists → Codex → adversarial → optional taste incl. Codex vision-taste 4b for UI diffs), reproducing every finding before fixing it.
---

# Advice-Refine-Test Loop

Single-pass review reliably misses fail-open paths, silently-swallowed exceptions, resource leaks, schema mismatches. Single-vendor review misses whatever that vendor's model is systematically blind to. Both-vendors-agree still misses what confirmation bias hides. Single-vendor VISUAL review misses geometric defects — radio-dot Y-centering, dot position, width alignment, selected-state color regressions — at a rate no single vendor improves on; multi-vendor visual taste plus a deterministic DOM-geometry oracle is the only combination that closes that gap. Benchmark evidence and the full protocol: `~/.claude/skills/advice-refine-test-loop/references/ui-taste-phases.md`.

The loop catches all four by staging the review:

- **Phase 1 (Opus + narrow specialists)** does broad correctness review plus targeted passes (secure defaults, footgun APIs) when the diff touches those areas.
- **Phase 2 (Codex/GPT-5.5)** cross-vendor review runs LAST so it only sees Opus-blessed state — preserves cross-vendor coverage while spending Codex's tighter ChatGPT Plus quota only on what Opus already cleared.
- **Phase 3 (adversarial)** is a single Opus dispatch framed as "assume the prior phases were wrong, find why" — catches the confirmation-bias class both prior phases share.
- **Phase 4 (taste)** applies to UI work only, after all correctness phases have ADVANCED. It is ONE blind parallel wave — lanes `4a` (two Opus critics), `4b` (Codex vision-taste) and the geometry oracle all dispatch concurrently, downstream of a deterministic diff and upstream of a single bounded fix pass. The VLM lanes work from byte-identical shots scoped to that diff's changed-region manifest; the oracle asserts against the live DOM and is scoped, at most, to whole components. No lane ever sees another's output. The protocol lives in `~/.claude/skills/advice-refine-test-loop/references/ui-taste-phases.md` — read it IN FULL when the trigger fires.

Every phase uses a **structured JSON verdict schema** (`verdict-schema.json`) so findings are parseable, not vibes. **The loop-bounds gate is the only authority on advancement; the verdict is one of its inputs and routes nothing.** Between phases, a **consolidator sub-loop** auto-fixes safe_auto LOW findings (severity × autofix_class AND-gate; see below).

## The Phases

```
PHASE 0:  Deterministic rules — ~/.claude/skills/advice-refine-test-loop/
                               scripts/deterministic-gate.sh. Ungated,
                               non-negotiable, re-run every round. No LLM
                               phase may overturn it (see "Phase 0")
PHASE 1:  Opus + specialists — loop rounds until the gate ends the phase
                               (see "Loop bounds")
PHASE 2:  Codex (GPT-5.5) — loop rounds until the gate ends the phase
                               (see "Loop bounds")
PHASE 3:  Adversarial — single Opus round, "the prior phases were wrong"
          framing (a gate `continue` resets to Phase 1; at most ONE reset
          per run)
PHASE 4:  UI taste review (UI-only, ONCE) — one blind parallel wave:
          stage 0  deterministic diff → changed-region manifest
          stage 1  lanes 4a (Opus defect + taste critics), 4b (Codex
                   vision-taste) and ORACLE dispatch CONCURRENTLY — the
                   VLM lanes on byte-identical shots, the ORACLE on the
                   live DOM; no lane sees another's output
          stage 2  combiner · stage 3 one bounded fix pass
          stage 4  post-fix regression gate vs the ORIGINAL baseline
          Read ~/.claude/skills/advice-refine-test-loop/
          references/ui-taste-phases.md IN FULL when the trigger fires
          (see "Phase 4" below)
```

Whenever the gate advances a phase, the **consolidator sub-loop** runs first if the phase's final round left any `severity=LOW AND autofix_class=safe_auto` finding (see "Consolidator" below).

Each Phase 1/2 round follows the same shape:

```
1. SELF-REVIEW: read each changed file top-to-bottom (not just diff)
2. VERIFY: tests + syntax + smoke green before inviting external eyes, and the
   Phase 0 gate against the round's current diff. **Run the two CONCURRENTLY —
   they share nothing.** The gate `cd`s to the repo and reads `git diff`; it
   writes no temp file, takes no lock, and mutates nothing, so neither run can
   observe the other. Serialising them pays the gate's wall time on every round
   of every gated phase and buys nothing.

   **It has exactly one race, and it is on the INDEX, not the worktree.** The
   gate's secret rules run `gitleaks git --staged`, which reads the index.
   Nothing may stage files while the gate is in flight — no `git add`, no
   commit, no `git stash`. An index mutated mid-scan produces a result
   attributable to neither the diff under review nor whatever staged over it,
   and gitleaks reports a hit as a BLOCKER either way. Step 7 is the only step
   of the round that stages and it is downstream of this one, so the constraint
   binds everything else that might: a hook, a helper agent, a background job.

   Concurrency changes what runs at the same time, never what has to be green
   before an advisor is invited. Wait for BOTH before step 3 — a red suite and
   an outstanding Phase 0 finding are each still a round that does not advise —
   and read the gate's exit code exactly as "Phase 0" states it: **any code
   outside `{0, 1}` is halt-and-pause with
   `pause_reason: deterministic-gate-unavailable`.** Racing the gate alongside
   the suite must never soften that into a dropped result or a swallowed
   timeout. An unread exit code is the fail-open this phase exists to prevent.

   On the phase's FIRST round, RECORD the SET OF PASSING TEST IDS from this run
   as the PHASE BASELINE; later rounds reuse it unchanged. `regressed_r` is
   defined against it
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

   Then DEDUPE the round's candidates against the priors with the
   equivalence-judge subagent — ONE dispatch per round, defined under "Dedupe
   `new_r` by MEANING" in "Loop bounds". **Dispatch it in the SAME tool-use
   block as step 4.5's validator.** Both read the findings array this step just
   parsed, neither reads the other's output, and nothing between them needs
   ordering. Sequencing them spends a full subagent round-trip per round, on
   every round of every gated phase, for no information. The dependency that
   makes them LOOK ordered is broken at 4.5 below.
4.5. VALIDATE: dispatch a fresh Opus subagent (`Agent(subagent_type="advisor", ...)`) with ALL of this round's findings JSON — every finding the phase's reviewers reported, merged into one array, undeduped because the judge that dedupes it is in flight alongside this dispatch (see "Send ALL findings, discard the converged rows" below) — + absolute paths of the files those findings reference. `index` is positional within the array you SEND; the parent maps indices back to the full findings list itself. Prompt: "VALIDATION dispatch. For each finding in the input array, read the file at the given line. Emit a JSON array `[{index, verified, unverifiable_confirmed, note}]` where `index` is the 0-based position of the finding in the input array. `verified=true` iff the described condition is empirically present at that location, `false` iff it is not. `unverifiable_confirmed=true` when the finding was tagged `unverifiable: true` by the parent advisor AND you agree no empirical check applies (subjective — taste, 'could be simpler', style preferences). Report what you found. What happens to a finding afterwards is not your concern."

   **Drop rule, with the `unverifiable` exemption stated so it cannot be read as a collision.** Parent drops `findings[i]` iff `verified == false` AND `findings[i].unverifiable != true`. A finding tagged `unverifiable: true` is NEVER dropped on `verified == false`: no empirical check applies to it, so `verified == false` carries no information about it — it is the expected answer, not a refutation. Unverifiable findings continue to step 5 as-is (whether or not the validator confirmed the tag); repro is still attempted and often confirms nothing, which is fine.

   **A dropped finding is CLOSED, still counted, and still logged.** A drop is not a skip and not a deferral: it closes the finding, and it stays in `new_r`. It never reaches step 5, so it never reproduces. Append it to `findings_log` at step 7 with this round's `repro: false`, exactly like an unreproducible one — otherwise next round's dedupe cannot see it and re-flags it as new.

   **Failure-mode handling (degrade-to-pre-4.5 on any failure).** Step 4.5 is an optimization, not a correctness gate:
   - Dispatch the validator ONCE. Never retry it, never PAUSE the loop on it.
   - Anything other than a parseable JSON array carrying exactly one row per sent finding — quota-exhausted, malformed JSON, a short or unparseable array, no usable result at all — means: skip step 4.5 for this round entirely, log `"validator unavailable: <cause>"` in the round's commit message, and proceed to step 5 with ALL findings intact (no drops, no tags — parent behaves as if step 4.5 did not exist).
   - Never patch a partial array; discard it whole.

   **Validator input boundary — findings JSON + absolute paths ONLY.** Never add the advisor's reasoning, exploration trace, or diff context to the validator prompt. The narrower input is the mechanism, not the model choice.

   **Send ALL findings, discard the converged rows.** Only PIVOTAL findings are validated, but the filtering happens on RETURN, not before the dispatch. Pivotal means SINGLETON, and singleton-ness is decided by the equivalence judge's within-round clustering — which is in flight in the same tool-use block. Filtering first would mean waiting for the judge, which reimposes exactly the round-trip the co-dispatch removes. So the parent sends every candidate the round produced and drops rows afterwards: a row for a finding the judge clustered with another reviewer's is DISCARDED UNREAD, never applied to anything. The cost is one extra JSON row and one extra file read per converged finding, inside a dispatch that was going out regardless; the round-trip saved is real and recurs every round.

   The skip itself is unchanged, only its position. A finding the primary AND a specialist flagged INDEPENDENTLY at the same location is not pivotal — the validator's verdict on it is never consulted, and it goes straight to step 5 on the strength of that convergence. A singleton finding (one reviewer only) IS pivotal, and its row is the one the drop rule reads. This trims step 4.5 only; it NEVER skips step 5 repro.

   **The two dispatches fail independently, and co-dispatch does not couple them.** A judge that fails falls back to trigram Jaccard for this round (see "Loop bounds") and leaves the validator's rows entirely usable — the fallback clusters within-round too, so the parent still knows which rows to discard. A validator that fails degrades the round to pre-4.5 behaviour per the failure-mode bullets above and leaves the judge's clustering entirely usable. Neither failure is evidence about the other, and neither is ever retried on the other's account.
5. REPRODUCE each BLOCKER/HIGH finding before fixing — advisors hallucinate.
   **Fan out one agent per finding, each in its OWN git worktree**
   (`Agent(subagent_type="general-purpose", …)`, all dispatched in one tool-use
   block — a repro writes a script, runs a suite and forces a state, so the
   read-only `advisor` type cannot do it). Findings do not depend on each
   other; nothing about reproducing one informs reproducing the next.

   **The per-finding worktree is MANDATORY, not hygiene.** Two repros in one
   tree produce phantom reds by the mechanism "Verification traps" already
   names below — a test that spawns subprocesses re-imports a module another
   agent is mid-write on — and the red that follows belongs to neither finding.
   Here that is worse than the case that section measures. There a phantom red
   costs a re-run to disprove; at step 5 it is SCORED. It lands in `repro_r`,
   the gate reads `repro_r` and never revises it, and step 6 then fixes a defect
   that was never there. Serialising the repros would buy the same guarantee;
   worktrees buy it cheaper, and the step already uses them — `selfinflicted_r`'s
   regression-by-fix check is `git worktree add <tmp> <sha>^`, below.

   **A worktree carries COMMITTED state only.** Confirm `git status --porcelain`
   is clean before fanning out. When it is not — the first round of a run often
   opens on an uncommitted diff — either commit the work under review first
   (step 7 commits it anyway) or run this round's repros serially in the main
   tree and say so in the round commit. A worktree missing the very lines a
   finding is about reports `repro: false` for a live defect, and `repro_r` is
   never revised. Create the worktrees OUTSIDE the repo and remove them
   (`git worktree remove`) before step 6: one left nested inside puts a second
   copy of every file under step 6's grep-before-report rule, which is how a fix
   agent "confirms" a test file it never touched.

   Each agent returns a verdict on ONE finding — reproduced or not, plus the
   repro artifact. The parent tallies; an agent's claim about any finding but
   its own is not a result. `new_r` and `repro_r` are both finalised at the END
   of this step, and so is `selfinflicted_r`: for every finding that reproduced,
   ask whether THIS RUN created it (re-run the repro against the pre-fix commit)
   or whether it reports an earlier fix that never reaches a production caller.
   Both are cheap here, where the repro already exists, and unrecoverable later
6. FIX by severity, re-verify after each. **Fix agents run in parallel over
   DISJOINT FILE SETS, and the partition is written into each brief**: the
   absolute paths that agent owns, and the statement that every other file in
   the repo is read-only to it. Two agents editing one file is a lost edit —
   the second writes over a copy it read before the first landed — and it
   reaches the diff as a fix that silently did not apply, which no later phase
   can distinguish from a fix that was never attempted. Findings touching the
   same file therefore go to the SAME agent, in severity order, even when they
   are otherwise unrelated. A finding whose fix cannot be confined to one
   agent's set is not parallel work: hand the whole set to one agent, or fix it
   in the parent.

   **The verification stays SINGLE and stays the PARENT's** — one run on the
   settled tree, not one per agent and not one per file set. Read `regressed_r`
   off the round's FINAL re-verification, diffed against the PHASE BASELINE —
   run by the PARENT, on a settled tree, never from an agent's reported count.
   Wait for every agent to return and for `git status --porcelain` to be clean
   before starting it. Parallel fix agents make each of the traps below more
   likely, not less, so read them at full strength before dispatching: "The
   parent runs the gate. An agent's count is never the gate", "Never run the
   gate while an agent is still editing", and "A fix can void a prior round's
   test without failing it". Every fix agent's brief carries the
   grep-before-report rule; see "Verification traps" below
7. COMMIT the round (per-round granularity, not a batch), then WRITE RUN
   STATE (`<notes>/arftl-run.json` — counters, `baselines`, `findings_log`; see
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
~/.claude/skills/advice-refine-test-loop/scripts/deterministic-gate.sh --json
# exit 0 = clean · 1 = findings · 2 = the gate could not run
```

**The path is ABSOLUTE, and that is load-bearing.** The loop runs with cwd in
the repo under review, so a repo-relative path resolves to nothing there. **Any
exit code other than 0 or 1 is exit 2's case** — 127 (not found), 126 (not
executable), 130 (interrupted) included. A missing script must never be
indistinguishable from a clean scan.

**When it runs.** Once before Phase 1, and again at every round's step 2
(VERIFY), against the round's current diff. A round whose fixes introduce a
Phase 0 match is not a passing round, whatever its advisor said. At step 2 it
runs CONCURRENTLY with the test suite — it is read-only and takes no lock — and
the single constraint that concurrency imposes is stated there: nothing may
stage files while `gitleaks git --staged` is in flight.

**Routing — this phase has none, which is the point.** Phase 0 findings do not
enter `new_r`, do not carry an `autofix_class`, are never validated at step
4.5, and are never subject to step 5 repro. There is nothing for an advisor to
dispose of: the pattern either still matches or it does not. While any Phase 0
finding is outstanding, the run cannot advance a phase and cannot ship,
regardless of every verdict in the file.

**Exit 2 is a stop, not a pass.** "The scanner did not run" must never be
indistinguishable from "the scanner found nothing" — a missing `gitleaks`, an
unreadable range, a non-repo path, a script the shell could not find or
execute. Exit 2 and every code outside `{0, 1}` halt-and-pause with
`pause_reason: deterministic-gate-unavailable`. Check the code explicitly;
never branch on `if gate; then` alone, which reads 127 as failure-to-find and
sends it down the same path as a real finding. A gate that fails open is not
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
- `selfinflicted_r` — of the findings counted in `repro_r`, how many THIS RUN
  created. Two qualifying classes, both established EMPIRICALLY, never by
  judgement:
  - **regression-by-fix** — run the finding's step-5 repro against the commit
    immediately before the suspect fix (`git worktree add <tmp> <sha>^`, or
    `git stash` the tree and check it out). Qualifies iff the repro does NOT
    demonstrate the defect there and DOES at HEAD. The suspect commits are
    exactly this run's `round_commits`; nothing outside that list can qualify,
    which is what keeps a pre-existing defect from being blamed on the loop.
  - **inert-fix** — an earlier round's fix does not execute on any production
    path, so the defect it claimed to close is still live. Establish it by
    finding the fixed symbol's callers (`grep -rn '<symbol>' <src>`) and showing
    that no non-test caller reaches the guard, stamp or gate that was added.
    A passing repro test is NOT evidence against this: the round-3 fix that
    round 4 caught was `migrate(embedder=)`, whose test called it directly while
    the only production caller never passed the argument.

  Counted once per finding, in the round that DEMONSTRATES the attribution, not
  the round that caused it. Invariant: `0 <= selfinflicted_r <= repro_r`.
  When the attribution check cannot run (no clean worktree, unbuildable parent
  commit), write `selfinflicted_r: null` for that round — never `0` — and say
  so in the round commit; a null suppresses only the two-consecutive-rounds
  condition below, never the whole gate.

`new_r` and `repro_r` are both FINALISED at the end of step 5, `regressed_r` at
the round's last verification run, `selfinflicted_r` at the end of step 5
alongside `repro_r`. None of the four is revised afterwards.

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
     [0] <reviewer> :: <file> :: <rationale>
     [1] ...

   Answer ONE yes/no question — "do these describe the same underlying
   defect?" — twice over: each CANDIDATE against each PRIOR, then each
   CANDIDATE against every other CANDIDATE FROM A DIFFERENT REVIEWER. No
   similarity scores, no numeric thresholds. Emit ONLY a JSON array
   [{"candidate": <int>, "match": <prior index or null>,
     "converged_with": [<candidate index>, ...]}],
   one row per candidate, in candidate order. If a candidate matches more than
   one prior, return the lowest matching prior index. If unsure, return null
   and an empty converged_with.
   ```

   **One dispatch, two clusterings.** `match` assigns `findings_log` identity
   and feeds `new_r`. `converged_with` is how Phase 1 merges the primary's
   findings with each specialist's, and it is what step 4.5 reads to decide
   which findings are PIVOTAL — a non-empty `converged_with` means two reviewers
   reached the same defect independently, so that finding is not a singleton and
   its validator row is discarded. Both clusterings ride the one call: the cost
   argument above forbids a second dispatch for the second question as firmly as
   it forbids pairwise dispatch for the first. In Phases 2 and 3 there is one
   reviewer, so `converged_with` is always empty and every finding is pivotal —
   the convergence skip is a Phase 1 mechanism and always was.

   **Default to "not equivalent" (`match: null`) when ambiguous** — the failure
   direction is then one extra round, never a missed defect. If the prior list
   exceeds 60 entries, split by CANDIDATE into batches of 10 candidates against
   the full prior list; never split the prior list, or a candidate can miss its
   own match. **Batching splits the `match` question only.** Every batch still
   carries the FULL candidate list for the `converged_with` question and emits
   rows for its own 10 — the candidate list is one round's findings and is
   small, while the prior list is what grows. Split it and a converged pair
   landing in different batches reads as two singletons, which inflates `new_r`
   by one and sends both rows to the validator.

   **Fallback trigger — the only one.** Dispatch the judge ONCE. Fall back to
   method 2 for this round iff it returns quota-exhausted, returns no usable
   result, or emits anything that is not a parseable JSON array carrying exactly
   one row per candidate. Do not retry, do not PAUSE; a partial or malformed
   array is not patched, it is discarded whole. Log
   `"dedupe fallback: <cause>"` in the round's commit message. On a batched
   dispatch, one failed batch falls back for that batch only. Method 2 answers
   BOTH questions — the same category-plus-trigram test applied
   candidate-to-prior and candidate-to-candidate — so a fallback round still
   knows which findings converged.

   **The judge is dispatched in the same tool-use block as step 4.5's
   validator, and that couples nothing.** The two never read each other's
   output. A fallback here leaves the validator's rows exactly as usable as they
   were; a validator failure leaves this clustering exactly as usable as it was.
   Neither is retried on the other's account.
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
#     and no counter downstream can be trusted. `selfinflicted_r` is checked
#     only when it is a number; null means the attribution check could not run.
if (( repro_r > new_r || new_r < 0 || repro_r < 0 || regressed_r < 0 )); then
  stop="accounting-error"
elif [[ "$selfinflicted_r" != null ]] && (( selfinflicted_r > repro_r || selfinflicted_r < 0 )); then
  stop="accounting-error"

# B1. DAMAGE. Tests green at phase start are red now, and this round did not
#     reproduce more than that costs.
elif (( regressed_r > 0 && repro_r <= regressed_r )); then
  stop="no-marginal-gain"

# B1.5 CHURN. The loop is fixing its own damage. Sits above B2/B3/B4 because a
#      churning round otherwise looks productive: it reproduces real defects,
#      breaks no baseline test, and ships green. `regressed_r` cannot see this
#      — it counts only BASELINE tests that are failing, and a fix that creates
#      a NEW defect, or that lands on dead code, fails nothing.
elif [[ "$selfinflicted_r" != null ]] && (( selfinflicted_r >= 2 && 2*selfinflicted_r >= repro_r )); then
  stop="self-inflicted-churn"
elif [[ "$selfinflicted_r" != null && "$selfinflicted_prev" != null ]] \
     && (( selfinflicted_r >= 1 && selfinflicted_prev >= 1 )); then
  stop="self-inflicted-churn"

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

`selfinflicted_prev` is the previous round's `selfinflicted_r` for the SAME
phase, read from `rounds[phase]`. On the phase's first round, and across any
round that recorded `null`, the two-consecutive-rounds branch cannot fire —
which is deliberate: one self-inflicted defect is ordinary noise in a fix wave,
and only a majority or a repeat is evidence about the design.

Reaching B2 guarantees `regressed_r == 0` when `new_r == 0` (B1 already took
the `new_r == 0 && regressed_r > 0` case), and reaching B3 guarantees
`new_r > 0`. The cascade is exhaustive over every integer triple; B1.5 narrows
what reaches B2-B4 but can never leave a triple unmatched, because both of its
branches require `selfinflicted_r >= 1`, hence `repro_r >= 1`.

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
| `self-inflicted-churn` | halt-and-pause | Do NOT advance, do NOT run another round. The loop is now generating its own findings, so more rounds converge on nothing. PAUSE handler with `pause_reason: self-inflicted-churn`, naming each self-inflicted finding and the commit that caused it. **On the human's (a) continue, the ADOPT step below runs FIRST — before round r+1's step 1.** |
| `ceiling` | halt-and-pause | Do NOT advance. Cost backstop, never read as convergence. PAUSE handler with `pause_reason: ceiling`. |
| `accounting-error` | halt-and-pause | Do NOT advance. PAUSE handler with `pause_reason: accounting-error` and the offending `(new_r, repro_r, regressed_r, selfinflicted_r)` tuple. |
| none (`continue_round=1`) | continue | Start round r+1. |

In Phase 3 only, `continue` means: reset to Phase 1 with the findings
pre-seeded, not another Phase 3 round. Capped at ONE reset per run; a second
Phase 3 `continue` halts-and-pauses with `pause_reason: phase3-reset-cap`.

**Every halt-and-pause stop routes to the EXISTING Phase 2 PAUSE handler** —
do not invent a second escalation path:

- **Interactive:** print `"Phase <P> stopped: <reason> after <n> rounds
  (new/repro/regressed/selfinflicted per round: <trace>). Options: (a) continue more rounds anyway,
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
consolidator sub-loop (3 rounds), Phase 4 (one blind wave — one pass per
surface per lane — plus one fix pass and one post-fix regression gate).

**Phase 0 is not gated and never routes through this table.** It is a
precondition, not a round: it produces no verdict, has no counters, and cannot
`continue`. An outstanding Phase 0 finding blocks advancement from wherever the
run currently is; `deterministic-gate-unavailable` (exit 2) is a halt-and-pause
through the existing PAUSE handler. See "Phase 0".

## The ADOPT step — stop inventing, go read the reference implementation

**A hand-rolled solution to a well-trodden problem is the root cause of most
churn, and no number of review rounds converges on it.** Each round finds a
fresh symptom of the same wrong design, fixes that symptom, and creates the
next one. The loop cannot detect this from the inside: every round reproduces
real defects and ships green, which is exactly what progress looks like.

This step is the ONLY response to `self-inflicted-churn`. It also runs
unprompted, before round 1, whenever the diff contains a hand-rolled
implementation of a problem with a standard answer. Non-exhaustive list of
what counts: retrieval and ranking, retry/backoff, pagination and cursors,
cache invalidation, auth and session handling, rate limiting, ingest
checkpointing and resume, migration and schema versioning, concurrency
primitives, date/time and timezone handling, text tokenizing and escaping.

1. **Name the standard problem in ONE line.** "Hybrid lexical + dense retrieval
   with reranking", not "search is bad". If it cannot be named in one line, the
   churn is not a design problem — say so explicitly and go back to the gate,
   because the rest of this step will produce a survey nobody can act on.
2. **Research it.** `mcp__research-agent__research` at depth `deep`. Ask for
   REFERENCE IMPLEMENTATIONS, concrete numbers and dated sources — explicitly
   ask it to be opinionated, and to flag where the common advice is outdated.
   A neutral survey is a failed call; re-ask rather than proceed on one. Warn
   the user first: `deep` is ~10-15 minutes of wall time.

   **When the research call cannot run, PAUSE. Never substitute your own
   recall for it.** The tool being absent or disconnected, an error return, and
   a second neutral survey after one re-ask all mean one thing: there is no
   reference implementation to diff against, and diffing against your own
   memory of one is precisely the rationalisation this step exists to stop.
   Halt-and-pause through the existing PAUSE handler with
   `pause_reason: adopt-research-unavailable`. The generic (a)/(b)/(c) do not
   map cleanly onto this stop, so spell them out in the pause line:

   - **(a) proceed unresearched** — run steps 3-5 with the comparison drawn
     from your own knowledge, record `report_path: null` in `adopt_steps`, and
     name the degradation in the round commit message and the Exit report. An
     ADOPT step taken this way NEVER counts as having consulted a reference, so
     a later `self-inflicted-churn` stop is the split-the-branch signal, not
     grounds for a second ADOPT step.
   - **(b) skip ADOPT** — resume the loop at round r+1 with the churn
     unaddressed. This is shipping a known-churning design consciously; say so
     in the Exit report.
   - **(c) abort.**
3. **Diff the design against what came back**, in writing, in three buckets:
   MATCHES / DELIBERATE DEVIATION / ACCIDENTAL DEVIATION. Every deviation lands
   in exactly one of the last two, and "deliberate" requires a constraint that
   was known BEFORE the research came back — a constraint discovered by reading
   the research is a rationalisation, not a reason.
4. **Replace every accidental deviation. Do not patch it.** One commit per
   replacement, the reference cited in the message. A replacement that keeps
   the local design and adds a guard to it is a patch wearing a hat, and it is
   what the next round will find.
5. **Only then resume the loop**, at round r+1 step 1. The replacements are part
   of the diff under review from that point on, exactly like any other commit.

Record the step in the run state: `adopt_steps: [{"round": <r>, "problem":
"<the one line>", "report_path": "<path>", "replaced": ["<sha>", ...]}]`. A
second `self-inflicted-churn` stop AFTER an ADOPT step has run is not another
ADOPT step — it means the diff is too entangled to converge, and it
halts-and-pauses with the split-the-branch recommendation stated verbatim in
the pause line.

## Run state

The parent's context is not durable storage. A compact, crash, or restart
mid-Phase-2 loses which phase and round were live and the entire fix history,
and the natural recovery — restart Phase 1 — re-burns both quotas on work
already done. The file below is what makes that recovery cheap instead.

**Write `<notes>/arftl-run.json` at every round commit — step 7, same step, no
exceptions.** `<notes>` is `~/.local/state/claude-tasks/<repo-name>/` per
CLAUDE.md, where `<repo-name>` is the basename of the repo under review; create
the dir if absent. **Userspace, never repo-relative.** A repo-local
`tasks/arftl-run.json` is swept into the round commit that step 7 makes one line
earlier — and `baselines` holds thousands of test ids and `findings_log` holds
every defect found in the diff, so that commit publishes the run's full defect
history the moment the repo does. Want it visible in the checkout: symlink
`<repo>/tasks` → `<notes>` and gitignore it.

```json
{
  "status": "running | pending-human | complete",
  "phase": "1 | 2 | 3 | 4",
  "round": 3,
  "rounds": {"1": [{"new": 5, "repro": 5, "regressed": 0, "selfinflicted": 0},
                   {"new": 2, "repro": 2, "regressed": 0, "selfinflicted": 1},
                   {"new": 0, "repro": 0, "regressed": 0, "selfinflicted": 0}],
             "2": [{"new": 1, "repro": 1, "regressed": 0, "selfinflicted": null}]},
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
  "adopt_steps": [],
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

**`phase` is `4` for the whole taste wave — never `4a` or `4b`.** Those are lane
names, not resume points. A run-state that could re-enter "at 4b" would restart
one lane with the other's results already in hand, rebuilding the serial relay
Phase 4 exists to avoid, out of the recovery path. A resume inside Phase 4
re-runs the WAVE: a partially-returned wave has no valid combiner input, and its
lanes are cheap relative to shipping on a half-built one.

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

**On skill start, before Phase 1:** if `<notes>/arftl-run.json` exists with
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
is a FAILED ROUND). It routes nothing — see the stop-routing table in "Loop
bounds", which is where advancement is decided.

- **PASS** = ZERO findings (empty `findings` array).
- **PASS_WITH_FIXES** = at least one MEDIUM or LOW finding, no BLOCKER/HIGH.
- **BLOCKS** = ≥1 BLOCKER or HIGH.

MEDIUMs never enter `new_r` and never block; they surface in the round's commit
message for the human to triage.

The advisor gets ONE part of this and no more: the COHERENCE rule — which verdict string goes with which findings array (`PASS` = empty; `PASS_WITH_FIXES` = no BLOCKER/HIGH; `BLOCKS` = ≥1 BLOCKER/HIGH). Never what a verdict costs, triggers, or buys. `PASS` with a non-empty findings array is unreachable and must never be emitted; LOW-only remainders are `PASS_WITH_FIXES`. `verdict-schema.json` is written to that boundary: its `verdict` description carries the coherence rule only, its `severity` description defines each level by blast radius, reversibility, silence and recoverability, and its `autofix_class` description defines each value by reversibility, how far the edit reaches, and whether human judgement is required. Keep it that way — the schema text ships to the Phase 2 advisor verbatim via `--output-schema`.

Codex enforces the schema structurally: `scripts/codex-advise.sh` pins `--output-schema` to this file on every call, in both Codex phases. Opus advisor enforces via prompt instruction (advisor.md system prompt requires JSON output).

## Phase 1: Opus + narrow specialists

**Primary dispatch:** `Agent(subagent_type="advisor", prompt=...)` — read-only Opus reviewer; see ~/.claude/agents/advisor.md. NEW SESSION REQUIRED after editing the advisor agent file — Claude Code loads agent definitions at session start. If `advisor` is unknown in the current session, restart before continuing the loop; do NOT fall back to `general-purpose` (loses both invariants: whitelist + bypassPermissions).

**Narrow-specialist sidecars (parallel with primary):** dispatch alongside the general advisor when the diff touches areas the specialist targets. Broad-review LLMs miss things narrow specialists surface reliably (per Gooen RoastMyPost pattern: many specialists > one broad critic).

| Specialist | Trigger — dispatch when diff touches | How to dispatch |
|---|---|---|
| `insecure-defaults` skill | secrets loading, auth code, env-var-driven config, module defaults, feature flags | Fresh Opus subagent via `Agent(subagent_type="advisor", prompt="Invoke the insecure-defaults skill on <paths>. If Skill tool is unavailable in this session (advisor.md was recently edited without a session restart), fall back to applying the skill's checklist manually from your training. Emit findings in verdict-schema.json format.")` |
| `sharp-edges` skill | new public API surface, config schema, CLI flag design, cryptographic function ergonomics | Same shape as above |

**`advisor.md` whitelists `Skill`** — safe because Skill invocation is metadata-only (loads instructions into the subagent's context); any tool the loaded skill prescribes that is not in the whitelist still fails, preserving the read-only invariant. NEW SESSION REQUIRED after editing advisor.md; specialist dispatches in a stale session fall back to manual checklist application (still useful, marginally lower recall).

Dispatch specialists in the SAME tool-use block as the primary Opus advisor — parallel, independent, read-only, **NO shared state**. No specialist sees the primary's findings, another specialist's findings, or any hint of what the others are looking at, until all have reported. One shared cue before they report collapses them into one reviewer. Merge afterwards by semantic-equivalence dedupe (same rule as `new_r`), not by `(file, line)`; specialist findings inherit the primary's phase counter but are tagged in commit messages (`[specialist:insecure-defaults]`). If BOTH the primary and a specialist independently flag the same location, that convergence is the confidence signal: the validator's verdict on it is never consulted (its row is discarded unread — the validator is sent every finding so it can co-dispatch with the dedupe judge, see step 4.5), and it reproduces first.

Skip specialists when the diff is clearly out-of-scope (pure prose docs, VM test fixture changes, cosmetic renames). Rule of thumb: if you'd forward the diff to a security reviewer at your org, dispatch the specialist.

**Usage-limit fallback (primary + specialists):** if any dispatch returns with "usage limit" in the result (Fable/default org-monthly quota exhausted), re-dispatch ONCE with `model="claude-opus-5"` — Opus is a separate quota bucket and typically completes. Do this only on limit-hit; other failures (crash, timeout, refusal) get diagnosed, not fallback-masked. Never re-dispatch a third time — if Opus also 429s, the round exits with a note in the round's commit message.

Loop rounds until the "Loop bounds" gate ends the phase, then apply its routing. For the merged primary+specialist round, `new_r` is counted over the semantically DEDUPED union (a defect both reviewers flag is one finding, not two), and `regressed_r` is counted once for the round, not per reviewer. The primary and every specialist report into the SAME round; their findings merge before step 4's cross-validation and before the gate.

## Phase 2: Codex (GPT-5.5) advisor

Dispatch through the wrapper — never call `codex exec` by hand:

```bash
~/.claude/skills/advice-refine-test-loop/scripts/codex-advise.sh \
  --prompt-file <path> --out <path>
# exit 0 = verdict produced · 2 = the script could not run
#      3 = quota · 4 = timeout/connectivity · 5 = no parseable verdict
```

The wrapper owns the flag set both Codex phases share — read-only sandbox, no `--dangerously-*` anything, the wall-clock cap, `--output-schema` forcing JSON so nothing regex-parses prose, and **exactly one retry per failure class, never a third attempt**. It is the single copy of that plumbing; Phase 4b calls the same script. Fixtures: `scripts/codex-advise-test.sh`.

Codex CLI runs GPT-5.5 (per `~/.codex/config.toml` default, ChatGPT Plus auth), so Phase 2 passes no `--model`. The prompt is the same "Advisor Prompt Shape" used in Phase 1, with an updated "Prior rounds fixed:" list covering the entire Phase 1 history (primary + specialists) so Codex doesn't re-flag what was already caught.

**Exit 0 is not yet a verdict you may act on.** The script checks that `.verdict` is one of `PASS` / `PASS_WITH_FIXES` / `BLOCKS`; it deliberately does NOT check coherence against the findings array, because that check applies to the Opus lane too and belongs at step 4. Run step 4's cross-validation on the output either way.

**Exits 3, 4 and 5 are the PAUSE triggers, and the script has already spent the one sanctioned retry.** Do not re-invoke it hoping for a better roll:

- **Interactive session:** print "Phase 2 blocked: <quota exhausted | no response | no parseable verdict> at <ts>. Options: (a) wait <T> for reset then resume, (b) ship consciously without cross-vendor coverage, (c) abort." Wait for user decision.
- **Unattended session** (loop triggered by "going to bed" / cron / no live human): append to `pending_for_human.md` per the CLAUDE.md format (`## <date>: Codex Phase 2 quota-exhausted / Why blocked / Steps for human / Everything else done`). Do NOT advance and do NOT ship. Exit the whole skill with status `pending-human`.

**All four failure modes (quota-exhausted / timeout / connectivity / verdict-missing) route to the SAME PAUSE handler** — the loop stops, unattended runs write to `pending_for_human.md` and exit `pending-human`, interactive runs prompt the user. Never fall through silently. A DISPATCH FAILURE never advances Phase 2; only a gate stop can, per the stop-routing table in "Loop bounds". **Exit 2 is not a review result either** — the script could not run, which is the Phase 0 lesson applied here: a missing tool must never read as a clean pass.

**Budget expectation:** Phase 2 should usually complete in 1-2 rounds because Opus already caught the bulk. If Codex demands ≥3 rounds, that IS the signal — Codex saw something Opus missed, and the extra rounds are the reason Phase 2 exists. Do not read a flat blocker COUNT as churn (see "Loop bounds"); the actual churn signal is Codex re-flagging findings that no longer reproduce, and the gate stops on that.

### Why Codex still runs last, and what that costs

**The ordering is a QUOTA decision. Do not defend it as a quality one.** Second-vendor review of another vendor's output can net-regress the work rather than improve it, and the damage travels through one channel: APPLYING the second vendor's feedback. Close that channel, and keep the order.

Required, non-negotiable:

- Every Codex finding clears parent-side reproduction (step 5) before ANY edit. Confidence is not evidence; a Codex BLOCKER that will not reproduce is discarded, not downgraded into a smaller fix.
- Codex never edits. Read-only sandbox, no `--dangerously-*` flags — `scripts/codex-advise.sh` refuses those flags outright rather than trusting the caller to omit them. The parent writes every fix.
- Do not soften a Codex finding into a "safe tidy-up" when repro fails. That is the regression path.

**Still run it.** Codex rounds after clean Opus rounds reliably surface defect classes Opus rounds miss whole — in particular **inference-by-absence**, where a reviewer treats a coverage gap (no test, no branch, no error path) as evidence that the case is handled. Hunt that class explicitly in Phase 2.

Loop rounds until the "Loop bounds" gate ends the phase. Which stops advance and which PAUSE is decided by the stop-routing table in "Loop bounds" — read it there, not here.

## Phase 3: Adversarial round (single Opus dispatch)

After Phase 2 ADVANCES (a gate stop routed `advance` or
`advance-with-findings-surfaced`).

**Phase 3 is ONE round of the same round shape and evaluates the SAME gate at
its step 8** — steps 1-8, repro included. Its one difference is already in the
stop-routing table: `continue` means reset to Phase 1 with the findings
pre-seeded, not another Phase 3 round. Increment `phase3_resets`; on a second
`continue`, PAUSE with `pause_reason: phase3-reset-cap`. Every other routing
applies that table unchanged.

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

**Budget:** exactly 1 dispatch per pass. Never loop adversarial itself; the only loop is Phase 3 → Phase 1, and the gate's `continue` is the only thing that starts it.

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

One lever is deliberately kept and is NOT severity policy: telling a reviewer to bias toward REPORTING when a downstream combiner suppresses false positives (lane 4b). That moves the reporting threshold, not the severity label. Do not extend it into "and mark them HIGH".

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

**`repro_r` is decided at step 5 and never revised** — defined in "Loop bounds". The case worth restating at the point of use: a finding that reproduced and is STILL BROKEN at the end of the round counts 1, because that is precisely the case the loop must keep working on and discounting it would advance the phase past a live defect. A fix counts as successful (`fixed: true`) only when the repro passes after it.

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
| Stop a phase because the blocker COUNT went flat | Count is a bad proxy — on a large diff it can sit flat while every round finds something real. Judge novelty, reproducibility, and regressions caused; that is what the "Loop bounds" gate measures. |
| Count only what a round FOUND, ignoring what its fixes BROKE | Benefit alone is not progress. Fixes regress previously-passing tests, and a loop that never counts `regressed_r` keeps iterating while it destroys working code — reported acceptance rises the whole time. Continue only while `repro_r > regressed_r`. |
| Treat the `n >= 8` ceiling as "the phase converged" | It is a COST backstop. A fixed round ceiling carries no information about whether the last round was worth running. `ceiling` routes to the PAUSE handler; `converged` is a different stop with a different meaning. |
| Dedupe `new_r` by `(file, line, claim)` | A reworded repeat at a shifted line then counts as NEW and inflates the continue signal the gate depends on. Dedupe by MEANING — equivalence-judge subagent, or trigram Jaccard ≥0.8 as fallback. |
| PAUSE and ask the human on a stop the loop can rule on itself | `converged` and `unreproducible` advance without asking, with the reason surfaced in the exit commit message. Asking a human to adjudicate unreproducible claims is the wrong use of their attention. |
| Advance (or run another round) on a `no-marginal-gain` stop | It means the loop is DAMAGING. Advancing ships the damage; continuing feeds it. halt-and-pause. Only the stop-routing table decides this — do not infer routing from a stop's name. |
| Treat a round that finds real defects as proof the loop is working | It is the same picture whether the loop is finding pre-existing defects or the ones it just created. Compute `selfinflicted_r` empirically every round — repro against the pre-fix commit, and check that each earlier fix is reachable from a production caller. A green suite says nothing here: a fix that creates a new defect, or that lands on dead code, fails no baseline test. |
| Respond to churn with another review round, a bigger reviewer, or a third opinion | More eyes on a wrong design produce more symptoms, not convergence. Churn routes to the ADOPT step: name the standard problem, research it, replace the accidental deviations. |
| Call a deviation from the reference design "deliberate" after reading the research | A constraint discovered by reading the research is a rationalisation. Deliberate means known BEFORE — otherwise it is an accidental deviation and it gets replaced. |
| Read `converged` as "the round found nothing" | `converged` requires `new_r == 0` — nothing proposed beyond stale re-reports that did not reproduce, AND nothing left open by an earlier round of the phase, per the OPEN definition. A round that proposes findings none of which reproduce is `unreproducible`, and it must surface those findings, not exit silently. |
| Freeze `new_r` before step 5 knows what reproduced | A relapse — closed in an earlier round, demonstrated again now — is then deduped out of the count and `converged` can fire over a live defect. Dedupe assigns identity at step 4; the count is finalised at the end of step 5. |
| Reset to Phase 1 a second time from Phase 3 | Capped at one reset. A diff still REPRODUCING fresh BLOCKER/HIGH after a full 1→2→3 pass wants splitting, not another lap through both vendors. |
| Skip writing `<notes>/arftl-run.json` because "the loop is nearly done" | Nearly-done is when a compact is most likely and the fix history is most expensive to lose. Write it at every round commit, same step. |
| Restart at Phase 1 after a compact or crash | Check `<notes>/arftl-run.json` first. Re-running phases the gate already advanced re-burns both quotas to reproduce a result you already have on disk. |
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
| Auto-fix based on severity alone | Severity is intrinsic risk, not apply-authority. A LOW rename that ripples 40 files is not `safe_auto` — it's `gated_auto` or `manual`. Consolidator MUST AND `severity=LOW` with `autofix_class=safe_auto`; anything else surfaces for the human. |
| Skip step 4.5 (validate) because "advisor is trustworthy" | Advisors hallucinate file paths, line numbers, and stale-state claims (they read pre-fix state). Validator is cheap (small prompt, file re-read) and catches the class before it becomes a wasted repro round in step 5. Exactly TWO skips are sanctioned and neither is this one: per-finding, when the primary and a specialist converged independently on the location — applied on RETURN, since the validator is sent every finding so it can co-dispatch with the dedupe judge, and a converged finding's row is discarded unread; wholesale for the round, when the validator itself fails (quota / malformed JSON / no usable result) and the round degrades to pre-4.5 behaviour. Any other skip is the anti-pattern. Neither sanctioned skip ever skips step 5. |
| Tell the advisor what a severity TRIGGERS ("HIGH forces another round", "LOW gets auto-fixed", "a violation of this rule is HIGH") | Consequence text moves the severity the advisor picks, by a different amount and a different direction per advisor, so it cannot be corrected downstream. Describe severity by blast radius / reversibility / silence / recoverability. Consequences live in parent-side code. |
| Let the consolidator fix a LOW without confirming it against the file | The weakest findings would then get the weakest gate, and unverified edits break working code. Read the stated location, confirm the condition is there, `skipped: unconfirmed` otherwise. |
| Give the specialists a peek at the primary's findings before they report | One shared cue collapses independent reviewers into one. Same block, no shared state, merge only after all have returned. |

## Phase 4: UI taste review (UI-work only)

The full protocol lives in
`~/.claude/skills/advice-refine-test-loop/references/ui-taste-phases.md`.
**Read that file in full before dispatching the wave** — not in part, and not
from memory. It is the only place the stage order, the lane prompts, the Codex
vision invocation, the combiner table, the hallucination drop rule, the
degrade-when-absent paths, the oracle's retrieval path, the measured integration
traps and the disposition ledger are written down.

**Shape, so it cannot be got wrong from memory.** Phase 4 is ONE wave, not a
relay. A deterministic diff runs first and yields the changed-region manifest;
then lane `4a` (two Opus critics), lane `4b` (Codex vision-taste) and the
geometry oracle dispatch CONCURRENTLY, in one tool-use block, with no lane
receiving another lane's output; then the parent combines, runs exactly ONE fix
pass, and closes with a post-fix regression gate that re-diffs against the
ORIGINAL baseline. The three VLM lanes read byte-identical shots scoped to the
manifest; the oracle reads the live DOM instead, takes no shot, and scopes only
to whole components. Quoting one reviewer's judgment into another's context moves
a VLM judge 19-26 pp (arXiv 2608.07920), and a defect fixed between lanes no
longer renders for the later lane to vote on — serial ordering therefore loses
coverage twice over. `4a` and `4b` are lane names used by the ledger and the
composition table; they imply no ordering, and the run state records the whole
wave as phase `4` so no resume can re-enter at one lane.

**Trigger — read the reference iff ALL THREE hold:**

- the diff touches HTML templates / view partials / CSS / rendered-view
  components (see `~/.claude/skills/visual-verify/SKILL.md` glob list), AND
- Phases 1-3 ALL ADVANCED — each exited on a gate stop routed `advance` or
  `advance-with-findings-surfaced`. A `halt-and-pause` stop never reaches Phase
  4; the run is paused, not advanced, AND
- regenerated shots exist for the touched surfaces.

Pure backend / infra / docs diffs skip Phase 4 entirely and go straight to Exit.

Neither deterministic component is a trigger condition. A project with no
geometry oracle and no screenshot-diff harness still runs the VLM lanes; what
changes is which guarantees the run may claim. The reference file states, per
component, what is lost and what must NOT be claimed — **measured 2026-08-27,
the one project using this skill has neither on its default branch, so that is
the normal path, not an edge case.** Recording a missing component as a passing
check is the fail-open failure this skill exists to name.

**Completion contract — stated here because it must hold whether or not the
reference file was read.** On a UI-touching diff every VLM lane runs, and each
runs to completion. Lane 4b is never silently skipped: the drift-back class of
defects (dot-above-text, selected-state regression, red-title-on-non-danger,
combobox-narrower-than-control) survives Phases 1-3 and the Opus lanes whole,
because every frontier VLM misses 60%+ of true single-CSS-property defects
(DiffSpot 2026, arXiv 2605.29615) and a single vendor's blind spot repeats across
rounds. A skipped 4b is worse than a skipped Phase 2. If 4b cannot run, that is a
PAUSE per the Phase 2 rules — never a completed phase.

**Two ship-gating constraints, duplicated here verbatim on purpose** — they
decide whether the run ships, so they must not live only behind a Read:

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

Every lane emits `verdict-schema.json` and is parsed by the same step-4 `jq`
cross-validation as every other phase. Every lane applies its severity FLOORS
parent-side, after parsing, never in a prompt (see "Elicit risk, not policy").
Phase 4 is not gated by "Loop bounds": one wave, one pass per surface per lane,
one bounded fix pass, one post-fix regression gate whose fix authority covers
oracle violations and the contamination it detects — nothing else, or the
one-fix-pass cap is fiction.

## Composition table (skill-facing summary)

| Phase | Vendor | Reviewer role | Combines with | Skip when |
|---|---|---|---|---|
| 1 | Anthropic (Opus + specialists) | Broad correctness + narrow security/API passes | Own phase output only | Never; foundation phase |
| 4.5 (in-round) | Anthropic (Opus, validator) | Re-verify findings against actual file state — sent ALL of the round's findings (the judge that dedupes them is in flight alongside), with only the PIVOTAL (singleton) rows applied. Input boundary, drop rule and degrade-on-failure behaviour all defined at step 4.5. A drop CLOSES the finding with `repro: false` and keeps it in `new_r` | Co-dispatched with step 4's dedupe judge in ONE tool-use block; feeds into step 5 (reproduce) | Per-finding on independent two-reviewer convergence, applied by discarding that row on return; wholesale on any validator failure — see step 4.5 |
| 2 | OpenAI (Codex / GPT-5.5) | Cross-vendor correctness on Opus-clean state | Phase 1 fix history carried in prompt | Never; PAUSE on quota, don't silently skip |
| 3 | Anthropic (Opus, adversarial) | "Prior phases were wrong — find why" | Same gate at step 8; a `continue` resets to Phase 1 | Never; one shot, cheap |
| 4 stage 0 | none (deterministic) | Capture + diff vs baseline → changed-region manifest; scopes the VLM lanes (the oracle takes no shots and has only component granularity). 100% of CSS changes vs 40.7% for the best VLM | Consumed as a filter by the combiner, not as findings | Non-UI diffs; degrades per "Phase 4" when the project has no diff harness |
| 4 lane 4a | Anthropic (Opus x2 — defect + taste critics) | UI defect + house-style critique — protocol per "Phase 4" | Combiner, after ALL lanes return. Same model twice = ONE vendor | Non-UI diffs |
| 4 lane 4b | OpenAI (Codex vision-taste) on regenerated shots | Second-vendor VLM catch-what-Opus-missed — protocol per "Phase 4" | Combiner, after ALL lanes return, per `docs/reviews/multi-model-taste-ledger.md` protocol | Non-UI diffs; NEVER skip on UI-touching diffs — drift-back class survives Phases 1-3 and the Opus lanes |
| 4 lane ORACLE | none (deterministic) | Geometry assertions against the LIVE DOM (no shots, component granularity only). Ground truth, not a vote — auto-fixes with no VLM vote required | Combiner. Dispatched in the same block as the VLM lanes; feeds no prompt. Its JSON is retrievable ONLY via the report — see "Getting the violations out" in the reference | Non-UI diffs; degrades per "Phase 4" when the project has no oracle |
| 4 stage 4 | deterministic + the same VLM lanes | Post-fix regression gate: re-diff vs the ORIGINAL baseline, VLM pass on newly-changed regions only | Catches reflow contamination from the one fix pass | Never on a UI diff that was fixed; no VLM sub-pass when nothing newly changed |
| 4 lane 4c (future) | Google (Gemini 2.5 Pro / 3.x) | Third-vendor VLM, high-recall miss catcher | A FOURTH lane in the same blind wave, same combiner rules | No API key wired |

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
- on a UI diff that reached Phase 4: the per-vendor hallucination DROP RATE with
  its denominator (`n/a` when no diff harness was available — **never `0`**), and,
  for each deterministic component the project lacks, that its precondition is
  UNMET rather than satisfied. "No oracle" is not "oracle green"; the reference
  file fixes the wording for both cases
- anything left in `pending_for_human.md`

Then set `<notes>/arftl-run.json` to `status: complete`. A run that exits `pending-human` keeps `status: pending-human` and its `pause_reason` — that file is how the next session knows a human decision is outstanding.
