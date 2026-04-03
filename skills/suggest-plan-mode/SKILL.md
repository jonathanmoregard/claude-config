---
name: suggest-plan-mode
description: >
  Analyses the current task and suggests switching to plan mode when signals indicate
  the task is large, risky, or architecturally complex. Use this skill when the user
  describes a migration, framework switch, architectural restructuring, refactor
  spanning many files, or any task where committing to an approach before exploring
  the codebase could lead to significant rework. Trigger on signals like: "migrate X
  to Y", "refactor across the codebase", "switch from X framework", "restructure",
  "move everything to", "change how we handle X everywhere", or mentions of 15+ files.
  Also trigger when the user seems about to start a large task without pausing to plan.
context: fork
allowed-tools: []
---

## Purpose

Analyse the current task description and determine whether it warrants plan mode
before execution. If so, explain why concisely and tell the user how to activate it.

## When to suggest plan mode

Suggest plan mode when you see one or more of these signals:

- **Scope signals**: mentions of 15+ files, "across the codebase", "everywhere", "all instances"
- **Migration signals**: switching frameworks, libraries, or languages ("migrate from X to Y", "replace X with Y throughout")
- **Architectural signals**: restructuring, splitting a monolith, changing how a system-wide concern is handled (auth, logging, error handling)
- **Uncertainty signals**: the user doesn't know what's affected ("not sure how many places", "need to find all the X")
- **Irreversibility signals**: changes that are hard to undo cleanly without a clear plan

Do NOT suggest plan mode for:
- Single-file fixes with a clear stack trace
- Adding a simple feature with known, limited scope
- Tasks where the user has already planned and is ready to execute
- Anything the user has already decided to do directly

## Output format

Be considered but brief. Structure your response as:

1. **What you noticed** — one or two specific signals from the task description that indicate complexity (not generic statements like "this seems large")
2. **Why plan mode helps here** — one concrete reason specific to this task (e.g. "understanding the full call graph before touching auth middleware prevents partial migrations that break sessions")
3. **How to activate it** — exact instructions:

---

To switch to plan mode before starting:
- **In the terminal**: restart with `claude --plan`
- **In the UI**: press `Shift+Tab` twice — Claude will confirm it's in plan mode

In plan mode, Claude explores and proposes before making any changes. You review and approve the plan before execution begins.

---

## Firing once per session

If plan mode has already been suggested or the user has acknowledged it and chosen to proceed directly, do not suggest it again. Check the conversation history — if a plan mode suggestion already appears, stay silent and exit.
