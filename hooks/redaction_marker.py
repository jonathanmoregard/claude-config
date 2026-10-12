"""The redaction marker's syntax, in one place (stdlib ``re`` only, cheap to import).

The output hooks write ``[REDACTED:<rule>:<n>chars]``; the aggregator's memory
search still returns the older ``[REDACTED:<rule>]``. Both stand in for a secret
the model never saw, so writing either into a file destroys that secret.
"""
import re

PREFIX = "[REDACTED:"
MARKER_RE = re.compile(r"\[REDACTED:[A-Za-z0-9_.\-]+(?::\d+chars)?\]")
