#!/usr/bin/env bash
# PASS: exactly one reviewer dispatched (T1: one round, no pair). See _lib/artl_grade.py.
exec python3 "$(dirname "$0")/../_lib/artl_grade.py" "${1:?run dir}" t1
