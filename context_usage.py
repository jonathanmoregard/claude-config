#!/usr/bin/env python3
"""Shared context-occupancy math for Claude Code transcripts.

ONE implementation of "how full is this session's context window?", because
there are now three consumers and a fourth copy of the window-detection table
is how this stopped working the last time (the prefix list went stale and the
nudge reported 89% full during a session at 18% of a 1M window):

  * ``context-baggage.py``       UserPromptSubmit nudge at 70% — advisory, human-facing.
  * ``self-compact-trigger.py``  Stop-hook self-compact trigger at 30% — autonomous.
  * ``scripts/idle-handoff.sh``  re-measures a self-compact request against the
                                 transcript before it widens its idle gate, via
                                 the CLI below (the marker is a notification,
                                 the transcript is the truth).

Underscored module name (the rest of ~/.claude uses hyphens) because this file
is *imported*: ``import context-baggage`` is not expressible in Python.

CLI:
    python3 context_usage.py <transcript.jsonl>       -> JSON reading on stdout
    python3 context_usage.py --fill-pct <transcript>  -> bare float, exit 1 if
                                                         the reading is only an
                                                         estimate

Occupancy semantics (unchanged from the original context-baggage.py):
  * current occupancy is the LAST ``type=="assistant"`` line's
    ``message.usage.{input,cache_creation_input,cache_read_input}_tokens`` —
    a running total, not a sum over turns;
  * window size comes from ``message.model``: an explicit ``[1m]`` marker wins,
    then a prefix table, then an observed-usage fallback (any turn above the
    200k base means the session is really 1M), then 200k;
  * ``CLAUDE_CONTEXT_MAX`` overrides everything.

When a transcript carries no usage at all (fresh session, or a transcript
written by something that does not record usage) the fill is a byte-count
*guess*: ``estimated=True``. Advisory consumers may use it; anything that spends
money must not, which is why ``--fill-pct`` exits non-zero in that case.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, NamedTuple

BASE_MAX = 200_000
ONE_M = 1_000_000

# Claude Code model IDs that run at 1M context by default. Prefixes, so
# dated variants (claude-opus-4-1-20250805) match too.
ONE_M_MODEL_PREFIXES = (
    "claude-opus-4-",
    "claude-sonnet-4-",
    "claude-opus-5",
    "claude-sonnet-5",
)

# Explicit 1M marker in the model ID, e.g. `claude-opus-5[1m]`. Checked
# before the prefix list so it keeps working across model generations
# without another edit here — the prefix list going stale is exactly how
# this hook came to report 89% full during a session that was at 18% of a
# 1M window (every reading was fullness-of-200k).
ONE_M_MARKER = "[1m]"

# Rough bytes-per-token used only for the no-usage estimate.
BYTES_PER_TOKEN = 4


def detect_max_tokens(model: str | None, observed_max: int) -> int:
    """Context window in tokens.

    Priority: CLAUDE_CONTEXT_MAX -> `[1m]` marker -> model prefix ->
    observed usage above the base window -> 200k.
    """
    override = os.environ.get("CLAUDE_CONTEXT_MAX")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    if model:
        if ONE_M_MARKER in model.lower():
            return ONE_M
        if any(model.startswith(p) for p in ONE_M_MODEL_PREFIXES):
            return ONE_M
    if observed_max > BASE_MAX:
        return ONE_M
    return BASE_MAX


def usage_total(usage: dict[str, Any] | None) -> int:
    """Occupancy implied by one `message.usage` block."""
    if not usage:
        return 0
    return (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    )


class Scan(NamedTuple):
    user_messages: int
    last_usage: dict[str, Any] | None
    last_model: str | None
    observed_max: int
    file_size: int


def scan_transcript(path: str) -> Scan | None:
    """Single pass over a transcript. None when it cannot be read at all."""
    try:
        file_size = os.path.getsize(path)
    except OSError:
        return None

    user_messages = 0
    last_usage: dict[str, Any] | None = None
    last_model: str | None = None
    observed_max = 0

    try:
        handle = open(path, "r", errors="replace")
    except OSError:
        return None

    with handle as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            obj_type = obj.get("type")
            if obj_type == "user":
                user_messages += 1
            elif obj_type == "assistant":
                msg = obj.get("message") or {}
                if not isinstance(msg, dict):
                    continue
                model = msg.get("model")
                if model and model != "<synthetic>":
                    last_model = model
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    last_usage = usage
                    total = usage_total(usage)
                    if total > observed_max:
                        observed_max = total

    return Scan(user_messages, last_usage, last_model, observed_max, file_size)


class Occupancy(NamedTuple):
    total_tokens: int
    window: int
    fill_pct: float
    user_messages: int
    model: str | None
    estimated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "window": self.window,
            "fill_pct": self.fill_pct,
            "user_messages": self.user_messages,
            "model": self.model,
            "estimated": self.estimated,
        }


def occupancy(path: str) -> Occupancy | None:
    """Current context occupancy for a transcript. None when unreadable."""
    scan = scan_transcript(path)
    if scan is None:
        return None
    window = detect_max_tokens(scan.last_model, scan.observed_max)
    if scan.last_usage is not None:
        total = usage_total(scan.last_usage)
        return Occupancy(total, window, total / window, scan.user_messages,
                         scan.last_model, False)
    # No usage recorded anywhere: byte-count guess, explicitly flagged.
    total = scan.file_size // BYTES_PER_TOKEN
    return Occupancy(total, window, scan.file_size / (window * BYTES_PER_TOKEN),
                     scan.user_messages, scan.last_model, True)


def main(argv: list[str]) -> int:
    fill_only = False
    args = [a for a in argv[1:] if a != "--fill-pct"]
    if len(args) != len(argv[1:]):
        fill_only = True
    if len(args) != 1:
        print(f"usage: {os.path.basename(argv[0])} [--fill-pct] <transcript.jsonl>",
              file=sys.stderr)
        return 2

    occ = occupancy(args[0])
    if occ is None:
        print(f"context_usage: cannot read transcript {args[0]}", file=sys.stderr)
        return 1
    if fill_only:
        # Refuse to hand a guess to a caller that spends money on it.
        if occ.estimated:
            print("context_usage: refusing --fill-pct for an estimated reading "
                  "(transcript records no token usage)", file=sys.stderr)
            return 1
        print(f"{occ.fill_pct:.6f}")
        return 0
    print(json.dumps(occ.as_dict()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
