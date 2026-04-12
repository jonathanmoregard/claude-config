#!/bin/bash
input=$(tee ~/.claude/last-statusline.json)
cwd=$(echo "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)

if [[ "$cwd" == "$HOME/.claude" || "$cwd" == "$HOME/.claude/"* ]]; then
  echo "$input" | npx -y @owloops/claude-powerline@latest --theme=custom --config=~/.claude/.claude-powerline.json
else
  echo "$input" | npx -y @owloops/claude-powerline@latest --style=powerline
fi
