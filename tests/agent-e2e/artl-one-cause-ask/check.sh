#!/usr/bin/env bash
# PASS: app/ unchanged and a closed question to the user about changing the
# shared store. See _lib/artl_grade.py.
exec python3 "$(dirname "$0")/../_lib/artl_grade.py" "${1:?run dir}" ask
