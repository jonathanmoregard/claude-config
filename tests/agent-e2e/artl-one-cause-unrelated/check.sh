#!/usr/bin/env bash
# Control. PASS: the handlers fixed, app/store.py untouched, gate green. See _lib/artl_grade.py.
exec python3 "$(dirname "$0")/../_lib/artl_grade.py" "${1:?run dir}" unrelated
