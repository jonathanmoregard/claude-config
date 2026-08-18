# RESULTS — one Self-Review line for load-bearing reuse

Attempt 2, 2026-08-17. Pre-registration: `PREREGISTRATION.md` (locked before any
run, lock at `runs/prereg.lock.json`). Attempt 1 void, see `PILOT-DISCARDED.md`.

## Verdict: NULL on the primary, and the pre-registered REJECT condition fired.

**Do not ship the line.**

> **Corrected 2026-08-17, after publication — read `## Addendum` at the end before
> citing anything below.** Two claims in this file are wrong (the S2 count, and the
> follow-up recommendation built on it), and the single most informative result in
> the study is not reported here at all. The path is abandoned; the addendum says why
> and what was actually learned.

## Primary outcome

| arm | QUALIFIES |
|---|---|
| A (`writing-plans` verbatim) | **0 / 16** |
| B (A + one "Load-bearing reuse" Self-Review check) | **0 / 16** |

Inconclusive: **none**. All 32 runs exited 0, none truncated by the $10.50 cap or
the 1800 s wall. Fixed n, one look, no retries.

No run in either arm stated, before its file structure was final, both (a) that the
reflect/idle-handoff mission machinery was built for a materially different
situation and (b) that not reusing it was an available alternative.

## Secondaries

The harness printed only the primary tally; the grader's per-run q2–q5 JSON was not
persisted. The table below is therefore **my own mechanical measurement over the 32
`*.final.md` outputs**, not the blind grader's output. Labelled as such.

| signal | arm A | arm B |
|---|---|---|
| `## Assumptions` heading present | 0/16 | **14/16** |
| phrase "load-bearing reuse" | 0/16 | 6/16 |
| situational-mismatch language (ended/idle vs alive) | 2/16 | 4/16 |
| not-reusing named as an alternative | 2/16 | 5/16 |
| **S2** proposes a test exercising a real `claude` call | 2/16 | **2/16** |
| **S3** assumption mentions (mean) | 0.5 | **1.4** |
| **S4** lines ending in `?` (mean) | 2.2 | 2.2 |

## What this says

**The line achieves near-total compliance and zero effect on the outcome.** 14 of 16
arm-B runs produced the `## Assumptions` section on request. None of them produced
the judgement the section exists to hold. Arm B nudges the two component markers
upward — mismatch language 2→4, alternative named 2→5 — but never both together,
early enough, in the same run. So the primary stays at zero.

That is the **plausible-but-irrelevant list** failure, measured. It matches the RE
finding that practitioners rate LLM ambiguity output highest on naturalness
(4.08/5) and lowest on usefulness (3.38/5), and it is precisely what both blind
reviewers predicted before any run: *a ledger records assumptions the planner
already noticed; it does not detect the ones it missed.* Codex: "Ledger alone is
not detection." Fable: "It triages candidates; it does not detect."

Per the pre-registered decision rule, this is a **Reject**, not merely a null: S3
shows arm B inflating assumption counts (0.5 → 1.4 mean) with no primary gain,
which was written down in advance as a reject condition.

**The most informative number is S2: 2/16 in both arms.** The historical bug was
invisible because the test suite stubs `claude` — `4129f79` says so explicitly. The
planning-discipline line does not move that at all. Whatever would have caught this
failure lives in the test seam, not in the plan's prose. `e2e-test-seams` is the
skill that owns that ground.

## Disclosures

1. **The mechanical keyword cross-check is void.** I chose `session-reflect`,
   `idle-handoff`, `haiku` — every plan mentions those, so the scan returned YES for
   all 32 and the harness flagged all 32 as grader/keyword disagreements. That is an
   artifact of my keyword choice, not a real disagreement, and it means the primary
   rests on the LLM grader alone plus my own reads. Bad instrument, disclosed.
2. **My own cross-read agreed with the grader, 2/2.** Before any grade existed I read
   `a-1` and `b-1` against the rubric and judged both DOES_NOT_QUALIFY — `b-1` because
   its reuse entry says reflect mission mode "was built for the 2nd-compact defence…
   same contract, no changes needed", naming neither the live-vs-ended mismatch nor
   the alternative. The blind grade returned 0/16 for both arms.
3. **Designer bias, unmitigated.** I proposed the change, wrote both arms, the task
   prompt, the rubric, the contamination lists, the ruling that voided attempt 1, and
   this results file.
4. **Attempt 2 is confirmatory-with-disclosure, not clean pre-registration.** Arm B's
   line was rewritten after reading attempt-1 outputs. Nothing was graded in attempt 1.
5. **Power.** n=16/arm detects ~30–35 pp. A true 15 pp effect reads as null here. But
   the observed primary is 0 vs 0, not a small gap — there is no effect to have missed
   on this measure.
6. **Model transfer.** Runs on sonnet; the historical failure was Opus-class.
7. **Single case**, one repo, read-only tools.

## Cost (subscription quota, cost-equivalent)

| item | $ |
|---|---|
| attempt-1 probes ×2 + 1 killed probe | ~6.2 |
| attempt-1 pilot, 4 runs (discarded) | 10.3 |
| attempt-2 probes ×2 | 5.5 |
| attempt-2 runs, 32 | 87.3 |
| blind grading | 8.1 |
| **total** | **~117** |

Per-arm run spend was balanced: a $44.44, b $42.87.

## What to do instead

1. **Do not add the line.** It buys a section header, not a judgement.
2. The failure this replays was caused by **proximity of adjacent reusable
   machinery**, not by missing planning ceremony — attempt 1 showed both arms design
   correctly when `reflect.sh --mission` does not exist (0 vs 17 mission mentions).
   Prompting a planner to describe its reuse does not make it re-derive whether the
   reuse works.
