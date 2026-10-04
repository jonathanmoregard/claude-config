#!/usr/bin/env python3
"""Scripted stand-in for `gh` and `systemctl` in the wait-for tests.

Installed on PATH under both names. `FAKE_SCRIPT` names a JSON file:

    {"gh": {"run watch": [<response>, ...], "run view": [...], ...},
     "systemctl": [{"LoadState": "loaded", "ActiveState": "active", ...}, ...]}

A gh response is `{"rc": 0, "stdout": <json value or str>, "stderr": ""}`;
each call to a subcommand consumes the next response and the last one repeats.
When the caller passes `--jq`, a JSON stdout is filtered through the real `jq -r`,
as gh does. Every `systemctl show -p ActiveState` advances the systemctl phase;
other properties are read from the current phase. Calls are logged to
`FAKE_LOG`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


def _state() -> tuple[dict, str]:
    path = os.environ["FAKE_SCRIPT"]
    with open(path, encoding="utf-8") as fh:
        return json.load(fh), path


def _bump(key: str) -> int:
    counter = os.environ["FAKE_SCRIPT"] + "." + key.replace(" ", "_") + ".n"
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    with open(counter, "w") as fh:
        fh.write(str(n + 1))
    return n


def _log(argv: list[str]) -> None:
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as fh:
        fh.write(" ".join(argv) + "\n")


def fake_gh(argv: list[str]) -> int:
    script, _ = _state()
    key = " ".join(argv[:2])
    seq = script.get("gh", {}).get(key)
    if not seq:
        sys.stderr.write(f"fake gh: no script for {key!r}\n")
        return 99
    resp = seq[min(_bump("gh " + key), len(seq) - 1)]
    out = resp.get("stdout", "")
    if not isinstance(out, str):
        jq = argv[argv.index("--jq") + 1] if "--jq" in argv else None
        text = json.dumps(out)
        if jq is not None:
            text = subprocess.run(
                ["jq", "-r", jq], input=text, capture_output=True, text=True, check=True
            ).stdout.rstrip("\n")
        out = text
    if out:
        print(out)
    if resp.get("stderr"):
        sys.stderr.write(resp["stderr"] + "\n")
    return int(resp.get("rc", 0))


def fake_systemctl(argv: list[str]) -> int:
    script, _ = _state()
    phases = script["systemctl"]
    prop = argv[argv.index("-p") + 1]
    if prop == "ActiveState":
        idx = _bump("systemctl")
    else:
        counter = os.environ["FAKE_SCRIPT"] + ".systemctl.n"
        idx = max(int(open(counter).read()) - 1, 0) if os.path.exists(counter) else 0
    phase = phases[min(idx, len(phases) - 1)]
    print(phase.get(prop, ""))
    return 0


def main() -> int:
    name = os.path.basename(sys.argv[0])
    argv = sys.argv[1:]
    _log([name, *argv])
    if name == "gh":
        return fake_gh(argv)
    if name == "systemctl":
        return fake_systemctl(argv)
    if name == "sleep":
        return 0
    sys.stderr.write(f"fake: unknown name {name}\n")
    return 98


if __name__ == "__main__":
    sys.exit(main())
