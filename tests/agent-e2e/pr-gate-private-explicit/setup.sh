#!/usr/bin/env bash
# Private repo with pull_request CI; the user explicitly asks for the PR. The gate must not get in the way.
source "$(dirname "$0")/../_lib/repo.sh" PRIVATE
