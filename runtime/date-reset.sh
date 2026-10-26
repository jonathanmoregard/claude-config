#!/usr/bin/env bash
# Clears the pending date suggestion so the reminder stops firing.
python3 - <<'EOF'
import json, os
path = os.path.expanduser("~/.local/state/claude-dates/state.json")
with open(path) as f:
    state = json.load(f)
state["pending_suggestion"] = None
with open(path, "w") as f:
    json.dump(state, f, indent=2)
EOF
