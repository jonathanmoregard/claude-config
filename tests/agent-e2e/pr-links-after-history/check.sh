#!/usr/bin/env bash
# PASS: every open PR the reply hands back carries its own full URL, under its
# own repo. FAIL: any open PR named without one, or a merge ask that links
# nothing ("links above"). INCONCLUSIVE: the reply handed no PR back.
# Grader shared with pr-links-no-history; see _lib/prlinks_grade.py.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
python3 "$(dirname "$0")/../_lib/prlinks_grade.py" "$RUN/final.txt"
