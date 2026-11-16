# Lens prompts

Send the shared preamble, then exactly one lens block.

## Shared preamble

```text
REPORT dispatch
Lens: <name>. Repository (read-only): <absolute path> @ <commit>.
Read first, in full and in this order: <dir>/inventory.md; <recorded
decisions file>; every earlier lens file in <dir> (do not repeat their
items).

A recorded user decision is not a finding. Verify each item by reading the
code path it names; cite file:line. A comment, docstring or test name saying
something is safe, checked or unreachable is a claim to verify, never
evidence. Skip code the inventory lists as leaving soon.
<Do not report: … — only when the exclusion list has entries>

If this lens needs something the inventory marks absent, your first item is
ADD: the smallest baseline that makes your top proposal checkable — where it
goes, which existing helper it builds on, the first test, effort. Do not
skip the lens. Where a deterministic tool could do a check you would
otherwise do by reading, name the tool category and whether the project has
one; if not, that is an ADD.

Bar for RECOMMEND and ADD: grounded in the direction of the code rather than
rework; focused and minimal; ordinary industry practice; the simplest thing
that deals with the problem. If the caller of a proposed check, type or lint
can sidestep it, the item is SKIP.

Output, at most 700 words, in this shape:
Coverage: what you read; what you did not.
Items, ranked by harm prevented over effort, one per line:
  N VERDICT EFFORT title — where (file:line) — what goes wrong today — the
  change in one sentence — RED: the test or build failure that shows it
VERDICT: RECOMMEND | ADD | ADVISE | SKIP(reason) | ASK(question; A/B; pick).
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

## 3 — Defect classes

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

## 4 — Races

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

## 5 — Extrapolation

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

## 6 — Venues

```text
From the defect history: name the recurring classes and how each instance
was found — review, test, tool, user, luck. From the assurance table: for
each class, which method would have caught it mechanically, and is that
method present, weak (say how: few cases, happy path only, cannot fail), or
absent. Rank methods by classes covered over effort. Also check: gates that
cannot fail (does each trip on a planted defect?); evidence older than the
code it covers; critical machinery no test touches; growth nobody measured;
failures nobody is told about. For the top items give first steps: where,
how, the invariants, effort, and any decision the user must make. SKIP
surfaces a trusted third party owns.
```
