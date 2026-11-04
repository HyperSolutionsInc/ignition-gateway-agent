"""Runtime settings, read once from the environment."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _load_or_create_secret(path: Path) -> str:
    try:
        value = path.read_text().strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value + "\n")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # Where the agent keeps its own state (package store, telemetry buffer, inbox).
    state_dir: Path = field(default_factory=lambda: Path(_env("AGENT_STATE_DIR", "/var/lib/gateway-agent")))

    # Ignition: the gateway's data volume is mounted into this container so project
    # resources can be written in place, and the REST API is used to import tags and
    # trigger a project rescan (so no gateway restart is needed).
    ignition_url: str = field(default_factory=lambda: _env("IGNITION_URL", "http://ignition:8088"))
    ignition_api_token: str = field(default_factory=lambda: _env("IGNITION_API_TOKEN"))
    ignition_data_dir: Path = field(default_factory=lambda: Path(_env("IGNITION_DATA_DIR", "/ignition-data")))
    ignition_project: str = field(default_factory=lambda: _env("IGNITION_PROJECT", "hyper-gateway"))
    tag_provider: str = field(default_factory=lambda: _env("IGNITION_TAG_PROVIDER", "default"))
    # Optional folder under the provider that every IMS tree is imported beneath, e.g. "ACI".
    tag_root: str = field(default_factory=lambda: _env("IGNITION_TAG_ROOT", ""))
    # How deep into the HAMF folder tree the IMS folder sits
    # (customer / campus / building / power cell = 4).
    ims_path_depth: int = field(default_factory=lambda: _env_int("IMS_PATH_DEPTH", 4))
    timer_interval_ms: int = field(default_factory=lambda: _env_int("TIMER_INTERVAL_MS", 1000))

    # Local HTTP API: timer scripts post telemetry here; operators post HAMF packages here.
    # Inside the container it has to listen on all interfaces so the Ignition container can reach
    # it over the Docker network; the compose file publishes it on loopback only, and every
    # endpoint except /healthz requires a token.
    http_host: str = field(default_factory=lambda: _env("AGENT_HTTP_HOST", "0.0.0.0"))  # nosec B104
    http_port: int = field(default_factory=lambda: _env_int("AGENT_HTTP_PORT", 8600))
    # Admin token for package management and status; ingest token for timer-script telemetry
    # (it is written into the gateway project, so it is kept separate and grants nothing else).
    # Generated on first start and kept in the state directory when not set explicitly.
    api_token: str = field(default_factory=lambda: _env("AGENT_API_TOKEN"))
    ingest_token: str = field(default_factory=lambda: _env("AGENT_INGEST_TOKEN"))
    # URL the Ignition timer scripts use to reach this agent.
    agent_url_for_ignition: str = field(
        default_factory=lambda: _env("AGENT_URL_FOR_IGNITION", "http://gateway-agent:8600")
    )

    # AWS IoT Core (MQTT over TLS with an X.509 device certificate).
    iot_enabled: bool = field(default_factory=lambda: _env_bool("IOT_ENABLED", True))
    iot_endpoint: str = field(default_factory=lambda: _env("IOT_ENDPOINT"))
    iot_port: int = field(default_factory=lambda: _env_int("IOT_PORT", 8883))
    iot_ca_path: str = field(default_factory=lambda: _env("IOT_CA_PATH", "/certs/AmazonRootCA1.pem"))
    iot_cert_path: str = field(default_factory=lambda: _env("IOT_CERT_PATH", "/certs/device.cert.pem"))
    iot_key_path: str = field(default_factory=lambda: _env("IOT_KEY_PATH", "/certs/device.private.key"))
    # The thing name is also the MQTT client id and the gateway's identity everywhere else.
    thing_name: str = field(default_factory=lambda: _env("IOT_THING_NAME"))
    client_id: str = field(default_factory=lambda: _env("IOT_CLIENT_ID"))
    # Telemetry goes through AWS IoT Basic Ingest: published to $aws/rules/<rule>/<topic>, it is
    # handed straight to the rule (no pub/sub messaging charge, no subscribers). The rule still
    # sees <topic>, so the thing name stays in it for policy scoping. Empty rule = normal pub/sub.
    telemetry_rule: str = field(default_factory=lambda: _env("IOT_TELEMETRY_RULE", "hyper_telemetry"))
    # {thing} is substituted. Everything else about the data travels in the payload.
    telemetry_topic: str = field(default_factory=lambda: _env("IOT_TELEMETRY_TOPIC", "hyper/{thing}/telemetry"))
    max_message_bytes: int = field(default_factory=lambda: _env_int("IOT_MAX_MESSAGE_BYTES", 100_000))
    shadow_enabled: bool = field(default_factory=lambda: _env_bool("SHADOW_ENABLED", True))
    health_interval_s: int = field(default_factory=lambda: _env_int("HEALTH_INTERVAL_S", 60))
    # Remote gateway restart through the shadow is off unless explicitly allowed.
    allow_remote_restart: bool = field(default_factory=lambda: _env_bool("ALLOW_REMOTE_RESTART", False))

    # Telemetry buffer limits (store-and-forward while AWS is unreachable).
    buffer_max_rows: int = field(default_factory=lambda: _env_int("BUFFER_MAX_ROWS", 5_000_000))

    @property
    def mqtt_client_id(self) -> str:
        return self.client_id or self.thing_name

    @property
    def telemetry_publish_topic(self) -> str:
        topic = self.telemetry_topic.format(thing=self.thing_name)
        return "$aws/rules/%s/%s" % (self.telemetry_rule, topic) if self.telemetry_rule else topic

    def package_topic(self, kind: str) -> str:
        """hyper/<thing>/package/request (gateway -> cloud) or .../chunk (cloud -> gateway)."""
        return "hyper/%s/package/%s" % (self.thing_name, kind)

    def ensure_tokens(self) -> None:
        """Fill api_token / ingest_token from the state directory, creating them if needed."""
        for attr, filename in (("api_token", "api_token"), ("ingest_token", "ingest_token")):
            if not getattr(self, attr):
                setattr(self, attr, _load_or_create_secret(self.state_dir / filename))

    def read_api_token(self) -> str:
        """The admin token for CLI calls: from the environment, else the state directory."""
        if self.api_token:
            return self.api_token
        try:
            return (self.state_dir / "api_token").read_text().strip()
        except OSError:
            return ""

    @property
    def inbox_dir(self) -> Path:
        return self.state_dir / "inbox"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "agent.db"

    @property
    def project_dir(self) -> Path:
        return self.ignition_data_dir / "projects" / self.ignition_project
