#!/usr/bin/env bash
set -euo pipefail
NP_NOW=02:30 NP_LOCK=unlocked NP_FIXTURE=slugify bash "$(dirname "$0")/../_lib/night-proceed.sh"
