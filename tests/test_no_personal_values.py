#!/usr/bin/env python3
"""The tracked tree carries no personal contact or network values.

Personal email addresses, tailnet addresses, and location or partner details
live in untracked local config under ~/.config/; tracked files reference those
files, never the values. A `sync-mcp-servers.sh export` of a stale live
registration is one way such a value comes back, so this guards the whole tree
rather than single files.

Values with no recognisable shape (a place name, coordinates) are read from
the local config itself; where that config is absent (CI) the check skips.
"""

from __future__ import annotations

from pathlib import Path
import json
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]

PATTERNS = {
    "hotmail address": re.compile(r"[\w.+-]+@hotmail[.]\w+", re.IGNORECASE),
    "klaffat.com address": re.compile(r"[\w.+-]+@klaffat[.]com", re.IGNORECASE),
    # A host in 100.64.0.0/10, the carrier-grade NAT range tailnets assign
    # from. Range notation (a trailing /prefix) documents, it does not address.
    "tailnet address": re.compile(
        r"(?<![\d.])100[.](?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])[.]\d{1,3}[.]\d{1,3}(?![\d./])"
    ),
}


# Local config file -> keys whose values must never appear in tracked files.
LOCAL_CONFIG_KEYS = {
    Path.home() / ".config" / "sunset-walk" / "config.json": ("place", "lat", "lon"),
    Path.home() / ".config" / "claude-dates" / "config.json": (
        "partner_name",
        "city",
        "city_lat",
        "city_lon",
    ),
}


def local_config_patterns() -> dict[str, re.Pattern[str]]:
    patterns: dict[str, re.Pattern[str]] = {}
    for config, keys in LOCAL_CONFIG_KEYS.items():
        if not config.is_file():
            continue
        values = json.loads(config.read_text(encoding="utf-8"))
        for key in keys:
            value = str(values.get(key, "")).strip()
            if value:
                label = f"{config.parent.name} {key}"
                patterns[label] = re.compile(
                    rf"(?<![\w.]){re.escape(value)}(?![\w])", re.IGNORECASE
                )
    return patterns


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    paths = [ROOT / name for name in out.decode("utf-8").split("\0") if name]
    # Symlinks and submodules belong to other repos; they answer for themselves.
    return [p for p in paths if p.is_file() and not p.is_symlink()]


class NoPersonalValuesTests(unittest.TestCase):
    def test_patterns_detect_their_value_class(self) -> None:
        # Assembled at runtime so this file never holds a literal match itself.
        samples = {
            "hotmail address": "someone@" + "hotmail.com",
            "klaffat.com address": "someone@" + "klaffat.com",
            "tailnet address": "host," + ".".join(["100", "101", "102", "103"]) + " ssh-ed25519",
        }
        for label, sample in samples.items():
            with self.subTest(label=label):
                self.assertRegex(sample, PATTERNS[label])
        for benign in ("192.0.2.10 and 10.0.100.64", "the 100.64.0.0/10 range"):
            with self.subTest(benign=benign):
                self.assertNotRegex(benign, PATTERNS["tailnet address"])

    def test_tracked_files_carry_no_personal_values(self) -> None:
        self.assert_no_hits(PATTERNS)

    def test_tracked_files_carry_no_local_config_values(self) -> None:
        patterns = local_config_patterns()
        if not patterns:
            self.skipTest("no local personal config on this machine")
        self.assert_no_hits(patterns)

    def assert_no_hits(self, patterns: dict[str, re.Pattern[str]]) -> None:
        hits: list[str] = []
        for path in tracked_files():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for label, pattern in patterns.items():
                for lineno, line in enumerate(text.splitlines(), 1):
                    if pattern.search(line):
                        # Name the location, never echo the value itself.
                        hits.append(f"{path.relative_to(ROOT)}:{lineno}: {label}")
        self.assertEqual(hits, [], "personal values in tracked files:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
