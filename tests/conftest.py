from __future__ import annotations

import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from gateway_agent.config import Settings
from gateway_agent.ignition import IgnitionClient
from gateway_agent.installer import Installer
from gateway_agent.store import Store


def leaf(name, tag_id, asset_id, asset_path, data_type="Float8", **extra):
    node = {
        "name": name,
        "tagType": "AtomicTag",
        "opcItemPath": "[dev]3/%d" % tag_id,
        "dataType": data_type,
        "tag_id": tag_id,
        "asset_id": asset_id,
        "asset_path": asset_path,
        "signal_path": name,
    }
    node.update(extra)
    return node


def folder(name, *children):
    return {"name": name, "tagType": "Folder", "tags": list(children)}


def provider(*children):
    return {"name": "", "tagType": "Provider", "tags": list(children)}


def ims_tree(ims="FWT-02", devices=("ENV",), base_id=100):
    """Customer/campus/building/IMS/PPM/<device>/<tags>, the shape HAMF exports."""
    dev_folders = []
    for i, dev in enumerate(devices):
        ap = "Switch.Petersburg.ACI.%s.PPM.%s" % (ims, dev)
        dev_folders.append(
            folder(dev, leaf("Temperature", base_id + 2 * i, 10 + i, ap), leaf("Leak", base_id + 2 * i + 1, 10 + i, ap, "Boolean"))
        )
    return provider(folder("Switch", folder("Petersburg", folder("ACI", folder(ims, folder("PPM", *dev_folders))))))


def to_bytes(obj):
    return json.dumps(obj).encode("utf-8")


def zipped(files: dict[str, object]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, obj in files.items():
            zf.writestr(name, json.dumps(obj))
    return buf.getvalue()


class FakeIgnition:
    """Records REST calls the agent makes; stands in for the gateway's /data/api/v1."""

    def __init__(self):
        self.calls = []
        self.fail_import = False

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body=b"{}"):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/StatusPing":
                    return self._reply(200, b'{"state":"RUNNING"}')
                outer.calls.append(("GET", self.path, None, self.headers.get("X-Ignition-API-Token")))
                return self._reply(200)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.calls.append(("POST", self.path, body, self.headers.get("X-Ignition-API-Token")))
                if outer.fail_import and self.path.startswith("/data/api/v1/tags/import"):
                    return self._reply(500, b"boom")
                return self._reply(200)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def paths(self, method="POST"):
        return [c[1].split("?")[0] for c in self.calls if c[0] == method]


@pytest.fixture
def fake_ignition():
    fake = FakeIgnition()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def settings(tmp_path: Path, fake_ignition):
    s = Settings()
    s.state_dir = tmp_path / "state"
    s.ignition_data_dir = tmp_path / "ignition-data"
    s.ignition_url = fake_ignition.url
    s.ignition_api_token = "test-token"
    s.thing_name = "gw-test"
    s.tag_root = ""
    s.ims_path_depth = 4
    s.timer_interval_ms = 1000
    s.agent_url_for_ignition = "http://gateway-agent:8600"
    return s


@pytest.fixture
def store(settings):
    return Store(settings.db_path)


@pytest.fixture
def installer(settings, store):
    return Installer(settings, store, IgnitionClient(settings.ignition_url, settings.ignition_api_token))
