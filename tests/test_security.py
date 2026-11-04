"""Security controls added after review round 1 (R-0004): API auth, URL schemes, token storage."""

import json
import os
import stat
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from gateway_agent import shadow
from gateway_agent.config import Settings
from gateway_agent.ignition import IgnitionClient
from gateway_agent.server import make_server
from gateway_agent.telemetry import Telemetry
from conftest import ims_tree, to_bytes


@pytest.fixture
def agent(settings, store, installer):
    server = make_server("127.0.0.1", 0, telemetry=Telemetry(settings, store), installer=installer,
                         status=lambda: {"ok": True}, api_token=settings.api_token,
                         ingest_token=settings.ingest_token)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()


def call(url, method="GET", body=None, token=None, bearer=False):
    headers = {}
    if token is not None:
        headers = {"Authorization": "Bearer " + token} if bearer else {"X-Agent-Token": token}
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


TELEMETRY = json.dumps({"ims": "FWT-02", "version": "v", "ticks": [{"ts": 1, "rows": [[1, 2.0]]}]}).encode()


def test_healthz_is_open(agent):
    assert call(agent + "/healthz") == 200


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/v1/status", None),
    ("GET", "/v1/packages", None),
    ("POST", "/v1/packages", b"{}"),
    ("PATCH", "/v1/packages/FWT-02", b"{}"),
    ("DELETE", "/v1/packages/FWT-02", None),
    ("POST", "/v1/telemetry", TELEMETRY),
])
def test_everything_else_needs_a_token(agent, method, path, body):
    assert call(agent + path, method, body) == 401
    assert call(agent + path, method, body, token="wrong-token-0123456789") == 401


def test_admin_token_works_both_header_styles(agent, settings):
    assert call(agent + "/v1/packages", token=settings.api_token) == 200
    assert call(agent + "/v1/packages", token=settings.api_token, bearer=True) == 200


def test_ingest_token_only_grants_telemetry(agent, settings):
    assert call(agent + "/v1/telemetry", "POST", TELEMETRY, token=settings.ingest_token) == 200
    assert call(agent + "/v1/packages", token=settings.ingest_token) == 401
    assert call(agent + "/v1/packages", "POST", to_bytes(ims_tree()), token=settings.ingest_token) == 401
    assert call(agent + "/v1/packages/FWT-02", "DELETE", token=settings.ingest_token) == 401


def test_admin_token_does_not_grant_telemetry(agent, settings):
    assert call(agent + "/v1/telemetry", "POST", TELEMETRY, token=settings.api_token) == 401


def test_server_refuses_to_start_without_tokens(settings, store, installer):
    with pytest.raises(ValueError):
        make_server("127.0.0.1", 0, telemetry=Telemetry(settings, store), installer=installer,
                    status=dict, api_token="", ingest_token="x")


def test_tokens_are_generated_once_with_private_permissions(tmp_path):
    s = Settings()
    s.state_dir = tmp_path
    s.api_token = s.ingest_token = ""
    s.ensure_tokens()
    assert len(s.api_token) >= 32 and len(s.ingest_token) >= 32 and s.api_token != s.ingest_token
    for name in ("api_token", "ingest_token"):
        mode = stat.S_IMODE(os.stat(tmp_path / name).st_mode)
        assert mode == 0o600
    again = Settings()
    again.state_dir = tmp_path
    again.api_token = again.ingest_token = ""
    again.ensure_tokens()
    assert (again.api_token, again.ingest_token) == (s.api_token, s.ingest_token)
    assert again.read_api_token() == s.api_token


def test_refresh_shared_rewrites_publisher_after_token_rotation(settings, installer, fake_ignition):
    installer.install(to_bytes(ims_tree()), "x.json", "cli")
    publisher = settings.project_dir / "ignition/script-python/hyper/publisher/code.py"
    assert installer.refresh_shared() is False
    settings.ingest_token = "rotated-token-0123456789abcdef"
    fake_ignition.calls.clear()
    assert installer.refresh_shared() is True
    assert "rotated-token-0123456789abcdef" in publisher.read_text()
    assert fake_ignition.paths() == ["/data/api/v1/scan/projects"]


def test_render_does_not_embed_the_live_token(settings, installer, tmp_path):
    settings.ingest_token = ""
    installer.render_to(to_bytes(ims_tree()), "x.json", tmp_path / "out")
    source = (tmp_path / "out/project/ignition/script-python/hyper/publisher/code.py").read_text()
    assert "REPLACE_WITH_AGENT_INGEST_TOKEN" in source


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x"])
def test_ignition_client_rejects_non_http_schemes(url):
    with pytest.raises(ValueError):
        IgnitionClient(url, "t")


@pytest.mark.parametrize("url", ["http://example.com/p.zip", "file:///etc/passwd", "ftp://example.com/p.zip"])
def test_package_download_requires_https(url, monkeypatch):
    monkeypatch.delenv("ALLOW_HTTP_PACKAGE_URLS", raising=False)
    with pytest.raises(ValueError, match="https"):
        shadow._download(url)


def test_package_download_refuses_redirect_to_other_scheme(monkeypatch):
    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            # The stdlib already refuses file: redirects but follows ftp:, which this must not.
            self.send_header("Location", "ftp://example.com/p.zip")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ALLOW_HTTP_PACKAGE_URLS", "1")  # lets the test server use http for the first hop
    try:
        with pytest.raises(ValueError, match="disallowed scheme"):
            shadow._download("http://127.0.0.1:%d/p.zip" % srv.server_address[1])
    finally:
        srv.shutdown()
