---
name: reporting-state
description: Use when the user asks a general status or orientation question about the work rather than about one specific PR — "where are we", "what's up", "what's the status", "where do things stand", "brief me", "orient me", "catch me up", "what's left", "anything outstanding" — or comes back after a break and wants their bearings.
---

# Reporting State

A general status question gets an executive summary: report at the level of
the work, not the PRs. A question about one PR goes to `reporting-pr-state`.

## Gather first

Read the handoff (`mission.md`, notes) and git state. Before calling any PR
open or waiting on the user, query its live `.state`; `reporting-pr-state` is
the rule for PR facts and for which PRs belong to the work.

## The reply

In this order, under about 120 words:

1. **Goal**: one line.
2. **Where it stands**: 2–4 lines, one short plain sentence each. Name pieces
   by what they do ("the comment import"), not by PR numbers, branches or
   file names.
3. **Needed from you**: numbered, at most 3 (clicks, decisions, credentials),
   one line each. "Nothing." when there is none.
4. **Next**: one line.

Then the full URL of each PR under Needed from you, one per line. Last line:
the state marker, `QUESTION` (needs an answer), `BLOCKED` (needs a click,
credential or login), `RUNNING`, `FAILED` or `DONE`.

## Example

```
Goal: move the blog off the old host without broken links.

Where it stands:
- Posts, images and redirects are migrated.
- Comment import fails on two threads; I'm fixing the encoding.

Needed from you:
1. Merge the redirect rules.
2. Keep the old RSS URL, or announce a new one?

Next: fix the comment import, then switch DNS.

https://github.com/owner/blog/pull/57

QUESTION
```
