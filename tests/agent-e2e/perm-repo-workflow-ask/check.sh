#!/usr/bin/env bash
# CI edits keep prompting inside an allow-listed checkout: the ask rule on
# ~/Repos/**/.github/workflows/** wins over the repo's Edit allow.
PERM_TOOLS=Write,Edit PERM_PATH=/home/jonathan/Repos/dependency-guard/.github/workflows/e2e-probe.yml PERM_EXPECT=prompted
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
