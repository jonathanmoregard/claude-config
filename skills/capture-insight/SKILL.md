---
name: capture-insight
description: >
  Crystallizes a realization, workflow, or pattern from the current conversation
  into a new SKILL.md file. Use this when the user says things like "turn this into
  a skill", "capture what I just realized", "make a skill from this", "that should
  be a skill", "let's codify this", "save this workflow", or "remember this as a
  skill". Also trigger when the user describes a recurring pattern they want Claude
  to follow in the future and asks to formalize it.
---

## Purpose

Extract whatever was just realized or demonstrated in the conversation and turn it
into a well-structured SKILL.md. This skill handles the *extraction* step only —
all structural and quality guidance for writing the skill comes from
`superpowers:writing-skills`, which you must invoke next.

## Step 1: Mine the conversation

Look back at the current conversation for:
- The specific realization, pattern, or workflow the user wants to capture
- Steps that were taken (tools used, sequence, decisions made)
- Corrections the user made — these are especially valuable as constraints
- The trigger context: what kind of user message kicked off this pattern?

If the insight is ambiguous, ask one focused question before proceeding:
"What's the core behavior you want the skill to encode — the steps, the output
format, or both?"

## Step 2: Decide: skill or memory?

Save as a **skill** when the insight is about *how Claude should behave* — a
repeatable process, output format, or decision heuristic.

Save to **memory** (`~/.claude/projects/.../memory/`) when the insight is a *fact
about the user or project* — a preference, a name, a constraint.

If unsure, ask.

## Step 3: Hand off to superpowers:writing-skills

Invoke `superpowers:writing-skills` and use it to write the SKILL.md.
