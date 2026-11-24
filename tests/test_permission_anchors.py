"""Home-path file rules are written once, as `~/`.

`~/` follows the HOME the CLI started with, the way AppArmor's @{HOME},
systemd's %h and Flatpak's ~/ do. A rule pinned to `//home/jonathan/` matches
nothing when HOME is elsewhere: in CI (HOME=/home/runner) the e2e agent was
prompted, so denied headless, reading its own skills. A HOME somewhere else is
another environment, and keeping the real home out of it is the sandbox's job:
the e2e harness runs only under bubblewrap, which hides the real home.

Only Read(...) and Edit(...) carry paths the CLI consults; Write, Grep and Glob
path rules are accepted and ignored, so they are out of scope here.
"""

import json
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.join(REPO_ROOT, "runtime", "settings.json")
RULE = re.compile(r"^(Read|Edit)\((.*)\)$")


class PermissionAnchorTest(unittest.TestCase):
    def test_home_rules_use_tilde(self):
        with open(SETTINGS, encoding="utf-8") as fh:
            perms = json.load(fh)["permissions"]
        for kind in ("allow", "deny", "ask"):
            for rule in perms[kind]:
                m = RULE.match(rule)
                if not m:
                    continue
                with self.subTest(kind=kind, rule=rule):
                    self.assertFalse(m.group(2).startswith("//home/"),
                                     f"{kind} {rule} is pinned to one home; write it as ~/")


if __name__ == "__main__":
    unittest.main()
