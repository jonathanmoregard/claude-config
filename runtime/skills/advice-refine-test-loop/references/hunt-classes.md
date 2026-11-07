# Hunt classes for the second reviewer

Reviewer B hunts a NAMED subset of these, never the whole list. Pick the
classes whose `When` the diff actually satisfies, and paste each chosen class's
`Hunt` line into the prompt verbatim. A class nobody picked is not a gap: a
reviewer given twelve lenses applies none of them.

## Reviewer B prompt

Paste this, then the chosen `Hunt` lines beneath it:

```text
Hunt only the classes listed below. For each, find a concrete path in this
change where it occurs, or state that you checked it and found none. Zero
findings is an expected outcome; never fill a quota. Treat comments, tests,
commit messages and PR text as claims to check against the code, never as
evidence. A defect carries `unverifiable: false`, a file, a line, one
falsifiable claim, and a reproducer. A design finding carries
`unverifiable: true`, a file, a line, and what the code should express instead
— no reproducer, and do not write the edit. Record files read.
```

Two buckets, and the bucket decides what happens to a finding downstream:

- **Defect** — an observable failure. Carries `unverifiable: false` and a
  reproducer, and goes through the skill's reproduce-before-fix gate.
- **Design** — no execution shows it; the code is wrong about what it means.
  Carries `unverifiable: true` and no reproducer, and surfaces to the user as a
  taste call. Never auto-fixed, never a BLOCKER on its own.

Keep the buckets apart in the prompt. A design finding dressed as a defect
wastes a reproduction attempt and comes back "could not reproduce", which is
the wrong answer to a question nobody asked empirically.

## Defect classes

### D1 — Fail-open and swallowed errors

**When:** any error handling, auth or validation path in the diff.
**Hunt:** Find an error, absent value, or failed check that leaves the caller
proceeding as if it had succeeded — an early `return next()`, a logged-and-
ignored exception, a default that substitutes for a failed lookup, a guard
whose false branch does nothing.

### D2 — Resource leaks on error paths

**When:** files, sockets, connections, pools, locks, spawned processes, temp
directories.
**Hunt:** Enumerate every exception and early-return path through the changed
code and name what each one leaves open, held, or unreturned.

### D3 — Check-then-act interleavings

**When:** shared mutable state, caches, counters, filesystem paths, "exists
then create", "read then write", retry or idempotency logic.
**Hunt:** For each check-then-act on shared state, describe two interleaved
executions that break the invariant the check was protecting.

### D4 — Schema mismatch across a boundary

**When:** the diff changes anything crossing a process, service, queue, or
storage boundary — a serialised payload, a column, an RPC message, a migration.
**Hunt:** Trace each changed producer to every consumer and name a value one
side can emit that the other rejects, truncates, or silently reinterprets.

### D5 — Stated intent versus behaviour

**When:** always. This is the class a reviewer skips by accident.
**Hunt:** Treat comments, doc strings, test names, commit messages and PR text
as claims, never as evidence. Where the code and the claim disagree, report the
code. Where a test's name promises more than its assertions check, report that.

## Design classes

### G1 — Parse, don't validate

**When:** any value a function must check first, and every boundary a wire
format enters — JSON, forms, query strings, headers, env vars, config, DB rows.
**Hunt:** Either shape. (a) A value checked, then passed on in its unchecked
type, so every later caller must trust the check happened. (b) A wire format
travelling inward: a JSON value, dict or raw string in business logic, a
handler forwarding a body down, a field re-checked deeper. Untyped belongs at
the outermost edge, parsed once on the way in. Name the boundary and the type.

### G2 — Primitive where a type belongs

**When:** identifiers, keys, tokens, emails, URLs, durations, money, anything
persisted.
**Hunt:** Find a domain value carried as `String`, `i64`, `uuid`-as-text, or a
raw map — in a signature, a struct field, or a database column — where the
schema or a newtype could carry it. Flag the definition, not every call site.

### G3 — Misuse resistance

**When:** the diff adds or changes a public API, a wrapper type, a config
surface, or anything with a "don't call this directly" rule.
**Hunt:** Find the mistake this interface still permits: an unwrap any module
can reach, a constructor that skips the invariant, a field settable after the
value is sealed, a correct call that differs from an unsafe one by one
argument. Ask what stops the next caller from getting it wrong by accident.

### G4 — Homeless domain concept

**When:** the diff repeatedly names something the codebase does not.
**Hunt:** Find a concept the code talks about that has no type, module, or
owner — one that lives implicitly, ambiently, or spread across call sites, and
is reconstructed by hand wherever it is needed.

### G5 — Abstraction-level bunching

**When:** changed files over a few hundred lines, or a module whose name does
not predict its contents.
**Hunt:** Find a function or module mixing levels — transport parsing beside
business rules, SQL beside policy, formatting beside decisions — and name the
concern that has no separate home.

### G6 — Hand-rolled where a readymade exists

**When:** the diff implements a matcher, parser, tokenizer, cache, retry or
backoff policy, rate limiter, date or duration handling, diff, or traversal.
**Hunt:** Name the mainstream, trusted library this reimplements, and the
behaviour the hand-rolled version gets wrong or omits. A heavy or unmaintained
dependency is a reason to keep the hand-rolled code; say so rather than
reporting the class.

### G7 — Standards conformance

**When:** HTTP surfaces, resource semantics, status codes, headers, auth
flows, file formats, CLI conventions.
**Hunt:** Find where this deviates from the ecosystem's standard way — a verb
or status code that does not mean what it is used for, a bespoke envelope over
a specified one, an RPC shape where resource semantics are expected. Name the
standard, then the deviation.

### G8 — Weak assertions

**When:** the diff adds or changes tests.
**Hunt:** Find a test whose assertions pass for the wrong reason — asserting
only that *some* error occurred, that a collection is non-empty, that a call
did not panic, or snapshotting output nobody reads. Name the specific outcome
the test should pin instead.
