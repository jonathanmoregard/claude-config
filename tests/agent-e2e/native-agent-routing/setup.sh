#!/usr/bin/env bash
set -euo pipefail
WORK="${E2E_WORK:?}"
mkdir -p "$WORK/src"
printf 'hello\n' > "$WORK/src/message.txt"
printf 'The application greeting is stored in src/message.txt.\n' > "$WORK/README.md"
