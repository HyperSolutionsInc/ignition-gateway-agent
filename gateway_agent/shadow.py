"""Device Shadow handling: config delivery to the gateway and health reporting back.

Named shadows on the gateway's thing:

  gateway        desired: ims list, rescan/restart nonces, publish_paused, telemetry_rule/topic
                 reported: the same keys + health (Ignition state, packages, outbox, link, cert)

  ims-<key>      one per IMS. desired: version, sha256, source, filename, enabled, interval_ms,
                 remove. reported: the same keys + status, tag_count, tag_folder, error, applied_at.

A shadow document is capped at 8 KB, so package content never sits in the shadow itself. The
`source` field says where to get it: an HTTPS URL (an S3 presigned URL, for example), an
inline gzip+base64 blob for small packages, or "mqtt" to have the cloud stream it in chunks
on hyper/<thing>/package/chunk. Content is checked against desired.sha256
before anything is installed. See docs/SHADOW.md for the full contract.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import ssl
import threading
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable

from . import __version__
from .config import Settings
from .installer import Installer
from .store import Store

log = logging.getLogger(__name__)

GATEWAY_SHADOW = "gateway"
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
CHUNK_TIMEOUT_S = 120


def ims_shadow_name(ims: str) -> str:
    return "ims-" + re.sub(r"[^A-Za-z0-9:_-]", "_", ims)[:60]


class ShadowAgent:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        installer: Installer,
        link: Any,
        *,
        health: Callable[[], dict[str, Any]],
        publisher: Any = None,
        ignition: Any = None,
    ):
        self.settings = settings
        self.store = store
        self.installer = installer
        self.link = link
        self.health = health
        self.publisher = publisher
        self.ignition = ignition
        self.thing = settings.thing_name
        self._names: dict[str, str] = {}  # shadow name -> IMS key
        self._work: queue.Queue = queue.Queue()
        self._chunks: dict[str, dict[str, Any]] = {}
        self._chunk_lock = threading.Lock()
        self.last_error: str | None = None

    # ------------------------------------------------------------ wiring

    def topic(self, name: str, suffix: str) -> str:
        return "$aws/things/%s/shadow/name/%s/%s" % (self.thing, name, suffix)

    def start(self) -> None:
        base = "$aws/things/%s/shadow/name/+/" % self.thing
        self.link.subscribe(base + "get/accepted", self._on_document)
        self.link.subscribe(base + "get/rejected", self._on_rejected)
        self.link.subscribe(base + "update/delta", self._on_delta)
        self.link.subscribe(base + "update/rejected", self._on_rejected)
        self.link.subscribe(self.settings.package_topic("chunk"), self._on_chunk)
        self.link.on_connect(self.sync_all)
        threading.Thread(target=self._worker, name="shadow-worker", daemon=True).start()
        threading.Thread(target=self._health_loop, name="shadow-health", daemon=True).start()

    def sync_all(self) -> None:
        """Ask for every shadow we care about; deltas sent while offline are not replayed."""
        self.request(GATEWAY_SHADOW)
        for pkg in self.store.packages():
            if pkg["status"] != "removed":
                self.request(self._register(pkg["ims"]))

    def request(self, name: str) -> None:
        self.link.publish(self.topic(name, "get"), b"{}", qos=1, timeout=10)

    def _register(self, ims: str) -> str:
        name = ims_shadow_name(ims)
        self._names[name] = ims
        return name

    # ------------------------------------------------------------ inbound

    def _on_delta(self, topic: str, payload: bytes) -> None:
        # The delta only lists changed keys; fetch the whole document and act on that.
        self.request(_shadow_name(topic))

    def _on_document(self, topic: str, payload: bytes) -> None:
        doc = json.loads(payload or b"{}")
        state = doc.get("state") or {}
        self._work.put((_shadow_name(topic), state.get("desired") or {}, state.get("reported") or {},
                        doc.get("version")))

    def _on_rejected(self, topic: str, payload: bytes) -> None:
        name = _shadow_name(topic)
        try:
            err = json.loads(payload)
        except ValueError:
            err = {"message": payload[:200].decode("utf-8", "replace")}
        if err.get("code") == 404 and topic.endswith("get/rejected"):
            return  # shadow not created yet; nothing desired
        if err.get("code") == 409 and topic.endswith("update/rejected"):
            # A versioned interim report lost to a newer update (see _apply_ims): expected.
            log.debug("shadow %s: stale interim report dropped", name)
            return
        self.last_error = "shadow %s: %s" % (name, err.get("message"))
        log.warning("shadow %s rejected: %s", name, err)

    def _on_chunk(self, topic: str, payload: bytes) -> None:
        msg = json.loads(payload)
        with self._chunk_lock:
            pending = self._chunks.get(msg.get("request_id"))
            if pending is None:
                return
            pending["parts"][int(msg["seq"])] = base64.b64decode(msg["data"])
            pending["total"] = int(msg["total"])
            if len(pending["parts"]) == pending["total"]:
                pending["done"].set()

    # ------------------------------------------------------------ worker

    def _worker(self) -> None:
        while True:
            name, desired, reported, version = self._work.get()
            try:
                if name == GATEWAY_SHADOW:
                    self._apply_gateway(desired, reported)
                elif name.startswith("ims-"):
                    self._apply_ims(name, desired, reported, version)
            except Exception as exc:
                self.last_error = "%s: %s" % (name, exc)
                log.exception("applying shadow %s failed", name)

    def _apply_gateway(self, desired: dict[str, Any], reported: dict[str, Any]) -> None:
        update: dict[str, Any] = {}
        for ims in desired.get("ims") or []:
            self.request(self._register(str(ims)))
        if "ims" in desired:
            update["ims"] = desired["ims"]

        if "publish_paused" in desired and self.publisher is not None:
            self.publisher.paused = bool(desired["publish_paused"])
            update["publish_paused"] = self.publisher.paused
        if desired.get("telemetry_topic"):
            self.settings.telemetry_topic = str(desired["telemetry_topic"])
            update["telemetry_topic"] = self.settings.telemetry_topic
        if isinstance(desired.get("telemetry_rule"), str):
            # "" switches to plain pub/sub, e.g. to watch the stream in the MQTT test client.
            self.settings.telemetry_rule = desired["telemetry_rule"]
            update["telemetry_rule"] = self.settings.telemetry_rule

        if "rescan" in desired and desired["rescan"] != reported.get("rescan"):
            self.ignition.scan_projects()
            update["rescan"] = desired["rescan"]
        if "restart" in desired and desired["restart"] != reported.get("restart"):
            if self.settings.allow_remote_restart:
                self.ignition.restart()
                update["restart"] = desired["restart"]
            else:
                update["restart_refused"] = "ALLOW_REMOTE_RESTART is off on this gateway"

        if update:
            self._report_changes(GATEWAY_SHADOW, update, reported)

    def _apply_ims(self, name: str, desired: dict[str, Any], reported: dict[str, Any],
                   doc_version: int | None = None) -> None:
        ims = self._names.get(name) or name[len("ims-"):]
        if not desired:
            return
        local = self.store.package(ims)

        if desired.get("remove"):
            if local and local["status"] != "removed":
                self.installer.remove(ims, "shadow")
            self._report_changes(name, {"remove": True, "status": "removed", "error": None, "applied_at": _now()},
                                 reported)
            return

        # Mirror every desired key the cloud sets (remove: false included), or the delta never clears.
        mirror = {k: desired.get(k) for k in ("version", "sha256", "filename", "enabled", "interval_ms", "remove")}
        local_sha = self.store.get("shadow_sha:" + ims)
        installed = local is not None and local["status"] == "installed"

        try:
            if desired.get("sha256") and (desired["sha256"] != local_sha or not installed):
                if not desired.get("source"):
                    return  # the cloud will set a source; nothing to fetch yet
                # AWS can apply two quick updates out of order (seen live: a fast failure was
                # overwritten by this report). Pinned to the document version it was read at, it
                # is rejected (409) if the final report gets there first.
                self._report(name, {"status": "downloading", "error": None}, version=doc_version)
                data = self._fetch(ims, desired)
                result = self.installer.install(
                    data,
                    desired.get("filename") or "%s.json" % ims,
                    "shadow",
                    ims_key=ims,
                    enabled=desired.get("enabled"),
                    interval_ms=desired.get("interval_ms"),
                )
                self.store.put("shadow_sha:" + ims, desired["sha256"])
            elif installed and _timer_settings_changed(local["manifest"], desired):
                result = self.installer.reconfigure(ims, enabled=desired.get("enabled"), interval_ms=desired.get("interval_ms"))
            elif installed:
                manifest = local["manifest"]
                self._report_changes(name, {**mirror, "status": "installed", "package_version": local["version"],
                                            "tag_count": manifest.get("tag_count"),
                                            "tag_folder": manifest.get("tag_folder"), "error": None},
                                     reported, clear_source="source" in desired)
                return
            else:
                return
        except Exception as exc:
            self._report(name, {**mirror, "status": "failed", "error": str(exc)[:500], "applied_at": _now()})
            raise

        self._report(
            name,
            {**mirror, "status": "installed", "package_version": result.version, "tag_count": result.tag_count,
             "tag_folder": result.tag_folder, "error": None, "applied_at": _now()},
            clear_source=True,
        )

    # ------------------------------------------------------------ package fetch

    def _fetch(self, ims: str, desired: dict[str, Any]) -> bytes:
        source = desired["source"]
        kind = source.get("type") if isinstance(source, dict) else source
        if kind == "url":
            data = _download(source["url"])
        elif kind == "inline":
            data = base64.b64decode(source["data"])
            if source.get("encoding", "gzip+base64") == "gzip+base64":
                data = gzip.decompress(data)
        elif kind == "mqtt":
            data = self._fetch_over_mqtt(ims, desired["sha256"])
        else:
            raise ValueError("unknown package source %r" % (kind,))
        digest = hashlib.sha256(data).hexdigest()
        if digest != desired["sha256"]:
            raise ValueError("package sha256 %s does not match desired %s" % (digest, desired["sha256"]))
        return data

    def _fetch_over_mqtt(self, ims: str, sha256: str) -> bytes:
        request_id = uuid.uuid4().hex
        pending = {"parts": {}, "total": None, "done": threading.Event()}
        with self._chunk_lock:
            self._chunks[request_id] = pending
        try:
            body = json.dumps({"ims": ims, "request_id": request_id, "sha256": sha256}).encode("utf-8")
            if not self.link.publish(self.settings.package_topic("request"), body, qos=1):
                raise RuntimeError("could not request package over MQTT (not connected)")
            if not pending["done"].wait(CHUNK_TIMEOUT_S):
                raise TimeoutError("package %s: got %d/%s chunks" % (ims, len(pending["parts"]), pending["total"]))
            return b"".join(pending["parts"][i] for i in range(pending["total"]))
        finally:
            with self._chunk_lock:
                self._chunks.pop(request_id, None)

    # ------------------------------------------------------------ outbound

    def _report_changes(self, name: str, update: dict[str, Any], reported: dict[str, Any],
                        clear_source: bool = False) -> None:
        """Report only what the shadow doesn't already say. AWS sends a fresh delta after every
        accepted update while any desired key differs from reported, so a no-op report answering
        a delta would be answered by another delta, forever (a refused restart, or a removed IMS
        whose desired still names a package, keep a delta open)."""
        volatile = ("applied_at",)
        if not clear_source and all(reported.get(k) == v for k, v in update.items() if k not in volatile):
            return
        self._report(name, update, clear_source=clear_source)

    def _report(self, name: str, reported: dict[str, Any], clear_source: bool = False,
                version: int | None = None) -> None:
        state: dict[str, Any] = {"reported": reported}
        if clear_source:
            # Presigned URLs and inline blobs are single-use; clearing them keeps the delta empty.
            state["desired"] = {"source": None}
        update: dict[str, Any] = {"state": state}
        if version is not None:
            update["version"] = version
        body = json.dumps(update).encode("utf-8")
        if not self.link.publish(self.topic(name, "update"), body, qos=1):
            log.warning("could not report shadow %s (offline)", name)

    def _health_loop(self) -> None:
        while True:
            time.sleep(self.settings.health_interval_s)
            try:
                if self.link.connected.is_set():
                    self._report(GATEWAY_SHADOW, {"health": self.health()})
            except Exception:
                log.exception("health report failed")


def build_health(settings: Settings, store: Store, link: Any, publisher: Any, telemetry: Any, ignition: Any,
                 shadow: ShadowAgent | None) -> dict[str, Any]:
    """Compact gateway health for the shadow (kept well under the 8 KB document limit)."""
    now = time.time()
    packages = {}
    for p in store.packages():
        if p["status"] == "removed":
            continue
        tick = telemetry.last_tick.get(p["ims"], {})
        packages[p["ims"]] = {
            "v": p["version"],
            "status": p["status"],
            "enabled": p["manifest"].get("enabled"),
            "tags": p["manifest"].get("tag_count"),
            "good": tick.get("good"),
            "bad": tick.get("bad"),
            "tick_age_s": round(now - tick["at"], 1) if tick else None,
        }
    disk = shutil.disk_usage(str(settings.state_dir))
    return {
        "agent_version": __version__,
        "reported_at": _now(),
        "ignition": {"state": ignition.status()},
        "link": {
            "connected_since": _iso(link.connected_since),
            "last_disconnect": link.last_disconnect_reason,
            "telemetry_topic": settings.telemetry_publish_topic,
        },
        "outbox": store.outbox_stats(),
        "published_messages": getattr(publisher, "published_messages", None),
        "last_publish": _iso(getattr(publisher, "last_publish", None)),
        "packages": packages,
        "cert_not_after": _cert_not_after(settings.iot_cert_path),
        "disk_free_mb": disk.free // (1024 * 1024),
        "last_error": shadow.last_error if shadow else None,
    }


def _timer_settings_changed(manifest: dict[str, Any], desired: dict[str, Any]) -> bool:
    return any(
        k in desired and desired[k] is not None and desired[k] != manifest.get(k) for k in ("enabled", "interval_ms")
    )


def _allowed_schemes() -> tuple[str, ...]:
    return ("https", "http") if os.environ.get("ALLOW_HTTP_PACKAGE_URLS") == "1" else ("https",)


class _SchemeCheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to the allowed schemes (no https -> http, file:, ftp:)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme not in _allowed_schemes():
            raise ValueError("package URL redirected to a disallowed scheme: %s" % newurl.split("?", 1)[0])
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_tls_context() -> ssl.SSLContext:
    """Certificate- and hostname-verifying context for package downloads (system CA store)."""
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    # These are the defaults for SERVER_AUTH; set explicitly so they can't drift.
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _download(url: str) -> bytes:
    if urllib.parse.urlsplit(url).scheme not in _allowed_schemes():
        raise ValueError("package URLs must be https")
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=download_tls_context()), _SchemeCheckedRedirects()
    )
    # Scheme checked above and on every redirect.
    with opener.open(url, timeout=60) as resp:  # nosec B310
        data = resp.read(MAX_PACKAGE_BYTES + 1)
    if len(data) > MAX_PACKAGE_BYTES:
        raise ValueError("package exceeds %d bytes" % MAX_PACKAGE_BYTES)
    return data


def _cert_not_after(path: str) -> str | None:
    try:
        info = ssl._ssl._test_decode_cert(path)  # stdlib helper; avoids a crypto dependency
        return info.get("notAfter")
    except Exception:
        return None


def _shadow_name(topic: str) -> str:
    # $aws/things/<thing>/shadow/name/<name>/...
    parts = topic.split("/")
    return parts[5] if len(parts) > 5 else ""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso(ts: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None
