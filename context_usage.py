#!/usr/bin/env python3
"""Shared context-occupancy math for Claude Code transcripts.

ONE implementation of "how full is this session's context window?", because
there are now three consumers and a fourth copy of the window-detection table
is how this stopped working the last time (the prefix list went stale and the
nudge reported 89% full during a session at 18% of a 1M window):

  * ``context-baggage.py``       UserPromptSubmit nudge at 90% — advisory, human-facing.
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
    200k base means the session is really 1M), then the 200k base as an
    explicitly-flagged ASSUMPTION;
  * ``CLAUDE_CONTEXT_MAX`` overrides the model window;
  * the result is capped at Claude Code's auto-compact window, which each
    entrypoint reads once (``auto_compact_window()``) and passes in.

Two independent ways a reading can be soft, both reported rather than hidden:
  * ``estimated=True``   — the NUMERATOR is a byte-count guess (the transcript
    records no usage at all: fresh session, or a writer that does not log it).
  * ``window_known=False`` — the DENOMINATOR is a guess: the model family is in
    neither prefix table and observed usage has not exceeded 200k, so the window
    is a lower bound. A 1M session measured against 200k reads ~5x high, which
    is exactly how `claude-fable-5` (this host's advisor model) was missing from
    the table and mis-measured.

Advisory consumers may use a soft reading; anything that spends money must not,
which is why ``--fill-pct`` exits non-zero for either case.
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Mapping, NamedTuple

BASE_MAX = 200_000
ONE_M = 1_000_000

# Claude Code model IDs that run at 1M context by default. Prefixes, so
# dated variants (claude-opus-4-1-20250805) match too.
#
# `claude-fable-5` was MISSING here while being the model the `advisor` agent
# runs on this host: a 1M fable session whose transcript carried no `[1m]`
# marker was measured against 200k and read ~5x high — precisely the stale-table
# failure this module was refactored to prevent. Any model family added to the
# fleet must be added here or to KNOWN_BASE_MODEL_PREFIXES; a family in neither
# is reported as an UNKNOWN window (see detect_window) instead of being silently
# assumed to be 200k.
ONE_M_MODEL_PREFIXES = (
    "claude-opus-4-",
    "claude-sonnet-4-",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-mythos-5",
)

# Families known to ship the 200k BASE window. Listing them explicitly is what
# makes the table's staleness self-evident: "known 200k" and "we have never
# heard of this model" stop being the same answer. Haiku is the live example —
# Haiku 4.5 is 200k, and there is no Haiku with a 1M window, so guessing it into
# the 1M list above would make every Haiku session read ~5x LOW instead.
# Only families that actually exist today are listed. A future model id belongs
# in neither table until someone checks its window: "unknown" is the honest
# answer, and it fails the expensive path off (see detect_window) instead of
# guessing a denominator in either direction.
KNOWN_BASE_MODEL_PREFIXES = (
    "claude-3-",
    "claude-haiku-4-",
)

# Explicit 1M marker in the model ID, e.g. `claude-opus-5[1m]`. Checked
# before the prefix list so it keeps working across model generations
# without another edit here — the prefix list going stale is exactly how
# this hook came to report 89% full during a session that was at 18% of a
# 1M window (every reading was fullness-of-200k).
ONE_M_MARKER = "[1m]"

# Rough bytes-per-token used only for the no-usage estimate.
BYTES_PER_TOKEN = 4

# Claude Code's auto-compact window: settings.json `autoCompactWindow`, or the
# CLAUDE_CODE_AUTO_COMPACT_WINDOW env var, which beats it. Claude Code compacts
# at min(model window, this) minus its own reserves, so a 1M session capped at
# 400k never gets near 1M — and "70% of 1M" would never be reached. The window
# every consumer here measures against is therefore min(model window, cap).
#
# Parsing mirrors Claude Code 2.1.310 (binary, verified by headless runs):
#   * the setting is an INTEGER in [100k, 1M]; anything else is silently dropped
#     (`"400k"` in settings.json is ignored, not parsed);
#   * the env var takes `400000`, `400k`, `1M`; out-of-range clamps to the
#     bounds, an unparseable value is ignored and the setting applies.
# Only the user-level settings.json is read: that is where this setup sets it.
# Per-model `modelSettings.<model>.autoCompactWindow` is not modelled.
AUTO_COMPACT_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
AUTO_COMPACT_MIN = 100_000
AUTO_COMPACT_MAX = 1_000_000


def user_settings_path() -> Path:
    """Claude Code's user settings file, where `autoCompactWindow` lives."""
    return Path.home() / ".claude" / "settings.json"


