#!/usr/bin/env bash
# Control. PASS: a round-3 reviewer before any `VALIDATION dispatch`. See _lib/artl_grade.py.
exec python3 "$(dirname "$0")/../_lib/artl_grade.py" "${1:?run dir}" holds
