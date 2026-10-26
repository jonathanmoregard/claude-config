---
name: finishing-up
description: Use when wrapping up work that produced a diff; when deciding whether to open a PR (private repos with PR-triggered CI hold the PR until the user says yes); before asking the user to review or merge a PR; after the user says a PR merged; when verifying deployment or smoke-testing deployed work; when CI, conflicts, mergeability, deployment, or smoke block completion; or when managing multiple branch/repository delivery targets.
---

# Finishing Up

Drive each delivery target (one branch in one repository) to verified
completion. Fresh commands establish facts; nothing remembered from an earlier
turn counts.

**Iron law: "ready for review" and "merged" are unfinished. Done means the
exact merged revision is deployed and a deployed-path smoke check passed —
or, for a repo with nothing to deploy, merged and the local default branch
pulled.**

The `completion-check` hook re-measures, at the start of every turn, each
checkout this session committed, pushed or created a worktree in, and names what
is still open: commits not pushed, a pushed branch without a PR (outside
PR-gated repos), failing PR checks, a merged PR whose primary checkout is still
on the branch or behind, a dirty worktree this session created. It reports
status; it never ends or blocks a turn. Treat it as a reading on the ladder, not
the ladder: close what belongs to the current task, and when a gap must stay
open (the user asked to hold it), say why in the reply.

## 1. Choose outcome

| Outcome | Use when | Action |
|---|---|---|
| **Ship** | Work belongs upstream; no contrary user intent | Complete the ladder below |
| **Hold PR** | PR-gated repo (private/internal + `pull_request` CI); branch not yet reviewed, or user not yet asked | Push branch; finish local gates and user review; then ask "Open the PR?" and stop. Resume Ship on yes |
| **Preserve** | User explicitly wants work retained, or another session owns it | Do not publish or alter; record exact worktree, branch, HEAD, blockers, owner, next action |
| **Abandon** | User explicitly authorizes discard | Resolve exact targets and ownership; remove only authorized work; report recovery status |

Never push or open a PR only to look finished. Never delete without authority
from global instructions. If the outcome is genuinely unclear, ask one focused
question.

## 2. Reach PR readiness

In each touched repository:

```bash
git -C <repo> status --short --branch          # clean, nothing unpushed
git -C <repo> fetch origin && git -C <repo> log --oneline origin/<branch>..HEAD   # empty
gh repo view <owner/repo> --json visibility    # PR-gated? with: ls .github/workflows; grep pull_request
gh pr view <branch> -R <owner/repo> \
  --json number,url,state,headRefOid,mergeable,mergeStateStatus,statusCheckRollup,reviewDecision
```

Ready means: tree clean, branch pushed, PR `OPEN`, `headRefOid` equals local
`HEAD`, `mergeable` `MERGEABLE`, every check in `statusCheckRollup` passed.
`UNKNOWN` mergeability is not ready: re-query `.state` first
(`reporting-pr-state`). No CI in a repository is not a blocker; existing CI
that did not run is. Fix branch-caused failures.

A PR-gated repo with no PR is not a fix-it-now blocker. A PR there costs
billed Actions minutes on open and on every later push, so it opens once, on a
branch the user has reviewed, after their explicit yes. Never `gh pr create` to
clear it on your own.

Waiting on CI: `~/.claude/scripts/wait-for pr <n> -R <owner/repo>` with
`run_in_background: true`, then end the turn.

### Live check before any merge or review ask

In the same turn as any message that asks the user to merge or review a PR,
or says one is "ready", query every PR the message names:

```bash
gh pr view <n> -R <owner/repo> --json state,mergedAt,autoMergeRequest,headRefOid,mergeStateStatus
```

- `MERGED`: report it merged (with `mergedAt`) and move to deployment; never ask.
- `CLOSED`: report it closed; never ask.
- `autoMergeRequest` set: say it will merge itself once checks pass; never ask.
- `headRefOid` differs from the head you verified: re-verify before any ask.

A result from an earlier turn, or from before a background task notification,
does not count: the user or a bot may have merged it meanwhile. 2026-09-23:
asked the user to merge two PRs, one merged 30 min earlier and one merged by
auto-merge seconds before. 2026-10-12: listed nixos-config #339 as awaiting a
merge click twice after it had merged.

The `pr-state-guard` Stop hook queries every PR linked in a reply and sends the
reply back when it presents a merged or closed PR as awaiting action. It is a
backstop for this check, not a replacement.

## 3. Hand off for review

Before asking for the merge, state how deployment will be verified: the command
that reads the deployed revision (it queries the deployed runtime or artifact,
not the feature worktree, and prints a full commit SHA) and the smoke command
that exercises the real deployed URL, service, binary or integration path. No
stub, source-tree substitute or mutated artifact. No secret in argv.

Present a concise change summary, green status, reviewed head, and the only
remaining human action: review and merge. The full PR URL goes at the bottom.

## 4. Verify a user-reported merge

A human "merged" message is a trigger, never evidence:

```bash
gh pr view <n> -R <owner/repo> --json state,mergedAt,headRefOid,mergeCommit
```

Accept merged only when `state` is `MERGED`, the final `headRefOid` equals the
head you handed off, and `mergeCommit.oid` is a full SHA. Never substitute the
branch head: squash, rebase and merge-commit strategies write different base
SHAs. If still open, closed, drifted, or unreadable, report the measured state.

## 5. Verify deployment and smoke

Run the revision command from step 3. Smoke only when the deployed SHA equals
`mergeCommit.oid`.

- Different valid SHA → deployment still rolling; schedule one durable
  follow-up (`scheduling-follow-through`). Never spin.
- Probe timeout, error or unparseable SHA → unknown; never call it green.
- Exact SHA + smoke fails → diagnose the deployed system.
- Exact SHA + smoke passes → done for that target.

Then switch the primary checkout back to the default branch and pull, and
remove worktrees this session created (global CLAUDE.md, **Worktrees**).

## Multiple targets

Treat every branch/repository independently:

- One target finishing says nothing about its siblings.
- A CI-red sibling stays open even if another deployment passed.
- Status reports name only targets belonging to this work.

## Red flags

- Calling a green, mergeable PR done
- Trusting "merged" without a same-turn `gh pr view`
- Asking for a merge or review without a same-turn live `gh pr view` of that PR
- Opening a PR in a PR-gated repo before the user's review and explicit yes
- Using the reviewed branch SHA as the deployed merge SHA
- Smoke-testing the source tree, a stub, a mock, or a changed artifact
- Running smoke before the exact deployed SHA matches
- Treating timeout, unavailable GitHub, missing merge SHA, or head drift as green
- Polling instead of one bounded durable follow-up
- Putting tokens or credentials in probe argv or output

## Scope

Use for diffs entering PR delivery. Default-branch direct-push work keeps its
short ladder: committed, pushed, local default branch clean and current with
origin. Read-only/research work has no delivery target.
