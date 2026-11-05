import base64
import gzip
import hashlib
import json
import threading

from gateway_agent.ignition import IgnitionClient
from gateway_agent.shadow import ShadowAgent, ims_shadow_name
from conftest import ims_tree, to_bytes


class FakeLink:
    def __init__(self):
        self.published = []
        self.connected = threading.Event()
        self.connected.set()
        self.on_publish = None

    def subscribe(self, topic, handler):
        pass

    def on_connect(self, cb):
        pass

    def publish(self, topic, payload, qos=1, timeout=15):
        self.published.append((topic, json.loads(payload)))
        if self.on_publish:
            self.on_publish(topic, payload)
        return True

    def updates(self, name):
        return [p["state"] for t, p in self.published if t.endswith("/name/%s/update" % name)]


def make_agent(settings, store, installer):
    link = FakeLink()
    agent = ShadowAgent(settings, store, installer, link, health=lambda: {},
                        ignition=IgnitionClient(settings.ignition_url, settings.ignition_api_token))
    return agent, link


def inline(data):
    return {"type": "inline", "encoding": "gzip+base64", "data": base64.b64encode(gzip.compress(data)).decode()}


def test_install_from_inline_source(settings, store, installer):
    agent, link = make_agent(settings, store, installer)
    data = to_bytes(ims_tree())
    desired = {"version": "7", "sha256": hashlib.sha256(data).hexdigest(), "source": inline(data), "interval_ms": 2000,
               "remove": False}
    agent._apply_ims("ims-FWT-02", desired, {})

    assert store.package("FWT-02")["status"] == "installed"
    assert store.package("FWT-02")["manifest"]["interval_ms"] == 2000
    final = link.updates("ims-FWT-02")[-1]
    assert final["reported"]["status"] == "installed" and final["reported"]["version"] == "7"
    assert final["reported"]["tag_count"] == 2
    assert final["reported"]["remove"] is False  # mirrored, so no delta is left behind
    assert final["desired"] == {"source": None}

    # Same desired again (e.g. after a reconnect GET): nothing is re-installed.
    link.published.clear()
    desired.pop("source")
    agent._apply_ims("ims-FWT-02", desired, final["reported"])
    assert link.updates("ims-FWT-02")[-1]["reported"]["status"] == "installed"


def test_hash_mismatch_is_reported_and_not_installed(settings, store, installer):
    agent, link = make_agent(settings, store, installer)
    data = to_bytes(ims_tree())
    try:
        agent._apply_ims("ims-FWT-02", {"version": "1", "sha256": "0" * 64, "source": inline(data)}, {})
    except ValueError:
        pass
    assert store.package("FWT-02") is None
    assert link.updates("ims-FWT-02")[-1]["reported"]["status"] == "failed"


def test_interim_report_is_pinned_to_document_version(settings, store, installer):
    # AWS may apply quick updates out of order; only the interim "downloading" report carries
    # the version, so a stale one is rejected instead of overwriting the final status.
    agent, link = make_agent(settings, store, installer)
    data = to_bytes(ims_tree())
    agent._apply_ims("ims-FWT-02", {"version": "1", "sha256": hashlib.sha256(data).hexdigest(),
                                    "source": inline(data)}, {}, 41)
    bodies = [p for t, p in link.published if t.endswith("/name/ims-FWT-02/update")]
    assert bodies[0]["state"]["reported"]["status"] == "downloading" and bodies[0]["version"] == 41
    assert bodies[-1]["state"]["reported"]["status"] == "installed" and "version" not in bodies[-1]


def test_version_conflict_on_update_is_not_an_error(settings, store, installer):
    agent, _ = make_agent(settings, store, installer)
    agent._on_rejected("$aws/things/t/shadow/name/ims-FWT-02/update/rejected",
                       json.dumps({"code": 409, "message": "Version conflict"}).encode())
    assert agent.last_error is None
    agent._on_rejected("$aws/things/t/shadow/name/ims-FWT-02/update/rejected",
                       json.dumps({"code": 400, "message": "bad"}).encode())
    assert agent.last_error is not None


