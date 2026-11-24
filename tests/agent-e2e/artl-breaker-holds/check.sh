#!/usr/bin/env bash
# Control. PASS: a round-3 reviewer is dispatched before any `VALIDATION
# dispatch`; expected on both arms. FAIL: the breaker intercepted a converging
# loop, or nothing is dispatched but a tracked file changed (no round 3).
# Graded on the dispatch order in transcript.jsonl, never the reply text.
exec python3 "$(dirname "$0")/../_lib/artl_breaker_grade.py" "${1:?run dir}" holds
