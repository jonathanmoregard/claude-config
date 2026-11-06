#!/usr/bin/env bash
# Auto-approve Edit/Write permission requests for files under ~/.claude/
# Only fires when Claude is started in ~/.claude (project settings scope).
input=$(cat)
path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // .tool_input.path // empty')
if [[ "$path" == /home/jonathan/.claude/* || "$path" == ~/.claude/* ]]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}'
fi
