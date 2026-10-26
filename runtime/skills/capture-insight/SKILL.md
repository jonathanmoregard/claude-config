---
name: capture-insight
description: >
  Trigger when: (1) Claude has a generalizable realization or unblocking insight
  during work, (2) the user teaches a best practice worth preserving, or
  (3) — DEFINITELY trigger — the user expresses happiness or excitement about
  an insight Claude surfaced (e.g. "that's a valuable insight", "great find",
  "exactly what I needed"). Also triggers on: "maybe turn it into a skill?",
  "capture what you just realized", "this approach seems best practice",
  "I'd like to collect learnings from" a finished piece of work.
---

## What to do

Always ask the user first:

> "Should I turn this into a skill?"

If no, stop. If yes, continue.

## Step 1: Mine the conversation

Look back at the current conversation for:
- The specific realization, pattern, or workflow to capture
- Steps that were taken (tools used, sequence, decisions made)
- Corrections the user made — these are especially valuable as constraints
- The trigger context: what kind of situation kicked off this pattern?

If the insight is ambiguous, ask one focused question:
"What's the core behavior you want the skill to encode — the steps, the output
format, or both?"

## Step 2: Decide: skill or memory?

Save as a **skill** when the insight is about *how Claude should behave* — a
repeatable process, output format, or decision heuristic.

Save to **memory** (`~/.claude/projects/.../memory/`) when the insight is a *fact
about the user or project* — a preference, a name, a constraint.

If unsure, ask.

## Step 3: Open a worktree, then hand off to superpowers:writing-skills

A new skill is a file under `~/.claude/skills/`, and `~/.claude` is read-only
and pinned to `master`. Cut the worktree **before** invoking the writer — that
skill does not know this rule and will otherwise write to the live checkout:

```bash
git -C ~/.claude fetch origin master
git -C ~/.claude worktree add ~/worktrees/claude-<slug> -b feat/<slug> origin/master
cd ~/worktrees/claude-<slug>
```

Then invoke `superpowers:writing-skills` and use it to write
`skills/<name>/SKILL.md` **in the worktree**. Commit, push, `gh pr create`; the
merge click is Jonathan's. Never commit to a branch this session did not create.

Memory writes need none of this — `~/.claude/projects/.../memory/` is untracked
runtime state.
