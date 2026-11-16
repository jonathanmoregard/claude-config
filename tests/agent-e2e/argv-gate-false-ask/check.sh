#!/usr/bin/env bash
exec python3 "$(dirname "$0")/../_lib/argv_gate_grade.py" false-ask "$1"
