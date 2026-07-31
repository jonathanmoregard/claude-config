# Pre-registration — <experiment name>

**Registered: <YYYY-MM-DD>, BEFORE any run was executed.** Frozen by
`runs/prereg.lock.json` (sha256 of this file, the task prompt, both arm files
and the grader rubric). No criterion below may be edited after the first run.
Any post-hoc analysis must be labelled as such in RESULTS.md.

## 1. The historical event being replayed

- **What went wrong:** <one paragraph>
- **Ground truth, and how it was discovered LATER:** <what the right answer
  turned out to be, and by what other means it came to light — a later round, a
  bug report, a customer, a different reviewer>
- **The commit that predates the discovery:** `<sha>` (<date>) — <why this is
  the right pin>

If any of these three is missing, stop. Without a real failure, later-discovered
ground truth, and a commit predating the discovery, you are testing vibes.

## 2. Hypothesis

Adding <the one change> to <skill/rule/prompt> increases the rate at which an
agent <does the measurable thing> when handed <the task>.

**Designer bias, disclosed:** <who proposed the change / wrote both arms / wrote
the task prompt / wrote this rubric>. If that is all the same person, say so
here and repeat it in RESULTS.md.

## 3. Arms — one variable

- **Arm A** = current text, verbatim. md5 `<hash>`, confirmed identical to the
  installed file at copy time. The installed skill is never edited.
- **Arm B** = Arm A plus exactly one surgical change: <describe>. Diff:
  `<+N lines, -M lines>`.

## 4. Task prompt

Frozen at `task_prompt.md`. Scrubbed of hints: it must not name the file,
symbol, or path where the answer lives, and must not mention the behaviour under
test. Pre-flight grep for `<answer terms>` over the prompt must return zero hits.

## 5. PRIMARY outcome — binary, pre-registered

> <the exact yes/no question the grader answers>

- **YES** if <specific, near-mechanical criteria; name the symbols that count>.
- **NO** otherwise.

Explicit non-qualifiers (do NOT score YES for these alone):
- <the near-miss that would tempt a generous grader>
- <generic hand-waving with no concrete instance>

## 6. SECONDARY measures — declared now, not after

<Declare them up front. Watch for opposite-direction effects: an intervention
can improve the primary while degrading a dimension nobody was watching.>

- SECONDARY: <categorical or binary measure>
- TERTIARY: <e.g. did the agent report doing the behaviour at all — separates
  "changed behaviour" from "redirected behaviour it was already doing">

## 7. Grading procedure

Blind. Runs shuffled with a fixed seed, re-keyed to opaque IDs, final message
only, passed inline to a grader with every tool denied. The grader receives the
criteria above and nothing else: no pre-registration header, no arm labels, no
hypothesis, no statement of which way the experimenter expects it to come out.
Un-blinding happens after the last grade is written.

Mechanical cross-check (secondary evidence, not the grade): case-insensitive
keyword scan for `<keywords>`. Disagreement between scan and grader is reported,
not silently resolved.

Human cross-check: the experimenter reads >=2 transcripts against the same
criteria BEFORE seeing grades, and reports agreement in RESULTS.md.

## 8. Analysis plan

- Raw counts per arm: PRIMARY YES / n, n excluding INCONCLUSIVE.
- A run is INCONCLUSIVE if the CLI exits non-zero, the budget cap truncates it
  before a final message, it is rate-limited, or it produces no final message.
  **Inconclusive runs are excluded and reported separately. They are NEVER
  retried into a replacement.**
- n = <N>/arm. This detects only <effect size> effects. State the smallest
  detectable effect in RESULTS.md.
- Fisher exact two-sided may be reported as a descriptive statistic, with the
  explicit caveat that this design cannot support a confident causal claim.
- **A null result is a reportable outcome and will be reported plainly.** No
  spin, no "trending toward", no post-hoc subgroup.

## 9. Decision rule — fixed now

- Adopt the change if: <condition on primary AND no regression on secondary>.
- Do not adopt if: <condition>.
- Null at this n: report as null, state the smallest detectable effect, and
  either fund the bigger n or drop it. Do not rationalise a trend.
