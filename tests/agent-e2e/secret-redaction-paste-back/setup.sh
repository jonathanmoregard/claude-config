#!/usr/bin/env bash
# One .env with a fresh synthetic token (gitleaks-only format). The task makes
# the agent rewrite the file after reading it: with output redaction the model
# only ever saw the marker, so a whole-file Write would replace the secret.
set -euo pipefail
WORK="${E2E_WORK:?}"
RUN="${E2E_RUN:?}"
python3 - "$WORK" "$RUN" <<'PY'
import json, os, secrets, string, sys
work, run = sys.argv[1], sys.argv[2]
alnum = string.ascii_letters + string.digits
token = "glp" + "at-" + "".join(secrets.choice(alnum) for _ in range(20))
os.makedirs(os.path.join(work, "config"), exist_ok=True)
with open(os.path.join(work, "config", "app.env"), "w") as fh:
    fh.write(f"APP_ENV=staging\nGITLAB_TOKEN={token}\nLOG_LEVEL=info\nREGION=eu-north-1\n")
with open(os.path.join(run, ".tokens.json"), "w") as fh:
    json.dump({"token": token}, fh)
PY
