import sqlite3, subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

DB = sqlite3.connect("app.db", check_same_thread=False)


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        if self.path.startswith("/user"):
            rows = DB.execute(f"SELECT name FROM users WHERE id = {q['id'][0]}").fetchall()
            self.send_response(200); self.end_headers(); self.wfile.write(str(rows).encode())
        elif self.path.startswith("/ping"):
            out = subprocess.run(f"ping -c1 {q['host'][0]}", shell=True, capture_output=True)
            self.send_response(200); self.end_headers(); self.wfile.write(out.stdout)


HTTPServer(("0.0.0.0", 8080), H).serve_forever()
