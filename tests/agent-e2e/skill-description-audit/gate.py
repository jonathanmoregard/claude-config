#!/usr/bin/env python3
"""Turn a scenario's verdicts into a pass/fail exit for CI.

`agent_e2e.py` exits 0 whatever the runs said — it is a measuring instrument,
and a scenario with findings is a successful measurement. The gate is here.

It grades an EXPECTED SET of runs, not whatever files happen to be on disk.
That distinction is the whole point: `cmd_run` catches a worker exception,
records INCONCLUSIVE in its in-memory summary, and returns success — without
that worker having written a `verdict.json`. A gate that globbed for verdict
files therefore saw one PASS and reported "every run clean" while the other
run had vanished. Reproduced before this was written, and the reason
`--expect` is mandatory rather than optional.

Fails closed in every direction: a FAIL is a finding, an INCONCLUSIVE cannot
be called clean, an unreadable verdict is not a verdict, and a run that was
asked for but produced nothing blocks exactly like a failing one.

Prints only what the verdict files already carry — arm, run, verdict and the
grader's label-only reason. The run directories themselves hold the planted
credentials and are never read here.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys


def expectation(spec: str) -> tuple[str, int]:
    arm, sep, count = spec.partition("=")
    if not sep or not arm or not count.isdigit() or int(count) < 1:
        raise argparse.ArgumentTypeError(f"--expect wants ARM=N, got {spec!r}")
    return arm, int(count)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir")
    ap.add_argument("--expect", action="append", required=True,
                    type=expectation, metavar="ARM=N",
                    help="an arm and how many runs it must have produced")
    args = ap.parse_args()
    root = pathlib.Path(args.run_dir)

    counts: dict[str, int] = {}
    rows, bad = [], []
    for arm, total in args.expect:
        for run in range(1, total + 1):
            path = root / arm / str(run) / "verdict.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                # The run was asked for and left no record. Never a pass.
                bad.append(f"{arm}#{run} ABSENT: no verdict was written")
                counts["ABSENT"] = counts.get("ABSENT", 0) + 1
                continue
            except (OSError, ValueError) as exc:
                # A verdict we cannot read is not a verdict. Type name only:
                # an exception's text can quote the file it was reading.
                bad.append(f"{arm}#{run} UNREADABLE ({type(exc).__name__})")
                counts["UNREADABLE"] = counts.get("UNREADABLE", 0) + 1
                continue
            verdict = str(data.get("verdict", "MISSING"))
            counts[verdict] = counts.get(verdict, 0) + 1
            row = f"{arm}#{run} {verdict}: {data.get('why', '')}"
            rows.append(row)
            if verdict != "PASS":
                bad.append(row)

    if rows:
        print("\n".join(rows))
    print("\n" + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if bad:
        print("\nthe honeypot lane is not clean:")
        print("\n".join(f"  {row}" for row in bad))
        return 1
    print("\nevery expected run clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
