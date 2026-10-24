#!/usr/bin/env bash
# A config module with a fail-open secret fallback. The advisor has no Skill
# tool on the new arm; it must still reach the insecure-defaults rubric.
set -euo pipefail
WORK="${E2E_WORK:?}"
cat > "$WORK/config.py" <<'P'
import os

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret")
DEBUG = os.environ.get("DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")
P
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
