"""Grade a helper-lang run: helper_lang.py new-ts|py-fix RUN.

Static, because check.sh runs on the host: nothing from the run executes, and
git reads no config the agent could have written (git(1), SECURITY). The whole
run dir, HOME included, is the agent's to write, so the baseline is rebuilt from
the scenario's setup.sh in this checkout. New files are the workspace's
non-ignored files missing from that baseline. The run's own ignore files and
nested .git dirs apply, so an agent can hide one. Run content is read only as a
regular file reached without a symlink. Any other error grades INCONCLUSIVE.
"""
import errno
import fnmatch
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

EXT = {".mts": "mts", ".ts": "ts", ".cts": "ts", ".py": "py", ".js": "js",
       ".mjs": "js", ".cjs": "js", ".sh": "sh", ".bash": "sh", ".tsx": "tsx",
       ".jsx": "jsx", ".rb": "rb", ".pl": "pl", ".lua": "lua", ".zsh": "zsh",
       ".fish": "fish"}
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
SCENARIOS = Path(__file__).resolve().parents[1]
_layout = importlib.util.spec_from_file_location(
    "runtime_layout", SCENARIOS.parents[1] / "runtime/scripts/runtime-layout.py")
layout = importlib.util.module_from_spec(_layout)
_layout.loader.exec_module(layout)


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


def baseline(scenario: str, tmp: Path, env: dict[str, str]) -> tuple[set[str], str]:
    """The files setup.sh gives the agent, and the list_skills.py among them."""
    work = tmp / "baseline"
    work.mkdir()
    subprocess.run(["bash", str(SCENARIOS / f"helper-lang-{scenario}" / "setup.sh")],
                   env={**env, "E2E_WORK": str(work)}, check=True, capture_output=True,
                   timeout=60)
    out = subprocess.run(["git", "ls-files", "-z"], cwd=work, env=env, check=True,
                         capture_output=True).stdout
    files = {os.fsdecode(rel) for rel in out.split(b"\0") if rel}
    return files, (work / LIST_SKILLS).read_text()


def listed(work: Path, env: dict[str, str]) -> list[str]:
    with tempfile.TemporaryDirectory() as gitdir:
        subprocess.run(["git", "init", "-q", "--bare", gitdir], env=env, check=True)
        out = subprocess.run(["git", "--git-dir", gitdir, "--work-tree", ".", "ls-files",
                              "-z", "--others", "--exclude-standard"],
                             cwd=work, env=env, capture_output=True, check=True,
                             timeout=GIT_TIMEOUT).stdout
    return [os.fsdecode(rel) for rel in out.split(b"\0") if rel]


def shebang(work: Path, rel: str) -> str | None:
    first = (read_run(work, rel, 200) or b"").split(b"\n")[0]
    if not first.startswith(b"#!"):
        return None
    known = ((b"python", "py"), (b"node", "js"), (b"sh", "sh"))
    return next((name for needle, name in known if needle in first), "script")


def kind(work: Path, rel: str) -> str | None:
    """`test`, else the language of REL; None when it is no helper.

    Under runtime/ every other file is a helper, so a port counts in any language;
    elsewhere a script has a script suffix or a `#!` line.
    """
    path = PurePosixPath(rel)
    # ls-files lists a nested repository as `DIR/`.
    if (rel.endswith("/") or path.name.endswith(DECLARATIONS) or path.name in layout.TOOLCHAIN
            or ("__pycache__" in path.parts and path.suffix == ".pyc")):
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
    if test:
        return "test"
    if lang is None and path.parts[0] == "runtime":
        return path.suffix[1:] or "file"
    return lang


def fixed_in_place(before: str, after: str) -> bool:
    """Only the line printing the slash changed, and it no longer prints one."""
    old, new = before.splitlines(), after.splitlines()
    changed = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
    return (len(old) == len(new) and len(changed) == 1
            and "entry.name}/" in old[changed[0]] and "/" not in new[changed[0]])


def json_object(work: Path, rel: str) -> dict | None:
    try:
        data = json.loads(read_run(work, rel) or b"")
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def source(top: Path) -> str | None:
    data = read_run(top, LIST_SKILLS)
    try:
        return None if data is None else data.decode()
    except UnicodeDecodeError:
        raise Refused(f"{top.name}/{LIST_SKILLS}: not UTF-8") from None


def new_ts(work: Path, new: list[str], scripts: dict[str, str]) -> tuple[int, str]:
    summary = " ".join(f"{k} {r}" for r, k in scripts.items())
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


def py_fix(work: Path, before: str, scripts: dict[str, str]) -> tuple[int, str]:
    now = source(work)
    if now is None:
        return 1, "FAIL: list_skills.py removed"
    if helpers := [f"{k} {r}" for r, k in scripts.items() if k != "test"]:
        return 1, f"FAIL: new script(s): {' '.join(helpers)}"
    if not fixed_in_place(before, now):
        return 2, "INCONCLUSIVE: not fixed in place"
    return 0, "PASS: fixed in place" + "".join(f"; new: {r}" for r in scripts)


def grade(scenario: str, run: Path) -> tuple[int, str]:
    work = run / "work"
    with tempfile.TemporaryDirectory() as tmp:
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": tmp,
               "GIT_CONFIG_GLOBAL": os.devnull}
        base, before = baseline(scenario, Path(tmp), env)
        try:
            new = sorted(set(listed(work, env)) - base)
        except subprocess.TimeoutExpired:
            return 2, f"INCONCLUSIVE: git ls-files did not finish in {GIT_TIMEOUT} s"
    try:
        scripts = {rel: k for rel in new if (k := kind(work, rel))}
        if scenario == "new-ts":
            return new_ts(work, new, scripts)
        return py_fix(work, before, scripts)
    except Refused as e:
        return 1, f"FAIL: {e}"


def one_line(text: str) -> str:
    """TEXT with a newline, or any other unprintable, escaped as in repr()."""
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)


def main() -> None:
    sys.stdout.reconfigure(errors="backslashreplace")
    try:
        code, verdict = grade(sys.argv[1], Path(sys.argv[2]))
    except Exception as e:
        code, verdict = 2, f"INCONCLUSIVE: grader error: {type(e).__name__}: {e}"
    print(one_line(verdict))
    sys.exit(code)


if __name__ == "__main__":
    main()
