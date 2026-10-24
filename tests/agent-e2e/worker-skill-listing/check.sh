#!/usr/bin/env bash
# Measures the worker's starting context. PASS: the worker's first call is
# under 20k input tokens (no skill listing). Also prints the main session's
# first-call size so both layers land in the summary.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
read -r main sub < <(python3 - "$RUN/transcript.jsonl" "$(dirname "$0")/../_lib" <<'PY'
import sys; sys.path.insert(0, sys.argv[2])
from skill_listing import main_first_context, subagent_first_context
print(main_first_context(sys.argv[1]), subagent_first_context(sys.argv[1]))
PY
)
[ "$sub" = None ] && { echo "INCONCLUSIVE: no subagent call (main=$main)"; exit 2; }
[ "$sub" -lt 20000 ] && pass "worker start ${sub} tok, main start ${main} tok"
fail "worker start ${sub} tok, main start ${main} tok"
