"""First-boot Ignition setup: API key, GatewayAgent level, permissions (rules verified on 8.3.7)."""

import json
import os
import stat

import pytest

from gateway_agent import bootstrap
from gateway_agent.cli import _bootstrap
from gateway_agent.ignition import IgnitionClient


def test_token_hash_matches_ignitions_own_example():
    # The key/hash pair Ignition 8.3.7 ships as the example in its api-token generate route.
    assert bootstrap.token_hash("p3wRdNxufWgMbovC14yPkr9o_GKJlno-K7c99LKexSo") == \
        "c4cNxcsHexJ5mlGPoinRgppto_nEj8mSnacF-ppNCjM"


def test_api_key_is_minted_once_and_private(settings):
    first = bootstrap.ensure_api_key(settings)
    name, key = first.split(":", 1)
    assert name == "gateway-agent" and len(key) == 43
    assert bootstrap.ensure_api_key(settings) == first
    mode = stat.S_IMODE(os.stat(bootstrap.api_key_path(settings)).st_mode)
    assert mode == 0o600
    IgnitionClient("http://127.0.0.1:8088", first)  # the header value passes the client's own check


def _res(settings, *parts):
    return settings.ignition_data_dir.joinpath("config", "resources", *parts)


def test_prepare_writes_key_and_level_into_external(settings):
    assert bootstrap.prepare(settings) is False  # no security properties before the first start
    header = bootstrap.ensure_api_key(settings)
    tok = json.loads(_res(settings, "external/ignition/api-token/gateway-agent/config.json").read_text())
    assert tok["profile"]["type"] == "basic-token"
    assert tok["profile"]["securityLevels"] == [{"name": "GatewayAgent", "children": []}]
    assert tok["settings"]["tokenHash"] == bootstrap.token_hash(header.split(":", 1)[1])
    assert _res(settings, "external/ignition/api-token/gateway-agent/resource.json").exists()
    levels = json.loads(_res(settings, "external/ignition/security-levels/config.json").read_text())
    names = [lv["name"] for lv in levels["securityLevels"]]
    assert names == ["Authenticated", "GatewayAgent"]  # keeps Ignition's default tree beside it
    bootstrap.prepare(settings)  # idempotent
    levels = json.loads(_res(settings, "external/ignition/security-levels/config.json").read_text())
    assert [lv["name"] for lv in levels["securityLevels"]].count("GatewayAgent") == 1


def _write_props(settings, collection, props):
    path = _res(settings, collection, "ignition/security-properties/config.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(props))
    return path


ADMIN = [{"name": "Authenticated", "children": [{"name": "Roles", "children": [{"name": "Administrator", "children": []}]}]}]


def test_finalize_grants_api_read_and_write_preferring_local(settings):
    default = {"readPermissions": {"securityLevels": list(ADMIN), "type": "AnyOf"},
               "writePermissions": {"securityLevels": list(ADMIN), "type": "AnyOf"}, "forceIdpAuth": True}
    core = _write_props(settings, "core", default)
    local = _write_props(settings, "local", default)
    assert bootstrap.finalize(settings) is True
    patched = json.loads(local.read_text())
    for key in ("readPermissions", "writePermissions"):
        assert [lv["name"] for lv in patched[key]["securityLevels"]] == ["Authenticated", "GatewayAgent"]
    assert patched["forceIdpAuth"] is True  # everything else untouched
    assert "GatewayAgent" not in core.read_text()  # local wins; core left as Ignition wrote it
    before = local.read_text()
    bootstrap.finalize(settings)
    assert local.read_text() == before


def test_finalize_refuses_an_allof_rule_it_cannot_satisfy(settings):
    _write_props(settings, "core", {"readPermissions": {"securityLevels": list(ADMIN), "type": "AllOf"},
                                    "writePermissions": {"securityLevels": [], "type": "AnyOf"}})
    with pytest.raises(RuntimeError, match="AllOf"):
        bootstrap.finalize(settings)


def test_bootstrap_command_exit_codes(settings, capsys):
    assert _bootstrap(settings, "prepare") == 3
    _write_props(settings, "core", {"readPermissions": {"securityLevels": [], "type": "AllOf"},
                                    "writePermissions": {"securityLevels": [], "type": "AllOf"}})
    assert _bootstrap(settings, "finalize") == 0
    assert _bootstrap(settings, "prepare") == 0
