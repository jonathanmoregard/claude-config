#!/usr/bin/env bash
# Control: a PR-specific question stays under reporting-pr-state.
python3 "$(dirname "$0")/../_lib/orient_grade.py" merged "$1"
