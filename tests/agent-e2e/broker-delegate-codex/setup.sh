#!/usr/bin/env bash
# Sandbox on, a stub ai_router_broker registered in both arms; the arms differ
# in whether their text and allow rules route a Codex job through it.
set -euo pipefail
lib="$(dirname "$0")/../_lib"
source "$lib/sandbox-on.sh"
source "$lib/broker-stub.sh" ai_router_broker
cd "${E2E_WORK:?}"
git init -q -b main .
printf '# release-notes\n\nNotes for the next release.\n' > README.md
git -c user.name=e2e -c user.email=e2e@example.invalid add README.md
git -c user.name=e2e -c user.email=e2e@example.invalid commit -q -m init
