---
name: avoiding-unrequested-flags
description: Use when about to put a new feature, code path, or scheduled job behind an on/off switch — an enabled/disabled key, a toggle registry entry, a use_*/feature_* switch, or an env-var kill switch. Fires hardest when the feature is expensive, automatic, unattended, or risky and shipping it switched off feels like the responsible move.
---

# Avoiding Unrequested Flags

## The Rule

Do not add a feature flag unless the user explicitly asks for one. Decide
whether the feature runs, and implement that.

## When To Apply

You are about to gate a new feature, code path, or scheduled job behind an
on/off switch.

Scope: switches that decide whether code runs. Thresholds, timeouts, caps and
CLI arguments are values — out of scope, leave them alone.

## How To Decide

1. Is the feature safe to run? → ship it running.
2. Is it unsafe to run? → do not ship it. Finish it, or leave it out of the branch.
3. Cannot tell? → ask the user which one it is.

Adding a key to a switch registry that already exists is adding a flag. Fixing
a switch that already ships is not.

## How To Check Yourself

Name who flips the switch, when they flip it, and what breaks if they never do.
If you cannot answer all three, remove the switch and apply the decision above.
