#!/usr/bin/env python3
"""
UserPromptSubmit hook: fires once per week when the evolve-reminder flag
is present. Cron sets the flag Monday morning; hook consumes it once.
"""
import sys
from pathlib import Path

FLAG = Path.home() / ".claude" / "homunculus" / ".evolve-reminder"

if FLAG.exists():
    FLAG.unlink()
    print(
        "Weekly instinct review due. "
        "Run /instinct-status to see learned patterns, then /evolve to promote them to skills/commands/agents."
    )
