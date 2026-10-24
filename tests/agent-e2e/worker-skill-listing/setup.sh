#!/usr/bin/env bash
# A tiny Python CLI; the task is a trivial worker-easy job. What is graded is
# how much context the worker starts with, not the code.
set -euo pipefail
WORK="${E2E_WORK:?}"
cat > "$WORK/cli.py" <<'P'
import argparse

VERSION = "1.2.0"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("name")
    a = p.parse_args()
    print(f"hello {a.name}")


if __name__ == "__main__":
    main()
P
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
