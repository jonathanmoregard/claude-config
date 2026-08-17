"""Registry of debounced sinks: local URLs that front a rate-sensitive upstream.

The point is to move the guarantee from a PARAMETER to an ADDRESS. A delay
argument is a knob — a caller can pass a weaker value and still succeed. A URL
is binary: point at the sink and you get its cadence, whether you meant to or
not, whether you are a crawler or a one-off probe.

Sinks are keyed by normalized upstream HOST, never by slug or full URL, so
`example.com`, `www.example.com` and `example.com/v2/thing` all share one floor.
Registering the same host twice RATCHETS to the stricter interval; it never
loosens, because "register it again, faster" would recreate the knob.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional
from urllib.parse import urlsplit

DEFAULT_CONFIG = os.environ.get(
    "EGRESS_SINK_CONFIG",
    os.path.join(os.path.expanduser("~"), ".claude", "egress-sinks.json"),
)
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 9464

# Conservative when the host publishes nothing: slower than any documented
# crawl-delay we have seen, because an unmeasured host is an unknown one.
FALLBACK_INTERVAL_S = 5.0

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def normalize_host(url_or_host: str) -> str:
    """Return the bare lowercase host: the unit a server actually rate-limits.

    Strips scheme, port, path, and a leading `www.` so URL spelling variants
    cannot each acquire their own budget.
    """
    raw = (url_or_host or "").strip()
    if not raw:
        raise ValueError("empty upstream")
    if "//" not in raw:
        raw = "https://" + raw
    host = (urlsplit(raw).hostname or "").lower()
    if not host:
        raise ValueError(f"cannot parse a host out of {url_or_host!r}")
    return host[4:] if host.startswith("www.") else host


def slug_for(host: str) -> str:
    return _SLUG_RE.sub("-", host).strip("-")


class SinkRegistry:
    def __init__(self, path: str = DEFAULT_CONFIG):
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            doc = {}
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"sink config {self.path} is unreadable: {exc}") from exc
        if not isinstance(doc, dict):
            raise RuntimeError(f"sink config {self.path} is not an object")
        doc.setdefault("bind", DEFAULT_BIND)
        doc.setdefault("port", DEFAULT_PORT)
        doc.setdefault("sinks", {})
        return doc

    def save(self, doc: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # -- queries --------------------------------------------------------------

    def hosts(self) -> list[str]:
        return sorted(self.load()["sinks"].keys())

    def resolve(self, slug: str) -> Optional[dict[str, Any]]:
        for host, entry in self.load()["sinks"].items():
            if entry.get("slug") == slug:
                return dict(entry, host=host)
        return None

    def proxy_url(self, host: str, doc: Optional[dict] = None) -> str:
        doc = doc or self.load()
        entry = doc["sinks"][host]
        return f"http://{doc['bind']}:{doc['port']}/s/{entry['slug']}"

    # -- mutation -------------------------------------------------------------

    def add(self, upstream: str, interval_s: Optional[float] = None,
            note: str = "") -> tuple[str, float, bool]:
        """Register (or ratchet) a sink. Returns (proxy_url, effective_interval, is_new).

        An interval may only ever get STRICTER. Re-registering a host with a
        faster interval is silently ignored rather than honoured, because the
        whole design exists to remove the faster-please knob.
        """
        host = normalize_host(upstream)
        scheme = urlsplit(upstream if "//" in upstream else "https://" + upstream).scheme
        requested = FALLBACK_INTERVAL_S if interval_s is None else float(interval_s)
        if requested <= 0:
            raise ValueError("interval must be positive")

        doc = self.load()
        entry = doc["sinks"].get(host)
        is_new = entry is None
        if entry is None:
            entry = {
                "slug": slug_for(host),
                "upstream_scheme": scheme or "https",
                "interval_s": requested,
                "note": note,
            }
            doc["sinks"][host] = entry
        else:
            entry["interval_s"] = max(float(entry.get("interval_s", 0.0)), requested)
            if note:
                entry["note"] = note
        self.save(doc)
        return self.proxy_url(host, doc), float(entry["interval_s"]), is_new
