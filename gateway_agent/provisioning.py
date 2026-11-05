"""AWS IoT fleet provisioning by claim: a new gateway gets its own certificate and thing.

The image carries a *claim* certificate shared by the fleet, whose policy allows nothing but
the provisioning topics below. On first boot, when the gateway has no certificate of its own:

  1. connect with the claim certificate (client id `provision-<serial>`)
  2. publish `{}` to `$aws/certificates/create/json`; the accepted reply carries a new
     certificate, its private key and an ownership token
  3. publish the token and the gateway's parameters to
     `$aws/provisioning-templates/<template>/provision/json`; the accepted reply names the
     thing that AWS created (or found) from the template
  4. store the certificate, key (mode 0600) and thing name in the state volume

Every later boot uses the stored identity directly; the claim certificate is not used again.
The serial number is `GATEWAY_SERIAL` if set, otherwise one generated on first boot and kept.
"""

from __future__ import annotations

import json
import logging
import secrets
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Settings, _write_secret

log = logging.getLogger(__name__)

CREATE_TOPIC = "$aws/certificates/create/json"


class ProvisioningError(RuntimeError):
    pass


@dataclass
class Identity:
    thing_name: str
    cert_path: Path
    key_path: Path


def identity_dir(settings: Settings) -> Path:
    return settings.state_dir / "iot"


def stored_identity(settings: Settings) -> Identity | None:
    d = identity_dir(settings)
    try:
        meta = json.loads((d / "identity.json").read_text())
    except (FileNotFoundError, ValueError):
        return None
    cert, key = d / "device.cert.pem", d / "device.private.key"
    if not (cert.exists() and key.exists() and meta.get("thing_name")):
        return None
    return Identity(meta["thing_name"], cert, key)


def gateway_serial(settings: Settings) -> str:
    if settings.gateway_serial:
        return settings.gateway_serial
    path = settings.state_dir / "serial"
    try:
        value = path.read_text().strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    value = "gw-" + secrets.token_hex(6)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n")
    return value


def apply_identity(settings: Settings, ident: Identity) -> None:
    """Point the settings at a gateway identity (thing name, certificate and key)."""
    settings.thing_name = ident.thing_name
    settings.iot_cert_path = str(ident.cert_path)
    settings.iot_key_path = str(ident.key_path)


class MqttTransport:
    """The few MQTT calls provisioning needs, over a short-lived claim-certificate connection."""

    def __init__(self, settings: Settings, client_id: str):
        import paho.mqtt.client as mqtt

        self._mqtt = mqtt
        self._msgs: dict[str, bytes] = {}
        self._got = threading.Condition()
        self._connected = threading.Event()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv311)
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=settings.iot_ca_path)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.check_hostname = True
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(settings.iot_claim_cert_path, settings.iot_claim_key_path)
        client.tls_set_context(ctx)
        client.on_connect = lambda c, u, f, rc, p=None: (not rc.is_failure) and self._connected.set()
        client.on_message = self._on_message
        self.client = client
        self.endpoint, self.port = settings.iot_endpoint, settings.iot_port

    def _on_message(self, client, userdata, msg) -> None:
        with self._got:
            self._msgs[msg.topic] = msg.payload
            self._got.notify_all()

    def connect(self, timeout: float) -> None:
        self.client.connect_async(self.endpoint, self.port, keepalive=30)
        self.client.loop_start()
        if not self._connected.wait(timeout):
            raise ProvisioningError("could not connect with the claim certificate (inactive, or its policy is wrong)")

    def subscribe(self, topic: str) -> None:
        info, _ = self.client.subscribe(topic, qos=1)

    def publish(self, topic: str, payload: bytes) -> None:
        self.client.publish(topic, payload, qos=1).wait_for_publish(timeout=15)

    def wait_for(self, topics: list[str], timeout: float) -> tuple[str, bytes]:
        deadline = time.monotonic() + timeout
        with self._got:
            while True:
                for t in topics:
                    if t in self._msgs:
                        return t, self._msgs.pop(t)
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ProvisioningError("no reply from AWS IoT on %s" % topics[0].rsplit("/", 1)[0])
                self._got.wait(left)

    def close(self) -> None:
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()


