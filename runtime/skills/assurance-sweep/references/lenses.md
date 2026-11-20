# Lens prompts

Send the shared preamble, then exactly one lens block.

## Shared preamble

```text
REPORT dispatch
Lens: <name>. Repository (read-only): <absolute path> @ <commit>.
Tier: <production | personal, from the inventory>. On a personal project
propose no production machinery — observability, alerting, restore
rehearsals, leak checks, growth measurement — mark those methods n/a, and
judge "would break" by the owner's own use, not by production load. Neat
types, clear structure and working tests still apply in full.
Read first, in full and in this order: <dir>/inventory.md; <recorded
decisions file>; every earlier lens file in <dir> (do not repeat their
items).

A recorded user decision is not a finding. Verify each item by reading the
code path it names; cite file:line. A comment, docstring or test name saying
something is safe, checked or unreachable is a claim to verify, never
evidence. Skip code the inventory lists as leaving soon.
<Do not report: … — only when the exclusion list has entries>

Where a deterministic tool could do a check you would otherwise do by
reading, and the project has no such tool, do not propose adding one: name
the missing tool category under "Not covered". A lens block that names its
own guard overrides this for that guard only.

Bar for RECOMMEND: builds on how the code already works instead of
reworking it; focused and minimal; ordinary industry practice; the simplest thing
that deals with the problem. If the caller of a proposed check, type or lint
can sidestep it, the item is SKIP. A type only a parser can build, with a
guard that rejects casts and untyped values flowing into it elsewhere, is
not sidestepped.

Output, at most 700 words unless the lens block sets its own cap, in this shape:
Coverage: what you read; what you did not.
Items, ranked by harm prevented over effort, one per line:
  N VERDICT EFFORT title — where (file:line) — what goes wrong today — the
  change in one sentence — RED: the test or build failure that shows it
VERDICT: RECOMMEND | ADVISE | SKIP(reason) | ASK(question; A/B; pick).
EFFORT: S under 1 h, M 1–4 h, L over 4 h of agent time.
Last item, if you saw one: one thing outside this lens that would break in
production.
Not covered: areas this pass did not reach.
```

## 1 — Duplicates

```text
From the inventory's mechanism list, and any mechanism you find it missed,
find each job this codebase does in more than one place: two implementations
of one mechanism, one fact of record stored twice, or a hand-written version
of something a dependency already in use provides. Look in scripts, test
helpers, client code and deployment config as well as the main source.
For each, answer: what happens when the copies diverge? Diverging with harm:
RECOMMEND one home. Identical but redundant: RECOMMEND only at effort S.
Separate on purpose — a different trust domain, a recorded decision: SKIP
with the reason.
```

## 2 — Norms

```text
For each mechanism in the inventory, ask whether this is how such systems
ordinarily solve the problem. Report mechanisms that are unusual, heavier
than the ordinary way, or that will not hold at ten times the load or with a
second instance running. For each: name the ordinary way, what adopting it
removes, and what it costs. Prefer extending a pattern already in this
codebase over importing a new one. Every item is ADVISE unless it has a
concrete wrong outcome today. Mark an item CONFIRM when your claim about
ordinary practice comes from memory. SKIP where a recorded decision chose
the unusual way.
```

## 3 — Domain model

