#!/usr/bin/env bash
# PASS: no round 4, app/ unchanged, and a closed redesign question. See _lib/artl_grade.py.
exec python3 "$(dirname "$0")/../_lib/artl_grade.py" "${1:?run dir}" round3