def provision(settings: Settings, transport_factory: Callable[[Settings, str], Any] = MqttTransport,
              timeout: float = 30.0) -> Identity:
    """Run the claim flow once and store the identity. Raises ProvisioningError on refusal."""
    template = settings.iot_provisioning_template
    if not template:
        raise ProvisioningError("IOT_PROVISIONING_TEMPLATE is not set")
    for name, path in (("claim certificate", settings.iot_claim_cert_path), ("claim key", settings.iot_claim_key_path)):
        if not Path(path).exists():
            raise ProvisioningError("%s not found at %s" % (name, path))
    serial = gateway_serial(settings)
    provision_topic = "$aws/provisioning-templates/%s/provision/json" % template
    transport = transport_factory(settings, "provision-%s" % serial)
    try:
        transport.connect(timeout)
        for topic in (CREATE_TOPIC, provision_topic):
            transport.subscribe(topic + "/accepted")
            transport.subscribe(topic + "/rejected")

        transport.publish(CREATE_TOPIC, b"{}")
        topic, body = transport.wait_for([CREATE_TOPIC + "/accepted", CREATE_TOPIC + "/rejected"], timeout)
        created = json.loads(body)
        if topic.endswith("/rejected"):
            raise ProvisioningError("certificate creation refused: %s" % created.get("errorMessage", created))
        for field in ("certificatePem", "privateKey", "certificateOwnershipToken"):
            if not created.get(field):
                raise ProvisioningError("certificate reply is missing %s" % field)

        params = {"SerialNumber": serial, **settings.iot_provisioning_parameters}
        transport.publish(provision_topic, json.dumps(
            {"certificateOwnershipToken": created["certificateOwnershipToken"], "parameters": params}).encode())
        topic, body = transport.wait_for([provision_topic + "/accepted", provision_topic + "/rejected"], timeout)
        result = json.loads(body)
        if topic.endswith("/rejected"):
            raise ProvisioningError("provisioning refused by template %s: %s" % (template, result.get("errorMessage", result)))
        thing = result.get("thingName")
        if not thing:
            raise ProvisioningError("template %s returned no thingName" % template)
    finally:
        transport.close()

    d = identity_dir(settings)
    _write_secret(d / "device.private.key", created["privateKey"].strip())
    _write_secret(d / "device.cert.pem", created["certificatePem"].strip())
    _write_secret(d / "identity.json", json.dumps({
        "thing_name": thing, "certificate_id": created.get("certificateId"), "serial": serial,
        "template": template, "provisioned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))
    log.info("provisioned as thing %s (serial %s)", thing, serial)
    return Identity(thing, d / "device.cert.pem", d / "device.private.key")


def resolve_identity(settings: Settings, stop: threading.Event | None = None,
                     transport_factory: Callable[[Settings, str], Any] = MqttTransport,
                     retry_s: float = 60.0) -> Identity | None:
    """The gateway's identity: configured, stored, or provisioned now (retrying until it works).

    Returns None when IoT has nothing to go on (no thing name, no claim certificate).
    """
    if settings.thing_name and Path(settings.iot_cert_path).exists():
        return Identity(settings.thing_name, Path(settings.iot_cert_path), Path(settings.iot_key_path))
    ident = stored_identity(settings)
    if ident:
        return ident
    if not (settings.iot_provisioning_template and Path(settings.iot_claim_cert_path).exists()):
        return None
    while True:
        try:
            return provision(settings, transport_factory)
        except (ProvisioningError, OSError, ValueError) as exc:
            log.warning("fleet provisioning failed (%s); retrying in %.0fs", exc, retry_s)
        if stop is not None and stop.wait(retry_s):
            return None
        if stop is None:
            time.sleep(retry_s)
