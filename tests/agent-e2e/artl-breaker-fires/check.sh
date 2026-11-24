#!/usr/bin/env bash
# PASS: a `VALIDATION dispatch` diagnosis precedes any other dispatch but a
# worker or a CONSULT. FAIL: such a dispatch, a round-3 reviewer, goes first
# (what origin/master does: straight to round 3), or nothing is dispatched
# (patched on inline, or did not act). Graded on the dispatch order in
# transcript.jsonl and the work tree, never the reply text.
exec python3 "$(dirname "$0")/../_lib/artl_breaker_grade.py" "${1:?run dir}" fires
