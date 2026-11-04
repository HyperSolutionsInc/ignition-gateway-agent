"""Runtime settings, read once from the environment."""

from __future__ import annotations

import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{16,128}")
# RFC 1123 hostname: dot-separated labels of letters, digits and inner hyphens.
_HOSTNAME_RE = re.compile(r"(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*")
# AWS IoT thing names and client ids.
_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9:_\-]{1,128}")


def _env_secret(name: str) -> str:
    """A secret from NAME, or from the file named by NAME_FILE (e.g. /run/secrets/...)."""
    value = os.environ.get(name, "")
    path = os.environ.get(name + "_FILE", "")
    if value and path:
        raise ValueError("set %s or %s_FILE, not both" % (name, name))
    if path:
        try:
            return Path(path).read_text().strip()
        except OSError as exc:
            raise ValueError("cannot read %s_FILE (%s): %s" % (name, path, exc)) from exc
    return value


def _load_or_create_secret(path: Path) -> str:
    try:
        value = path.read_text().strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    value = secrets.token_urlsafe(32)
    _write_secret(path, value)
    return value


def _write_secret(path: Path, value: str) -> None:
    """Write a secret readable only by the agent user, replacing any old file atomically.

    On any failure the temporary file is removed and the old secret is left in place. After the
    swap the file is read back and its permissions checked, so a rotation never reports success
    with a secret that is missing, different, or readable by others.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if path.read_text().strip() != value or mode & 0o077:
        raise OSError("secret %s did not verify after writing (mode %o)" % (path, mode))


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
    ignition_api_token: str = field(default_factory=lambda: _env_secret("IGNITION_API_TOKEN"))
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
    # Each can come from the environment, from a file named by <NAME>_FILE (Docker/Kubernetes
    # secrets), or, if neither is set, is generated on first start into the state directory.
    api_token: str = field(default_factory=lambda: _env_secret("AGENT_API_TOKEN"))
    ingest_token: str = field(default_factory=lambda: _env_secret("AGENT_INGEST_TOKEN"))
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

    def validate_iot(self) -> None:
        """IOT_ENDPOINT is a bare hostname (MQTT over TLS on IOT_PORT), never a URL."""
        if not _HOSTNAME_RE.fullmatch(self.iot_endpoint or ""):
            raise ValueError("IOT_ENDPOINT must be a hostname such as abc123-ats.iot.us-east-2.amazonaws.com "
                             "(no scheme, port or path), got %r" % self.iot_endpoint)
        if not 1 <= self.iot_port <= 65535:
            raise ValueError("IOT_PORT must be 1-65535, got %r" % self.iot_port)
        if not _CLIENT_ID_RE.fullmatch(self.mqtt_client_id or ""):
            raise ValueError("IOT_THING_NAME / IOT_CLIENT_ID must be 1-128 characters of A-Z a-z 0-9 : _ -")

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
            if not TOKEN_RE.fullmatch(getattr(self, attr)):
                raise ValueError("AGENT_%s must be 16-128 characters of A-Z a-z 0-9 _ -" % attr.upper())

    def rotate_tokens(self, admin: bool, ingest: bool) -> list[str]:
        """Replace generated tokens in the state directory. Returns the names rotated.

        Tokens supplied through the environment or a secrets file are managed outside the agent
        and are refused here. The running agent picks the new tokens up on restart, and then
        rewrites the gateway publisher with the new ingest token.
        """
        rotated = []
        for wanted, env, filename in ((admin, "AGENT_API_TOKEN", "api_token"),
                                      (ingest, "AGENT_INGEST_TOKEN", "ingest_token")):
            if not wanted:
                continue
            if os.environ.get(env) or os.environ.get(env + "_FILE"):
                raise ValueError("%s is set externally; rotate it where it is managed" % env)
            _write_secret(self.state_dir / filename, secrets.token_urlsafe(32))
            rotated.append(filename)
        return rotated

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
