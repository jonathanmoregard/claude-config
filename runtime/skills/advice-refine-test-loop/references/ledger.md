# Review ledger

One file per repository: `<notes>/review-ledger.tsv`. Create it with the
header row on first use. Tab-separated, one row per line, no tabs or newlines
inside a field, `-` for an empty field.

| # | Column | Values |
|---|---|---|
| 1 | date | `YYYY-MM-DD` |
| 2 | source | `artl` or `sweep` |
| 3 | run | short commit, then round or lens: `a1b2c3d/r2`, `a1b2c3d/races` |
| 4 | check | `A`, `variant`, a class id (`D3`, `G12`), or a lens name |
| 5 | trigger | what made the check run: the part of the diff that met its `When`; `always` for A; for a sweep, why it was run |
| 6 | where | `file:line`; `-` on a `none` row |
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
   `yes`, `no` or `deferred`).
5. Write `fp_reason` in a few plain words, and reuse an earlier reason's
   exact wording when it fits, so reasons can be counted.

## Reading it

Tally before any change to the hunt classes or lenses, and when the user
asks. Across every repository:

```bash
~/.claude/skills/advice-refine-test-loop/scripts/ledger-tally.sh [check...]
```

It prints `check runs findings reproduced fixed refuted advice_taken propose`
per check (named checks only, when given), then one `exclude?` line per
`fp_reason` that meets rule 3 below. `propose` applies rules 1 and 2.

Then propose, to the user, never on your own:

1. **Drop** a class or lens that ran five or more times with nothing
   reproduced and no advice taken.
2. **Reword or drop** a class or lens with six or more findings of which half
   or more were refuted.
3. **Exclude** a category when the same `fp_reason` appears on three or more
   rows across two or more runs: one entry in `exclusions.md`.

`A` cannot be dropped; it is reworded. A change the user accepts goes through
the normal change process for these files, tally included.
