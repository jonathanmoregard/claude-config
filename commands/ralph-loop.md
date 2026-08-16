---
description: "Start a session-bound Ralph Loop in this session"
argument-hint: "PROMPT [--max-iterations N] [--completion-promise TEXT]"
allowed-tools: ["Bash(~/.claude/scripts/ralph-loop-setup.sh:*)"]
hide-from-slash-command-tool: "true"
---

# Ralph Loop (session-bound)

Execute the setup script to arm the loop for this session id:

```!
~/.claude/scripts/ralph-loop-setup.sh $ARGUMENTS
```

The gate is keyed to this session's id under `~/.claude/ralph-loop/`, not to the
current directory. It fires wherever this session stops — including after a `cd`
into another repo or worktree — and never fires in a sibling session.

Work on the task. When you try to exit, the Ralph loop feeds the SAME PROMPT
back to you. You'll see your previous work in files and git history, allowing
you to iterate and improve.

CRITICAL RULE: If a completion promise is set, you may ONLY output it when the
statement is completely and unequivocally TRUE. Do not output false promises to
escape the loop, even if you think you're stuck. To stop early, run
`/cancel-ralph`.
