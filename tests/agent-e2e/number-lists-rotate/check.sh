#!/usr/bin/env bash
exec python3 "$(dirname "$(readlink -f "$0")")/../number-lists-lib/grade.py" "$1"
