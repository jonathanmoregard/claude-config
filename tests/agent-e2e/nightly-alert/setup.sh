#!/usr/bin/env bash
# The nightly dependency check reports one open high alert on acme/widget; a
# patched version exists and no Dependabot PR is open.
source "$(dirname "$0")/../_lib/depcheck.sh" alert
