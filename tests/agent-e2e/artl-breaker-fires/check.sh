#!/usr/bin/env bash
# PASS: a `VALIDATION dispatch` before any round-3 reviewer. See _lib/artl_grade.py.
exec python3 "$(dirname "$0")/../_lib/artl_grade.py" "${1:?run dir}" fires
