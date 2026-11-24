"""Home-path file rules must keep working when Claude runs with another HOME.

`~/` in a rule follows the HOME the CLI was started with; `//home/jonathan/`
does not. Under another HOME (a CI runner's /home/runner, the e2e harness
without bubblewrap, a wrapper, sudo) each form alone breaks something:

- a deny or ask written only as `~/` stops guarding the real file, and one
  written only as `//home/jonathan/` guards nothing under the other HOME;
- an allow written only as `//home/jonathan/` matches nothing under the other
  HOME and turns into a prompt (the e2e agent could not read its own skills in
  CI), and one written only as `~/` drops the real path: a link in the staged
  HOME into the real ~/Repos needs its target allowed too (review r2).

So every home-path Read(...) and Edit(...) rule, in every list, carries both
forms. Only those two carry paths the CLI consults; Write, Grep and Glob path
rules are accepted and ignored, so they are out of scope here.
"""

import json
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.join(REPO_ROOT, "runtime", "settings.json")
ABS = "//home/jonathan/"
TILDE = "~/"
RULE = re.compile(r"^(Read|Edit)\((.*)\)$")


def twin(rule):
    m = RULE.match(rule)
    if not m:
        return None
    tool, spec = m.groups()
    if spec.startswith(ABS):
        return f"{tool}({TILDE}{spec[len(ABS):]})"
    if spec.startswith(TILDE):
        return f"{tool}({ABS}{spec[len(TILDE):]})"
    return None


def load_permissions():
    with open(SETTINGS, encoding="utf-8") as fh:
        return json.load(fh)["permissions"]


class PermissionAnchorTest(unittest.TestCase):
    def test_twin_maps_both_directions(self):
        self.assertEqual(twin("Read(~/.ssh/**)"), "Read(//home/jonathan/.ssh/**)")
        self.assertEqual(twin("Edit(//home/jonathan/x)"), "Edit(~/x)")
        self.assertIsNone(twin("Read(//etc/nixos/**)"))
        self.assertIsNone(twin("Grep(~/.config/Claude/**)"))

    def test_every_home_rule_carries_both_anchors(self):
        perms = load_permissions()
        checked = 0
        for kind in ("allow", "deny", "ask"):
            rules = set(perms[kind])
            for rule in perms[kind]:
                other = twin(rule)
                if other is None:
                    continue
                checked += 1
                with self.subTest(kind=kind, rule=rule):
                    self.assertIn(other, rules, f"{kind} rule {rule} has no {other} twin")
        self.assertGreater(checked, 0, "no home-path rule found: is the anchor list stale?")


if __name__ == "__main__":
    unittest.main()
