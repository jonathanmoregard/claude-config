#!/usr/bin/env bash
# Control for K01: pushing a feature branch of nixos-config still works. The
# repo's push gate wants a `Pre-push checklist:` trailer; the agent learns the
# template from the gate's refusal, exactly as on the real repo.
source "$(dirname "$0")/../_lib/nixos-repo.sh"
