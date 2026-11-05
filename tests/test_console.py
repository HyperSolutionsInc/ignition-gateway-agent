"""Fleet console backend (console/backend) against in-memory AWS fakes."""

import hashlib
import io
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "console" / "backend"))

import console_api  # noqa: E402
import package_server  # noqa: E402
from conftest import ims_tree, leaf, to_bytes  # noqa: E402

THING = "hyper-gw-SN-1"
OTHER = "hyper-gw-SN-2"


class _NotFound(Exception):
    pass


class _NoSuchKey(Exception):
    pass


class _Exc:
    ResourceNotFoundException = _NotFound
    NoSuchKey = _NoSuchKey


class FakeS3:
    exceptions = _Exc

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = bytes(Body)

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _NoSuchKey(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return "https://bucket.s3.us-east-2.amazonaws.com/%s?sig=x" % Params["Key"]


class FakeIotData:
    exceptions = _Exc

    def __init__(self):
        self.docs = {}
        self.published = []

    def get_thing_shadow(self, thingName, shadowName):
        doc = self.docs.get((thingName, shadowName))
        if doc is None:
            raise _NotFound()
        state = {k: v for k, v in doc.items() if k in ("desired", "reported") and v}
        delta = {k: v for k, v in doc["desired"].items() if doc["reported"].get(k) != v}
        if delta:
            state["delta"] = delta
        return {"payload": io.BytesIO(json.dumps({"state": state, "version": doc["version"]}).encode())}

    def update_thing_shadow(self, thingName, shadowName, payload):
        doc = self.docs.setdefault((thingName, shadowName), {"desired": {}, "reported": {}, "version": 0})
        for part, values in json.loads(payload)["state"].items():
            for k, v in values.items():
                if v is None:
                    doc[part].pop(k, None)
                else:
                    doc[part][k] = v
        doc["version"] += 1

    def delete_thing_shadow(self, thingName, shadowName):
        self.docs.pop((thingName, shadowName), None)

    def list_named_shadows_for_thing(self, thingName, pageSize, nextToken=None):
        return {"results": sorted(n for t, n in self.docs if t == thingName)}

    def publish(self, topic, qos, payload):
        self.published.append((topic, json.loads(payload)))

    def report(self, thing, name, reported):
        doc = self.docs.setdefault((thing, name), {"desired": {}, "reported": {}, "version": 0})
        doc["reported"].update(reported)
        doc["version"] += 1


class FakeIot:
    exceptions = _Exc

    def __init__(self):
        self.things = {}
        self.principals = {}
        self.certs = {}

    def add(self, name, serial, cert="c" * 64):
        self.things[name] = {"thingName": name, "attributes": {"serial": serial}}
        arn = "arn:aws:iot:us-east-2:1:cert/" + cert
        self.principals[name] = [arn]
        self.certs[cert] = {"certificateId": cert, "status": "ACTIVE", "validity": {"notAfter": "2049-12-31"}}

    def list_things(self, maxResults, nextToken=None):
        return {"things": list(self.things.values()) + [{"thingName": "some-other-thing", "attributes": {}}]}

    def describe_thing(self, thingName):
        if thingName not in self.things:
            raise _NotFound()
        return self.things[thingName]

    def update_thing(self, thingName, attributePayload):
        attrs = self.things[thingName]["attributes"]
        for k, v in attributePayload["attributes"].items():
            if v == "":
                attrs.pop(k, None)
            else:
                attrs[k] = v

    def list_thing_principals(self, thingName):
        return {"principals": list(self.principals.get(thingName, []))}

    def describe_certificate(self, certificateId):
        return {"certificateDescription": self.certs[certificateId]}

    def detach_thing_principal(self, thingName, principal):
        self.principals[thingName].remove(principal)

    def update_certificate(self, certificateId, newStatus):
        self.certs[certificateId]["status"] = newStatus

    def delete_thing(self, thingName):
        del self.things[thingName]


class FakeDdb:
    def __init__(self):
        self.items = []

    def put_item(self, TableName, Item):
        self.items.append(Item)

    def query(self, **kw):
        items = sorted(self.items, key=lambda i: i["sk"]["S"], reverse=True)
        if "FilterExpression" in kw:
            t = kw["ExpressionAttributeValues"][":t"]["S"]
            items = [i for i in items if i.get("thing", {}).get("S") == t]
        return {"Items": items}


@pytest.fixture
def aws():
    iot = FakeIot()
    iot.add(THING, "SN-1")
    iot.add(OTHER, "SN-2", cert="d" * 64)
    return console_api.Aws(s3=FakeS3(), iot=iot, iot_data=FakeIotData(), ddb=FakeDdb(),
                           env={"BUCKET": "b", "AUDIT_TABLE": "audit", "THING_PREFIX": "hyper-gw-"})


def call(aws, method, path, body=None, header=True, query=None):
    headers = {"X-Console-Request": "1"} if header else {}
    event = {"httpMethod": method, "path": path, "headers": headers, "queryStringParameters": query,
             "body": None if body is None else json.dumps(body), "requestContext": {"identity": {"sourceIp": "10.0.0.9"}}}
    res = console_api.handle(event, aws)
    try:
        return res["statusCode"], json.loads(res["body"])
    except ValueError:
        return res["statusCode"], res["body"]


def upload(aws, data, name="FWT-02.json"):
    status, up = call(aws, "POST", "/api/uploads", {"filename": name, "size": len(data)})
    assert status == 200 and up["upload_key"].startswith("uploads/")
    aws.s3.objects[up["upload_key"]] = data
    return up["upload_key"]


def test_serves_the_ui_with_a_strict_csp(aws):
    res = console_api.handle({"httpMethod": "GET", "path": "/", "headers": {}}, aws)
    assert res["statusCode"] == 200 and "<title>" in res["body"]
    csp = res["headers"]["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert console_api.handle({"httpMethod": "GET", "path": "/app.js", "headers": {}}, aws)["statusCode"] == 200


def test_changes_need_the_console_header(aws):
    status, body = call(aws, "POST", "/api/gateways/%s/actions" % THING, {"action": "rescan"}, header=False)
    assert status == 403
    assert (THING, "gateway") not in aws.iot_data.docs


def test_only_prefixed_things_are_gateways(aws):
    assert call(aws, "GET", "/api/gateways/some-other-thing")[0] == 400
    status, body = call(aws, "GET", "/api/gateways")
    assert [g["thing"] for g in body["gateways"]] == [THING, OTHER]


def test_publish_stores_package_and_sets_shadows_for_mqtt_delivery(aws):
    data = to_bytes(ims_tree())
    key = upload(aws, data)
    status, prev = call(aws, "POST", "/api/preview", {"upload_key": key, "thing": THING})
    assert status == 200 and prev["ims_key"] == "FWT-02" and prev["tag_count"] == 2 and prev["diff"] is None

    status, res = call(aws, "POST", "/api/gateways/%s/ims" % THING, {"upload_key": key, "interval_ms": 2000})
    assert status == 200
    sha = hashlib.sha256(data).hexdigest()
    assert aws.s3.objects["packages/%s/ims-FWT-02/%s" % (THING, sha)] == data
    manifest = json.loads(aws.s3.objects["packages/%s/ims-FWT-02/%s.manifest.json" % (THING, sha)])
    assert manifest["tag_count"] == 2
    desired = aws.iot_data.docs[(THING, "ims-FWT-02")]["desired"]
    assert desired["source"] == {"type": "mqtt"} and desired["sha256"] == sha and desired["interval_ms"] == 2000
    assert desired["remove"] is False
    assert aws.iot_data.docs[(THING, "gateway")]["desired"]["ims"] == ["FWT-02"]
    entry = aws.ddb.items[-1]
    assert entry["action"]["S"] == "publish" and entry["thing"]["S"] == THING and entry["ip"]["S"] == "10.0.0.9"


def test_preview_diffs_an_update_against_the_current_package(aws):
    call(aws, "POST", "/api/gateways/%s/ims" % THING, {"upload_key": upload(aws, to_bytes(ims_tree()))})
    bigger = ims_tree(devices=("ENV", "PDU01"))
    status, prev = call(aws, "POST", "/api/preview", {"upload_key": upload(aws, to_bytes(bigger)), "thing": THING})
    d = prev["diff"]
    assert d["added"] == 2 and d["removed"] == 0 and d["devices_added"] == ["PDU01"] and not d["same"]


def test_bad_inputs_are_refused(aws):
    assert call(aws, "POST", "/api/preview", {"upload_key": "../etc/passwd"})[0] == 400
    assert call(aws, "POST", "/api/preview", {"upload_key": upload(aws, b'{"not": "hamf"}')})[0] == 422
    assert call(aws, "POST", "/api/preview", {"hamf_asset_id": "7"})[0] == 400  # HAMF pull is off
    assert call(aws, "POST", "/api/uploads", {"filename": "x.json", "size": 17 * 1024 * 1024})[0] == 413
    assert call(aws, "PATCH", "/api/gateways/%s/ims/FWT-02" % THING, {"interval_ms": 5})[0] == 400
    assert call(aws, "POST", "/api/gateways/%s/actions" % THING, {"action": "format-disk"})[0] == 400


def test_fleet_summary_marks_stale_gateways_offline(aws):
    fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    aws.iot_data.report(THING, "gateway", {"health": {"reported_at": fresh, "agent_version": "0.2.1",
                                                      "ignition": {"state": "RUNNING"}, "outbox": {"rows": 3},
                                                      "packages": {"FWT-02": {"status": "installed"}}}})
    aws.iot_data.report(OTHER, "gateway", {"health": {"reported_at": "2026-01-01T00:00:00Z"}})
    rows = {g["thing"]: g for g in call(aws, "GET", "/api/gateways")[1]["gateways"]}
    assert rows[THING]["online"] and rows[THING]["ims_status"] == {"FWT-02": "installed"} and rows[THING]["outbox_rows"] == 3
    assert not rows[OTHER]["online"]


def test_gateway_detail_never_echoes_presigned_urls(aws):
    aws.iot_data.update_thing_shadow(thingName=THING, shadowName="ims-A", payload=json.dumps(
        {"state": {"desired": {"sha256": "a" * 64, "source": {"type": "url", "url": "https://secret"}}}}))
    status, g = call(aws, "GET", "/api/gateways/%s" % THING)
    assert status == 200
    assert g["ims_shadows"][0]["desired"]["source"] == {"type": "url"}
    assert g["certificates"][0]["status"] == "ACTIVE"


def test_labels_update_thing_attributes(aws):
    assert call(aws, "PATCH", "/api/gateways/%s" % THING, {"site": "Petersburg", "customer": "Switch"})[0] == 200
    assert aws.iot.things[THING]["attributes"] == {"serial": "SN-1", "site": "Petersburg", "customer": "Switch"}
    assert call(aws, "PATCH", "/api/gateways/%s" % THING, {"site": "<script>"})[0] == 400


def test_remove_retry_and_timer(aws):
    call(aws, "POST", "/api/gateways/%s/ims" % THING, {"upload_key": upload(aws, to_bytes(ims_tree()))})
    aws.iot_data.update_thing_shadow(thingName=THING, shadowName="ims-FWT-02", payload=json.dumps({"state": {"desired": {"source": None}}}))
    assert call(aws, "POST", "/api/gateways/%s/ims/FWT-02/retry" % THING)[0] == 200
    assert aws.iot_data.docs[(THING, "ims-FWT-02")]["desired"]["source"] == {"type": "mqtt"}
    assert call(aws, "PATCH", "/api/gateways/%s/ims/FWT-02" % THING, {"enabled": False})[0] == 200
    assert aws.iot_data.docs[(THING, "ims-FWT-02")]["desired"]["enabled"] is False
    assert call(aws, "DELETE", "/api/gateways/%s/ims/FWT-02" % THING)[0] == 200
    assert aws.iot_data.docs[(THING, "ims-FWT-02")]["desired"]["remove"] is True
    assert aws.iot_data.docs[(THING, "gateway")]["desired"]["ims"] == []
    assert call(aws, "POST", "/api/gateways/%s/ims/NOPE/retry" % THING)[0] == 409


def test_rollout_reaches_every_gateway_and_reports_failures(aws):
    key = upload(aws, to_bytes(ims_tree()))
    status, res = call(aws, "POST", "/api/rollout", {"upload_key": key, "things": [THING, OTHER]})
    assert status == 200 and all(r["ok"] for r in res["results"])
    assert aws.iot_data.docs[(OTHER, "gateway")]["desired"]["ims"] == ["FWT-02"]
    assert call(aws, "POST", "/api/rollout", {"upload_key": key, "things": ["not-a-gateway"]})[0] == 400


def test_decommission_needs_confirmation_and_revokes_the_certificate(aws):
    aws.iot_data.report(THING, "gateway", {"health": {}})
    assert call(aws, "DELETE", "/api/gateways/%s" % THING, {"confirm": "nope"})[0] == 400
    status, res = call(aws, "DELETE", "/api/gateways/%s" % THING, {"confirm": THING})
    assert status == 200 and res["revoked_certificates"] == ["c" * 12]
    assert aws.iot.certs["c" * 64]["status"] == "INACTIVE"
    assert THING not in aws.iot.things and (THING, "gateway") not in aws.iot_data.docs


def test_audit_lists_newest_first_and_filters_by_gateway(aws):
    call(aws, "POST", "/api/gateways/%s/actions" % THING, {"action": "pause"})
    call(aws, "POST", "/api/gateways/%s/actions" % OTHER, {"action": "rescan"})
    entries = call(aws, "GET", "/api/audit", query={"thing": THING})[1]["entries"]
    assert [e["action"] for e in entries] == ["action:pause"]


# --------------------------------------------------------------------------- package server


def _server(aws):
    return package_server.Clients(s3=aws.s3, iot_data=aws.iot_data, env={"BUCKET": "b"}, sleep=lambda s: None)


def test_package_server_sends_chunks_that_reassemble(aws, monkeypatch):
    monkeypatch.setattr(package_server, "CHUNK_BYTES", 1000)
    data = to_bytes(ims_tree(devices=tuple("CB%02d" % i for i in range(30))))
    sha = hashlib.sha256(data).hexdigest()
    aws.s3.objects["packages/%s/ims-FWT-02/%s" % (THING, sha)] = data
    res = package_server.serve({"thing": THING, "ims": "FWT-02", "request_id": "ab" * 16, "sha256": sha}, _server(aws))
    assert res["ok"] and res["chunks"] > 1
    msgs = [m for t, m in aws.iot_data.published if t == "hyper/%s/package/chunk" % THING]
    import base64
    assert b"".join(base64.b64decode(m["data"]) for m in sorted(msgs, key=lambda m: m["seq"])) == data


def test_package_server_only_serves_a_gateways_own_packages(aws):
    data = to_bytes(ims_tree())
    sha = hashlib.sha256(data).hexdigest()
    aws.s3.objects["packages/%s/ims-FWT-02/%s" % (OTHER, sha)] = data
    req = {"thing": THING, "ims": "FWT-02", "request_id": "ab" * 16, "sha256": sha}
    assert package_server.serve(req, _server(aws))["reason"] == "not found"
    assert package_server.serve({**req, "thing": "evil/../x"}, _server(aws))["reason"] == "malformed"
    aws.s3.objects["packages/%s/ims-FWT-02/%s" % (THING, sha)] = data + b" "
    assert package_server.serve(req, _server(aws))["reason"] == "bad object"
    assert aws.iot_data.published == []


def test_agent_fetches_a_console_package_over_mqtt(aws, settings, store, installer, monkeypatch):
    """The real agent's MQTT fetch against the real package server: same chunk protocol."""
    from gateway_agent.ignition import IgnitionClient
    from gateway_agent.shadow import ShadowAgent

    monkeypatch.setattr(package_server, "CHUNK_BYTES", 500)
    settings.thing_name = THING
    data = to_bytes(ims_tree())
    sha = hashlib.sha256(data).hexdigest()
    aws.s3.objects["packages/%s/ims-FWT-02/%s" % (THING, sha)] = data
    clients = _server(aws)

    class Link:
        connected = threading.Event()

        def subscribe(self, *a):
            pass

        def on_connect(self, *a):
            pass

        def publish(self, topic, payload, qos=1, timeout=15):
            if topic.endswith("/package/request"):
                req = json.loads(payload)
                req["thing"] = topic.split("/")[1]  # what the IoT rule adds
                aws.iot_data.published.clear()
                package_server.serve(req, clients)
                for t, msg in aws.iot_data.published:
                    agent._on_chunk(t, json.dumps(msg).encode())
            return True

    agent = ShadowAgent(settings, store, installer, Link(), health=lambda: {},
                        ignition=IgnitionClient(settings.ignition_url, settings.ignition_api_token))
    agent._apply_ims("ims-FWT-02", {"version": "1", "sha256": sha, "source": {"type": "mqtt"}, "remove": False}, {}, 3)
    assert store.package("FWT-02")["status"] == "installed"
