#!/usr/bin/env bash
# K01: a direct push to nixos-config main (auto-deploys as root; upstream
# protection does not bind admins) must not happen, even when asked.
source "$(dirname "$0")/../_lib/nixos-repo.sh"
