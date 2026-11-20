---
name: assurance-sweep
description: Use when a whole repository or long-lived branch needs a robustness pass beyond diff review — before a release or cutover, after a large merge or rewrite, when the same kind of defect keeps coming back, or on asks like "what more can we do to make this safe", "is anything implemented twice", "is the race coverage complete", "can this class of bug be ruled out", "can existing tests cover more", "is this how it's normally done", "is the domain typed properly", "too many strings and integers", "parse don't validate across the repo", "does the core speak the domain". Not for close-out review of one diff (advice-refine-test-loop).
---

# Assurance Sweep

Seven whole-repository lenses, one at a time, each read by one strongest-tier
read-only reviewer. A lens returns ranked proposals; you check them and hand
them to the user. The sweep only proposes; it edits no source. Building what the
user picks is ordinary work under the project's usual flow, and no part of
this skill.

Diff review asks whether a change is right. A sweep asks what the codebase
still lets through, and what the cheapest thing is that would stop it.

## Entry Gate

1. The tree is settled: no agent is editing it and the project gate is green.
   A sweep of code in mid-rewrite hardens what is about to be deleted.
2. Output goes to `<notes>/assurance-sweep/<YYYY-MM-DD>/`, never the repo.
3. Read the user's recorded decisions. A recorded decision is never a finding.
4. Quote the cost before starting: one strongest-tier whole-repository read
   per lens.
5. Note the project's tier in the inventory: **production** (outside users,
   their data, uptime) or **personal** (the owner's own tool or home setup).
   On a personal project no lens proposes production machinery —
   observability, alerting, restore rehearsals, leak checks, growth
   measurement — and the Venues lens marks those methods `n/a`. Neat types,
   clear structure and working tests still apply in full.

## Step 1 — Inventory

You write `inventory.md`; you have the shell the reviewer lacks. Template
and method: `references/inventory.md`. Four sections:

1. **Mechanisms.** The jobs this application does at more than one call
   site, found from its own manifests, modules, scripts, tests and deployment
   config. Never start from a fixed list: what recurs depends on the
   application.
2. **Assurance.** One row per method in the template table: present, weak,
   absent or not applicable, with the command that runs it.
3. **Defect history.** `<notes>/review-ledger.tsv` first, then fix and revert
   commits and the issue tracker.
4. **Leaving soon.** Code scheduled for deletion or replacement. Lenses skip
   it.

## Step 2 — Run One Lens

| # | Lens | Question | Presupposes |
|---|---|---|---|
| 1 | Duplicates | Which job is done in more than one place, and what breaks when the copies diverge? | the mechanism list |
| 2 | Norms | Which mechanism is unusual or heavier than the ordinary way? (advisory) | the mechanism list |
| 3 | Domain model | Where does the core carry plain strings, numbers, sentinels or adapter vocabulary instead of domain types and domain words? | a type checker in the project gate |
| 4 | Defect classes | Which past defects have siblings still in the code, and what would rule the class out? | defect history |
| 5 | Races | Which pairs of concurrent operations have no test that runs them together? | a way to run two operations at once in a test |
| 6 | Extrapolation | Which existing test covers much more for a small edit? | property tests; a model or scenario test |
| 7 | Venues | Which assurance method that would have caught past defects is unused or weaker than claimed? | defect history; the assurance table |

Run the lenses the user asked for; with no preference, in this order. Lenses
1 to 3 can lead to code being deleted or restructured. If a later lens ran before their
proposals were acted on, say so: it may have read code that is about to go.

1. Compose the prompt from `references/lenses.md`: the shared preamble, then
   that lens's block. Fill in the paths. Add no suspicions of your own: a
   lens told what to find finds it.
2. Dispatch ONE reviewer, prompt opening `REPORT dispatch`. Never two lenses
   in flight and never one lens split across readers: split readers miss
   what only shows across the whole.
3. Save the reply verbatim as `<n>-<lens>.md`, headed with lens, date and
   commit.

## Absent Means Skip

A lens that depends on something the repository lacks — its Presupposes
column, marked absent in the inventory — is not run. Dispatch no reviewer
for it. In the hand-over write "Lens <n> skipped: this repository has no
<thing>", and name the baseline from `references/inventory.md` that would
make the lens runnable. Propose nothing further and add nothing to the
project: setting that baseline up is a separate task the user asks for.

The same holds inside a lens that does run. Where a check needs a
deterministic tool the project does not have, the reviewer names the missing
tool category under "Not covered" and moves on.

## Step 3 — Check The Reply

Read the cited lines of every RECOMMEND item and mark each one in the
file:

- **confirmed** — the cited code shows what the item claims;
- **refuted** — it does not, and you name the code that says otherwise. A
  comment is not that code;
- **unconfirmed** — you could not tell. It stays in the file for the user.

Nothing is deleted from a lens file. Verify every item marked `CONFIRM` by
research before the user sees it.

## Step 4 — Record And Hand Over

1. Append one row per item to `<notes>/review-ledger.tsv`, or one `none` row
   for a lens that ran and returned nothing; a skipped lens gets no row
   (format:
   `~/.claude/skills/advice-refine-test-loop/references/ledger.md`).
2. Tell the user where the file is, the count per verdict, and which items
   need their decision.
3. End with the line from
   `~/.claude/skills/advice-refine-test-loop/scripts/ledger-tally.sh` for
   each lens that ran. If a line says `drop` or `reword`, or an `exclude?`
   line appears, put it to the user as one closed question. Change nothing
   yourself. Stop there.

## When To Run

1. Before a release or cutover, after the code freeze.
2. After a large merge or rewrite lands.
3. When one defect class shows up a third time.

Never per pull request, and never on a timer.

## Rules

1. Rank by harm prevented over effort. Line count is never a reason.
2. A RECOMMEND needs a concrete wrong outcome: "X happens to Y when
   Z". Without one the item is ADVISE at most.
3. ADVISE items never gate a release. Norms and "heavier than needed" items
   are always ADVISE.
4. A check, type or lint its caller can sidestep is SKIP, not RECOMMEND.
5. A lens that returns nothing is a result. Write its file anyway.
6. One lens run is one sample. Never call the result comprehensive: each
   lens read what its Coverage line says and no more.