def _env_cap(raw: str) -> int | None:
    text = raw.strip()
    scale = {"k": 1_000, "m": 1_000_000}.get(text[-1:].lower())
    try:
        value = float(text[:-1]) * scale if scale else int(text)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 1:  # NaN, inf or non-positive: unusable
        return None
    return min(AUTO_COMPACT_MAX, max(AUTO_COMPACT_MIN, round(value)))


def auto_compact_window(settings_path: Path, environ: Mapping[str, str]) -> int | None:
    """The configured auto-compact cap in tokens, or None when none applies.

    Called once by each entrypoint (hook or CLI) with its own settings path and
    environment; the result is passed down as a parameter, never stored.
    """
    raw = environ.get(AUTO_COMPACT_ENV)
    if raw is not None:
        cap = _env_cap(raw)
        if cap is not None:
            return cap
    try:
        value = json.loads(Path(settings_path).read_text()).get("autoCompactWindow")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if AUTO_COMPACT_MIN <= value <= AUTO_COMPACT_MAX else None


class Window(NamedTuple):
    size: int
    source: str
    known: bool


def detect_window(model: str | None, observed_max: int,
                  auto_compact_window: int | None = None) -> Window:
    """Context window in tokens, plus WHERE that number came from.

    Priority: CLAUDE_CONTEXT_MAX -> `[1m]` marker -> model prefix table ->
    observed usage above the base window -> the base window as an ASSUMPTION.
    Then capped at ``auto_compact_window`` (see auto_compact_window()), the
    point past which Claude Code will not let the context grow.

    ``known`` is False only for that assumption: a model family in neither prefix
    table, whose observed usage has not yet exceeded 200k. The size is then a
    lower bound, not a fact — a 1M session reads 5x high against it. Consumers
    that spend money on the reading must refuse it (self-compact-trigger.py
    does); advisory consumers may use it and say so. A cap BELOW that lower
    bound is known regardless: the real window is at least the bound, so the
    cap is what binds.
    """
    win = _model_window(model, observed_max)
    if auto_compact_window is not None and auto_compact_window < win.size:
        return Window(auto_compact_window, "auto_compact_window", True)
    return win


def _model_window(model: str | None, observed_max: int) -> Window:
    override = os.environ.get("CLAUDE_CONTEXT_MAX")
    if override:
        try:
            # Positive only: a 0 or negative override would make every consumer
            # divide by it.
            parsed = int(override)
            if parsed > 0:
                return Window(parsed, "env_override", True)
        except ValueError:
            pass
    if model:
        if ONE_M_MARKER in model.lower():
            return Window(ONE_M, "marker", True)
        if any(model.startswith(p) for p in ONE_M_MODEL_PREFIXES):
            return Window(ONE_M, "prefix_1m", True)
    if observed_max > BASE_MAX:
        # Evidence beats BOTH tables, so this is checked before the known-base
        # list: a turn above the base window proves the session is not running at
        # 200k, whatever the model is called. (Ordering the base table first
        # would make a mislabelled 1M session read 5x high while claiming the
        # window was known.)
        return Window(ONE_M, "observed_above_base", True)
    if model:
        if any(model.startswith(p) for p in KNOWN_BASE_MODEL_PREFIXES):
            return Window(BASE_MAX, "prefix_base", True)
        return Window(BASE_MAX, "assumed_base_unknown_model", False)
    # No model recorded at all (empty or synthetic-only transcript). Same
    # assumption, same honesty about it.
    return Window(BASE_MAX, "assumed_base_no_model", False)


def detect_max_tokens(model: str | None, observed_max: int) -> int:
    """Back-compatible size-only accessor. Prefer detect_window()."""
    return detect_window(model, observed_max).size


