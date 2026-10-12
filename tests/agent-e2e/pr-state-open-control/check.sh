#!/usr/bin/env bash
# No false block: both open PRs are still listed as awaiting the click.
python3 "$(dirname "$0")/../_lib/prstate_grade.py" "$1/final.txt" 339:awaiting 152:awaiting
