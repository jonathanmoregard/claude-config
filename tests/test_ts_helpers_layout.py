"""TypeScript helpers are `.mts`, so no package.json has to set a module type.

A package.json `"type"` scopes every file below it that has no closer
package.json. This tree is linked into `~/.claude` and served from a checkout
root, and both hold third-party CommonJS code with no package.json of its own
(the trailofbits plugin harnesses), which `"type": "module"` breaks with
`require is not defined in ES module scope`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                         text=True, check=True).stdout
    return out.splitlines()


def test_no_package_json_declares_a_module_scope():
    scoped = [path for path in tracked()
              if Path(path).name == "package.json"
              and json.loads((REPO / path).read_text()).get("type") == "module"]
    assert scoped == [], "name the helper .mts instead of scoping a directory"


def test_typescript_files_are_mts():
    # A .ts or .cts takes its module kind from the nearest package.json, and
    # tsconfig.json only includes .mts.
    loose = [path for path in tracked()
             if path.endswith((".ts", ".cts")) and not path.endswith(".d.ts")]
    assert loose == []
