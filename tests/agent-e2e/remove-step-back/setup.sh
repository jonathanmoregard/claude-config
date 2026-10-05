#!/usr/bin/env bash
set -euo pipefail
WORK="${E2E_WORK:?}"
RUN="${E2E_RUN:?}"
BIN="${E2E_BIN:?}"

cat > "$BIN/claude" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${E2E_RUN:?}/step-back-claude.log"
printf 'SKIP\n'
STUB
chmod +x "$BIN/claude"

cat > "$WORK/probe.py" <<'PY'
import json
import os
import subprocess
import time
from pathlib import Path

home = Path.home()
run = Path(os.environ["E2E_RUN"])
settings = json.loads((home / ".claude" / "settings.json").read_text())
commands = []
for group in settings.get("hooks", {}).get("Stop", []):
    for hook in group.get("hooks", []):
        command = hook.get("command", "")
        if "step-back" in command:
            commands.append(command)

transcript = run / "four-turns.jsonl"
rows = []
for index in range(4):
    rows.append({"type": "user", "message": {"content": f"request {index}"}})
    rows.append({"type": "assistant", "message": {"content": f"response {index}"}})
transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
payload = json.dumps({
    "session_id": "remove-step-back-e2e",
    "transcript_path": str(transcript),
})
for command in commands:
    subprocess.run(
        ["bash", "-c", command],
        input=payload,
        text=True,
        env=os.environ,
        check=True,
    )

call_log = run / "step-back-claude.log"
deadline = time.monotonic() + 5
while commands and not call_log.exists() and time.monotonic() < deadline:
    time.sleep(0.05)
calls = len(call_log.read_text().splitlines()) if call_log.exists() else 0
result = f"hooks={len(commands)} calls={calls}"
(run / "probe-result.txt").write_text(result + "\n")
print(result)
PY
