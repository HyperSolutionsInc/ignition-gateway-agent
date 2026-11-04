import json

import pytest

from gateway_agent.telemetry import Telemetry, TelemetryError


def post(n_rows, ts=1762000000000):
    return {
        "ims": "FWT-02",
        "version": "abc",
        "dropped": 0,
        "ticks": [{"ts": ts, "rows": [[i, 10, None, "ENV", "d", 1.5] for i in range(n_rows)], "bad": 1, "total": n_rows + 1}],
    }


def test_rows_become_ts_telemetry_shape(settings, store):
    t = Telemetry(settings, store)
    assert t.accept(post(2)) == 2
    (_, ims, payload), = store.peek(10)
    msg = json.loads(payload)
    assert ims == "FWT-02"
    assert (msg["v"], msg["gw"], msg["ims"], msg["pkg"]) == (1, "gw-test", "FWT-02", "abc")
    assert msg["rows"][0] == {
        "ts": "2025-11-01T12:26:40.000+00:00", "tag_id": 0, "asset_id": 10, "producer_id": None, "ims_id": "ENV",
        "value_double": 1.5, "value_int": None, "value_bool": None, "value_string": None,
    }
    assert t.last_tick["FWT-02"]["good"] == 2 and t.last_tick["FWT-02"]["bad"] == 1


def test_value_kinds(settings, store):
    t = Telemetry(settings, store)
    body = post(0)
    body["ticks"][0]["rows"] = [[1, 1, None, "D", "i", 7], [2, 1, None, "D", "b", True], [3, 1, None, "D", "s", "on"]]
    t.accept(body)
    rows = json.loads(store.peek(1)[0][2])["rows"]
    assert (rows[0]["value_int"], rows[1]["value_bool"], rows[2]["value_string"]) == (7, True, "on")


def test_messages_are_packed_under_limit(settings, store):
    settings.max_message_bytes = 2000
    Telemetry(settings, store).accept(post(100))
    msgs = store.peek(100)
    assert len(msgs) > 1
    assert all(len(p) <= 2000 for _, _, p in msgs)
    assert sum(len(json.loads(p)["rows"]) for _, _, p in msgs) == 100


def test_buffer_cap_drops_oldest(settings, store):
    settings.buffer_max_rows = 5
    t = Telemetry(settings, store)
    t.accept(post(3, ts=1))
    t.accept(post(3, ts=2))
    stats = store.outbox_stats()
    assert stats["rows"] == 3 and stats["dropped_rows_total"] == 3


def test_basic_ingest_topic(settings):
    settings.telemetry_rule = "hyper_telemetry"
    settings.telemetry_topic = "hyper/{thing}/telemetry"
    assert settings.telemetry_publish_topic == "$aws/rules/hyper_telemetry/hyper/gw-test/telemetry"
    settings.telemetry_rule = ""
    assert settings.telemetry_publish_topic == "hyper/gw-test/telemetry"
    assert settings.package_topic("request") == "hyper/gw-test/package/request"


def test_malformed(settings, store):
    t = Telemetry(settings, store)
    with pytest.raises(TelemetryError):
        t.accept({"ims": "x"})
    with pytest.raises(TelemetryError):
        t.accept({"ims": "x", "ticks": [{"ts": 1, "rows": [[1, 2]]}]})
