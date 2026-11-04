"""Local HTTP API.

  POST   /v1/telemetry           timer scripts post tag readings here (hyper.publisher)
  POST   /v1/packages            upload a HAMF export (.json or .zip body); ?ims=KEY to name it
  GET    /v1/packages            installed IMS packages
  DELETE /v1/packages/<ims>      remove an IMS (its tags and timer)
  PATCH  /v1/packages/<ims>      {"enabled": bool, "interval_ms": int}
  GET    /v1/status              health summary (same content the shadow reports)
  GET    /healthz                liveness

The API is meant for the gateway's own Docker network: publish its port on loopback or the
plant LAN only.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .hamf import HamfError
from .installer import Installer
from .telemetry import Telemetry, TelemetryError

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024 * 1024


def make_server(
    host: str,
    port: int,
    *,
    telemetry: Telemetry,
    installer: Installer,
    status: Callable[[], dict[str, Any]],
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "gateway-agent"

        def log_message(self, fmt: str, *args: Any) -> None:  # route through logging, quietly
            log.debug("%s %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:
            path = self._path()
            if path == "/healthz":
                return self._send(200, {"ok": True})
            if path == "/v1/status":
                return self._send(200, status())
            if path == "/v1/packages":
                return self._send(200, installer.store.packages())
            return self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            path = self._path()
            body = self._body()
            if body is None:
                return
            if path == "/v1/telemetry":
                try:
                    queued = telemetry.accept(json.loads(body))
                except (ValueError, TelemetryError) as exc:
                    return self._send(400, {"error": str(exc)})
                return self._send(200, {"queued": queued})
            if path == "/v1/packages":
                query = self._query()
                filename = query.get("filename") or ("upload.zip" if body[:2] == b"PK" else "upload.json")
                try:
                    result = installer.install(body, filename, "http", ims_key=query.get("ims") or None)
                except HamfError as exc:
                    return self._send(422, {"error": str(exc)})
                except Exception as exc:
                    log.exception("install failed")
                    return self._send(500, {"error": str(exc)})
                return self._send(200, result.__dict__)
            return self._send(404, {"error": "not found"})

        def do_PATCH(self) -> None:
            ims = self._ims_from_path()
            body = self._body()
            if ims is None or body is None:
                return
            opts = json.loads(body or b"{}")
            try:
                result = installer.reconfigure(ims, enabled=opts.get("enabled"), interval_ms=opts.get("interval_ms"))
            except KeyError as exc:
                return self._send(404, {"error": str(exc)})
            return self._send(200, result.__dict__)

        def do_DELETE(self) -> None:
            ims = self._ims_from_path()
            if ims is None:
                return
            removed = installer.remove(ims, "http")
            return self._send(200 if removed else 404, {"removed": removed})

        # -------------------------------------------------------- helpers

        def _path(self) -> str:
            return urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"

        def _query(self) -> dict[str, str]:
            return {k: v[-1] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

        def _ims_from_path(self) -> str | None:
            prefix = "/v1/packages/"
            path = self._path()
            if not path.startswith(prefix) or len(path) == len(prefix):
                self._send(404, {"error": "not found"})
                return None
            return urllib.parse.unquote(path[len(prefix):])

        def _body(self) -> bytes | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(413, {"error": "body too large"})
                return None
            return self.rfile.read(length) if length else b""

        def _send(self, code: int, obj: Any) -> None:
            data = json.dumps(obj, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def serve_in_background(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    thread.start()
    return thread
