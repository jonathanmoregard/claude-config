#!/usr/bin/env bash
# The nightly dependency check still reports the high alert on acme/widget, but
# Dependabot's security-update PR #12 already fixes it and is green, waiting
# for the merge click.
source "$(dirname "$0")/../_lib/depcheck.sh" alertpr
