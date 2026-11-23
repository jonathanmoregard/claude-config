#!/usr/bin/env bash
# The close-out-review situation from artl-two-blind-reviewers, unchanged.
# Graded here: every reviewer prompt carries exclusions.md's entries.
set -euo pipefail
bash "$(dirname "$0")/../artl-two-blind-reviewers/setup.sh"
