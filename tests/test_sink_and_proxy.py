"""Sink registry, CLI and debounce proxy.

No test here reaches the internet: the "upstream" is a local stub HTTP server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import REPO_ROOT
from egress_guard.limiter import EgressGuard
from egress_guard.sink import SinkRegistry, normalize_host

CLI = os.path.join(REPO_ROOT, "bin", "egress-sink")
LINT = os.path.join(REPO_ROOT, "bin", "lint-direct-egress")


# -- host normalisation ----------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("https://api.example.com/v1/things?x=1", "api.example.com"),
        ("http://www.example.com", "example.com"),
        ("example.com:8443/path", "example.com"),
        ("HTTPS://API.Example.COM", "api.example.com"),
    ],
)
def test_host_normalisation(given, expected):
    """Spelling variants must not each get their own budget."""
    assert normalize_host(given) == expected


def test_variants_share_one_sink(tmp_path):
    reg = SinkRegistry(str(tmp_path / "sinks.json"))
    url_a, _, new_a = reg.add("https://example.com/graphql", 5.0)
    url_b, _, new_b = reg.add("https://www.example.com/other", 5.0)
    assert new_a is True and new_b is False
    assert url_a == url_b
    assert reg.hosts() == ["example.com"]


# -- ratchet ---------------------------------------------------------------


def test_interval_only_ratchets_stricter(tmp_path):
    reg = SinkRegistry(str(tmp_path / "sinks.json"))
    reg.add("https://example.com", 2.0)
    _, interval, _ = reg.add("https://example.com", 10.0)
    assert interval == 10.0
    _, interval, _ = reg.add("https://example.com", 0.1)
    assert interval == 10.0, "a faster re-registration must be ignored"


def test_cli_add_prints_proxy_url_and_not_the_upstream(tmp_path):
    cfg = str(tmp_path / "sinks.json")
    out = subprocess.run(
        [sys.executable, CLI, "--config", cfg, "add", "https://api.example.com",
         "--interval", "7"],
        capture_output=True, text=True, check=True,
    )
    assert "http://127.0.0.1:9464/s/api-example-com" in out.stdout
    assert "7s" in out.stdout
    # The upstream must not be pasteable out of stdout.
    assert "api.example.com" not in out.stdout


def test_cli_rejects_nonpositive_interval(tmp_path):
    cfg = str(tmp_path / "sinks.json")
    out = subprocess.run(
        [sys.executable, CLI, "--config", cfg, "add", "https://example.com",
         "--interval", "0"],
        capture_output=True, text=True,
    )
    assert out.returncode == 2


# -- lint ------------------------------------------------------------------


def test_lint_flags_direct_upstream_reference(tmp_path):
    cfg = str(tmp_path / "sinks.json")
    SinkRegistry(cfg).add("https://api.example.com", 5.0)
    src = tmp_path / "client.py"
    src.write_text('ENDPOINT = "https://api.example.com/graphql"\n', encoding="utf-8")

    out = subprocess.run(
        [sys.executable, LINT, "--config", cfg, str(tmp_path)],
        capture_output=True, text=True,
    )
    assert out.returncode == 1
    assert "client.py:1" in out.stdout
    assert "127.0.0.1:9464/s/api-example-com" in out.stdout


def test_lint_passes_when_code_uses_the_sink(tmp_path):
    cfg = str(tmp_path / "sinks.json")
    url, _, _ = SinkRegistry(cfg).add("https://api.example.com", 5.0)
    (tmp_path / "client.py").write_text(f'ENDPOINT = "{url}/graphql"\n', encoding="utf-8")

    out = subprocess.run(
        [sys.executable, LINT, "--config", cfg, str(tmp_path)],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stdout


def test_lint_allows_marked_lines(tmp_path):
    cfg = str(tmp_path / "sinks.json")
    SinkRegistry(cfg).add("https://api.example.com", 5.0)
    (tmp_path / "notes.md").write_text(
        "we crawl api.example.com  <!-- egress-sink:allow -->\n", encoding="utf-8"
    )
    out = subprocess.run(
        [sys.executable, LINT, "--config", cfg, str(tmp_path)],
        capture_output=True, text=True,
    )
    assert out.returncode == 0


# -- proxy -----------------------------------------------------------------


class _Stub(BaseHTTPRequestHandler):
    hits: list[float] = []

    def do_GET(self):  # noqa: N802
        _Stub.hits.append(time.time())
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def stub_upstream():
    _Stub.hits = []
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address
    srv.shutdown()


@pytest.fixture
def running_proxy(tmp_path, stub_upstream, state_dir, policy_file):
    from egress_guard import proxy as proxy_mod

    host, port = stub_upstream
    cfg = tmp_path / "sinks.json"
    doc = {
        "bind": "127.0.0.1",
        "port": 0,
        "sinks": {
            f"{host}:{port}": {  # keyed by the stub's host
                "slug": "stub", "upstream_scheme": "http", "interval_s": 1.0, "note": ""
            }
        },
    }
    # normalize_host drops the port, so register under the bare host and point
    # the proxy at the stub via an explicit netloc entry.
    doc["sinks"] = {host: {"slug": "stub", "upstream_scheme": "http",
                           "interval_s": 1.0, "note": "", "netloc": f"{host}:{port}"}}
    cfg.write_text(json.dumps(doc), encoding="utf-8")

    registry = SinkRegistry(str(cfg))
    proxy_mod._Handler.registry = registry
    proxy_mod._Handler.guard = EgressGuard(
        state_dir=state_dir, policy_path=policy_file(defaults={"min_interval_s": 0.0})
    )
    srv = proxy_mod._Server(("127.0.0.1", 0), proxy_mod._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address, registry
    srv.shutdown()


def test_proxy_spaces_requests_by_the_sink_interval(running_proxy):
    import urllib.request

    (phost, pport), _ = running_proxy
    for _ in range(3):
        with urllib.request.urlopen(f"http://{phost}:{pport}/s/stub/thing", timeout=30) as r:
            assert r.status == 200

    gaps = [b - a for a, b in zip(_Stub.hits, _Stub.hits[1:])]
    assert gaps, "upstream saw no requests"
    assert min(gaps) >= 1.0 - 0.05, f"sink interval not applied: {gaps}"


def test_proxy_refuses_with_403_and_no_retry_after(running_proxy, tmp_path):
    import urllib.error
    import urllib.request

    (phost, pport), registry = running_proxy
    host = registry.hosts()[0]
    from egress_guard import proxy as proxy_mod

    proxy_mod._Handler.guard.record_block(host, reason="bot challenge", cooldown_s=3600)

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"http://{phost}:{pport}/s/stub/thing", timeout=30)
    assert exc.value.code == 403, "429 would train retry middleware to hammer"
    assert exc.value.headers.get("Retry-After") is None
    detail = json.loads(exc.value.read())
    assert "CooldownActive" in detail["error"]
    assert "bot challenge" in detail["detail"]


def test_unknown_slug_is_a_404_naming_the_cli(running_proxy):
    import urllib.error
    import urllib.request

    (phost, pport), _ = running_proxy
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"http://{phost}:{pport}/s/nope/thing", timeout=30)
    assert exc.value.code == 404
    assert "egress-sink add" in json.loads(exc.value.read())["hint"]
