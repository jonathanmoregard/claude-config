#!/usr/bin/env bash
# CI edits keep prompting under the new Repos allow: the ask rule on
# **/.github/workflows/** wins, and headless turns that prompt into a denial.
PERM_TOOLS=Write,Edit PERM_PATH=/home/jonathan/Repos/e2e-perm-probe/.github/workflows/ci.yml PERM_EXPECT=prompted
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
