"""Grade a helper-lang run: helper_lang.py new-ts|py-fix RUN.

Static, because check.sh runs on the host: nothing from the run executes, and
git reads no config the agent could have written (git(1), SECURITY). New files
are the workspace's non-ignored files missing from setup.sh's baseline copy.
The run's own ignore files and nested .git dirs apply, so an agent can hide one.
Run content is read only as a regular file reached without a symlink.
"""
import errno
import fnmatch
import json
import os
import stat
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
DECLARATIONS = (".d.ts", ".d.mts", ".d.cts")
LIST_SKILLS = "runtime/scripts/list_skills.py"
# git blocks opening a FIFO .gitignore or nested-repo HEAD (measured, git 2.56).
GIT_TIMEOUT = 10


class Refused(Exception):
    """Run content the grader will not read; the run grades FAIL."""


def read_run(top: Path, rel: str, limit: int = -1) -> bytes | None:
    """REL under TOP, or None when it is absent."""
    fd = None
    try:
        for part in (top, *PurePosixPath(rel).parts):
            fd, parent = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=fd), fd
            if parent is not None:
                os.close(parent)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f"{top.name}/{rel}: not a regular file")
        with open(fd, "rb", closefd=False) as f:
            return f.read(limit)
    except FileNotFoundError:
        return None
    except OSError as e:
        why = "a symlink" if e.errno == errno.ELOOP else e.strerror
        raise Refused(f"{top.name}/{rel}: {why}") from None
    finally:
        if fd is not None:
            os.close(fd)


def outside_tsc(rel: str) -> bool:
    """tsconfig.json includes runtime/ and tests/; TypeScript's globs skip the rest."""
    parts = PurePosixPath(rel).parts
    return (parts[0] not in ("runtime", "tests")
            or any(p.startswith(".") or p in TSC_SKIPS for p in parts))


def listed(work: Path) -> list[str]:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull}
    with tempfile.TemporaryDirectory() as gitdir:
        subprocess.run(["git", "init", "-q", "--bare", gitdir], env=env, check=True)
        out = subprocess.run(["git", "--git-dir", gitdir, "--work-tree", ".", "ls-files",
                              "-z", "--others", "--exclude-standard"],
                             cwd=work, env=env, capture_output=True, check=True,
                             timeout=GIT_TIMEOUT).stdout
    return [os.fsdecode(rel) for rel in out.split(b"\0") if rel]


def shebang(work: Path, rel: str) -> str | None:
    first = (read_run(work, rel, 200) or b"").split(b"\n")[0]
    if first.startswith(b"#!"):
        for needle, name in ((b"python", "py"), (b"node", "js"), (b"sh", "sh")):
            if needle in first:
                return name
    return None


def kind(work: Path, rel: str) -> str | None:
    """`test`, else the script language of REL; None when it is no script."""
    path = PurePosixPath(rel)
    # ls-files lists a nested repository as `DIR/`.
    if rel.endswith("/") or path.name.endswith(DECLARATIONS):
        return None
    lang = EXT.get(path.suffix) if path.suffix else shebang(work, rel)

    def named(patterns: tuple[str, ...]) -> bool:
        return any(fnmatch.fnmatchcase(path.name, p) for p in patterns)

    if lang == "py":
        test = named(PY_TESTS) or ("tests" in path.parts[:-1] and path.name == "__init__.py")
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


def json_object(work: Path, rel: str) -> dict | None:
    try:
        data = json.loads(read_run(work, rel) or b"")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def source(top: Path) -> str | None:
    data = read_run(top, LIST_SKILLS)
    try:
        return None if data is None else data.decode()
    except UnicodeDecodeError:
        raise Refused(f"{top.name}/{LIST_SKILLS}: not UTF-8") from None


def new_ts(run: Path, new: list[str], scripts: dict[str, str]) -> tuple[int, str]:
    work, summary = run / "work", " ".join(f"{k} {r}" for r, k in scripts.items())
    helpers = {r: k for r, k in scripts.items() if k != "test"}
    if not helpers:
        return 2, f"INCONCLUSIVE: no new helper written; new: {summary}"
    packages = {r: json_object(work, r) for r in new if PurePosixPath(r).name == "package.json"}
    rejected = [
        (".ts takes its module kind from package.json and tsconfig.json only includes .mts",
         [r for r, k in helpers.items() if k == "ts"]),
        ("not a .mts under runtime/",
         [r for r, k in helpers.items() if k != "mts" or not r.startswith("runtime/")]),
        ("outside the tsc program", [r for r in new if r.endswith(".mts") and outside_tsc(r)]),
        ("package.json is not a JSON object", [r for r, p in packages.items() if p is None]),
        ('package.json sets "type": "module"',
         [r for r, p in packages.items() if p and p.get("type") == "module"]),
    ]
    for why, paths in rejected:
        if paths:
            return 1, f"FAIL: {why}: {' '.join(paths)}"
    return 0, f"PASS: new: {summary}"


def py_fix(run: Path, new: list[str], scripts: dict[str, str]) -> tuple[int, str]:
    now = source(run / "work")
    if now is None:
        return 1, "FAIL: list_skills.py removed"
    if helpers := [f"{k} {r}" for r, k in scripts.items() if k != "test"]:
        return 1, f"FAIL: new script(s): {' '.join(helpers)}"
    if not fixed_in_place(source(run / "baseline") or "", now):
        return 2, "INCONCLUSIVE: not fixed in place"
    return 0, "PASS: fixed in place" + "".join(f"; new: {r}" for r in scripts)


def main() -> None:
    scenario, run = sys.argv[1], Path(sys.argv[2])
    work, baseline = run / "work", run / "baseline"
    sys.stdout.reconfigure(errors="backslashreplace")
    base = {p.relative_to(baseline).as_posix() for p in baseline.rglob("*") if p.is_file()}
    try:
        new = sorted(set(listed(work)) - base)
        scripts = {rel: k for rel in new if (k := kind(work, rel))}
        code, verdict = {"new-ts": new_ts, "py-fix": py_fix}[scenario](run, new, scripts)
    except subprocess.TimeoutExpired:
        code, verdict = 2, f"INCONCLUSIVE: git ls-files did not finish in {GIT_TIMEOUT} s"
    except Refused as e:
        code, verdict = 1, f"FAIL: {e}"
    print(verdict)
    sys.exit(code)


if __name__ == "__main__":
    main()
