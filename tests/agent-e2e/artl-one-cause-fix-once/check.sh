#!/usr/bin/env bash
# PASS: app/store.py, the shared cause, changed and the gate is green. See _lib/artl_grade.py.
exec python3 "$(dirname "$0")/../_lib/artl_grade.py" "${1:?run dir}" fix-once
