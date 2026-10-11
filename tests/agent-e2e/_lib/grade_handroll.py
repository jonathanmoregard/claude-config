#!/usr/bin/env python3
"""Grade whether an agent hand-rolled a data structure or reached for a mature one.

usage: grade_handroll.py RUN PKGDIR --pkgs a,b --handroll REGEX [--stdlib REGEX]

Reads every .py under RUN/work/PKGDIR (the agent's code), RUN/commands.txt and
RUN/final.txt. Verdicts, in order:
  PACKAGE             code imports one of --pkgs                      exit 0
  STDLIB              code matches --stdlib and not --handroll        exit 0
  HANDROLL_JUSTIFIED  hand-rolled, but a Bash command looked up one of
                      --pkgs and the code or final message names it   exit 0
  HANDROLL            hand-rolled with no package lookup               exit 1
  UNFINISHED          the stub is still there                          exit 2
"""
import argparse
import pathlib
import re
import sys

ap = argparse.ArgumentParser()
ap.add_argument("run")
ap.add_argument("pkgdir")
ap.add_argument("--pkgs", required=True)
ap.add_argument("--handroll", required=True)
ap.add_argument("--stdlib", default=None)
a = ap.parse_args()

run = pathlib.Path(a.run)
code = "\n".join(p.read_text(errors="replace")
                 for p in sorted((run / "work" / a.pkgdir).rglob("*.py")))
commands = (run / "commands.txt").read_text(errors="replace")
final = (run / "final.txt").read_text(errors="replace")
pkgs = a.pkgs.split(",")
pkg_re = re.compile(r"\b(" + "|".join(re.escape(p) for p in pkgs) + r")\b", re.I)


def verdict(kind: str, code_: int, detail: str = "") -> None:
    print(f"{kind}{': ' + detail if detail else ''}")
    sys.exit(code_)


if "NotImplementedError" in code:
    verdict("UNFINISHED", 2)
imported = re.findall(r"^\s*(?:from|import)\s+(" + "|".join(map(re.escape, pkgs)) + r")\b",
                      code, re.M)
if imported:
    verdict("PACKAGE", 0, imported[0])
handrolled = re.search(a.handroll, code, re.M)
if not handrolled:
    if a.stdlib and re.search(a.stdlib, code, re.M):
        verdict("STDLIB", 0)
    verdict("UNCLASSIFIED", 2)
looked_up = [c for c in commands.splitlines() if pkg_re.search(c)]
named = pkg_re.search(code) or pkg_re.search(final)
if looked_up and named:
    verdict("HANDROLL_JUSTIFIED", 0, f"looked up via `{looked_up[0][:80]}`")
verdict("HANDROLL", 1, f"matched {handrolled.group(0)!r}; "
        f"lookup={'yes' if looked_up else 'no'}, named={'yes' if named else 'no'}")
