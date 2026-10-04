"""Same-UID credential files must be covered by a `Read(...)` deny rule (K10).

Read rules also bind Grep (measured live 2026-10-03 on 2.1.288: with only the
Read rules below, Grep on each path came back "Permission to read ... denied").
This is defence in depth only — Bash can still `cat` the files — but it closes
the file tools, which nothing legitimately points at these paths: 0 of 22,329
Read/Grep/Glob calls in the last 30 days of transcripts did.

The check resolves each rule the way the CLI anchors it (`~/` = home,
`//` = filesystem root, `**` = any depth) and asks whether a concrete
credential path falls under it, so a typo in a rule fails here.
"""

import fnmatch
import json
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = "/home/jonathan"

CREDENTIAL_PATHS = [
    HOME + "/.claude/.credentials.json",
    HOME + "/.codex/auth.json",
    HOME + "/.google_workspace_mcp/credentials/token.json",
    "/run/agenix/gcal-ical-url",
]


def rule_globs(deny, tool="Read"):
    for rule in deny:
        if not (rule.startswith(tool + "(") and rule.endswith(")")):
            continue
        spec = rule[len(tool) + 1:-1]
        if spec.startswith("~/"):
            yield HOME + spec[1:]
        elif spec.startswith("//"):
            yield spec[1:]


def covered(path, globs):
    for g in globs:
        if fnmatch.fnmatchcase(path, g):
            return True
        if g.endswith("/**") and path.startswith(g[:-2]):
            return True
    return False


class CredentialReadDenyTest(unittest.TestCase):
    def test_each_credential_file_is_read_denied(self):
        with open(os.path.join(REPO_ROOT, "settings.json"), encoding="utf-8") as fh:
            deny = json.load(fh)["permissions"]["deny"]
        globs = list(rule_globs(deny))
        for path in CREDENTIAL_PATHS:
            with self.subTest(path=path):
                self.assertTrue(covered(path, globs), f"no Read deny rule covers {path}")


if __name__ == "__main__":
    unittest.main()
