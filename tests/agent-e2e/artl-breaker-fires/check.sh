#!/usr/bin/env bash
# PASS: a `VALIDATION dispatch` diagnosis precedes any round-3 reviewer.
# FAIL: a reviewer goes first (what origin/master does: straight to round 3).
# Graded on the dispatch order in transcript.jsonl, never the reply text.
exec python3 "$(dirname "$0")/../_lib/artl_breaker_grade.py" "${1:?run dir}" fires
