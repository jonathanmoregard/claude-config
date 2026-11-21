#!/usr/bin/env bash
# artl-gate-evidence-present plus a pushed binary: the project icon, a real
# PNG the README shows. The seeded passing review reads every text file but
# not the binary, as a live advisor cannot review one. The PR must still open.
source "$(dirname "$0")/../_lib/artl-gate.sh"
mkdir -p docs
python3 - <<'PY'
import struct, zlib
def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
w = h = 16
rows = b"".join(b"\0" + b"".join(bytes((x * 16, y * 16, 128)) for x in range(w))
                for y in range(h))
png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
open("docs/icon.png", "wb").write(png)
PY
printf '\n![widget icon](docs/icon.png)\n' >> README.md
git add -A
git commit -qm "docs: add the project icon to the README"
git push -q
source "$(dirname "$0")/../_lib/artl-seed-review.sh"
