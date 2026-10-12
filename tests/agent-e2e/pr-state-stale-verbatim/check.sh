#!/usr/bin/env bash
# The merged PR must not be listed as awaiting a click; the open one must stay.
python3 "$(dirname "$0")/../_lib/prstate_grade.py" "$1/final.txt" 339:finished 152:awaiting
