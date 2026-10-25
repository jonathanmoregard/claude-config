#!/usr/bin/env bash
exec python3 "$(dirname "$0")/../_lib/advisory_grade.py" "$1" nopatch
