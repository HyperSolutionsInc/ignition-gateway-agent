"""Ignition 8.3 gateway REST client.

Uses an API key sent as `X-Ignition-API-Token: <name>:<key>` (bootstrap.py mints it). The
paths in ENDPOINTS are checked against Ignition 8.3.7's own /openapi.json. 8.3.7 has no
REST call that deletes tags, so the installer deletes them from the tag-definition files and
asks for a config scan.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

ENDPOINTS = {
    "status": "/StatusPing",
    "gateway_info": "/data/api/v1/gateway-info",
    "scan_projects": "/data/api/v1/scan/projects",
    "scan_config": "/data/api/v1/scan/config",
    "tags_import": "/data/api/v1/tags/import",
    "tags_export": "/data/api/v1/tags/export",
    "projects_list": "/data/api/v1/projects/list",
    "restart": "/data/api/v1/restart-tasks/restart",
}


_TOKEN_RE = re.compile(r"[\x21-\x7e]{8,1024}")


class IgnitionError(RuntimeError):
    pass


class IgnitionClient:
    def __init__(self, base_url: str, api_token: str, timeout: float = 30.0):
        if urllib.parse.urlsplit(base_url).scheme not in ("http", "https"):
            raise ValueError("IGNITION_URL must be http:// or https://, got %r" % base_url)
        # The token goes into a request header: refuse anything that could break out of it.
        if api_token and not _TOKEN_RE.fullmatch(api_token):
            raise ValueError("IGNITION_API_TOKEN must be 8-1024 printable characters with no spaces")
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout

    # ------------------------------------------------------------ reads

    def status(self) -> str:
        """Gateway run state ("RUNNING", "STARTING", ...) or "UNREACHABLE"."""
        try:
            body = self._request("GET", ENDPOINTS["status"], auth=False)
            return str(json.loads(body).get("state", "UNKNOWN"))
        except (IgnitionError, ValueError):
            return "UNREACHABLE"

    def gateway_info(self) -> dict[str, Any]:
        return json.loads(self._request("GET", ENDPOINTS["gateway_info"]))

    def project_names(self) -> list[str]:
        body = json.loads(self._request("GET", ENDPOINTS["projects_list"]))
        return [p.get("name") for p in body.get("items", [])]

    def export_tags(self, provider: str, path: str) -> Any:
        query = {"provider": provider, "path": path, "type": "json", "recursive": "true"}
        return json.loads(self._request("GET", ENDPOINTS["tags_export"], query=query))

    # ------------------------------------------------------------ writes

    def import_tags(self, provider: str, base_path: str, node: dict[str, Any], policy: str = "Overwrite") -> None:
        """Import one tag node (a folder and everything in it) beneath base_path.

        The body is the node's JSON as an export file, sent as octet-stream. `Overwrite`
        replaces tags that exist but leaves tags absent from the node in place.
        """
        query = {"provider": provider, "path": base_path, "type": "json", "collisionPolicy": policy}
        body = json.dumps(node).encode("utf-8")
        result = json.loads(self._request("POST", ENDPOINTS["tags_import"], query=query, body=body,
                                          content_type="application/octet-stream") or b"{}")
        if result.get("failureCount"):
            raise IgnitionError("tag import reported %d failure(s): %s" % (
                result["failureCount"], json.dumps(result.get("failures"))[:400]))

    def scan_projects(self) -> None:
        self._request("POST", ENDPOINTS["scan_projects"])

    def scan_config(self) -> None:
        self._request("POST", ENDPOINTS["scan_config"])

    def restart(self) -> None:
        self._request("POST", ENDPOINTS["restart"], query={"confirm": "true"})

    def wait_until_running(self, timeout_s: float, poll_s: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.status() == "RUNNING":
                return True
            time.sleep(poll_s)
        return self.status() == "RUNNING"

    # ------------------------------------------------------------ transport

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        auth: bool = True,
        content_type: str = "application/json",
    ) -> bytes:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", content_type)
        if auth:
            if not self.api_token:
                raise IgnitionError("no Ignition API key; it is minted at first boot (bootstrap) or set by IGNITION_API_TOKEN")
            req.add_header("X-Ignition-API-Token", self.api_token)
        try:
            # base_url's scheme is checked in __init__ and path/query are built here, never taken from input.
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec B310
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", "replace")
            raise IgnitionError("%s %s -> HTTP %d: %s" % (method, path, exc.code, detail)) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise IgnitionError("%s %s failed: %s" % (method, path, exc)) from exc
