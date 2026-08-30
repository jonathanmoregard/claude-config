---
name: reporting-pr-state
description: Use when answering anything about the state of a pull request, branch, or merge — "did X merge", "is it merged", "what's the PR status", "what still needs my click", "switch?", "anything to merge" — and on any general status or standing question while work is open: "where do things stand", "where are we", "what's the status", "what's left", "brief me", "anything outstanding". Also when resuming from a handoff, mission.md, or session notes that mention open PRs, and when a status question is only one part of a longer multi-part request.
---

# Reporting PR State

## Overview

**A PR's state is not a fact you remember. It is a fact you query, at the moment you are asked.**

Notes, `mission.md`, and earlier turns in the same session record what was true when written. A human merges in a browser and nothing tells you. Any report built from stored state is a guess wearing the costume of a fact.

## The Iron Law

```
QUERY .state BEFORE REPORTING ANY PR AS OPEN
```

Not `mergeable`. Not the check rollup. Not the handoff file. `.state`.

## Do This First

```bash
gh pr view <n> --repo <owner>/<repo> \
  --json number,title,state,mergedAt,mergedBy,mergeable,mergeStateStatus,isDraft,url,statusCheckRollup
```

For a sweep, state first and always:

```bash
gh pr list --repo <owner>/<repo> --state all \
  --json number,title,state,url,mergedAt
```

`--state open` is the default and it silently hides exactly the PR you are about to misreport.

## The UNKNOWN Trap

`mergeable` / `mergeStateStatus` return `UNKNOWN` for **two unrelated reasons**:

| Cause | Real state | Right move |
|---|---|---|
| PR is already MERGED or CLOSED | terminal | Read `.state`. Stop polling. |
| GitHub is lazily recomputing | still OPEN | Poll again, or just read the checks |

**`UNKNOWN` is ambiguous by construction, so it can never be resolved by staring at it.** Both cases appeared within one hour of the same session: one PR returned `UNKNOWN` because it had been merged hours earlier, another returned `UNKNOWN` for ~100s after a push and then settled to `MERGEABLE`.

Treating `UNKNOWN` as "GitHub is being slow" is the specific failure this skill exists to prevent. It is a prompt to query `.state`, never a prompt to retry.

## Scope: only the work's own PRs

**A status report covers the PRs belonging to the work being reported on.** Not every PR open on the machine, not the ones another session is driving, not ones noticed in passing while doing something else.

The Iron Law says query before reporting a PR as open. It does not say report every PR you can find. A sweep that enumerates unrelated work buries the one PR that needs a click under things the reader has to triage — and it silently claims ownership of work another session is mid-flight on.

| Situation | Report it? |
|---|---|
| The PR this work produced | Yes — always, with its URL |
| A PR this work depends on, conflicts with, or is blocked by | Yes, **and say which** in the same breath |
| A PR another session is driving, noticed via `gh pr list` or a shared state dir | No |
| An unrelated PR that happens to be time-sensitive | One line, marked as an aside |

Relevance is stated, not implied by listing. If you cannot say in a clause why a PR belongs in this report, it does not.

`gh pr list` across a repo answers "what exists", never "what is mine to report". Scope by the branch the work is on.

## Report Format

Every PR mentioned in a report carries its **full URL on its own line**. Not the bare number, not `#178`, not a markdown label — the link, so it is one click away.

```
nixos #178 — MERGED 2026-08-15 21:58 CEST by jonathanmoregard (7d36a8b4)
https://github.com/jonathanmoregard/nixos-config/pull/178
```

Open ones get state + checks + what it is blocked on:

```
aggregator #3 — OPEN, MERGEABLE/CLEAN, 2/2 checks green
https://github.com/jonathanmoregard/aggregator/pull/3
```

## Merged ≠ Deployed

On a repo with auto-deploy, "merged" is the start of the pipeline, not the end. When a merged PR was supposed to *do* something, verify the thing:

```bash
systemctl status <unit>            # did the unit land
systemctl --user list-timers '<glob>'
gh run list --workflow=<wf> --branch main --limit 1
```

Report the deployed effect, not the merge event. "Merged" plus a dead timer is a red herring dressed as success.

## Red Flags — STOP and Query

- About to write "OPEN", "still needs your click", or "waiting on you" from a note rather than a query
- Reading `UNKNOWN` and reaching for `sleep`
- Reporting checks green as though it settled whether the PR is open
- A handoff file and the API disagreeing, and trusting the file
- "Nothing has landed since, so it can't have changed" — a merge is not a commit to your branch
- Answering "did it merge?" without the word MERGED or OPEN coming from a `.state` field in that same turn

## Rationalization Table

| Excuse | Reality |
|---|---|
| "mission.md said OPEN an hour ago" | An hour is long enough to merge, deploy, and forget. Query. |
| "UNKNOWN just means GitHub is recomputing" | It equally means MERGED. That's the trap. Read `.state`. |
| "Checks are the real signal" | Checks stay green forever on a merged PR. They say nothing about open/closed. |
| "I checked at the start of the session" | State at session start is not state now. |
| "It's a quick question, not worth a call" | The call is one command and ~1s. Being wrong costs the user a wasted click. |
| "No new commits, so nothing changed" | Merging happens in a browser and leaves no trace in your context. |

## Real-World Impact

2026-08-15: reported nixos #178 as "OPEN, 17 green, needs your click". It had been MERGED for over three hours, the deploy had already run, and the timer it installed was live and mid-ingest. The evidence was on screen — `UNKNOWN / UNKNOWN` — and got explained away as lazy recomputation, because `.state` was never in the query.
