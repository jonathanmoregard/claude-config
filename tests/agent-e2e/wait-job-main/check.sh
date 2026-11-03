#!/usr/bin/env bash
exec python3 "$(dirname "$0")/../_lib/wait_grade.py" "${1:?run dir}" main
