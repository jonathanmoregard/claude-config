# Pre-registration — does one Self-Review line make a plan surface a load-bearing assumption?

Locked 2026-08-17, before any run. Nothing below may change after the lock; the
harness refuses to run if the rubric, task prompt or either arm is edited.

## Hypothesis

Adding one check to `writing-plans`' Self-Review makes the planning agent state
both readings of an ambiguous instruction fragment *before* the file structure is
final, instead of silently committing to one.

## The historical failure being replayed

Repo `~/.claude` (branch `feat/self-compact`). On 2026-08-15 the user asked for
"rsi reflect and self-compact (**they fit together**). Self-compact: claude auto
writes mission.md and then resets + pick up autonomously at 30% of 1m context".

"They fit together" carries two readings:

- **X (taken):** share machinery — the handoff is written by `session-reflect`'s
  mission mode, i.e. `claude -p --resume <sid> --fork-session --model haiku`.
- **Y (correct):** ship as one package — the live session, which already holds the
  context, writes its own handoff; reflect stays for sessions that have ENDED.

X shipped. Nine days later a live end-to-end found it: reflect replays the whole
session as the prompt, the trigger fires at ~300k against haiku's 200k window, so
on a real 382,543-token session it returned HTTP 400 `prompt_too_long` after
billing $0.23. The feature was guaranteed to fail for exactly the sessions it
exists to serve, and the suite could not see it because it stubs `claude`.
Correction: `4129f79` — "Sharing that machinery was the error."

Ground truth was discovered **later and by other means** (a live run, not anything
in-tree). Nothing at the pin specifies reusing reflect.

## Pin and contamination

- **Pin:** `7958edecaceda679ed8edb975e2e4f26145be320` (2026-08-08 16:48), the parent
  of `139b107`, the commit that first wired `REFLECT_SH` into `idle-handoff.sh`.
- **must_be_present** (the situation really is in the tree): `REFLECT_MODEL:=haiku`,
  `skills/session-reflect/reflect.sh`.
- **must_be_absent** (the answer is not in the tree): `self.compact`, `self_compact`,
  `writes its own handoff`, `no separate writer`, `prompt_too_long`,
  `session is (still )?alive`, `replays the whole session`.
- **later_shas** (must not be ancestors of the pin): `139b107`, `d5180c1`,
  `4129f79`, `136d27a`.

## Arms

- **A:** `writing-plans` SKILL.md verbatim, md5 `fb54b593ac30a85ac27f9d873f8b7bcc`,
  identical to the installed plugin file.
- **B:** A plus exactly one Self-Review check ("Load-bearing assumptions"), a
  3-line diff. No other difference.

Injected with `--append-system-prompt` and `--setting-sources ""`. The installed
skill is never edited.

## Primary outcome — binary, per run

**QUALIFIES** iff the plan, *before its file structure / task list is final*,
states that the handoff writer is an open decision with at least two named
readings — one that reuses the existing session-reflect / resumed-worker
machinery, and one where the live session writes the handoff itself.

Both readings must be named. The run does not need to pick Y.

**Non-qualifiers** (explicitly do NOT count):

1. Naming `session-reflect`, `reflect.sh`, haiku or the resumed worker as a *risk*,
   *concern* or *dependency* without stating the alternative approach.
2. Any assumptions list that appears only *after* the file structure or task
   breakdown is already committed.
3. A generic clarifying question ("what should the handoff writer be?", "should I
   reuse existing tooling?") with no two readings stated.
4. Picking Y silently with no statement that X was the alternative.
5. Listing the ambiguity only in a closing "open questions" / "risks" section.
6. Restating the user's phrase "they fit together" without decomposing it.

## Secondaries (declared now, watched for opposite-direction effects)

- **S1** — plan names a concrete file/module for the handoff writer (either arm).
- **S2** — plan proposes a test that would exercise a real `claude` call rather
  than a stub. (The historical bug was invisible because the suite stubs `claude`.)
- **S3** — total count of `## Assumptions`-style entries. Watching for the failure
  mode where arm B produces a long plausible-but-irrelevant list.
- **S4** — plan asks the user a question at all (either arm).

## Analysis plan

- n = 16 per arm, 32 runs, interleaved a1,b1,a2,b2,…
- Primary compared as a difference in proportions. Report the raw counts.
- Mechanical keyword cross-check on the primary, reported alongside the LLM
  grade; disagreements are reported, never silently resolved.
- I cross-read ≥2 transcripts myself before seeing any grade.
- Grading is blind: outputs shuffled and re-keyed, arm labels stripped, grader
  gets criteria only — no hypothesis, no expected direction.

## Decision rule

- **Adopt** arm B if primary favours B by ≥5/16 (≈31pp) with no secondary moving
  against it by a comparable margin.
- **Reject** if primary favours A, or if S3 shows arm B inflating assumption counts
  without primary gain.
- **Null otherwise** — reported plainly as a null. No "trending toward".

## Power

n=16/arm detects only large effects; the smallest reliably detectable difference
here is roughly 30–35pp. A true 15pp effect will very likely read as null. Stated
in advance so the null is not over-read.

## Inconclusive policy

A run that exits non-zero, is truncated by the budget cap, or returns no final
message is INCONCLUSIVE: excluded from the denominator, reported separately, and
**never retried into a replacement**. Fixed n, one look. n is not raised after a peek.

## Declared threats to validity

1. **Designer bias.** I proposed the change, wrote both arms, the task prompt and
   the rubric. No independent author. Disclosed, not mitigated.
2. **Model transfer.** The historical failure occurred on an Opus-class model; runs
   here are on sonnet for cost. A result may not transfer up-tier.
3. **Prompt anachronism.** The task prompt is the user's requirement as stated on
   2026-08-15, replayed at a 2026-08-08 pin. The requirement did not change in
   between, but the wording is from the later date.
4. **Single case.** One historical failure. The feature-flag failure (`136d27a`,
   "USER DECISION 2026-08-16. Deleted, with no replacement") is the intended
   replication, but its ground truth arrived same-day, so it is not run as primary.
5. **Read-only tools.** Runs get Read/Grep/Glob only, so the plan is the whole
   deliverable. Real planning sessions have more tools and may behave differently.
