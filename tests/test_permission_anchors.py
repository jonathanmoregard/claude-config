"""Home-anchored file rules must keep working when Claude runs with another HOME.

`~/` in a rule follows the HOME the CLI was started with; `//home/jonathan/`
does not. The two fail in opposite directions when HOME is something else
(a CI runner's /home/runner, a wrapper, sudo):

- a deny or ask written only as `~/` stops guarding the real file, and one
  written only as `//home/jonathan/` guards nothing under the other HOME, so
  each needs both forms: the real path stays closed whatever HOME is, and a
  test run under another HOME still exercises the rule;
- an allow written as `//home/jonathan/` matches nothing under another HOME and
  turns into a prompt, while an allow under `~/` only ever opens a read or edit
  inside the HOME the operator chose. Allows use `~/` alone.

Only Read(...) and Edit(...) carry paths the CLI consults; other path rules
(Write, Grep, Glob) are accepted and ignored, so they are out of scope here.
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

    def test_home_denies_and_asks_carry_both_anchors(self):
        perms = load_permissions()
        for kind in ("deny", "ask"):
            rules = set(perms[kind])
            for rule in perms[kind]:
                other = twin(rule)
                if other is None:
                    continue
                with self.subTest(kind=kind, rule=rule):
                    self.assertIn(other, rules, f"{kind} rule {rule} has no {other} twin")

    def test_home_allows_use_tilde_only(self):
        for rule in load_permissions()["allow"]:
            m = RULE.match(rule)
            if not m:
                continue
            with self.subTest(rule=rule):
                self.assertFalse(
                    m.group(2).startswith(ABS),
                    f"allow {rule} is pinned to /home/jonathan; write it as ~/",
                )


if __name__ == "__main__":
    unittest.main()
