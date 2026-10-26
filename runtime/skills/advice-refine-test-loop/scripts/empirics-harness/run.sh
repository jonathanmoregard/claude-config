#!/usr/bin/env bash
# Empirics harness for advice-refine-test-loop — measures FP rate on real diffs.
# Not yet implemented; see ./README.md for design and how to add fixtures.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: run.sh (--diff <path> | --pr <ref>) [--pipeline baseline|candidate] [--out <dir>]

  --diff <path>       Path to a unified-diff file (fixture or ad-hoc).
  --pr <ref>          Git ref for the PR head (e.g. origin/pr/123 or a SHA).
  --pipeline <which>  Which arftl variant to run (default: candidate).
                      baseline  = pre-Stage-4.5 pipeline
                      candidate = current pipeline including Stage 4.5 + autofix_class
  --out <dir>         Output directory for findings JSON (default: /tmp/arftl-empirics-<ts>/).

Exit codes:
  0  findings written
  1  not yet implemented / usage error
  2  fixture/diff missing or unreadable

See README.md for how to add a labeled fixture and compute FP rate delta.
EOF
}

if [[ $# -eq 0 ]]; then
  usage
  exit 1
fi

# Minimal arg-shape validation so future callers get a useful error, not a silent skip.
case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
  --diff|--pr)
    : # accepted flag shape
    ;;
  *)
    echo "error: unrecognised argument: ${1}" >&2
    usage
    exit 1
    ;;
esac

echo "not yet implemented — see README.md" >&2
exit 1