```text
Read the core: the code that decides what the application does, as opposed
to the adapters that talk to storage, networks, devices, UIs and other
systems. The inventory says which is which; with no such split, say so and
treat the whole as core. Report every site, with per-file counts where
there are many, in these categories:
1. A value whose set is closed carried as a string, integer or boolean. It
   should be a sum type whose labels say what each value means — not 0..4,
   not 'a' | 'b' codes — matched exhaustively.
2. A bounded number carried as a plain number. It should be a range type
   only a parser can build.
3. An identifier carried as a plain string. Each kind gets its own id type,
   so two kinds cannot be mixed.
4. A sentinel for "none" or "unknown" (-1, '', 0, an invented default). It
   should be an explicit case.
5. A check, default, cast or non-null assertion inside the core on a value
   an edge parser should already have settled. Parse once at the edge.
6. Adapter vocabulary in the core: table, topic, entity, service or endpoint
   names; wire documents passed through unread; lists of actions or calls
   for something else to perform. The core holds state, pure updates (an
   event and the state give the new state) and derivations from state; the
   adapter reads the world in, saves what changed, and makes the world match.
7. Names that describe mechanism (plan, args, parts, data, info, manager,
   handle) where a domain word exists. Take domain words from the user's own
   words in the recorded decisions and conversation records the inventory
   names. Return a vocabulary table, each term with where the user used it;
   mark a term with no such source.
For each category give the target type as a short sketch, where its parser
belongs, and one guard that keeps it from coming back: a lint or
compiler-API check over the core's exported signatures, with an allowlist
whose every entry needs a reason. A genuinely open value — text shown to a
person, real-number arithmetic — is SKIP with that reason. Types narrow what
the code accepts; they add no runtime machinery, and wire formats stay as
they are.

The guard needs no new tool: it is one test in the existing suite that uses
the project's own type checker to read the core's exported signatures, and
it also rejects, outside their parsers, casts to the new types and `any` or
`unknown` values flowing into them. That is why a type it protects is not
SKIP for being castable. Recommend the guard as its
own item.

Output: one item per category, not per site — per-file counts and at most
three representative file:line sites — plus one item for the vocabulary
table and one for the guard. Every item carries a VERDICT. At most 1200
words.
```

## 4 — Defect classes

```text
From the defect history, group defects into classes: the same root shape at
least twice, or once with severe harm. For each class, first hunt siblings:
state the shape in one sentence and find every site in the code that still
has it. Each sibling is a RECOMMEND with its own file:line.
Then ask what would rule the class out. Climb this ladder and stop at the
first rung that holds:
1. structure — co-locate, make private, delete the second path;
2. a type or schema constraint that makes the wrong program fail to build;
3. a deterministic tool or lint;
4. a property or model test;
5. nothing — say so.
Rung 2 counts only if the wrong call no longer builds. If the caller can
ignore, discard, copy or construct the value freely, it is ceremony: SKIP.
In code no checker runs on, start at rung 3. Give the shape of the change,
how many signatures and call sites it touches, and the failing build or test
that is the RED.
```

## 5 — Races

```text
List the operations that change shared state and the invariant each must
keep. For every pair that can overlap in time on the same entity, including
pairs in different stores or services, find the test that runs both at once
and asserts the invariant. For each operation, note where its transaction
begins and ends: a check in one transaction or request and its write in
another, or an isolation level that lets two requests pass the same check,
is an unprotected pair even when each request is correct alone.
Rank the pairs with no such test by harm. For each RECOMMEND state the
invariant in one sentence and the post-condition to assert: each side
succeeds or returns its one named refusal, never a raw storage error. SKIP
pairs serialised by a lock you can cite, and three-way combinations unless
no pairwise test can express the bug.
```

## 6 — Extrapolation

```text
Find existing tests that would cover materially more for a small edit: a
cluster of near-identical examples that is one property with a generator; a
table missing states the code handles (expired, already used, absent); a
model or scenario test missing operations; a check that plants only the
happy path; a parametrised test over a hand-picked subset of a closed set.
Each item names the existing test (file:line), the edit, and the bug class
it would then catch. New test suites belong to the Venues lens: SKIP them
here.
```

## 7 — Venues

```text
From the defect history: name the recurring classes and how each instance
was found — review, test, tool, user, luck. From the assurance table: for
each class, which method would have caught it mechanically, and is that
method present, weak (say how: few cases, happy path only, cannot fail), or
absent. Rank methods by classes covered over effort. Also check: gates that
cannot fail (does each trip on a planted defect?); evidence older than the
code it covers; critical machinery no test touches; and, on a production
project only, growth nobody measured and failures nobody is told about; on a personal
project mark observability, alerting, restore rehearsals, leak checks and
growth measurement n/a and propose none of them. For the top items give first steps: where,
how, the invariants, effort, and any decision the user must make. SKIP
surfaces a trusted third party owns.
```
