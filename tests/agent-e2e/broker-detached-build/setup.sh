#!/usr/bin/env bash
# Sandbox on, a stub jobs_broker registered in both arms; the arms differ in
# whether their text and allow rules route a long build through it.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/../_lib/sandbox-on.sh"
source "$here/../_lib/broker-stub.sh" jobs_broker
cd "${E2E_WORK:?}"
git init -q -b main .
cp "$here/flake.nix" flake.nix
git -c user.name=e2e -c user.email=e2e@example.invalid add flake.nix
git -c user.name=e2e -c user.email=e2e@example.invalid commit -q -m init
