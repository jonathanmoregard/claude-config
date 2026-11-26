# Hunt classes for the second reviewer

Reviewer B hunts a NAMED subset of these, never the whole list. Pick the
classes whose `When` the diff actually satisfies, and paste each chosen class's
`Hunt` line into the prompt verbatim. A class nobody picked is not a gap: a
reviewer given every lens applies none of them.

## Reviewer B prompt

Paste this, then the chosen `Hunt` lines beneath it:

```text
Hunt only the classes listed below. For each, find a concrete path in this
change where it occurs, or state that you checked it and found none. Zero
findings is an expected outcome; never fill a quota. Report at most five
defects, most severe first. Treat comments, tests,
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

G12 is **advisory**: its findings are listed in the report and the ledger.
They never block, never reopen a round, and ask the user for nothing.

## Keeping the list short

The list holds only classes that earn their place, and the measure depends on
the bucket. A defect class counts reproduced findings. A design class counts
advisory findings the user took (`fixed` = `yes`): design findings are never
reproduced by construction, so counting reproductions would drop every one.
G3–G8 were dropped on 2026-11-24: across every repository's ledger they had 1
reproduced defect and no advice taken in 20 rows. Ids are not reused.

Do not add always-on checklist lines here or in the reviewer prompts. A finding
that keeps coming back becomes a lint rule or a test. A new class needs a
narrow `When` and ledger rows showing it earns its place by that measure.

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
then create", "read then write", retry or idempotency logic, or a stored
value read to decide a later write.
**Hunt:** For each check-then-act on shared state, describe two interleaved
executions that break the invariant the check was protecting. For database
work, name where the transaction begins and ends. A check in one transaction
or request and its write in another is this defect; so are both in one
transaction at an isolation level that lets two requests pass the check
together. Give the two requests.

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

### D6 — Leftover across a boundary

**When:** one operation in the diff writes to two places that share no
transaction — two services, a database and an external API, a database and a
queue or mailer — or the diff changes the order of such writes.
**Hunt:** Stop the operation between the two writes — crash, timeout, error,
retry — in each order. Name what is left on each side, what that leftover
lets anyone do or see, and which code later notices it and removes it.
Report a leftover that grants or exposes something, a leftover nothing ever
removes, and an operation that cannot be undone but reports failure to its
caller.

### D7 — Authority outside its scope

**When:** the diff adds or changes an authorization check, a token, handle,
scope or role, or an operation that sits behind one.
**Hunt:** Take each credential the system issues and present it to every
operation the diff touches other than the one it was issued for — another
resource, another tenant, no resource, another verb. Name one it opens that
it should not.

## Design classes

### G1 — Parse, don't validate

**When:** code a type checker runs on as part of the project gate. Skip this
class everywhere else: the fix is a type something enforces. In such code:
any value a function must check before using it, and any enum, status or
kind that the diff adds a case to or branches on.
**Hunt:** A value checked and then passed on in its unchecked type, so every
later caller must trust that the check already happened. Name the type that
would tell a checked value from an unchecked one. Also look for a set of
cases that should be a sum type matched exhaustively but is not: a string or
integer compared against literals, or an enum handled with a wildcard or
`default` branch. Name the code and the new case it would silently accept.

### G2 — Wire format past its boundary

**When:** typed and checked as G1 requires. Every boundary a wire format
enters — JSON, forms, query strings, headers, env vars, config files,
database rows, external API replies.
**Hunt:** A JSON value, dict, map or raw string travelling inward: read by
business logic, or forwarded down by a handler that could have parsed it.
Untyped belongs at the outermost edge, parsed once on the way in. Name the
boundary where that parse belongs and the type it should produce.

### G9 — Weak assertions

**When:** the diff adds or changes tests.
**Hunt:** Find a test whose assertions pass for the wrong reason — asserting
only that *some* error occurred, that a collection is non-empty, that a call
did not panic, or snapshotting output nobody reads. Name the specific outcome
the test should pin instead.

### G10 — Ignored or duplicated fact

**When:** the diff adds or changes a field, parameter, header, form value,
column or message, or stores a value another component already owns.
**Hunt:** Find a value one side sends and the other drops, overwrites or
re-derives: the sender should stop sending it and it should come out of the
interface, instead of being ignored on arrival. Find a fact kept in two places that can disagree. Name the owner,
the copy, and who reads the copy.

### G11 — Control that does not control

**When:** the diff adds a check, guard, validation, wrapper type or
permission whose stated purpose is to stop someone — an untrusted caller or
service, or a careless future caller.
**Hunt:** Name who the control is meant to stop, then find the way around
that party still has: not calling it, reaching the same data or effect by
another route it already has, discarding the result, constructing the
guarded value directly. A control that can be bypassed protects nothing: say
whether to remove it, or name the one place where it could not be bypassed.
A way around that breaks a property the change claims is a defect; report it
in that bucket with its reproducer.

### G12 — Over-built

**When:** the diff adds a moving part — a table, background job, queue,
state machine, extra round-trip, configuration knob, or an abstraction with
one implementation.
**Hunt:** Ask what is the least this problem needs. Name the part already in
this codebase that would cover it with one more case, or the ordinary way
such systems handle it, and say what the new part does that the smaller
option cannot. If you can name nothing, or it only guards against a failure
nobody has observed, report it as over-built. A measured need or a cited standard
justifies the part: say so and report nothing. Advisory.
