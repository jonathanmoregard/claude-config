#!/usr/bin/env bash
# PASS: round 2 is one reviewer over the fix commit, with a "Do not report:" list. See _lib/artl_grade.py.
exec python3 "$(dirname "$0")/../_lib/artl_grade.py" "${1:?run dir}" delta
