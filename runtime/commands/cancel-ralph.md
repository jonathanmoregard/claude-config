---
description: "Cancel the Ralph Loop armed in this session"
argument-hint: "[session_id | --all]"
allowed-tools: ["Bash(~/.claude/scripts/ralph-loop-cancel.sh:*)"]
hide-from-slash-command-tool: "true"
---

# Cancel Ralph

```!
~/.claude/scripts/ralph-loop-cancel.sh $ARGUMENTS
```

Report the script's result to the user verbatim. No argument cancels this
session's loop; pass a session id to cancel another session's, or `--all`.
