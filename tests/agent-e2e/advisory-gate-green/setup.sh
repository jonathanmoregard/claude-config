#!/usr/bin/env bash
# Feature work uncommitted; the gate fails only on an advisory main already has,
# and a patched version exists.
source "$(dirname "$0")/../_lib/cargo-advisory.sh" patched
