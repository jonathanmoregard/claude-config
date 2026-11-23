#!/usr/bin/env bash
# A small TypeScript repo (the homelights fixture). The user mentions a change
# to make later; graded: it lands as its own decision file with front matter.
set -euo pipefail
bash "$(dirname "$0")/../sweep-domain-model/setup.sh"
