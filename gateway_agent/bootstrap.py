"""First-boot setup of the bundled Ignition gateway, so the image comes up with no hands on it.

Ignition 8.3 has no setting that creates an API key, so the agent writes one as a config
resource. Verified against Ignition 8.3.7:

  * A key is an `ignition/api-token` resource. Its `tokenHash` is
    base64url(SHA-256(raw key bytes)), unpadded; the key itself is base64url(32 random bytes).
    Requests send `X-Ignition-API-Token: <resource name>:<key>`.
  * A key can't be granted role levels (Authenticated/Roles/...): those come only from a
    logged-in user. It gets its own top-level security level, `GatewayAgent`.
  * The gateway's API read and write permissions must list that level. They live in the
    `security-properties` resource, which first boot creates (in `local` or `core`) and
    rewrites during its migration, so it is patched only while the gateway is stopped, after
    its first start.

The key and the level go in the `external` collection (core and local stay Ignition's own);
the permission patch edits whichever collection holds security-properties.

Two calls, both idempotent, made by the container entrypoint while Ignition is not running:

  prepare()   every boot, before starting Ignition: mint the key if needed, write the key
              and level resources, patch the permissions if the file exists. Returns False
              when the permissions file doesn't exist yet (first boot).
  finalize()  after the first start-and-stop: patch the permissions. Returns True when done.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

from .config import Settings, _write_secret

log = logging.getLogger(__name__)

# The api-token resource's name (Ignition shows it as the key's name); not a secret.
TOKEN_NAME = "gateway-agent"  # nosec B105
LEVEL_NAME = "GatewayAgent"
COLLECTIONS_ABOVE_EXTERNAL = ("local", "core")  # highest priority first


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def token_hash(key: str) -> str:
    """The hash Ignition stores for a key: base64url(SHA-256(decoded key bytes))."""
    raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    return _b64(hashlib.sha256(raw).digest())


def _resources(settings: Settings) -> Path:
    return settings.ignition_data_dir / "config" / "resources"


def _resource_meta() -> dict[str, Any]:
    return {
        "scope": "A",
        "version": 1,
        "restricted": False,
        "overridable": True,
        "files": ["config.json"],
        "attributes": {
            "lastModification": {"actor": "gateway-agent", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        },
    }


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n")
    os.replace(tmp, path)


def api_key_path(settings: Settings) -> Path:
    return settings.state_dir / "ignition_api_token"


def ensure_api_key(settings: Settings) -> str:
    """The header value for the agent's Ignition key (`gateway-agent:<key>`), minted once."""
    path = api_key_path(settings)
    try:
        value = path.read_text().strip()
        if value.startswith(TOKEN_NAME + ":"):
            return value
    except FileNotFoundError:
        pass
    value = "%s:%s" % (TOKEN_NAME, _b64(secrets.token_bytes(32)))
    _write_secret(path, value)
    return value


def write_token_resource(settings: Settings, header_value: str) -> None:
    key = header_value.split(":", 1)[1]
    folder = _resources(settings) / "external" / "ignition" / "api-token" / TOKEN_NAME
    config = {
        "profile": {
            "type": "basic-token",
            "secureChannelRequired": False,  # the agent talks to Ignition over loopback in the same container
            "securityLevels": [{"name": LEVEL_NAME, "children": []}],
            "timestamp": int(time.time() * 1000),
        },
        "settings": {"tokenHash": token_hash(key)},
    }
    existing = folder / "config.json"
    if existing.exists():
        try:
            if json.loads(existing.read_text())["settings"]["tokenHash"] == config["settings"]["tokenHash"]:
                return
        except (ValueError, KeyError):
            pass
    _write_json(existing, config)
    _write_json(folder / "resource.json", _resource_meta())


def write_security_level(settings: Settings) -> None:
    """Add the GatewayAgent level, keeping any levels already defined in `external`."""
    folder = _resources(settings) / "external" / "ignition" / "security-levels"
    path = folder / "config.json"
    levels: dict[str, Any] = {"securityLevels": []}
    if path.exists():
        levels = json.loads(path.read_text())
    else:
        # The gateway's own default tree, so this file never hides a level it defines.
        levels["securityLevels"].append(
            {"name": "Authenticated", "description": "Represents a user who has been authenticated by the system.",
             "children": [{"name": "Roles", "description": "Represents the roles that a user has.",
                           "children": [{"name": "Administrator", "children": [],
                                         "description": "System generated security level representing read and write "
                                                        "privileges to Gateway configuration"}]}]})
    if any(level.get("name") == LEVEL_NAME for level in levels["securityLevels"]):
        return
    levels["securityLevels"].append(
        {"name": LEVEL_NAME, "description": "API access for the gateway-agent service", "children": []})
    _write_json(path, levels)
    _write_json(folder / "resource.json", _resource_meta())


def security_properties_path(settings: Settings) -> Path | None:
    for collection in COLLECTIONS_ABOVE_EXTERNAL:
        path = _resources(settings) / collection / "ignition" / "security-properties" / "config.json"
        if path.exists():
            return path
    return None


def patch_security_properties(settings: Settings) -> bool:
    """Grant GatewayAgent API read and write access. False if the file doesn't exist yet."""
    path = security_properties_path(settings)
    if path is None:
        return False
    props = json.loads(path.read_text())
    changed = False
    for key in ("readPermissions", "writePermissions"):
        block = props.setdefault(key, {"securityLevels": [], "type": "AnyOf"})
        if block.get("type") != "AnyOf" and block.get("securityLevels"):
            # AllOf with other levels would demand every level at once; the key holds only one.
            raise RuntimeError("%s in %s is AllOf; can't grant %s safely" % (key, path, LEVEL_NAME))
        block["type"] = "AnyOf"
        if not any(level.get("name") == LEVEL_NAME for level in block["securityLevels"]):
            block["securityLevels"].append({"name": LEVEL_NAME, "children": []})
            changed = True
    if changed:
        _write_json(path, props)
        log.info("granted %s API read/write in %s", LEVEL_NAME, path)
    return True


def prepare(settings: Settings) -> bool:
    header = ensure_api_key(settings)
    write_token_resource(settings, header)
    write_security_level(settings)
    return patch_security_properties(settings)


def finalize(settings: Settings) -> bool:
    return patch_security_properties(settings)
