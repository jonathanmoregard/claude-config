#!/usr/bin/env bash
# The nightly dependency check reports Dependabot's weekly group PR #7 red: a
# bumped crate renamed a function the code calls.
source "$(dirname "$0")/../_lib/depcheck.sh" redpr