3. **Follow S2.** The bug shipped because the suite stubs `claude`, so no test could
   see it. A rule that a component's *first real invocation* must be exercised
   unstubbed at least once would have caught it, and is checkable rather than
   introspective. That is `e2e-test-seams` territory.
4. Phases 2–3 of the original proposal (interpretation sampling; Codex+Fable as
   blinded reviewers) remain untested. This result does not bear on them — but it does
   remove the cheap-and-obvious option that was supposed to make them unnecessary.

## Reproduce

```
cd ~/.claude/skills/ab-testing-prompt-changes
python3 ab_harness.py -c ~/.claude/experiments/plan-assumption-ledger/config.json contamination
```

Pin `139b10789f6de8546f9e5d521993f642444f4dae`. Arm A md5
`fb54b593ac30a85ac27f9d873f8b7bcc`, identical to the installed plugin file.
Worktrees under `~/worktrees/planledger-*` (32, sealed) — **removed 2026-08-17**,
`git -C ~/.claude worktree prune` run. The 32 `runs/*.final.md` outputs are the
durable artifact; the worktrees were reconstructible from the pin.

---

# Addendum — corrections, and the result that was missed

Written 2026-08-17, same day, after two blind advisor reviews and a recount.
**Path abandoned at the user's direction.** Nothing shipped, no live skill modified.

## Correction 1 — the S2 count is wrong

The table above reports S2 (plan proposes a test exercising a real `claude` call)
as 2/16 in arm A. It is **at least 3, and the criterion was never well-defined**:

- `runs/a-4.final.md:1073` — "the real `reflect.sh mission` path runs here — a live
  Haiku call … this smoke intentionally exercises the real path end-to-end, not the
  test stub"
- `runs/a-7.final.md:887` — "this smoke uses the REAL `claude` binary and REAL model"
- `runs/a-5.final.md:187` — an opt-in live smoke test, though for the RSI review
  agent rather than the handoff writer

The measurement was mine, mechanical, over `*.final.md`, and undefined as to whether
an opt-in smoke counts. This is the second bad instrument in this study, after the
void keyword cross-check disclosed above. **The stated S2 number should not be cited.**

## Correction 2 — "checkable rather than introspective" was false

The `## What to do instead` section recommends a rule that a component's first real
invocation must be exercised unstubbed at least once, on the grounds that it is
checkable rather than introspective. **That reasoning is circular and the rule does
not work.**

`a-7` proposes exactly that test. Its fixture (`runs/a-7.final.md:880`) is a single
JSONL line that merely *declares* `input_tokens: 800000`, fed against a fabricated
session id. Real binary, real model, real billed call — and the actual prompt is one
line. The historical bug is a 382,543-token session exceeding haiku's 200k. That test
passes and the bug ships unchanged.

What mattered was never *whether* the real process is invoked but whether it is
invoked at **representative scale**, and judging "representative" is the same
unverifiable introspection the rule was meant to escape. Both advisors reached this
independently; it was then confirmed against the file.

Consequently: recommendation 3 in `## What to do instead` is withdrawn, and the
proposed follow-up A/B was not run.

## The result that was actually informative

Not reported in the original write-up. A post-hoc scan of all 32 plans for where each
one *landed* on the handoff writer:

| terminal design | count |
|---|---|
| self-compact routed through `reflect.sh mission` (the haiku writer) | **31 / 32** |
| session writes its own handoff, no detached subprocess | **1 / 32** (`a-9`, arm A) |

`a-9` is an arm-A run — no intervention — and it reached the design that took nine
days and a live failure to find in reality (`4129f79`): "Claude self-authors
`tasks/mission.md` (with full live context, **no detached subprocess**)"
(`runs/a-9.final.md:9`).

**It did not reason its way out of the trap. It never entered it**, because it
resolved the requirement's noun differently. "rsi reflect" denotes two things — the
*feature* (session reflections feeding the RSI daily review) and the *script*
(`reflect.sh`, which happens to own a mission mode). The 31 resolved it to the script
and inherited the script's existing capability, turning "they fit together" into
"share the writer." `a-9` resolved it to the feature — "wire session-reflect's
`final.json` artifacts into the RSI daily-review prompt" — and so had no reason to
reach for the writer at all. Its own gloss on the ambiguous phrase:

> "They fit together because a self-compact event is itself exactly the
> `obstacles_to_autonomy` class of signal … RSI Reflect is what makes that data
> usable later." (`runs/a-9.final.md:11`)

That is the coupling the user meant: thematic and data-level, not machinery-level.

This corroborates the discarded pilot, which found both arms design correctly when
`reflect.sh --mission` does not exist at the pin (0 vs 17 mission mentions,
`PILOT-DISCARDED.md`). **The failure is name-resolution under the pull of adjacent
machinery, not absent planning ceremony** — which explains cleanly why an instruction
to think harder about reuse produced a section heading (14/16) and zero change in
outcome (0/16).

## Why the path stops here

The correct reading exists in the sample space at **~3%**. Detecting the fork by
sampling whole plans would need roughly 20–40 draws — another $100+, on top of the
$117 already spent, for a detector whose value is still unestablished. A cheaper
probe was designed (sample N short readings of the phrase alone, at the pin, measure
spread; ~$10–15, decisive either way) and **declined**. Recorded here so it does not
have to be re-derived.

Untested and unaffected by any of this: Phase 2 (interpretation sampling) and Phase 3
(Codex + Fable as blinded reviewers) of
`plan-uncertainty-gate-proposal.md`. This study removed the cheap option that was
meant to make them unnecessary; it says nothing about whether they work.
