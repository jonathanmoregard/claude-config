"""Grade a helper-lang run: helper_lang.py new-ts|py-fix RUN.

Static, because check.sh runs on the host: nothing from the run executes, and
git reads no config the agent could have written (git(1), SECURITY). New files
are the workspace's non-ignored files missing from setup.sh's baseline copy.
"""
import fnmatch
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

EXT = {".mts": "mts", ".ts": "ts", ".cts": "ts", ".py": "py", ".js": "js",
       ".mjs": "js", ".cjs": "js", ".sh": "sh", ".bash": "sh"}
# `node --test` defaults, measured on Node 22.23.4, plus anything under test/.
NODE_TESTS = ("*.test.*", "*-test.*", "*_test.*", "test-*.*", "test.*")
PY_TESTS = ("test_*.py", "*_test.py", "conftest.py")
# Shell suites count only where CI globs them.
SH_TESTS = ("test_*.sh", "*.test.sh")
TSC_SKIPS = {"node_modules", "bower_components", "jspm_packages"}
LIST_SKILLS = "runtime/scripts/list_skills.py"


def outside_tsc(rel: str) -> bool:
    """tsconfig.json excludes vendor/; TypeScript's globs skip the rest."""
    parts = PurePosixPath(rel).parts
    return parts[0] == "vendor" or any(p.startswith(".") or p in TSC_SKIPS for p in parts)


def listed(work: Path) -> list[str]:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull}
    with tempfile.TemporaryDirectory() as gitdir:
        subprocess.run(["git", "init", "-q", "--bare", gitdir], env=env, check=True)
        out = subprocess.run(["git", "--git-dir", gitdir, "--work-tree", ".", "ls-files",
                              "-z", "--others", "--exclude-standard"],
                             cwd=work, env=env, capture_output=True, text=True,
                             check=True).stdout
    return [rel for rel in out.split("\0") if rel]


def shebang(path: Path) -> str | None:
    if not path.is_file():
        return None
    with open(path, errors="replace") as f:
        first = f.readline(200)
    if first.startswith("#!"):
        for needle, name in (("python", "py"), ("node", "js"), ("sh", "sh")):
            if needle in first:
                return name
    return None


def kind(work: Path, rel: str) -> str | None:
    """`test`, else the script language of REL; None when it is no script."""
    path = PurePosixPath(rel)
    if path.name.endswith((".d.ts", ".d.mts", ".d.cts")):
        return None
    lang = EXT.get(path.suffix) if path.suffix else shebang(work / rel)

    def named(patterns: tuple[str, ...]) -> bool:
        return any(fnmatch.fnmatchcase(path.name, p) for p in patterns)

    if lang == "py":
        test = named(PY_TESTS) or (path.parts[0] == "tests" and path.name == "__init__.py")
    elif lang == "sh":
        test = str(path.parent) == "tests" and named(SH_TESTS)
    else:
        test = (lang in ("js", "mts") and bool(path.suffix)
                and (named(NODE_TESTS) or "test" in path.parts[:-1]))
    return "test" if test else lang


def fixed_in_place(before: str, after: str) -> bool:
    """Only the line printing the slash changed, and it no longer prints one."""
    old, new = before.splitlines(), after.splitlines()
    changed = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
    return (len(old) == len(new) and len(changed) == 1
            and "entry.name}/" in old[changed[0]] and "/" not in new[changed[0]])


def new_ts(run: Path, new: list[str], scripts: dict[str, str]) -> tuple[int, str]:
    work, summary = run / "work", " ".join(f"{k} {r}" for r, k in scripts.items())
    helpers = {r: k for r, k in scripts.items() if k != "test"}
    if not helpers:
        return 2, f"INCONCLUSIVE: no new helper written; new: {summary}"
    rejected = [
        (".ts takes its module kind from package.json and tsconfig.json only includes .mts",
         [r for r, k in helpers.items() if k == "ts"]),
        ("not a .mts under runtime/",
         [r for r, k in helpers.items() if k != "mts" or not r.startswith("runtime/")]),
        ("outside the tsc program", [r for r in new if r.endswith(".mts") and outside_tsc(r)]),
        ('package.json sets "type": "module"',
         [r for r in new if PurePosixPath(r).name == "package.json"
          and json.loads((work / r).read_text()).get("type") == "module"]),
    ]
    for why, paths in rejected:
        if paths:
            return 1, f"FAIL: {why}: {' '.join(paths)}"
    return 0, f"PASS: new: {summary}"


def py_fix(run: Path, new: list[str], scripts: dict[str, str]) -> tuple[int, str]:
    now = run / "work" / LIST_SKILLS
    if not now.is_file():
        return 1, "FAIL: list_skills.py removed"
    if helpers := [f"{k} {r}" for r, k in scripts.items() if k != "test"]:
        return 1, f"FAIL: new script(s): {' '.join(helpers)}"
    if not fixed_in_place((run / "baseline" / LIST_SKILLS).read_text(), now.read_text()):
        return 2, "INCONCLUSIVE: not fixed in place"
    return 0, "PASS: fixed in place" + "".join(f"; new: {r}" for r in scripts)


def main() -> None:
    scenario, run = sys.argv[1], Path(sys.argv[2])
    work, baseline = run / "work", run / "baseline"
    base = {p.relative_to(baseline).as_posix() for p in baseline.rglob("*") if p.is_file()}
    new = sorted(set(listed(work)) - base)
    scripts = {rel: k for rel in new if (k := kind(work, rel))}
    code, verdict = {"new-ts": new_ts, "py-fix": py_fix}[scenario](run, new, scripts)
    print(verdict)
    sys.exit(code)


if __name__ == "__main__":
    main()
