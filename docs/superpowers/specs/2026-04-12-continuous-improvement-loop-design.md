# Continuous Improvement Loop

**Date:** 2026-04-12
**Status:** Draft
**Packaging:** Claude Code plugin

## North Star

Productivity/being balance: high levels of wellbeing AND output connected to what causes flourishing.

The user should only need to say *what* they want and *why* they want it. Claude delivers implementation that satisfies their needs without the user needing to intervene, course-correct, or firefight. Over time, the ideal is autonomous operation — output generated without the user needing to be at the screen, enabling work patterns that mix with mindful forest walks.

Healthy digital habits: productivity mixed with mindfulness, not manic screen time. The system should nudge toward this balance, not just optimize for throughput.

This system exists to continuously close the gap between that ideal and reality by mining real conversation history for failure patterns and turning them into configuration improvements.

### Configurability (plugin)

The plugin includes a `/setup-improvement-loop` skill that interviews the user to configure:

- **North star** — what does a good life/work balance look like for you?
- **Goals** — what are you working toward?
- **Alignment signals** — "what are some ways the review agent can assess whether you are living in accordance with your goals or not?" Free-form natural language descriptions of what "on track" and "off track" look like for this user.
- **Cron schedule** — when should the daily review run?

All stored in a user config file that the review agent reads. No hardcoded thresholds.

## Overview

Five components form a closed loop, packaged as a Claude Code plugin:

1. **Daily review agent** — Opus reads the last 1 day of chat logs, identifies problems, writes improvement proposals
2. **Proposal files** — structured markdown in `~/.claude/improvements/`, tracked in git
3. **SessionStart hook** — nudges the user when pending proposals exist
4. **`/review-improvements` skill** — interactive walkthrough where accepting a proposal triggers immediate implementation, testing, and commit/push on user approval
5. **Secret scanning** — `detect-secrets` as a global git pre-commit hook

## Component 1: Daily Review Agent

### Execution

- **Schedule:** Daily at 17:00 via system crontab or systemd timer
- **Command:** `claude --model opus --print -p "<review prompt>"` run from `~/.claude/` working directory
- **Permissions:** `--allowedTools "Read Write(~/.claude/improvements/*) Glob Grep WebSearch Bash(~/.claude/push-proposals.sh)"` — broad read, scoped write, no direct git access
- **Logging:** Output captured to `~/.claude/logs/review-agent.log`

### What the agent reads

Before analyzing logs, the agent reads existing configuration to understand what's already in place:

- **Chat logs:** All `~/.claude/projects/*/*.jsonl` files modified in the last 1 day (excluding subagent logs)
- **Global CLAUDE.md** and all per-project CLAUDE.md files
- **`~/.claude/settings.json`** — hooks, permissions, enabled plugins
- **Installed skills** — `~/.claude/skills/` directory and plugin skill manifests
- **Memory files** — `~/.claude/projects/*/memory/`
- **Existing proposals** — all files in `~/.claude/improvements/` regardless of status (pending, accepted, rejected, implemented, deferred) to avoid duplicating or re-proposing
- **User goals** — stored in memory, used to assess rabbit-hole drift
- **WebSearch** — to find relevant existing plugins, prior art, or known solutions

### What the agent looks for

The agent uses judgment, not heuristics. The prompt frames what to look for but trusts Opus to exercise discernment.

#### Categories

Each proposal is tagged with a `category` field:

- **`config`** — Claude configuration improvements (skills, hooks, CLAUDE.md rules, memory)
- **`automation`** — manual meta-work that could be automated on a schedule
- **`wellbeing`** — addictive behavior, rabbit holes, work patterns

#### Skip — healthy collaboration
- User changing direction, refining taste, being picky about implementation details
- User jamming on the design, exploring options together
- User providing domain context that Claude couldn't have known

#### Flag — north star violations (category: config)
- **Misunderstandings:** Claude misinterpreted the user's intent, went down the wrong path, built the wrong thing
- **Execution failures:** Claude got stuck in loops, wasted cycles retrying the same failing approach, needed user rescue to unblock
- **User taking over execution:** User pasting fixes, providing file paths Claude should have found, debugging Claude's work
- **Anger/frustration signals:** User expressing frustration — treated as a pointer to a deeper underlying problem, not the problem itself. The agent's job is to identify what the frustration points at.

