#!/usr/bin/env bash
# A large transcript-shaped JSONL, big enough that reading it whole is unattractive.
set -euo pipefail
WORK="${E2E_WORK:?}"
python3 - "$WORK/session.jsonl" <<'PY'
import json, sys, random
random.seed(7)
with open(sys.argv[1], "w") as f:
    for i in range(4000):
        c = "routine step %d " % i + "x" * random.randint(200, 900)
        if i in (812, 1907, 3311):
            c = "Bash started in background with id qz81kd; " + c
        if i == 3312:
            c = "<task-notification><task-id>qz81kd</task-id><status>completed</status></task-notification>"
        f.write(json.dumps({"type": random.choice(["user", "assistant"]), "timestamp": "2026-11-20T10:%02d:%02dZ" % (i // 60 % 60, i % 60), "origin": "harness", "message": {"content": c}}) + "\n")
PY
