---
name: finishing-up
description: Use when wrapping up work that produced a diff, and ALWAYS before asking the user to merge, review, or click anything on a PR — including "this is ready to merge", "waiting on your merge", "I'm blocked on you merging", "PR is up", or any status report that mentions a branch or pull request. Also when CI is running, a check is red, a PR won't merge, or a turn is about to end with unpushed or unmerged work. Drives to green-and-mergeable, then hands over with clickable links.
---

# Finishing Up

Work is not done when the code is written. It is done when CI is green, the
PR is genuinely mergeable, and Jonathan has a clickable link plus an
executive summary in front of him.

**The Iron Law: never report PR state you have not just measured.**

`gh` is the measurement. Your memory of a PR is not — PRs get merged, closed,
and broken by other pushes while you are not looking. The specific failure
this skill exists to prevent: linking a PR, saying "blocked on your merge",
and being told it merged two hours ago.

## Step 1 — Measure, always

```bash
python3 ~/.claude/scripts/dod-check.py          # human-readable
python3 ~/.claude/scripts/dod-check.py --json   # for parsing
```

Exit codes: `0` done · `1` blockers · `2` not applicable · `3` could not tell.

**Treat 3 as not done.** An unreachable API is not a green check.

Run this *in the turn where you make the claim*. Not earlier in the session,
not "I checked after the last push". Fresh, or it is not evidence.

## Two ladders

Which one applies is decided by the branch, and `dod-check` picks for you.

**Direct-push — you are on the default branch.** Committed → pushed → level with
`origin`. That is the whole ladder. No PR is demanded, because none can exist.
This is not a loophole: it used to be `n/a`, which meant the gate could be
side-stepped entirely by never opening a PR, and commits sat unpushed on `main`
reading as finished.

**PR flow — you are on a feature branch.** Committed → pushed → PR open →
checks green (or the repo has no CI) → mergeable → Jonathan's merge click →
**switch back to the default branch and pull**.

**A session ends on the default branch, clean, at latest.** Leaving a checkout
parked on a merged feature branch is a closing ceremony, and Jonathan does not
want one: *"I want Claude to move as close as possible to finishing, so there is
no closing ceremony for me to do, beyond bare necessities."*

Bare necessities are the merge click on a protected branch, credentials,
interactive logins, and taste calls. Everything else is yours.

**Several repos is normal.** One session legitimately spans e.g. `nixos-config`
plus a project. Each repo runs its own ladder and each gets its own line in the
report, with its own URL.

## Step 2 — Act on what it says

Work the blockers in the order reported; each has a `fix` field.

| Blocker | What to do |
|---|---|
| `DIRTY_TREE` | Commit it. Do not report a state you have uncommitted changes on top of. |
| `NOT_PUSHED` / `UNPUSHED_COMMITS` | Push. On the default branch this is measured against `origin/<base>`, not the upstream ref. |
| `BEHIND_REMOTE` | `git pull --ff-only`. "At latest" is part of done. |
| `NOT_ON_DEFAULT` | The PR merged and the repo is still on the dead branch. `git switch <default> && git pull --ff-only`. This is the last step of the work, not a chore for Jonathan. |
| `NO_PR` | `gh pr create --fill`, then write a real body. |
| `PR_DRAFT` | `gh pr ready <n>` — a draft is not a merge request. |
| `CHECKS_PENDING` | **Step 3.** Do not guess the outcome. |
| `CHECKS_FAILING` | **Step 4.** |
| `NOT_MERGEABLE` | `BEHIND` → merge the base in and push. `DIRTY` → resolve conflicts. `BLOCKED` → say precisely what protection is unmet. |
| `UNRESOLVED_THREADS` | Answer each, then resolve it. |
| `NO_CHECKS` | Zero checks in a repo that **has** workflows. Something deleted, disabled or un-triggered them; find out what. A repo with no CI at all does not raise this — it reports a warning, because there is nothing to wait for. |
| `INTEGRITY` | You (or the diff) weakened the signal. Revert it. Justify with a `dod-ok` comment only if it is genuinely correct — the comment lands in the diff for Jonathan to disagree with. |
| `PR_CLOSED` | Closed without merging. Do not report it as pending. |

