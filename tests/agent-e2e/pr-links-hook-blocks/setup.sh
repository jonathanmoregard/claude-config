#!/usr/bin/env bash
# Same fixture as pr-links-after-history (four open PRs across two repos, one
# merged), so the shared grader applies unchanged. What differs is the final
# question: this one invites bare numbers instead of a status report, which is
# the only shape in which the lapse was ever measured.
exec bash "$(dirname "$0")/../pr-links-after-history/setup.sh" "$@"
