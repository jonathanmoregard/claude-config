#!/usr/bin/env bash
# Contrast for pr-links-after-history: the same five PRs, the same live state
# and the same final question, with the staged conversation replaced by one
# line of user-supplied context. No link appears anywhere in the prompt.
#
# It differs from its sibling in three things at once: the length of the
# history, whether links were given earlier, and whether earlier assistant
# turns used bare numbers. A gap between the two says "the history matters",
# not which of the three it is.
exec bash "$(dirname "$0")/../pr-links-after-history/setup.sh" "$@"