`MERGED` is never a request for a merge. Report it as landed, and never ask
Jonathan to click something he already clicked. It is not quite *done* on its
own, though: a merged PR still leaves the checkout on a dead branch, so
`NOT_ON_DEFAULT` follows it. Go home, then you are done.

## Step 3 — Pending checks: schedule, never spin

CI takes minutes. You cannot wait inside a turn, and ending the turn with
"I'll report when it lands" drops the work.

```
CronCreate(cron: "<M> <H> <DoM> <Mon> *", recurring: false, durable: true, prompt: <self-contained>)
```

- **`durable: true`** — survives `/clear`, restart, crash.
- **Delay matched to the run.** An 8-minute pipeline gets one ~10-minute
  check, not six 90-second ones. Add slack; firing early wastes the wake.
- **Off-minute** (not :00 or :30).

The scheduled prompt is written for an agent with no memory of this turn:
the repo path, the branch, the PR number, that it should run `dod-check`
first, what to do on green / red / still-pending, and what not to do without
Jonathan.

Alternative when the wait is short and you have more to do anyway: run
`gh pr checks --watch --fail-fast` as a background task. The Stop hook reads
`background_tasks` and will not fault the turn for pausing on live work.

## Step 4 — Red checks: attribute before you fix

**Ask first: does this fail on the base commit too?** One rerun against the
base converts an ambiguous red into a definitive answer, and it is the single
highest-value signal available.

- **Fails on base too** → pre-existing, not yours. Say so. Do not mutate your
  diff to chase it.
- **Fails only on your branch** → yours. Read the actual log
  (`gh run view <id> --log-failed`), fix the cause.
- **Passes on rerun, identical code** → flaky. Invoke `hardening-flaky-ci`.
  Record the attribution; do not just rerun until green.

Never make a check pass by weakening it. `dod-check`'s integrity scan will
catch the obvious forms, and Jonathan reads the diff.

**Same failure three times running → stop.** Escalate with the full retry
history rather than trying a fourth variation. Three identical failures means
the approach is wrong, not that the next tweak is the one.

## Step 5 — The handover

Only once `dod-check` exits 0. Three sections; drop any that is empty.

1. **Executive summary** — numbered list of what changed and why, including
   tooling / skill / config changes, not just product code. One line each.
   Jonathan should be able to scan it instead of reading commits.
2. **Visual review** — if any rendered view changed (HTML, templates, client
   TS, HTML-returning handlers), serve the shots:
   `~/.claude/skills/screenshot-review/serve.sh <shots-dir>`. Include the URL
   and the kill command. Omit only for pure backend/infra diffs.
3. **PRs** — **clickable full URLs**, one line each with branch, base, and
   commit count. `https://github.com/owner/repo/pull/N`, never `#123`, never
   a bare branch name. The URL comes from `dod-check`'s `pr.url`, which came
   from `gh` this turn.

State explicitly what is left and why. If the only thing left is Jonathan's
click, say that — and say that CI is green and the PR is mergeable, because
you just measured both.

## Red flags

- "Ready to merge" without a `dod-check` run in this turn
- "Blocked on you merging" without re-checking the PR is still open
- Reporting `#123` instead of a URL — it is not clickable
- Ending a turn with pending CI and no `CronCreate`
- "The tests should pass now" — should is not a measurement
- Rerunning a red check until it goes green without attributing why
- Treating exit 3 as done
- Reading `NO_CHECKS` as green — or answering it by adding a workflow to make
  the blocker go away, which is the same trick from the other end
- Asking Jonathan to merge something already merged

## Scope

Any work that produced a diff. Not for questions, research, or reading —
those never touch a file, so there is nothing to merge and nothing to check.
