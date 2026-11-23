#!/usr/bin/env bash
# PASS: the answer sends a Codex-only rendering change to the top-level codex/
# directory of the config repo claude-config.
#
# A no-tools Opus judge reads the final message and returns {repo, directory,
# quote} as schema-enforced enum fields (judge.py, _lib/structured_judge.py).
# Grading compares those fields; nothing here matches the answer's text.
# A judge failure is INCONCLUSIVE.
RUN="${1:?run dir}"
out=$(python3 -I "$(dirname "$0")/judge.py" "$RUN")
code=$?
echo "$out"
exit "$code"
