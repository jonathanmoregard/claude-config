"""Classify the script files a run added to the workspace, by language.

Usage: helper_lang.py WORK BASE -> prints one `lang path` line per new script.
New means absent at BASE: committed since, staged, or untracked.
"""
import subprocess
import sys
from pathlib import Path

EXT = {".ts": "ts", ".mts": "ts", ".py": "py", ".js": "js", ".mjs": "js",
       ".cjs": "js", ".sh": "sh", ".bash": "sh"}


def git(work: str, *args: str) -> list[str]:
    out = subprocess.run(["git", "-C", work, *args], capture_output=True,
                         text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def lang(path: Path) -> str | None:
    if path.suffix in EXT:
        return EXT[path.suffix]
    if path.suffix or not path.is_file():
        return None
    first = path.read_text(errors="replace").split("\n", 1)[0]
    if not first.startswith("#!"):
        return None
    for needle, name in (("python", "py"), ("node", "js"), ("sh", "sh")):
        if needle in first:
            return name
    return None


def main() -> None:
    work, base = sys.argv[1], sys.argv[2]
    added = set(git(work, "diff", "--name-only", "--diff-filter=A", base))
    added |= set(git(work, "ls-files", "--others", "--exclude-standard"))
    for rel in sorted(added):
        kind = lang(Path(work) / rel)
        if kind:
            print(kind, rel)


main()
