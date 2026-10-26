---
name: integrating-feedback
description: Use when processing a long or multi-item batch of user feedback — a UX/design review, a PR review, a list of change requests, a voice-memo walkthrough, "fix all these", numbered per-item comments — anytime items could be dropped, half-fixed, or where the user says "you didn't fix X I already asked for". Also use before reporting a batch of fixes "done".
---

# Integrating feedback without dropping items

Items get dropped for **structural, not motivational** reasons: compound items hide
sub-asks, prose triage gives every item equal weight, self-reports over-trust "done",
and omissions cluster at the *end* of long lists. Willpower doesn't fix this — a
ledger + a verification gate does. (Provenance: aviation/surgical omission research +
requirements-traceability + "close the loop" UX practice; a real drop — a two-line
item where only one line was removed and it was reported done — motivated this.)

## The rule
**Build a ledger BEFORE any work; no item closes until it's VERIFIED against the
artifact (not the plan); a final full-ledger pass in random order is mandatory before
reporting a batch done.**

## Intake → ledger (do this first, before touching code)
Parse the feedback into one row per **atomic** ask. Persist as a file (survives context
resets). Columns:

| field | notes |
|---|---|
| `id` | stable `FB-001`, children `FB-001a/b`; never reuse/reorder |
| `raw` | verbatim quote — don't paraphrase at intake |
| `type` | `must-fix / suggestion / question / discussion / nit / praise` |
| `severity` | `blocker / major / minor / info` |
| `area` | file/component (enables clustering) |
| `acceptance` | testable pass-criteria (rewrite "faster"→"<2s p95"); required before work |
| `verify` | `screenshot / test / repro / n-a-question` |
| `evidence` | screenshot pair / test id / commit — filled at close |
| `status` | `open / in-progress / done / deferred / declined / question-open` |
| `you_said_we_did` | one line keyed to the user's words; required to close |
| `round` | increments per review round (catches whack-a-mole) |

## Hard rules
- **Atomize.** Any item with "and", multiple verbs, or ≥2 pass-signals → split into
  children; work each child independently. Compound items are the #1 drop cause.
- **Never implement `question` / `discussion` items** — surface them for the user to
  decide. Route back, don't guess.
- **Traceability.** Each commit references the `FB-id`(s). Emit `unaddressed = ledger −
  referenced` before finishing.
- **Verify against the artifact, not the plan.** For UI: regenerate the screenshot and
  confirm the change is actually visible / the removed text is actually absent. A
  partial fix (one of two lines) is a FAIL, not a done.
- **Cluster gate.** ≥4 items in one area, or an item re-appearing in a later `round` →
  one cluster brief, not N patches; and flag the recurring pattern.
- **Deferrals carry a `resume_when` trigger** — never "I'll get to it."

## Final verification gate (before saying "done")
1. Re-read the WHOLE ledger in **random order**, asking "what would this look like if it
   were still broken?" — not "is it done?" (fixed-order breeds rubber-stamping).
2. Every row is `done` (with evidence) / `deferred` (with resume_when) / `declined` /
   `question-open`. No `open` rows.
3. Emit a **coverage line** (`addressed/total`) and a **Gaps** section naming every
   unclosed id with a reason. No free-form summary substitutes.
4. Report back as a **per-id "you-said-we-did" table**, keyed to the user's words.

## Red flags — you're about to drop something
- Reporting a fix without having looked at the regenerated artifact.
- A summary paragraph instead of a per-id table.
- An item with "and" that you fixed "mostly".
- "I'll circle back" with no `resume_when`.
- The user says "again" / "I already told you" → a prior item was closed unverified;
  find it, and audit the whole prior round for the same failure.

Full research: see the project's feedback-capture research report if one was saved.
