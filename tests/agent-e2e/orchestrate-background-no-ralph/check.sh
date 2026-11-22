#!/usr/bin/env bash
exec python3 "$(dirname "$0")/grade.py" "${1:?run dir}"
