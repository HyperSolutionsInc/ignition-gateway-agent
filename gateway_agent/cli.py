"""Command line entry point.

  gateway-agent run                       start the agent (HTTP API, inbox, AWS IoT link, shadow)
  gateway-agent import FILE [--ims KEY]   send a HAMF export to the running agent and install it
  gateway-agent list                      installed IMS packages
  gateway-agent remove IMS                remove an IMS from the gateway
  gateway-agent set IMS [--enable|--disable] [--interval-ms N]
  gateway-agent render FILE OUT_DIR       write the generated Ignition artifacts to a folder only
  gateway-agent status                    health summary from the running agent
  gateway-agent check-iot                 connect to AWS IoT with the configured certificate
  gateway-agent rotate-tokens [--admin] [--ingest]
                                          replace generated API tokens (both by default); restart to apply
  gateway-agent bootstrap prepare|finalize
                                          first-boot Ignition setup, run by the image entrypoint
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gateway-agent")
    parser.add_argument("--agent-url", default=None, help="running agent (default http://127.0.0.1:$AGENT_HTTP_PORT)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    p = sub.add_parser("import")
    p.add_argument("file", type=Path)
    p.add_argument("--ims", default=None, help="IMS key (defaults to the IMS folder name in the export)")
    sub.add_parser("list")
    p = sub.add_parser("remove")
    p.add_argument("ims")
    p = sub.add_parser("set")
    p.add_argument("ims")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--enable", dest="enabled", action="store_true", default=None)
    g.add_argument("--disable", dest="enabled", action="store_false")
    p.add_argument("--interval-ms", type=int, default=None)
    p = sub.add_parser("render")
    p.add_argument("file", type=Path)
    p.add_argument("out_dir", type=Path)
    p.add_argument("--ims", default=None)
    sub.add_parser("status")
    p = sub.add_parser("check-iot")
    p.add_argument("--timeout", type=float, default=15.0)
    p = sub.add_parser("rotate-tokens")
    p.add_argument("--admin", action="store_true", help="rotate only the admin token")
    p.add_argument("--ingest", action="store_true", help="rotate only the ingest token")
    p = sub.add_parser("bootstrap", help="first-boot Ignition setup (run by the image entrypoint)")
    p.add_argument("phase", choices=["prepare", "finalize"])
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = Settings()
    except ValueError as exc:
        print("configuration error: %s" % exc, file=sys.stderr)
        return 2
    agent = args.agent_url or "http://127.0.0.1:%d" % settings.http_port
    token = settings.read_api_token()

    if args.cmd == "run":
        return _run(settings)
    if args.cmd == "import":
        name = urllib.request.quote(args.file.name)
        query = "?filename=%s" % name + ("&ims=%s" % urllib.request.quote(args.ims) if args.ims else "")
        return _call(agent, token, "POST", "/v1/packages" + query, args.file.read_bytes())
    if args.cmd == "list":
        return _call(agent, token, "GET", "/v1/packages")
    if args.cmd == "remove":
        return _call(agent, token, "DELETE", "/v1/packages/" + urllib.request.quote(args.ims, safe=""))
    if args.cmd == "set":
        body = json.dumps({"enabled": args.enabled, "interval_ms": args.interval_ms}).encode()
        return _call(agent, token, "PATCH", "/v1/packages/" + urllib.request.quote(args.ims, safe=""), body)
    if args.cmd == "status":
        return _call(agent, token, "GET", "/v1/status")
    if args.cmd == "render":
        return _render(settings, args.file, args.out_dir, args.ims)
    if args.cmd == "check-iot":
        return _check_iot(settings, args.timeout)
    if args.cmd == "bootstrap":
        return _bootstrap(settings, args.phase)
    if args.cmd == "rotate-tokens":
        both = not (args.admin or args.ingest)
        try:
            rotated = settings.rotate_tokens(admin=both or args.admin, ingest=both or args.ingest)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print("rotated %s. Restart the agent to apply; it then rewrites the gateway publisher and rescans."
              % " and ".join(rotated))
        return 0
    return 2


# ---------------------------------------------------------------- commands


def _run(settings: Settings) -> int:
    from .ignition import IgnitionClient
    from .inbox import Inbox
    from .installer import Installer
    from .server import make_server, serve_in_background
    from .shadow import ShadowAgent, build_health
    from .store import Store
    from .telemetry import Telemetry

    from . import bootstrap

    log = logging.getLogger("gateway_agent")
    log.info("gateway-agent %s starting", __version__)
    try:
        settings.ensure_tokens()
    except ValueError as exc:
        log.error("configuration error: %s", exc)
        return 2
    if not settings.ignition_api_token:
        # The bundled image's key, minted by `gateway-agent bootstrap` before Ignition started.
        try:
            settings.ignition_api_token = bootstrap.api_key_path(settings).read_text().strip()
        except FileNotFoundError:
            pass
    install_secret_redaction([settings.api_token, settings.ingest_token, settings.ignition_api_token])
    store = Store(settings.db_path)
    ignition = IgnitionClient(settings.ignition_url, settings.ignition_api_token)
    installer = Installer(settings, store, ignition)
    telemetry = Telemetry(settings, store)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    parts: dict[str, Any] = {"link": None, "publisher": None, "shadow": None}

    def health() -> dict[str, Any]:
        return build_health(settings, store, parts["link"] or _Offline(), parts["publisher"], telemetry, ignition,
                            parts["shadow"])

    # The API comes up first so timer scripts can hand over readings while the rest starts.
    server = make_server(settings.http_host, settings.http_port, telemetry=telemetry, installer=installer,
                         status=health, api_token=settings.api_token, ingest_token=settings.ingest_token)
    serve_in_background(server)
    log.info("HTTP API on %s:%d", settings.http_host, settings.http_port)

    log.info("waiting for Ignition at %s", settings.ignition_url)
    if ignition.wait_until_running(settings.ignition_wait_s):
        try:
            installer.refresh_shared()
        except Exception:
            log.exception("could not refresh shared project files; will retry on the next install")
    else:
        log.warning("Ignition is not running after %ds; carrying on, installs will fail until it is",
                    settings.ignition_wait_s)
    Inbox(settings.inbox_dir, installer).start()
    log.info("inbox %s", settings.inbox_dir)

    if settings.iot_enabled:
        threading.Thread(target=_start_iot, name="iot-start", daemon=True,
                         args=(settings, store, installer, ignition, health, parts, stop)).start()

    stop.wait()
    log.info("stopping")
    server.shutdown()
    if parts["link"] is not None:
        parts["publisher"].stop()
        parts["link"].stop()
    return 0


def _start_iot(settings, store, installer, ignition, health, parts, stop) -> None:
    """Find or provision the gateway's AWS identity, then bring up the link, publisher and shadows."""
    from .iot import IotLink, Publisher
    from .provisioning import apply_identity, resolve_identity
    from .shadow import ShadowAgent

    log = logging.getLogger("gateway_agent")
    ident = resolve_identity(settings, stop)
    if ident is None:
        if not stop.is_set():
            log.error("no AWS IoT identity: set IOT_THING_NAME and a certificate, or a claim certificate and "
                      "IOT_PROVISIONING_TEMPLATE (or IOT_ENABLED=false). Readings keep buffering.")
        return
    apply_identity(settings, ident)
    try:
        link = IotLink(settings)
    except (OSError, ValueError) as exc:
        log.error("cannot set up the AWS IoT link: %s", exc)
        return
    publisher = Publisher(settings, store, link)
    parts.update(link=link, publisher=publisher)
    if settings.shadow_enabled:
        shadow = ShadowAgent(settings, store, installer, link, health=health, publisher=publisher, ignition=ignition)
        parts["shadow"] = shadow
        shadow.start()
    publisher.start()
    link.start()
    log.info("AWS IoT link starting as %s", settings.thing_name)


