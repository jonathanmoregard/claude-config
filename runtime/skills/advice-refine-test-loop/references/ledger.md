# Review ledger

One file per repository: `<notes>/review-ledger.tsv`. Create it with the
header row on first use. Tab-separated, one row per line, no tabs or newlines
inside a field, `-` for an empty field.

| # | Column | Values |
|---|---|---|
| 1 | date | `YYYY-MM-DD` |
| 2 | source | `artl` or `sweep` |
| 3 | run | `artl`: `<token>/r<N>`, always. The token is picked once, at the Entry Gate: the start commit and the time, `<short commit>-<HHMM>`, as in `a1b2c3d-1432/r2`. N counts this close-out's rounds from 1. A re-invoked close-out, or a restart after a reframe, picks a new token. `sweep`: short commit, then lens, as in `a1b2c3d/races` |
| 4 | check | `A`, a class id (`D3`, `G12`), `variant` for a finding raised by a sibling line, `reframe` for a rabbit-hole thread (`rabbit-hole.md`), or a lens name |
| 5 | trigger | what made the check run: the part of the diff that met its `When`; `always` for A; for a sweep, why it was run |
| 6 | where | the finding's schema `file` and `line` verbatim, `/abs/path:<int>` (0 = whole file); `-` on a `none` row |
| 7 | claim | one sentence |
| 8 | outcome | `reproduced`, `refuted`, `unconfirmed`, `advisory`, `none` |
| 9 | fixed | `yes`, `no`, `deferred`, `open`, `-` |
| 10 | fp_reason | required when `refuted`, else `-` |

Rules:

1. One row per finding. A check that ran and reported nothing gets one row
   with outcome `none`: without it a check that never finds anything cannot
   be told from one that never ran.
2. `advisory` is for design findings and sweep `ADVISE` items. Their `fixed`
   column records whether the user took the advice.
3. For a sweep item, `reproduced` means the cited lines show what the item
   claims.
4. `fixed` is the only column edited after the row is written (`open` →
   `yes`, `no` or `deferred`). One exception: a row that
   `convergence-check.mts` names on exit 11 or 12 is corrected in place, in
   whichever column it names, before the rerun (`rabbit-hole.md` 1.3).
5. Write `fp_reason` in a few plain words, and reuse an earlier reason's
   exact wording when it fits, so reasons can be counted.
6. A review finding's `where` is the `file:line` its reviewer reported, never
   paraphrased. `scripts/convergence-check.mts` checks every column of this
   close-out's rows against the table above: exit 12 when only `where`
   departs from it, 11 for anything else. Sweep rows are not read by it.
7. `reframe` rows, one per thread of a diagnosis: outcome `reproduced` for
   `wrong_shape` or `intractable_as_framed`, `none` for `tractable`, in a
   mixed diagnosis too; `fixed` is `-` on a `none` row.
8. A finding that repeats one already in the ledger, open or adjudicated,
   gets no new row: the earlier row stands and keeps its outcome (`SKILL.md`,
   Rounds, step 4). Rows are never matched or merged afterwards.

## Reading it

Tally before any change to the hunt classes or lenses, and when the user
asks. Across every repository:

```bash
~/.claude/skills/advice-refine-test-loop/scripts/ledger-tally.sh [check...]
```

It prints `check runs findings reproduced fixed refuted advice_taken propose`
per check (named checks only, when given), then one `exclude?` line per
`fp_reason` that meets rule 3 below. `propose` applies rules 1 and 2.
`reframe` rows are skipped: a rabbit-hole diagnosis is not a reviewer check,
so there is nothing to drop or reword.

Then propose, to the user, never on your own:

1. **Drop** a class or lens that ran five or more times with nothing
   reproduced and no advice taken.
2. **Reword or drop** a class or lens with six or more findings of which half
   or more were refuted.
3. **Exclude** a category when the same `fp_reason` appears on three or more
   rows across two or more runs: one entry in `exclusions.md`.

Before putting an `exclude?` line to the user, read
`exclusion-decisions.tsv`: the categories the user already accepted or
rejected. Drop, without asking, every candidate that means the same as a
listed category, however it is worded; the tally counts exact strings and
cannot tell. Every answer to rule 3, yes or no, gets a row there
(`fp_reason`, `accepted` or `rejected`, date) in the same change.

`A` cannot be dropped; it is reworded. A change the user accepts goes through
the normal change process for these files, tally included.
