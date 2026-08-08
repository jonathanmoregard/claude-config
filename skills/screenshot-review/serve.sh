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

# Free a TCP port robustly — fuser may be absent, so fall back to ss.
free_port() {
  local port="$1" pids
  command -v fuser >/dev/null 2>&1 && fuser -k "${port}/tcp" 2>/dev/null || true
  pids="$(ss -ltnpH 2>/dev/null | awk -v port="$port" '$4 ~ ":"port"$"' \
          | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)"
  [ -n "$pids" ] && kill $pids 2>/dev/null || true
}

# Stop any previous instance (server + its staging dir), then free the port.
[ -f "$STATE/pid" ] && kill "$(cat "$STATE/pid")" 2>/dev/null || true
free_port "$PORT"
[ -f "$STATE/root" ] && rm -rf "$(cat "$STATE/root")" 2>/dev/null || true
sleep 0.5

# Stage a serve-root of symlinks (no copying; your folder is left untouched).
ROOT="$(mktemp -d)"
ln -s "$TOOL" "$ROOT/index.html"
mkdir "$ROOT/$NAME"
shopt -s nullglob nocaseglob
imgs=("$DIR"/*.png "$DIR"/*.jpg "$DIR"/*.jpeg "$DIR"/*.webp "$DIR"/*.avif "$DIR"/*.gif "$DIR"/*.bmp)
shopt -u nocaseglob nullglob
[ "${#imgs[@]}" -gt 0 ] || { echo "no images found in $DIR" >&2; exit 1; }
for f in "${imgs[@]}"; do ln -s "$f" "$ROOT/$NAME/"; done
# Element-pin sidecars: <base>.dom.json next to <base>.png enables Figma-style
# element pinning in the tool (hover-highlight + click-to-comment). Also link
# annotations.json (may not exist yet — the server writes it into $DIR).
shopt -s nullglob
for f in "$DIR"/*.dom.json; do ln -s "$f" "$ROOT/$NAME/"; done
shopt -u nullglob
ln -s "$DIR/annotations.json" "$ROOT/$NAME/annotations.json"
python3 - "$ROOT/$NAME" >"$ROOT/$NAME/manifest.json" <<'PY'
import json, os, sys
d = sys.argv[1]
exts = ('.png', '.jpg', '.jpeg', '.webp', '.avif', '.gif', '.bmp')
print(json.dumps(sorted(f for f in os.listdir(d)
                        if f.lower().endswith(exts) and f != 'manifest.json')))
PY

# Serve in the background: static files + POST /save that writes element-pin
# annotations back into the SOURCE dir (annotations.json) for the agent to read.
python3 - "$ROOT" "$DIR" "$PORT" >"$STATE/log" 2>&1 <<'PY' &
import http.server, json, os, sys
root, srcdir, port = sys.argv[1], sys.argv[2], int(sys.argv[3])

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=root, **kw)
    def do_POST(self):
        if self.path.rstrip('/') != '/save':
            self.send_error(404); return
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0 or n > 5_000_000:
            self.send_error(413); return
        data = self.rfile.read(n)
        try:
            json.loads(data)
        except ValueError:
            self.send_error(400); return
        tmp = os.path.join(srcdir, '.annotations.json.tmp')
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, os.path.join(srcdir, 'annotations.json'))
        self.send_response(204); self.end_headers()
    def log_message(self, *a):
        pass

http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
PY
SRV=$!
echo "$SRV" >"$STATE/pid"; echo "$ROOT" >"$STATE/root"
sleep 1
URL="http://localhost:$PORT/index.html?dir=/$NAME"
( xdg-open "$URL" >/dev/null 2>&1 & ) || true
echo "Serving $DIR (${#imgs[@]} images) → $URL   [pid $SRV]"
echo "Stop: kill $SRV   (or fuser -k $PORT/tcp)"
