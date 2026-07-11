#!/usr/bin/env bash
# Screenshot review — serve a folder of images to the review tool and open it.
# Project-independent: point it at ANY folder of images.
#   Usage: serve.sh <image-dir> [port]
set -euo pipefail

DIR="${1:?usage: serve.sh <image-dir> [port]}"
PORT="${2:-8791}"
DIR="$(realpath "$DIR")"
[ -d "$DIR" ] || { echo "not a directory: $DIR" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$HERE/tool/index.html"
NAME="$(basename "$DIR")"
STATE="${XDG_CACHE_HOME:-$HOME/.cache}/screenshot-review"
mkdir -p "$STATE"

# Stop any previous instance (server + its staging dir), free the port.
[ -f "$STATE/pid" ] && kill "$(cat "$STATE/pid")" 2>/dev/null || true
fuser -k "$PORT/tcp" 2>/dev/null || true
[ -f "$STATE/root" ] && rm -rf "$(cat "$STATE/root")" 2>/dev/null || true
sleep 0.3

# Stage a serve-root of symlinks (no copying; your folder is left untouched).
ROOT="$(mktemp -d)"
ln -s "$TOOL" "$ROOT/index.html"
mkdir "$ROOT/$NAME"
shopt -s nullglob nocaseglob
imgs=("$DIR"/*.png "$DIR"/*.jpg "$DIR"/*.jpeg "$DIR"/*.webp "$DIR"/*.avif "$DIR"/*.gif "$DIR"/*.bmp)
shopt -u nocaseglob nullglob
[ "${#imgs[@]}" -gt 0 ] || { echo "no images found in $DIR" >&2; exit 1; }
for f in "${imgs[@]}"; do ln -s "$f" "$ROOT/$NAME/"; done
python3 - "$ROOT/$NAME" >"$ROOT/$NAME/manifest.json" <<'PY'
import json, os, sys
d = sys.argv[1]
exts = ('.png', '.jpg', '.jpeg', '.webp', '.avif', '.gif', '.bmp')
print(json.dumps(sorted(f for f in os.listdir(d)
                        if f.lower().endswith(exts) and f != 'manifest.json')))
PY

# Serve in the background and open the tool pointed at the folder.
( cd "$ROOT" && exec python3 -m http.server "$PORT" --bind 127.0.0.1 ) >"$STATE/log" 2>&1 &
SRV=$!
echo "$SRV" >"$STATE/pid"; echo "$ROOT" >"$STATE/root"
sleep 1
URL="http://localhost:$PORT/index.html?dir=/$NAME"
( xdg-open "$URL" >/dev/null 2>&1 & ) || true
echo "Serving $DIR (${#imgs[@]} images) → $URL   [pid $SRV]"
echo "Stop: kill $SRV   (or fuser -k $PORT/tcp)"
