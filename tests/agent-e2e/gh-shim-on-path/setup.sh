#!/usr/bin/env bash
# Is bin/agent/gh what `gh` resolves to in the agent's shell, an interpreter
# it starts, and a subagent's shell? A stub gh sits first on the harness PATH
# ($E2E_BIN), as a real gh would: only the SessionStart PATH hook can put the
# wrapper ahead of it. settings.json `env` cannot (it does not interpolate
# ${PATH}; measured).
source "$(dirname "$0")/../_lib/repo.sh" PUBLIC
