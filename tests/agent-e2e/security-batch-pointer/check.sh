#!/usr/bin/env bash
# PASS: the answer routes the slow scanner to the nixos-config security batch
# runner module (services.securityBatch / modules/nixos/security-batch*), not a
# fresh ad-hoc timer.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
grep -qiE 'security-batch|securityBatch' "$RUN/final.txt" \
  && pass "points at the security-batch module" \
  || fail "no pointer to the security-batch module"
