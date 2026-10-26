"""Local debounce proxy: the address that carries the cadence.

Requests arrive at `http://127.0.0.1:PORT/s/<slug>/<path>` and are forwarded to
the registered upstream only after the shared limiter grants a slot for that
upstream's HOST. Every client on this machine that points here — crawler,
throwaway probe, subagent, curl — shares one cadence, one budget, one cooldown,
because they share the address rather than agreeing on a parameter.

Deliberate choices:

* A refusal is **403, without `Retry-After`**. 429 plus Retry-After is what every
  retry middleware is built to wait out and repeat; a request that must not be
  made is not a request to try again in a moment.
* Cost is weighed by BYTES, request plus response. The proxy cannot see that a
  GraphQL body asked for 5000 rows, and a per-request cost of 1 would quietly
  retire the budget that exists for exactly those queries.
* The upstream sees this proxy's own honest User-Agent. Nothing here disguises
  what we are; the goal is less traffic, not different-looking traffic.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from egress_guard.limiter import EgressGuard, EgressGuardError
from egress_guard.sink import SinkRegistry

# One cost unit is roughly 50 rows; bytes are the only proxy-visible proxy for
# "how much did this cost the origin", so ~25KB of traffic counts as one unit.
BYTES_PER_COST_UNIT = 25_000

USER_AGENT = os.environ.get(
    "EGRESS_SINK_USER_AGENT",
    "egress-sink/1.0 (local debounce proxy; automated but rate-limited)",
)

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "egress-sink"
    registry: SinkRegistry
    guard: EgressGuard

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write("[egress-sink] " + (fmt % args) + "\n")

    # -- helpers --------------------------------------------------------------

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # NO Retry-After, deliberately: see module docstring.
        self.end_headers()
        self.wfile.write(body)

    def _route(self) -> tuple[dict[str, Any], str] | None:
        parts = urlsplit(self.path)
        segments = parts.path.split("/", 3)
        # ["", "s", "<slug>", "rest..."]
        if len(segments) < 3 or segments[1] != "s":
            self._send_json(404, {
                "error": "not a sink path",
                "expected": "/s/<slug>/<upstream path>",
                "registered": self.registry.hosts(),
            })
            return None
        entry = self.registry.resolve(segments[2])
        if entry is None:
            self._send_json(404, {
                "error": f"no sink registered with slug {segments[2]!r}",
                "hint": "register it: egress-sink add <upstream-url> --interval <seconds>",
            })
            return None
        rest = segments[3] if len(segments) > 3 else ""
        # `netloc` exists so a sink can point at a non-default port (a local
        # stub in tests, an internal service); the LIMITER is still keyed by the
        # bare host, so variants cannot each acquire their own budget.
        upstream = urlunsplit((
            entry.get("upstream_scheme", "https"), entry.get("netloc") or entry["host"],
            "/" + rest, parts.query, "",
        ))
        return entry, upstream

    # -- the one method that matters ------------------------------------------

    def _proxy(self) -> None:
        routed = self._route()
        if routed is None:
            return
        entry, upstream = routed
        host = entry["host"]

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        # Charge for what we are about to send; the response is charged after.
        cost = max(1.0, (length or 0) / BYTES_PER_COST_UNIT)

        try:
            grant = self.guard.acquire(
                host, cost=cost, min_interval_s=float(entry["interval_s"])
            )
        except EgressGuardError as exc:
            # A hard stop, not a hiccup. 403 so no retry layer waits it out.
            self._send_json(403, {
                "error": type(exc).__name__,
                "resource": host,
                "detail": str(exc),
                "note": (
                    "Refused by the local egress guard. Do NOT retry, do not point "
                    "at the upstream directly, and do not raise the limit in code — "
                    "widening happens in the egress policy file, deliberately."
                ),
            })
            return

        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP
        }
        headers["User-Agent"] = USER_AGENT
        req = urllib.request.Request(upstream, data=body, headers=headers,
                                     method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                payload = resp.read()
                status, resp_headers = resp.status, resp.headers
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            status, resp_headers = exc.code, exc.headers
            if status in (403, 429, 503):
                # Pushback. Record it so EVERY client on this machine is refused,
                # not just the one that happened to see the response.
                self.guard.record_block(
                    host, reason=f"upstream returned {status}"
                )
        except (urllib.error.URLError, TimeoutError) as exc:
            self._send_json(502, {"error": "upstream unreachable", "detail": str(exc)})
            return

        # Charge the response too, now that its size is known.
        try:
            self.guard.record_extra_cost(host, len(payload) / BYTES_PER_COST_UNIT)
        except EgressGuardError:
            pass  # the request already went out; refusing now would be theatre

        self.send_response(status)
        for key, value in (resp_headers or {}).items():
            if key.lower() not in HOP_BY_HOP and key.lower() != "content-length":
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Egress-Sink-Waited", f"{grant.waited_s:.3f}")
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _proxy  # noqa: N815


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(config_path: str | None = None, state_dir: str | None = None,
          policy_path: str | None = None) -> None:
    registry = SinkRegistry(config_path) if config_path else SinkRegistry()
    doc = registry.load()
    _Handler.registry = registry
    _Handler.guard = EgressGuard(state_dir=state_dir, policy_path=policy_path)
    with _Server((doc["bind"], int(doc["port"])), _Handler) as httpd:
        sys.stderr.write(
            f"[egress-sink] listening on http://{doc['bind']}:{doc['port']} "
            f"for {len(doc['sinks'])} sink(s)\n"
        )
        httpd.serve_forever()


if __name__ == "__main__":
    serve()
