#!/usr/bin/env bash
# Empty workspace. caveman:caveman-help is set to name-only in the candidate
# config; the model must still find and invoke it by name.
set -euo pipefail
git -C "${E2E_WORK:?}" init -q
