#!/usr/bin/env bash
# PASS: the job went through the broker's delegate and the reply carries the
# codename only the stub's Codex returns.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
delegated=no; broker_called ai_router_broker delegate && delegated=yes
relayed=no; grep -qi 'Marigold-7Q' "$RUN/final.txt" && relayed=yes
bash_router=no; grep -q 'ai-router' "$RUN/commands.txt" && bash_router=yes
facts="broker delegate=$delegated, codename relayed=$relayed, ai-router via Bash=$bash_router"
[ "$delegated$relayed" = yesyes ] && pass "$facts"
fail "$facts"