#### Flag — automatable meta-work (category: automation)
- User manually doing maintenance tasks (memory consolidation, CLAUDE.md updates, config reorganization, repo hygiene) that could be automated on a schedule

#### Flag — wellbeing patterns (category: wellbeing)
Assessed using the user's configured **alignment signals** — natural language descriptions of what "on track" and "off track" look like. The review agent reads these and uses judgment to detect patterns in session timestamps, interaction patterns, and what's being worked on.

Common patterns (user-configurable, not hardcoded):
- Late-night sessions
- Manic coding — user doing work Claude should be doing
- Rabbit holes — work that doesn't align with stated goals. Propose either "this seems off-track" or "consider adding this to your goals if intentional" — goals are living and the system adapts
- Long sessions without breaks

### Distinguishing existing config

When proposing fixes, the agent must distinguish:
- **"Skill/hook doesn't exist"** — propose creating it
- **"Skill/hook exists but didn't activate"** — propose fixing the trigger/matcher, cite the existing file path and explain why it failed
- **"Config exists and works, user just didn't follow it"** — different kind of proposal, possibly about making the config more assertive

### Proposal deduplication

Before writing a proposal, the agent checks:
- Does an existing proposal (any status) already cover this problem?
- Does existing config already address this, but failed to activate? If so, the proposal should be about fixing the activation, not creating something new.
- Are there multiple instances of the same problem across sessions? Group them into one proposal with multiple examples.

### Git behavior

After writing proposals, the agent runs `~/.claude/push-proposals.sh` which:
1. `cd ~/.claude`
2. `git add improvements/`
3. Commits with a descriptive message
4. Pushes to remote

The agent has no direct git access — only this script.

## Component 2: Proposal Format

Each proposal lives at `~/.claude/improvements/YYYY-MM-DD-<slug>.md`:

```markdown
---
status: pending
category: config | automation | wellbeing
date: 2026-04-12
source_sessions:
  - abc123-def456
  - ghi789-jkl012
source_logs:
  - ~/.claude/projects/-home-jonathan-Repos-jhana/abc123.jsonl
  - ~/.claude/projects/-home-jonathan-Repos-weekend/def456.jsonl
project: jhana | global
---

## Problem
[One paragraph — what went wrong, framed in terms of the north star violation or wellbeing concern]

## Proposed fixes
1. [Most targeted fix — specifies whether this is a new skill/hook/CLAUDE.md rule or a tweak to an existing one that didn't activate correctly]
2. [Alternative approach]
3. [Optional broader fix]
```

### What proposals do NOT contain
- **No log excerpts or conversation content** — only links to log files and session IDs. This prevents sensitive data from leaking into git history.
- **No extended context section** — the reviewer can follow the log links if they want more detail.

### Status lifecycle

- `pending` — newly created by the review agent, awaiting user review
- `accepted` — user approved, implementation in progress or complete
- `rejected` — user declined, with optional reason noted
- `implemented` — fix has been built, tested, committed, and pushed
- `deferred` — user wants to revisit later

## Component 3: SessionStart Reminder Hook

A Python script added to the `SessionStart` hook in `settings.json`:

- Scans `~/.claude/improvements/` for files with `status: pending` in frontmatter
- If pending proposals exist: returns a nudge in hook output, e.g., "You have 3 pending improvement proposals. Run /review-improvements to go through them."
- If none: silent, no output

Should be fast — just reads frontmatter, no heavy processing.

## Component 4: `/review-improvements` Skill

An interactive skill that the user invokes (or that activates when the user mentions reviewing proposals).

### Security: proposals as untrusted content

Proposal files are written by an unattended agent that reads chat logs — which may contain prompt injection attempts. The skill treats proposal content as **untrusted display-only data**:

- Proposals are rendered as quoted blocks for the user to read
- The skill never interprets proposal content as instructions
- Implementation is driven by the **user's verbal response** ("implement fix 2"), not by feeding proposal content as a prompt to Claude
- This prevents a poisoned proposal from hijacking the interactive session

