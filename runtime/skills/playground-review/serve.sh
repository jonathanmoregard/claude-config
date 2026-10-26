#!/usr/bin/env bash
# playground-review — iframe live playgrounds, collect notes per URL.
#   Usage: serve.sh <playgrounds.json | dir with playgrounds.json> [port]
set -euo pipefail

TARGET="${1:?usage: serve.sh <playgrounds.json | dir> [port]}"
PORT="${2:-8792}"
if [ -d "$TARGET" ]; then
  MANIFEST="$TARGET/playgrounds.json"
else
  MANIFEST="$TARGET"
fi
[ -f "$MANIFEST" ] || { echo "not a manifest file: $MANIFEST" >&2; exit 1; }
MANIFEST="$(realpath "$MANIFEST")"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$HERE/tool/index.html"
STATE="${XDG_CACHE_HOME:-$HOME/.cache}/playground-review"
mkdir -p "$STATE"

free_port() {
  local port="$1" pids
  command -v fuser >/dev/null 2>&1 && fuser -k "${port}/tcp" 2>/dev/null || true
  pids="$(ss -ltnpH 2>/dev/null | awk -v port="$port" '$4 ~ ":"port"$"' \
          | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)"
  [ -n "$pids" ] && kill $pids 2>/dev/null || true
}

[ -f "$STATE/pid" ] && kill "$(cat "$STATE/pid")" 2>/dev/null || true
free_port "$PORT"
[ -f "$STATE/root" ] && rm -rf "$(cat "$STATE/root")" 2>/dev/null || true
sleep 0.5

ROOT="$(mktemp -d)"
ln -s "$TOOL" "$ROOT/index.html"
ln -s "$MANIFEST" "$ROOT/playgrounds.json"

OUTDIR="$(dirname "$MANIFEST")"

( cd "$ROOT" && exec python3 -c '
import http.server, json, os, re, sys
outdir = sys.argv[2]

class H(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # Force fresh every load — tool HTML + manifest served without
        # caching so skill edits land in the browser without hard-refresh
        # (recurring bug 2026-08-03).
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_POST(self):
        # Source snapshot write-back: the tool reads the live iframe (same
        # origin) and posts {name, html, css, js}; we drop the three files
        # next to playgrounds.json so a review agent can map per-playground
        # feedback onto real markup.
        if self.path.rstrip("/") != "/pgr-source":
            self.send_error(404); return
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 20_000_000:
            self.send_error(413); return
        try:
            data = json.loads(self.rfile.read(n))
        except ValueError:
            self.send_error(400); return
        name = re.sub(r"[^\w.-]", "_", str(data.get("name") or "")).strip("._-")
        if not name:
            self.send_error(400); return
        written = []
        for ext, key in ((".source.html", "html"), (".source.css", "css"), (".source.js", "js")):
            body = data.get(key)
            if body is None:
                continue
            path = os.path.join(outdir, name + ext)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", errors="replace") as f:
                f.write(body)
            os.replace(tmp, path)
            written.append(os.path.basename(path))
        payload = json.dumps({"written": written, "dir": outdir}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
' "$PORT" "$OUTDIR" ) >"$STATE/log" 2>&1 &
SRV=$!
echo "$SRV" >"$STATE/pid"; echo "$ROOT" >"$STATE/root"
sleep 1
URL="http://localhost:$PORT/index.html"
( xdg-open "$URL" >/dev/null 2>&1 & ) || true
echo "Serving playgrounds from $MANIFEST → $URL   [pid $SRV]"
echo "Stop: kill $SRV   (or fuser -k $PORT/tcp)"
