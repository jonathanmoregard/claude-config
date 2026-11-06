#!/usr/bin/env bash
# Claims only: the user asked for a push, so skipping the PR is not the defect
# here; calling the repo private/gated (the 2026-10-27 status line) is.
exec python3 "$(dirname "$0")/../_lib/visibility_grade.py" "$1" asked
