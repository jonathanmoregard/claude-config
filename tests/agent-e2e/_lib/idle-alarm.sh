#!/usr/bin/env bash
# Shared setup for the idle-alarm-* scenarios: a notify-send stub that logs to
# RUN/notify.log, a short idle limit (WAIT_FOR_AGENT_IDLE, set through the
# staged settings.json `env`, which the session and its Bash calls inherit).
# Each scenario also needs an empty `persist-session` marker file: without it
# subagent transcripts never land under ~/.claude/projects and wait-for exits 3.
set -euo pipefail
WORK="${E2E_WORK:?}" RUN="${E2E_RUN:?}" BIN="${E2E_BIN:?}"
IDLE="${IDLE_ALARM_IDLE:-60}"
cat > "$BIN/notify-send" <<STUB
#!/bin/sh
echo "\$*" >> "$RUN/notify.log"
STUB
chmod +x "$BIN/notify-send"
python3 - "$HOME/.claude/settings.json" "$IDLE" <<'PY'
import json, sys
path, idle = sys.argv[1:]
s = json.load(open(path))
s.setdefault("env", {})["WAIT_FOR_AGENT_IDLE"] = idle
json.dump(s, open(path, "w"), indent=2)
PY
