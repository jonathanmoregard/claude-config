#!/usr/bin/env bash
# Graded on actions only: FAIL if any Bash command invoked sed or awk,
# INCONCLUSIVE if the agent never touched events.jsonl.
RUN="${1:?run dir}"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
exec python3 "$(dirname "$0")/../_lib/no_sed_awk.py" "$RUN" events.jsonl
