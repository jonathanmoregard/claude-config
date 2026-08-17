# Pre-registration (attempt 2) — does one Self-Review line make a plan interrogate inherited reuse?

Locked 2026-08-17, before any run of attempt 2. Attempt 1 is void; see
`PILOT-DISCARDED.md` and `PREREGISTRATION-pilot-void.md`. Its 4 runs are archived
in `runs-pilot-discarded/`, ungraded, and are not carried into any result.

## Hypothesis

Adding one check to `writing-plans`' Self-Review makes the planning agent state,
before the file structure is final, that a component it plans to reuse was built
for a different situation than the requirement needs — and name not-reusing it as
an alternative.

## The historical failure being replayed

Repo `~/.claude`, branch `feat/self-compact`. On 2026-08-15 the user asked for
"rsi reflect and self-compact (they fit together). Self-compact: claude auto
writes mission.md and then resets + pick up autonomously at 30% of 1m context".

At that moment `scripts/idle-handoff.sh` already existed and already invoked
`session-reflect`'s mission mode as the handoff writer. The plan inherited that
coupling rather than re-deriving whether it could work. It could not: reflect's
worker runs `claude -p --resume <sid> --fork-session --model haiku`, which replays
the whole session as the prompt, while the trigger fires at ~30% of a 1M window
(~300k) against haiku's 200k. On a real 382,543-token session it returned HTTP 400
`prompt_too_long` after billing $0.23 — guaranteed to fail for exactly the
sessions the feature exists to serve, and invisible to the suite because the suite
stubs `claude`.

Corrected nine days later in `4129f79`: "session-reflect is for sessions that have
ENDED … self-compact fires while the session is ALIVE. **Sharing that machinery
was the error.**"

Ground truth was discovered later and by other means — a live end-to-end run, not
anything in-tree.

## Pin and contamination

- **Pin:** `139b10789f6de8546f9e5d521993f642444f4dae` (2026-08-08), the commit that
  introduced mission mode + the idle-handoff timer. This is the state the
  2026-08-15 session actually inherited.
- Attempt 1 pinned this commit's *parent*, which removed mission mode (0 mentions
  vs 17) and `idle-handoff.sh` entirely — deleting the cause of the failure. That
  is why attempt 1 is void.
- **must_be_present:** `REFLECT_SH` in `scripts/idle-handoff.sh`; `mission mode`;
  `REFLECT_MODEL:=haiku`.
- **must_be_absent:** `prompt_too_long`, `writes its own handoff`,
  `no separate writer`, `replays the whole session`, `sessions that have ended`,
  `too (large|long) for haiku`, `context window.{0,60}haiku`.
- **later_shas** (must not be ancestors of the pin): `d5180c1`, `4129f79`,
  `136d27a`.
- **Known adjacent fact, available to both arms equally:** `context-baggage.py`
  mentions `200k` three times, all about detecting the *current session's* window
  size, not haiku's limit. Not the conclusion. Recorded rather than filtered.

## Arms

- **A:** `writing-plans` SKILL.md verbatim, md5 `fb54b593ac30a85ac27f9d873f8b7bcc`,
  identical to the installed plugin file.
- **B:** A plus exactly one Self-Review check, "Load-bearing reuse". 3-line diff,
  no other difference.

Injected via `--append-system-prompt` with `--setting-sources ""`. The installed
skill is never edited.

## Primary outcome — binary, per run

**QUALIFIES** iff, *before the plan's file structure / task list is final*, the
plan states BOTH:

1. that the existing reflect/idle-handoff mission-writing machinery was built for a
   different situation than this requirement needs — naming the difference in
   substance (e.g. ended/idle sessions vs a live session; an external resumed
   worker vs the session that already holds the context; a small-model worker vs a
   large context), and
2. that not reusing it — the running session producing the handoff itself — is an
   available alternative.

The run does not need to choose the alternative.

**Non-qualifiers** (do NOT count):

1. Naming reflect / idle-handoff / haiku as a dependency, risk or note without
   stating what it was built for versus what is needed.
2. Any assumptions list appearing only after the file structure or task breakdown
   is committed.
3. A generic clarifying question with no substantive difference named.
4. Choosing the alternative silently, with no statement that reuse was the other option.
5. The point appearing only in a closing "risks" / "open questions" section.
6. Noting that reuse requires *modification* ("extend mission mode to…") without
   naming the situational mismatch.
7. Reciting "they fit together" without decomposing it.

## Secondaries (declared now; watched for opposite-direction effects)

- **S1** — plan names a concrete file/module as the handoff writer.
- **S2** — plan proposes a test exercising a real `claude` call rather than a stub.
- **S3** — count of stated assumptions. Watching for arm B inflating a
  plausible-but-irrelevant list.
- **S4** — plan asks the user any question.

## Analysis plan

- n = 16 per arm, 32 runs, interleaved a1,b1,a2,b2,…
- Primary as a difference in proportions; raw counts reported.
- Mechanical keyword cross-check reported alongside the LLM grade; disagreements
  reported, never silently resolved.
- I cross-read ≥2 transcripts myself before seeing any grade. (This is what caught
  attempt 1's defect, so it is load-bearing, not ceremony.)
- Blind grading: shuffled, re-keyed, arm labels stripped, criteria only.

## Decision rule

- **Adopt** B if primary favours B by ≥5/16 (≈31pp) with no secondary moving
  against it comparably.
- **Reject** if primary favours A, or if S3 shows B inflating assumption counts
  without primary gain.
- **Null otherwise**, reported plainly. No "trending toward".

## Power

n=16/arm detects roughly a 30–35pp difference. A true 15pp effect will read as
null. Stated in advance.

## Inconclusive policy

Non-zero exit, budget truncation, or no final message ⇒ INCONCLUSIVE: excluded
from the denominator, reported separately, never retried into a replacement.
Fixed n, one look. n is not raised after a peek.

## Declared threats to validity

1. **Designer bias.** I proposed the change, wrote both arms, the task prompt, the
   rubric, the contamination lists, and the ruling that voided attempt 1. No
   independent reviewer.
2. **Second attempt after a peek.** Attempt 1's outputs were read before this
   design was written. Nothing was graded and the reason for voiding was that the
   task did not instantiate the failure — but the sequence is disclosed, and arm
   B's line was rewritten after reading attempt-1 outputs. A stricter reading
   would call this exploratory; the honest label for attempt 2 is
   **confirmatory-with-disclosure**, not clean pre-registration.
3. **Model transfer.** Failure occurred on an Opus-class model; runs are on sonnet.
4. **Prompt anachronism.** Requirement wording is from 2026-08-15, pin is
   2026-08-08. The requirement did not change between those dates.
5. **Single case.** One historical failure. The feature-flag failure (`136d27a`)
   is the intended replication but its ground truth arrived same-day.
6. **Read-only tools.** Read/Grep/Glob only, so the plan is the whole deliverable.
