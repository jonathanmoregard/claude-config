"""TypeScript helpers are `.mts`, so no package.json sets `"type": "module"`.

A package.json `"type"` scopes every file below it that has no closer
package.json. This tree is linked into `~/.claude` and served from a checkout
root, and both hold third-party CommonJS code with no package.json of its own
(the trailofbits plugin harnesses), which `"type": "module"` breaks with
`require is not defined in ES module scope`.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TSC = REPO / "node_modules/.bin/tsc"
_spec = importlib.util.spec_from_file_location(
    "helper_lang", REPO / "tests/agent-e2e/_lib/helper_lang.py")
helper_lang = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper_lang)


def tracked(root: Path = REPO) -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                         text=True, check=True).stdout
    return out.splitlines()


def unchecked_mts(root: Path) -> list[str]:
    if not TSC.exists():
        pytest.fail(f"{TSC} missing: run `npm ci --ignore-scripts` first")
    out = subprocess.run([str(TSC), "-p", "tsconfig.json", "--listFilesOnly"],
                         cwd=root, capture_output=True, text=True).stdout
    program = {Path(line).resolve() for line in out.splitlines()}
    return [path for path in tracked(root)
            if path.endswith(".mts") and (root / path).resolve() not in program]


def test_tsc_checks_every_tracked_mts():
    unchecked = unchecked_mts(REPO)
    assert unchecked == [], (
        "outside the tsc program: excluded by tsconfig.json, or a path TypeScript's"
        " include globs skip (a dot-prefixed name, node_modules, bower_components,"
        f" jspm_packages): {unchecked}")


def test_tsc_program_skips_dot_names_package_dirs_and_excludes(tmp_path):
    shutil.copy(REPO / "tsconfig.json", tmp_path)
    (tmp_path / "node_modules").symlink_to(REPO / "node_modules")
    checked = ["policy/a.mts", "docs/deep/b.mts"]
    skipped = ["runtime/bower_components/f.mts", "runtime/jspm_packages/g.mts",
               "runtime/w/.e.mts", "runtime/w/.profile/c.mts",
               "runtime/x/node_modules/h.mts", "vendor/i.mts"]
    for rel in checked + skipped:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("export {};\n")
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-f", "tsconfig.json", *checked, *skipped],
                   cwd=tmp_path, check=True)
    assert unchecked_mts(tmp_path) == skipped
    assert [rel for rel in checked + skipped if helper_lang.outside_tsc(rel)] == skipped


def test_no_package_json_sets_type_module():
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