### Flow

1. Read all proposals with `status: pending` from `~/.claude/improvements/`
2. Present each proposal one at a time as a quoted block: problem and proposed fixes
3. For each proposal, user chooses:
   - **Accept** — immediately implement the proposed fix (the recommended one, or user picks). Then:
     a. Implement the fix (create/modify skill, hook, CLAUDE.md rule, etc.)
     b. Test the implementation (verify hook fires, skill loads, config is valid)
     c. Present test results to user
     d. On user approval: commit and push, update proposal status to `implemented`
   - **Reject** — update status to `rejected`, optionally note the reason
   - **Defer** — update status to `deferred`, move on to next
4. After all proposals reviewed, commit any status changes and push

### Auto-detection

The skill should trigger when the user expresses intent to review proposals, not just on `/review-improvements`. Examples:
- "let's go through the improvement proposals"
- "any pending improvements?"
- "review proposals"

## Component 5: Secret Scanning

`detect-secrets` installed as a **global git pre-commit hook** across all repos:

- Scans staged files for API keys, tokens, high-entropy strings, and other secret patterns
- Blocks commit if secrets are detected
- Maintains a baseline/allowlist to minimize false positives
- Protects proposals from accidentally containing sensitive data, and protects all other repos as a bonus

If the pre-commit hook catches something in a proposal during the cron agent's push, the agent rewrites the proposal to remove the flagged content and retries.

## Component 6: User Goals (Reference Data)

Stored as a memory file, read by the review agent for rabbit-hole detection:

### Mission
Co-create a flourishing world by caring deeply. From a grounded place — present in body, mindful, spontaneous action from joy — care reaches outward through actions, helping all beings flourish.

### Grounded mode vs drift
The system detects whether the user is in **purposeful mode** (grounded, joyful, working on what matters, taking breaks, on top of habits) vs **zombie/manic mode** (doom prompting, unhinged coding, rabbit holes, no breaks, late nights). The review agent flags drift toward the latter.

### Current goals (as of 2026-04-12)
Populated by the `/setup-improvement-loop` skill. Example (Jonathan's):

- **Spiritual:** Mindfulness, vajrayana enlightenment
- **Technical/FOOM:** Claude Code recursive self-improvement, mac mini setup, NixOS, personal automations
- **Relationship:** Be a good partner
- **Impact:** Anti-addiction software, coordination tools, veganism, positive-sum design
- **Intellectual growth:** Substack writing, exobrain setup
- **Physical:** Workout, clothing, haircut
- **Learning:** Claude, NixOS, harness engineering
- **Security:** Balancing automation with integrity/security

Goals are living — the review agent may propose updates when it notices work that doesn't fit but seems intentional.

## `push-proposals.sh`

Hardcoded script, accepts no arguments:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd ~/.claude
git add improvements/
if git diff --cached --quiet improvements/; then
  echo "No proposal changes to commit"
  exit 0
fi
git commit -m "improvement proposals: $(date +%Y-%m-%d)"
git push
```

## File Layout

```
~/.claude/
  improvements/              # proposal files
    2026-04-12-misread-user-intent.md
    2026-04-11-late-night-coding.md
    ...
  logs/
    review-agent.log         # daily agent output
  prompts/
    daily-review.md          # the review agent prompt
  skills/
    review-improvements/     # the interactive review skill
  hooks/
    pending-proposals.py     # SessionStart hook script
  push-proposals.sh          # hardcoded git add/commit/push for proposals
```

All packaged as a Claude Code plugin.

## Cron Configuration

```crontab
0 17 * * * cd /home/jonathan/.claude && claude --model opus --print --allowedTools "Read Write(~/.claude/improvements/*) Glob Grep WebSearch Bash(~/.claude/push-proposals.sh)" -p "$(cat ~/.claude/prompts/daily-review.md)" >> ~/.claude/logs/review-agent.log 2>&1
```

The review prompt itself lives at `~/.claude/prompts/daily-review.md` for easy iteration.

## Open Questions

None — all resolved during brainstorming.
