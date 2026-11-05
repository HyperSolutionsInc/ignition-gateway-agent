"""AWS IoT Core link: MQTT over TLS (X.509 device certificate), outbox publisher."""

from __future__ import annotations

import logging
import queue
import ssl
import threading
import time
from typing import Callable

from .config import Settings
from .store import Store

log = logging.getLogger(__name__)

MessageHandler = Callable[[str, bytes], None]


def tls_context(settings: Settings) -> ssl.SSLContext:
    """Mutual-TLS context for AWS IoT Core: verify the server against the Amazon root CA only."""
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=settings.iot_ca_path)
    # These are the defaults for SERVER_AUTH; set explicitly so they can't drift.
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(settings.iot_cert_path, settings.iot_key_path)
    return ctx


class IotLink:
    """One persistent MQTT connection, shared by the publisher and the shadow handler."""

    def __init__(self, settings: Settings):
        import paho.mqtt.client as mqtt  # imported here so the rest of the agent runs without it

        settings.validate_iot()
        self.settings = settings
        self._mqtt = mqtt
        self._handlers: list[tuple[str, MessageHandler]] = []
        self._on_connect: list[Callable[[], None]] = []
        self.connected = threading.Event()
        self.connected_since: float | None = None
        self.last_disconnect_reason: str | None = None
        # Handlers run here, not on paho's network thread: a handler that publishes and waits
        # for the PUBACK would otherwise block the thread that delivers it (seen live: every
        # shadow delta stalled all MQTT traffic for 10 s and timed out a report).
        self._inbound: queue.Queue = queue.Queue()
        threading.Thread(target=self._dispatch, name="iot-dispatch", daemon=True).start()

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=settings.mqtt_client_id,
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )
        client.tls_set_context(tls_context(settings))
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.max_inflight_messages_set(20)
        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        client.on_message = self._handle_message
        self.client = client

    def start(self) -> None:
        log.info("connecting to %s:%d as %s", self.settings.iot_endpoint, self.settings.iot_port, self.settings.mqtt_client_id)
        self.client.connect_async(self.settings.iot_endpoint, self.settings.iot_port, keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()

    def subscribe(self, topic_filter: str, handler: MessageHandler) -> None:
        self._handlers.append((topic_filter, handler))
        if self.connected.is_set():
            self.client.subscribe(topic_filter, qos=1)

    def on_connect(self, callback: Callable[[], None]) -> None:
        self._on_connect.append(callback)

    def publish(self, topic: str, payload: bytes, qos: int = 1, timeout: float = 15.0) -> bool:
        if not self.connected.is_set():
            return False
        info = self.client.publish(topic, payload, qos=qos)
        if info.rc != self._mqtt.MQTT_ERR_SUCCESS:
            return False
        if qos == 0:
            return True
        try:
            info.wait_for_publish(timeout=timeout)
        except (RuntimeError, ValueError):
            return False
        return info.is_published()

    # ------------------------------------------------------------ paho callbacks

    def _handle_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code.is_failure:
            log.warning("AWS IoT refused the connection: %s", reason_code)
            self.last_disconnect_reason = str(reason_code)
            return
        log.info("connected to AWS IoT")
        self.connected_since = time.time()
        self.connected.set()
        for topic_filter, _ in self._handlers:
            client.subscribe(topic_filter, qos=1)
        for cb in self._on_connect:
            threading.Thread(target=_safe(cb), daemon=True).start()

    def _handle_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self.connected.clear()
        self.connected_since = None
        self.last_disconnect_reason = str(reason_code)
        # AWS closes the socket without a CONNACK when the certificate is inactive or the policy
        # does not allow this client id; that shows up here as an unexpected disconnect.
        log.warning("disconnected from AWS IoT: %s", reason_code)

    def _handle_message(self, client, userdata, msg) -> None:
        self._inbound.put((msg.topic, msg.payload))

    def _dispatch(self) -> None:
        while True:
            topic, payload = self._inbound.get()
            for topic_filter, handler in list(self._handlers):
                if self._mqtt.topic_matches_sub(topic_filter, topic):
                    try:
                        handler(topic, payload)
                    except Exception:
                        log.exception("handler for %s failed", topic)


class Publisher(threading.Thread):
    """Drains the outbox to AWS IoT, deleting each message only after its PUBACK."""

    def __init__(self, settings: Settings, store: Store, link: IotLink):
        super().__init__(name="publisher", daemon=True)
        self.settings = settings
        self.store = store
        self.link = link
        self.paused = False
        self.published_messages = 0
        self.last_publish: float | None = None
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            if self.paused or not self.link.connected.wait(timeout=5):
                self._stop.wait(1)
                continue
            batch = self.store.peek(50)
            if not batch:
                self._stop.wait(0.5)
                continue
            topic = self.settings.telemetry_publish_topic
            for message_id, _ims, payload in batch:
                if not self.link.publish(topic, payload):
                    self._stop.wait(2)
                    break
                self.store.ack(message_id)
                self.published_messages += 1
                self.last_publish = time.time()


def _safe(fn: Callable[[], None]) -> Callable[[], None]:
    def run() -> None:
        try:
            fn()
        except Exception:
            log.exception("on-connect callback failed")

    return run
