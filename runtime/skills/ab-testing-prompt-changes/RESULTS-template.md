# Does <the one change> improve <skill/rule/prompt>?

An A/B replay of a real historical failure, with known ground truth.

**Verdict up front: <yes / no / no evidence either way>.** Arm A <x>/<n>, Arm B
<y>/<n> on the pre-registered primary. <test> p = <p>. <One sentence on the
secondary measures, including any that run the other way.> n=<N>/arm is
<directional at best / adequately powered for a Xpp swing>.

---

## 1. The event being replayed

<What failed, when, how the right answer was discovered later, and why this pin
is the last commit that predates the discovery.>

| Round / step | Instance | Location |
|---|---|---|

**Hypothesis under test:** <one sentence>.

## 2. Design

**Pinned tree.** `<sha>` — <why>. <2n> detached worktrees at
`<worktree_root>/<prefix>-{a,b}-{1..n}`, verified at the pinned SHA by the
harness before it spends anything.

**Two variants.** Arm A = <installed file> verbatim (md5 `<hash>`, confirmed
identical at copy time; the installed skill was never edited). Arm B = the same
file plus **one** change: <describe>. Total diff: <+N/-M lines>.

**Identical task**, `task_prompt.md`. <State what the prompt does and does not
name. If it names anything near the answer, say so here AND in threats.>

**Isolation.** <table of flags and why each matters>

Jobs were **interleaved** (a1, b1, a2, b2, …) at concurrency <c>, so any drift
in service conditions hits both arms alike.

**Model/budget.** `<model>`, `--max-budget-usd <X>` per run. **Measured, not
guessed:** <k> probe runs cost `$<min>`–`$<max>` over <turns> turns / `$<first>`
for the first turn alone. The cap is 3–4x the *worst* probe, floored at the
priciest run this rig has produced (`$<prior>`), applied identically to both
arms before any run started — a cap set from one short probe would have been
`$<naive>`, and at that cap <k>/<2n> runs would have truncated into
`error_max_budget_usd` and scored INCONCLUSIVE, buying zero data in both arms.

**Batch integrity.** <One `run` invocation, or resumed: state how many runs came
from each wall-clock window, and confirm no INCONCLUSIVE run was re-run.>

## 3. Contamination checks (all pre-spend)

| Check | Result |
|---|---|
| `git grep <bug symbol> <pin>` | present at … — the bug instance **is** in the tree |
| `grep -rniE "<answer phrasing>"` over `*.py`/`*.md`/tests/comments | <n> hits |
| `docs/plans/` listing | <what's there, whether any records the finding> |
| `git merge-base --is-ancestor <later sha> <pin>` | <NO = descendant, good> |
| repo `CLAUDE.md` / `.claude/` at the pin | <none / contents> |
| `git status` in each worktree, post-run | clean |

**Verdict: <CLEAN / DIRTY>.** <If dirty, the experiment is void — say so and
stop; do not report numbers from a contaminated tree.>

## 4. Pre-registered grading

`PREREGISTRATION.md`, frozen by `runs/prereg.lock.json` at <timestamp> — before
the first run. Not edited since.

- **PRIMARY (binary):** <criterion, with the explicit non-qualifiers>
- **SECONDARY:** <…>
- **TERTIARY:** <…>

**Blinding.** <How. What the grader could and could not see.>

## 5. Raw per-run results

<2n>/<2n> runs completed; **<k> INCONCLUSIVE** (excluded, not retried).

| anon | run | arm | PRIMARY | keyword scan | SECONDARY | TERTIARY | $ | turns |
|---|---|---|---|---|---|---|---|---|

### Counts

| Measure | Arm A (current) | Arm B (+change) |
|---|---|---|
| **PRIMARY** | **<x>/<n>** | **<y>/<n>** |
| SECONDARY | | |
| TERTIARY | | |

<test>, two-sided, on the primary 2×2: **p = <p>**. Descriptive statistic only.

## 6. Grader reliability

Grader/keyword-scan disagreed on <k> of <2n>: <each one, and which is right>.

I read <k> transcripts myself before seeing any grade (<tags>, chosen by tag,
judged against the same criteria). My reads: <…>. **Grader agreement with my
independent reading: <k>/<k>.**

## 7. Spend

| Item | $ |
|---|---|
| <2n> experiment runs (A $<a> / B $<b>) | |
| <2n> blind gradings | |
| probe run | |
| **Total** | |

Per-run range $<lo>–$<hi>, mean $<mean>. Arm costs within $<d> of each other, so
neither arm was advantaged by compute.

## 8. Verdict

<Plain statement. If null, say null — a null is a valid result. Do not spin a
one-run difference into a trend.>

<Observations worth more than the headline, each labelled underpowered and
not-pre-registered where that applies. In particular: did the intervention move
the behaviour it targeted, or only redirect behaviour that was already
happening? Did any secondary measure move the OTHER way?>

**Recommendation: <adopt / don't adopt / test at n=X>.**

## 9. Threats to validity

- **Underpowered.** n=<N>/arm. Smallest detectable effect ≈ <X>pp.
- **Designer bias — the same person proposed the intervention, wrote both
  variants, wrote the task prompt, and designed the rubric.** No independent
  pre-registration review.
- **Blind to labels, not to fingerprints.** <How the intervention might shape
  output in ways a grader could notice; what bounds it.>
- **Task shape ≠ the real event.** <What the rig cannot observe.>
- **One finding, one repo, one model.**
- **Prompt leakage.** <Does the task prompt point at the answer? Which direction
  does that push both arms?>
- <Any deviation from the pre-registered plan, and why.>

## 10. Reproducing

```
python3 ab_harness.py contamination -c config.json
python3 ab_harness.py worktrees     -c config.json
python3 ab_harness.py probe         -c config.json   # then set max_budget_usd
python3 ab_harness.py run           -c config.json
python3 ab_harness.py grade         -c config.json
```

Committed: full transcripts (`runs/*.jsonl`), graded final messages
(`runs/*.final.md`), per-run index with costs (`runs/index.json`), the blinded
copies the grader saw (`grading/anon/`), grades (`grading/grades.json`), and the
anon-to-arm mapping (`grading/mapping.json`).