def test_mqtt_chunked_source(settings, store, installer):
    agent, link = make_agent(settings, store, installer)
    data = to_bytes(ims_tree())

    def cloud(topic, payload):
        if topic == "hyper/gw-test/package/request":
            req = json.loads(payload)
            assert req["ims"] == "FWT-02"
            parts = [data[i:i + 100] for i in range(0, len(data), 100)]
            for seq, part in enumerate(parts):
                msg = {"ims": "FWT-02", "request_id": req["request_id"], "seq": seq, "total": len(parts),
                       "data": base64.b64encode(part).decode()}
                agent._on_chunk("hyper/gw-test/package/chunk", json.dumps(msg).encode())

    link.on_publish = cloud
    agent._apply_ims("ims-FWT-02", {"version": "1", "sha256": hashlib.sha256(data).hexdigest(), "source": "mqtt"}, {})
    assert store.package("FWT-02")["status"] == "installed"


def test_timer_change_without_new_package(settings, store, installer):
    agent, link = make_agent(settings, store, installer)
    data = to_bytes(ims_tree())
    sha = hashlib.sha256(data).hexdigest()
    agent._apply_ims("ims-FWT-02", {"sha256": sha, "source": inline(data)}, {})
    agent._apply_ims("ims-FWT-02", {"sha256": sha, "enabled": False}, {})
    assert store.package("FWT-02")["manifest"]["enabled"] is False
    assert link.updates("ims-FWT-02")[-1]["reported"]["enabled"] is False


def test_remove(settings, store, installer):
    agent, link = make_agent(settings, store, installer)
    data = to_bytes(ims_tree())
    agent._apply_ims("ims-FWT-02", {"sha256": hashlib.sha256(data).hexdigest(), "source": inline(data)}, {})
    agent._apply_ims("ims-FWT-02", {"remove": True}, {})
    assert store.package("FWT-02")["status"] == "removed"
    assert link.updates("ims-FWT-02")[-1]["reported"]["status"] == "removed"


def test_gateway_shadow_requests_listed_ims_and_refuses_restart(settings, store, installer, fake_ignition):
    agent, link = make_agent(settings, store, installer)
    agent._apply_gateway({"ims": ["FWT-02", "FWT-03"], "rescan": 1, "restart": 1}, {})
    gets = [t for t, _ in link.published if t.endswith("/get")]
    assert gets == [
        "$aws/things/gw-test/shadow/name/ims-FWT-02/get",
        "$aws/things/gw-test/shadow/name/ims-FWT-03/get",
    ]
    reported = link.updates("gateway")[-1]["reported"]
    assert reported["rescan"] == 1 and "restart" not in reported and "restart_refused" in reported
    assert "/data/api/v1/scan/projects" in fake_ignition.paths()
    assert "/data/api/v1/restart" not in fake_ignition.paths()


def test_gateway_shadow_can_switch_off_basic_ingest(settings, store, installer):
    agent, link = make_agent(settings, store, installer)
    settings.telemetry_rule = "hyper_telemetry"
    agent._apply_gateway({"telemetry_rule": ""}, {})
    assert settings.telemetry_publish_topic == "hyper/gw-test/telemetry"
    assert link.updates("gateway")[-1]["reported"]["telemetry_rule"] == ""


def test_shadow_name_sanitising():
    assert ims_shadow_name("FWT 02/a") == "ims-FWT_02_a"


def test_iot_handlers_run_off_the_network_thread(settings, monkeypatch):
    # A handler that publishes and waits for its PUBACK must not run on paho's network thread.
    import ssl
    from types import SimpleNamespace

    from gateway_agent import iot

    monkeypatch.setattr(iot, "tls_context", lambda s: ssl.create_default_context())
    settings.iot_endpoint = "abc123-ats.iot.us-east-2.amazonaws.com"
    link = iot.IotLink(settings)
    seen = threading.Event()
    threads = []

    def handler(topic, payload):
        threads.append(threading.current_thread().name)
        seen.set()

    link.subscribe("$aws/things/gw-test/shadow/name/+/update/delta", handler)
    link._handle_message(None, None, SimpleNamespace(topic="$aws/things/gw-test/shadow/name/gateway/update/delta",
                                                     payload=b"{}"))
    assert seen.wait(2)
    assert threads == ["iot-dispatch"]
