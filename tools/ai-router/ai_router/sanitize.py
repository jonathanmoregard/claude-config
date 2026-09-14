"""Terminal-escape sanitising for provider output (contract §Jobs, sanitize.py).

Everything ``peek``, ``log`` and ``result`` print passes through ``clean``:
ANSI CSI / OSC / C1 escapes are stripped and each line is capped at
``MAX_LINE`` characters so a hostile or runaway provider cannot drive the
caller's terminal.
"""

from __future__ import annotations

import re

MAX_LINE = 4000

# CSI: ESC [ params… final byte.  OSC: ESC ] … (BEL | ESC \).  Other two-byte
# ESC sequences.  C1 control bytes 0x80–0x9F when they arrive as code points.
_CSI = r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
_OSC = r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"
_ESC2 = r"\x1b[@-Z\\-_]"
_C1 = r"[\x80-\x9f]"
_ESCAPES = re.compile(f"{_CSI}|{_OSC}|{_ESC2}|{_C1}")
# Other C0 controls except tab/newline/carriage return.
_C0 = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_escapes(text: str) -> str:
    """Remove ANSI/OSC/C1 escape sequences and stray control bytes."""
    return _C0.sub("", _ESCAPES.sub("", text))


def cap_line(line: str, limit: int = MAX_LINE) -> str:
    """Truncate one line to ``limit`` characters, marking the cut."""
    if len(line) <= limit:
        return line
    return line[: limit - 1] + "…"


def clean(text: str, limit: int = MAX_LINE) -> str:
    """Strip escapes and cap every line; the only path to a caller's stdout."""
    return "\n".join(cap_line(line, limit) for line in strip_escapes(text).split("\n"))
