#!/usr/bin/env bash
exec python3 "$(dirname "$0")/../_lib/depcheck_grade.py" "$1"
