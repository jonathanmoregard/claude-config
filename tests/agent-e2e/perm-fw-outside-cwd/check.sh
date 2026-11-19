#!/usr/bin/env bash
# Firmware checkouts are editable only from a session started in them (their
# own .claude/settings.local.json). From anywhere else the edit must prompt:
# the next flash builds whatever is there.
PERM_TOOLS=Write,Edit PERM_PATH=/home/jonathan/Repos/qmk_firmware/E2E_NOTES.md PERM_EXPECT=prompted
source "$(dirname "$0")/../_lib/perm_scenario.sh" "$1"