def _bootstrap(settings: Settings, phase: str) -> int:
    """Called by the image's entrypoint while Ignition is stopped. Exit 3 = start Ignition once first."""
    from . import bootstrap

    done = bootstrap.prepare(settings) if phase == "prepare" else bootstrap.finalize(settings)
    if done:
        print("Ignition bootstrap: API key and %s access in place" % bootstrap.LEVEL_NAME)
        return 0
    print("Ignition bootstrap: security properties not created yet; start the gateway once, then finalize")
    return 3


class SecretRedactor(logging.Filter):
    """Replaces any configured secret with *** in every log record, as a backstop: no code path
    is meant to log a token, but an error message carrying one must not reach the logs."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = message
        for secret in self.secrets:
            redacted = redacted.replace(secret, "***")
        if redacted != message:
            record.msg, record.args = redacted, ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            for secret in self.secrets:
                record.exc_text = record.exc_text.replace(secret, "***")
        return True


def install_secret_redaction(secrets: list[str]) -> SecretRedactor:
    redactor = SecretRedactor(secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
    return redactor


class _Offline:
    connected_since = None
    last_disconnect_reason = "IoT disabled"


def _render(settings: Settings, file: Path, out_dir: Path, ims: str | None) -> int:
    from .ignition import IgnitionClient
    from .installer import Installer
    from .store import Store

    db = out_dir / ".render.db"
    store = Store(db)
    try:
        art = Installer(settings, store, IgnitionClient(settings.ignition_url, "")).render_to(
            file.read_bytes(), file.name, out_dir, ims
        )
    finally:
        store.close()
        for leftover in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            leftover.unlink(missing_ok=True)
    print(json.dumps(art.manifest, indent=2))
    return 0


def _check_iot(settings: Settings, timeout: float) -> int:
    from .iot import IotLink
    from .provisioning import apply_identity, stored_identity

    if not settings.thing_name:
        ident = stored_identity(settings)
        if ident:
            apply_identity(settings, ident)
    if not (settings.iot_endpoint and settings.mqtt_client_id):
        print("no identity yet: set IOT_THING_NAME, or let the gateway provision itself on first boot", file=sys.stderr)
        return 2
    try:
        link = IotLink(settings)
    except (OSError, ValueError) as exc:
        print("cannot load the device certificate/key: %s (the key must be readable by uid %d)" % (exc, os.getuid()),
              file=sys.stderr)
        return 1
    link.start()
    ok = link.connected.wait(timeout)
    if ok:
        print("connected to %s as %s" % (settings.iot_endpoint, settings.mqtt_client_id))
    else:
        print(
            "could not connect within %.0fs (last reason: %s). TLS succeeding but the connection being closed "
            "usually means the certificate is inactive or its policy does not allow client id %r."
            % (timeout, link.last_disconnect_reason, settings.mqtt_client_id),
            file=sys.stderr,
        )
    link.stop()
    return 0 if ok else 1


def _call(agent: str, token: str, method: str, path: str, body: bytes | None = None) -> int:
    if urllib.parse.urlsplit(agent).scheme not in ("http", "https"):
        print("--agent-url must be http:// or https://", file=sys.stderr)
        return 2
    if not token:
        print("no admin token: set AGENT_API_TOKEN or run inside the agent container", file=sys.stderr)
        return 2
    req = urllib.request.Request(agent + path, data=body, method=method, headers={"X-Agent-Token": token})
    try:
        # Scheme checked above; the path is built from fixed endpoints.
        with urllib.request.urlopen(req, timeout=300) as resp:  # nosec B310
            print(json.dumps(json.loads(resp.read()), indent=2))
            return 0
    except urllib.error.HTTPError as exc:
        print(exc.read().decode("utf-8", "replace"), file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print("agent not reachable at %s: %s" % (agent, exc.reason), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
