---
name: assurance-sweep
description: Use when a whole repository or long-lived branch needs a robustness pass beyond diff review — before a release or cutover, after a large merge or rewrite, when the same kind of defect keeps coming back, or on asks like "what more can we do to make this safe", "is anything implemented twice", "is the race coverage complete", "can this class of bug be ruled out", "can existing tests cover more", "is this how it's normally done". Not for close-out review of one diff (advice-refine-test-loop).
---

# Assurance Sweep

Six whole-repository lenses, one at a time, each read by one strongest-tier
read-only reviewer. A lens returns ranked proposals; you check them and hand
them to the user. The sweep proposes and edits no source. Building what the
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
| 3 | Defect classes | Which past defects have siblings still in the code, and what would rule the class out? | defect history |
| 4 | Races | Which pairs of concurrent operations have no test that runs them together? | a way to run two operations at once in a test |
| 5 | Extrapolation | Which existing test covers much more for a small edit? | property tests; a model or scenario test |
| 6 | Venues | Which assurance method that would have caught past defects is unused or weaker than claimed? | defect history; the assurance table |

Run the lenses the user asked for; with no preference, in this order. Lenses
1 and 2 can lead to deleted code, so say when a later lens was read before
their proposals were acted on.

1. Compose the prompt from `references/lenses.md`: the shared preamble, then
   that lens's block. Fill in the paths. Add no suspicions of your own: a
   lens told what to find finds it.
2. Dispatch ONE reviewer, prompt opening `REPORT dispatch`. Never two lenses
   in flight and never one lens split across readers: split readers miss
   what only shows across the whole.
3. Save the reply verbatim as `<n>-<lens>.md`, headed with lens, date and
   commit.

## Absent Means Add

A lens that presupposes something the repository lacks is not skipped and
does not pretend. Its first item is `ADD`: the smallest baseline that makes
the lens's top proposal checkable — where it goes, which existing helper it
builds on, the first test, the effort. The same holds for a check a
deterministic tool could do: find the project's tool of that category, and
where there is none the item is `ADD` that tool. Baselines per method are in
`references/inventory.md`. An ADD that needs a new dependency, a gate change
or a paid service says so in the item.

## Step 3 — Check The Reply

Read the cited lines of every RECOMMEND and ADD item and mark each one in
the file:

- **confirmed** — the cited code shows what the item claims;
- **refuted** — it does not, and you name the code that says otherwise. A
  comment is not that code;
- **unconfirmed** — you could not tell. It stays in the file for the user.

Nothing is deleted from a lens file. Verify every item marked `CONFIRM` by
research before the user sees it.

## Step 4 — Record And Hand Over

1. Append one row per item to `<notes>/review-ledger.tsv`, or one `none` row
   for a lens that returned nothing (format:
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
2. A RECOMMEND or ADD needs a concrete wrong outcome: "X happens to Y when
   Z". Without one the item is ADVISE at most.
3. ADVISE items never gate a release. Norms and "heavier than needed" items
   are always ADVISE.
4. A check, type or lint its caller can sidestep is SKIP, not RECOMMEND.
5. A lens that returns nothing is a result. Write its file anyway.
6. One lens run is one sample. Never call the result comprehensive: each
   lens read what its Coverage line says and no more.
