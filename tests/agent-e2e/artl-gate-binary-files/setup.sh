#!/usr/bin/env bash
# artl-gate-evidence-present plus binary build outputs on the branch (the
# 2026-11-20 firmware PR shape): the seeded advisor reads the text files and,
# as a real one would, not the binaries.
source "$(dirname "$0")/../_lib/artl-gate.sh"

mkdir -p build
python3 - <<'PY'
from pathlib import Path
elf = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8 + bytes(range(256)) * 160
Path("build/backoff.elf").write_bytes(elf)
Path("build/backoff.bin").write_bytes(bytes((i * 37) % 256 for i in range(48_000)))
PY
git add -A
git commit -qm "build: backoff firmware images"
git push -q

source "$(dirname "$0")/../_lib/artl-seed-review.sh"
