#!/usr/bin/env bash
exec python3 "$(dirname "$0")/../_lib/gh_shim_grade.py" false-ask "$1"
