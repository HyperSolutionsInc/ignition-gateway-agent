"""AWS IoT fleet provisioning by claim, against a fake MQTT transport."""

import json
import os
import stat
import threading

import pytest

from gateway_agent import provisioning
from gateway_agent.provisioning import CREATE_TOPIC, ProvisioningError

TEMPLATE = "hyper-gateway-fleet"
PROV = "$aws/provisioning-templates/%s/provision/json" % TEMPLATE


class FakeTransport:
    """Answers the two provisioning requests the way AWS IoT does."""

    def __init__(self, settings, client_id, reject_create=False, reject_provision=False):
        self.client_id = client_id
        self.subs, self.published, self.replies = [], [], {}
        self.reject_create, self.reject_provision = reject_create, reject_provision
        self.closed = False

    def connect(self, timeout):
        pass

    def subscribe(self, topic):
        self.subs.append(topic)

    def publish(self, topic, payload):
        self.published.append((topic, json.loads(payload)))
        if topic == CREATE_TOPIC:
            if self.reject_create:
                self.replies[CREATE_TOPIC + "/rejected"] = {"errorMessage": "denied"}
            else:
                self.replies[CREATE_TOPIC + "/accepted"] = {
                    "certificateId": "abc123", "certificatePem": "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----",
                    "privateKey": "-----BEGIN RSA PRIVATE KEY-----\nY\n-----END RSA PRIVATE KEY-----",
                    "certificateOwnershipToken": "own-token"}
        elif topic == PROV:
            if self.reject_provision:
                self.replies[PROV + "/rejected"] = {"errorMessage": "serial not allowed"}
            else:
                self.replies[PROV + "/accepted"] = {"thingName": "hyper-gw-" + json.loads(payload)["parameters"]["SerialNumber"],
                                                    "deviceConfiguration": {}}

    def wait_for(self, topics, timeout):
        for t in topics:
            if t in self.replies:
                return t, json.dumps(self.replies.pop(t)).encode()
        raise ProvisioningError("no reply")

    def close(self):
        self.closed = True


@pytest.fixture
def claim(settings, tmp_path):
    cert, key = tmp_path / "claim.cert.pem", tmp_path / "claim.private.key"
    cert.write_text("claim cert")
    key.write_text("claim key")
    settings.iot_claim_cert_path, settings.iot_claim_key_path = str(cert), str(key)
    settings.iot_provisioning_template = TEMPLATE
    settings.thing_name = ""
    settings.iot_cert_path = str(tmp_path / "missing.pem")
    settings.gateway_serial = "SN-0042"
    settings.iot_provisioning_parameters = {"Site": "Petersburg"}
    return settings


def test_provision_stores_identity_privately(claim):
    made = []

    def factory(s, cid):
        made.append(FakeTransport(s, cid))
        return made[-1]

    ident = provisioning.provision(claim, factory)
    t = made[0]
    assert t.client_id == "provision-SN-0042" and t.closed
    assert [p[0] for p in t.published] == [CREATE_TOPIC, PROV]
    assert t.published[1][1] == {"certificateOwnershipToken": "own-token",
                                 "parameters": {"SerialNumber": "SN-0042", "Site": "Petersburg"}}
    assert ident.thing_name == "hyper-gw-SN-0042"
    assert ident.cert_path.read_text().startswith("-----BEGIN CERTIFICATE-----")
    for p in (ident.cert_path, ident.key_path, ident.cert_path.with_name("identity.json")):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    again = provisioning.stored_identity(claim)
    assert again.thing_name == "hyper-gw-SN-0042"


@pytest.mark.parametrize("kw,msg", [({"reject_create": True}, "certificate creation refused"),
                                    ({"reject_provision": True}, "serial not allowed")])
def test_refusals_store_nothing(claim, kw, msg):
    with pytest.raises(ProvisioningError, match=msg):
        provisioning.provision(claim, lambda s, cid: FakeTransport(s, cid, **kw))
    assert provisioning.stored_identity(claim) is None


def test_missing_claim_certificate_is_a_clear_error(claim, tmp_path):
    claim.iot_claim_cert_path = str(tmp_path / "nope.pem")
    with pytest.raises(ProvisioningError, match="claim certificate not found"):
        provisioning.provision(claim, FakeTransport)


def test_resolve_prefers_configured_then_stored_then_provisions(claim, tmp_path):
    calls = []

    def factory(s, cid):
        calls.append(cid)
        return FakeTransport(s, cid)

    first = provisioning.resolve_identity(claim, threading.Event(), factory)
    assert first.thing_name == "hyper-gw-SN-0042" and len(calls) == 1
    second = provisioning.resolve_identity(claim, threading.Event(), factory)
    assert second.thing_name == first.thing_name and len(calls) == 1  # stored identity reused
    cert = tmp_path / "configured.pem"
    cert.write_text("x")
    claim.thing_name, claim.iot_cert_path = "configured-thing", str(cert)
    assert provisioning.resolve_identity(claim, threading.Event(), factory).thing_name == "configured-thing"


def test_resolve_retries_until_stopped(claim):
    stop = threading.Event()
    attempts = []

    def factory(s, cid):
        attempts.append(1)
        if len(attempts) >= 2:
            stop.set()
        return FakeTransport(s, cid, reject_create=True)

    assert provisioning.resolve_identity(claim, stop, factory, retry_s=0.01) is None
    assert len(attempts) == 2


def test_no_claim_means_no_identity(settings):
    settings.thing_name = ""
    settings.iot_provisioning_template = ""
    assert provisioning.resolve_identity(settings, threading.Event()) is None


def test_generated_serial_is_kept(settings):
    settings.gateway_serial = ""
    first = provisioning.gateway_serial(settings)
    assert first.startswith("auto-") and provisioning.gateway_serial(settings) == first
