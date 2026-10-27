#!/usr/bin/env bash
exec python3 "$(dirname "$0")/../_lib/visibility_grade.py" "$1" hookfact

