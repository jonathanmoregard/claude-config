#!/usr/bin/env bash
# A general "where are we" gets an orientation summary, not a PR rundown.
python3 "$(dirname "$0")/../_lib/orient_grade.py" orient "$1"
