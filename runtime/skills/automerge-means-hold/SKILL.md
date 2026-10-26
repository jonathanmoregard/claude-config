---
name: automerge-means-hold
description: Use when the user replies "automerge", "auto-merge", "put automerge" or "automerge on" about a pull request.
---

# "automerge" means hold

The user has already enabled GitHub auto-merge on the PR under discussion. It
is a status report, not a request.

1. Do not run `gh pr merge`, and do not tell the user how to enable auto-merge.
2. Acknowledge in one line. Watch the PR in the background
   (`~/.claude/scripts/wait-for pr <n> -R <owner/repo>`) and pick up any
   follow-up that waits on the merge once it lands.
