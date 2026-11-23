#!/usr/bin/env bash
exec python3 "$(dirname "$0")/../_lib/night_proceed_grade.py" "${1:?run dir}" forcepush
