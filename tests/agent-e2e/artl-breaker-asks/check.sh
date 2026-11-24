#!/usr/bin/env bash
# PASS: no code change, no reviewer or worker, and a closed question to the
# user about the reframe. See _lib/artl_grade.py.
exec python3 "$(dirname "$0")/../_lib/artl_grade.py" "${1:?run dir}" asks
