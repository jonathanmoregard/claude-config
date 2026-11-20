# Inventory

Write `<notes>/assurance-sweep/<date>/inventory.md` in this shape.

```markdown
# Inventory — <repo> @ <commit>, <date>

Tier: production | personal. Core: <dirs>. Adapters: <dirs>.
User's words: <recorded decisions file, README, conversation records the
sweep may read for domain vocabulary>.

## 1. Mechanisms
| # | Job | Homes (file:line) | Relied on by |

## 2. Assurance
| # | Method | State (present / weak / absent / n/a) | Where, and the command |

## 3. Defect history
| # | Defect | Where | How found | Fixed in |

## 4. Leaving soon
| # | Code | Why, and when |
```

## Finding the mechanisms

A mechanism is any job done at more than one call site that the application
would break without. Find them from the application, in this order:

1. The dependency manifests. Each dependency names a job. Note every place
   that does that job without the dependency.
2. Module and directory names, then the public items of each.
3. A search for recurring verbs: retry, backoff, timeout, lock, lease, pool,
   cache, clock, redact, parse, migrate, spawn, schedule, clean up.
4. Scripts, test helpers, client code and deployment config. Second
   implementations often live outside the main source.
5. Facts of record: for each stored id, status, key or address, every table,
   column, cache or message that carries it.

Stop at about thirty rows. Anything with one home and one caller is not
listed.

## Assurance methods, and the baseline when absent

Fill one row per method. Mark `n/a` where the repository has nothing for the
method to act on (no migrations, no second store), and for production-only
methods on a personal project. Name the project's own
tool where one exists; the table names categories only.

| # | Method | Minimal baseline | Effort |
|---|---|---|---|
| 1 | Static checks in the gate: type checker, linter, exhaustiveness | the stack's standard checker switched on in the project gate | S–M |
| 2 | Dependency auditor | one audit command in the gate: advisories, licences, unknown packages | S |
| 3 | Migration linter | one lint run over the migration directory | S |
| 4 | Property tests | the stack's standard library as a development dependency, and one property replacing the largest cluster of example tests | S–M |
| 5 | Stateful or model test | one model test over the most stateful component: a list of operations, a small in-memory model, compare after each step | M–L |
| 6 | Race tests | one helper that starts two operations together, repeats, and fails on a hang; the suite fails on lock or serialisation errors from the store | M |
| 7 | Fault injection between two writes | a test double on the seam between the two stores that fails before or after the call | M |
| 8 | Must-not-build tests | one test asserting that a forbidden use fails to build or type-check, run in the normal suite | S |
| 9 | Fuzzing | one fuzz target for the parser taking the least-trusted input, seeded from existing test inputs, time-boxed | M |
| 10 | Mutation testing | a manual run scoped to one critical module or to a diff; survivors become tests | M |
| 11 | Gate self-test | each gate lane trips on one planted defect | S per lane |
| 12 | Leak check (production only) | distinctive values planted through real flows in an end-to-end run, then searched for in logs and dumps | M |
| 13 | Growth measurement (production only) | row, memory and handle counts before and after one load run | S |
| 14 | Alerting (production only) | failure of each long-running unit reaches a person | S–M |
| 15 | Restore rehearsal (production only) | one scripted restore of the newest backup into a scratch instance | M–L |
| 16 | Review ledger | `<notes>/review-ledger.tsv` with its header row | S |

A baseline that needs a new dependency, a gate change or a paid service
says so where it is named.

No ledger and no recorded defects: fill section 3 from fix and revert
commits and mark row 16 absent.
