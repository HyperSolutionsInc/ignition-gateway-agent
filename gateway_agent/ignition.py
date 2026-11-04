"""Ignition 8.3 gateway REST client.

Uses an API key (Gateway > Security > API Keys) sent as X-Ignition-API-Token. Endpoint paths
are collected in ENDPOINTS so they can be adjusted against the gateway's own /openapi listing
without touching the call sites.
"""

from __future__ import annotations

import json
import logging
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
    "tags_delete": "/data/api/v1/tags/delete",
    "restart": "/data/api/v1/restart",
}


class IgnitionError(RuntimeError):
    pass


class IgnitionClient:
    def __init__(self, base_url: str, api_token: str, timeout: float = 30.0):
        if urllib.parse.urlsplit(base_url).scheme not in ("http", "https"):
            raise ValueError("IGNITION_URL must be http:// or https://, got %r" % base_url)
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

    def export_tags(self, provider: str, path: str) -> Any:
        query = {"provider": provider, "path": path, "type": "json", "recursive": "true"}
        return json.loads(self._request("GET", ENDPOINTS["tags_export"], query=query))

    # ------------------------------------------------------------ writes

    def import_tags(self, provider: str, base_path: str, tags: list[dict[str, Any]], policy: str = "Overwrite") -> None:
        """Import tag nodes beneath base_path (provider-relative, no [provider] prefix)."""
        query = {"provider": provider, "path": base_path, "type": "json", "collisionPolicy": policy}
        body = json.dumps({"tags": tags}).encode("utf-8")
        self._request("POST", ENDPOINTS["tags_import"], query=query, body=body)

    def delete_tags(self, provider: str, paths: list[str]) -> None:
        body = json.dumps({"provider": provider, "paths": paths}).encode("utf-8")
        self._request("POST", ENDPOINTS["tags_delete"], body=body)

    def scan_projects(self) -> None:
        self._request("POST", ENDPOINTS["scan_projects"])

    def scan_config(self) -> None:
        self._request("POST", ENDPOINTS["scan_config"])

    def restart(self) -> None:
        self._request("POST", ENDPOINTS["restart"])

    # ------------------------------------------------------------ transport

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        auth: bool = True,
    ) -> bytes:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if auth:
            if not self.api_token:
                raise IgnitionError("IGNITION_API_TOKEN is not set; create an API key in the gateway")
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
