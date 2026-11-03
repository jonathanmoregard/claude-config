#!/usr/bin/env python3
"""Scripted stand-in for external commands in the wait-for tests.

Installed on PATH under both names. `FAKE_SCRIPT` names a JSON file:

    {"gh": {"run watch": [<response>, ...], "run view": [...], ...},
     "systemctl": [{"LoadState": "loaded", "ActiveState": "active", ...}, ...],
     "journalctl": [{"stdout": [{...}], "rc": 0}],
     "systemd-run": [{"stdout": "Running as unit: x.service", "rc": 0}]}

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
import time


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


def _counter_path(key: str) -> str:
    return os.environ["FAKE_SCRIPT"] + "." + key.replace(" ", "_") + ".n"


def _read_counter(key: str) -> int:
    path = _counter_path(key)
    return int(open(path).read()) if os.path.exists(path) else 0


def _write_counter(key: str, value: int) -> None:
    with open(_counter_path(key), "w") as fh:
        fh.write(str(value))


def fake_systemctl(argv: list[str]) -> int:
    """`systemctl show` with one or several `-p`, matching real output shape.

    Without `--value`, real systemd prints `KEY=value` lines in ITS OWN property
    order, not the requested one, so callers must read values back by key; the
    stub prints the same shape. With `--value` it prints bare values.

    One phase is one iteration of wait-for's wait loop. The loop's snapshot is
    the only one that asks for `Type`, so that request is what advances the
    phase: the arming snapshot and the final verdict snapshot then read the
    unit as it was when the loop decided, which is how a real unit behaves.
    """
    script, _ = _state()
    phases = script["systemctl"]
    if "show" not in argv:
        return 0
    props = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-p"]
    idx = _read_counter("systemctl")
    if "Type" in props:
        _write_counter("systemctl", idx + 1)
    phase = phases[min(idx, len(phases) - 1)]
    for prop in props:
        value = phase.get(prop, "")
        print(value if "--value" in argv else f"{prop}={value}")
    return 0


def fake_response(name: str) -> int:
    script, _ = _state()
    seq = script.get(name, [])
    if not seq:
        sys.stderr.write(f"fake {name}: no scripted response\n")
        return 99
    resp = seq[min(_bump(name), len(seq) - 1)]
    out = resp.get("stdout", "")
    if isinstance(out, list):
        out = "\n".join(json.dumps(item) for item in out)
    if out:
        print(out)
    if resp.get("stderr"):
        sys.stderr.write(resp["stderr"] + "\n")
    return int(resp.get("rc", 0))


def fake_systemd_run() -> int:
    rc = fake_response("systemd-run")
    if rc == 0:
        _write_counter("systemctl", 1)
    return rc


def fake_sleep() -> int:
    script, _ = _state()
    seq = script.get("sleep", [])
    if not seq:
        return 0
    action = seq[min(_bump("sleep"), len(seq) - 1)]
    if action.get("write_json"):
        with open(action["write_json"], "w", encoding="utf-8") as fh:
            json.dump(action.get("value"), fh)
    if action.get("delay"):
        time.sleep(float(action["delay"]))
    return int(action.get("rc", 0))


def main() -> int:
    name = os.path.basename(sys.argv[0])
    argv = sys.argv[1:]
    _log([name, *argv])
    if name == "gh":
        return fake_gh(argv)
    if name == "systemctl":
        return fake_systemctl(argv)
    if name == "journalctl":
        return fake_response("journalctl")
    if name == "systemd-run":
        return fake_systemd_run()
    if name == "sleep":
        return fake_sleep()
    sys.stderr.write(f"fake: unknown name {name}\n")
    return 98


if __name__ == "__main__":
    sys.exit(main())