# Keys that make up one turn's occupancy. Order is irrelevant; the sum is.
_USAGE_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def usage_total(usage: dict[str, Any] | None) -> int:
    """Occupancy implied by one `message.usage` block.

    Every value is coerced rather than trusted. A transcript line carrying
    ``"input_tokens": null`` (or a string, or a dict) used to raise TypeError
    here, and ``context-baggage.py`` calls occupancy() with no try/except — so a
    single malformed line made the UserPromptSubmit hook traceback and exit 1,
    taking the 90% nudge down with it. A non-numeric value contributes nothing;
    the rest of the block still counts.
    """
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in _USAGE_KEYS:
        value = usage.get(key)
        # bool is an int subclass; True would count as 1 token. Reject it.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        total += int(value)
    return total


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
    window_source: str = "unknown"
    window_known: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "window": self.window,
            "fill_pct": self.fill_pct,
            "user_messages": self.user_messages,
            "model": self.model,
            "estimated": self.estimated,
            # Two independent qualifiers, both reported so no consumer has to
            # guess: `estimated` = we do not know the NUMERATOR (no recorded
            # usage), `window_known=False` = we do not know the DENOMINATOR
            # (model family absent from both prefix tables).
            "window_source": self.window_source,
            "window_known": self.window_known,
        }


def occupancy(path: str, auto_compact_window: int | None = None) -> Occupancy | None:
    """Current context occupancy for a transcript. None when unreadable.

    ``auto_compact_window`` is the configured cap (auto_compact_window()); the
    window and fill are measured against min(model window, cap).
    """
    scan = scan_transcript(path)
    if scan is None:
        return None
    win = detect_window(scan.last_model, scan.observed_max, auto_compact_window)
    if scan.last_usage is not None:
        total = usage_total(scan.last_usage)
        return Occupancy(total, win.size, total / win.size, scan.user_messages,
                         scan.last_model, False, win.source, win.known)
    # No usage recorded anywhere: byte-count guess, explicitly flagged.
    total = scan.file_size // BYTES_PER_TOKEN
    return Occupancy(total, win.size,
                     scan.file_size / (win.size * BYTES_PER_TOKEN),
                     scan.user_messages, scan.last_model, True,
                     win.source, win.known)


def main(argv: list[str]) -> int:
    fill_only = "--fill-pct" in argv[1:]
    tokens_only = "--tokens" in argv[1:]
    args = [a for a in argv[1:] if a not in ("--fill-pct", "--tokens")]
    if len(args) != 1 or (fill_only and tokens_only):
        print(f"usage: {os.path.basename(argv[0])} [--fill-pct|--tokens] <transcript.jsonl>",
              file=sys.stderr)
        return 2

    if tokens_only:
        # The ABSOLUTE occupancy, with no denominator involved.
        #
        # self-compact fires on this rather than on a percentage. A percentage
        # needs a window to divide by, and the window is the least trustworthy
        # number here: it comes from a model-name prefix table that goes stale,
        # or from CLAUDE_CONTEXT_MAX, which is a display override that had no
        # business deciding when a session resets itself. Tokens are measured
        # from the transcript's own recorded usage and cannot be talked into
        # being something else.
        occ = occupancy(args[0])
        if occ is None:
            print(f"context_usage: cannot read transcript {args[0]}", file=sys.stderr)
            return 1
        if occ.estimated:
            # The NUMERATOR is a guess: no recorded usage, only a byte count.
            # Still refused, because spending on a guess is the thing to avoid.
            print("context_usage: refusing --tokens for an estimated reading "
                  "(transcript records no token usage)", file=sys.stderr)
            return 1
        print(occ.total_tokens)
        return 0

    occ = occupancy(args[0], auto_compact_window(user_settings_path(), os.environ))
    if occ is None:
        print(f"context_usage: cannot read transcript {args[0]}", file=sys.stderr)
        return 1
    if fill_only:
        # Refuse to hand a guess to a caller that spends money on it — whether
        # the guess is in the numerator (no recorded usage) or the denominator
        # (model family in neither prefix table, so the window is an assumption
        # a 1M session would read 5x high against).
        if occ.estimated:
            print("context_usage: refusing --fill-pct for an estimated reading "
                  "(transcript records no token usage)", file=sys.stderr)
            return 1
        if not occ.window_known:
            print("context_usage: refusing --fill-pct for an unknown context "
                  f"window (model={occ.model!r}, window_source="
                  f"{occ.window_source}); add the family to "
                  "ONE_M_MODEL_PREFIXES or KNOWN_BASE_MODEL_PREFIXES",
                  file=sys.stderr)
            return 1
        print(f"{occ.fill_pct:.6f}")
        return 0
    print(json.dumps(occ.as_dict()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
