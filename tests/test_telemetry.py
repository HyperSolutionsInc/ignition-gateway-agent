import json

import pytest

from gateway_agent.telemetry import Telemetry, TelemetryError


def post(n_rows, ts=1762223000000, bad=1, value=1.5):
    return {
        "ims": "FWT-02",
        "version": "abc",
        "dropped": 0,
        "ticks": [{"ts": ts, "rows": [[i + 1, value] for i in range(n_rows)], "bad": bad, "total": n_rows + bad}],
    }


def messages(store):
    return [json.loads(p) for _, _, p in store.peek(1000)]


def test_compact_envelope(settings, store):
    t = Telemetry(settings, store)
    assert t.accept(post(2)) == 2
    (msg,) = messages(store)
    assert msg == {
        "v": 2, "gw": "gw-test", "ims": "FWT-02", "pkg": "abc",
        "ticks": [{"ts": 1762223000000, "bad": 1, "r": [[1, 1.5], [2, 1.5]]}],
    }
    assert t.last_tick["FWT-02"]["good"] == 2 and t.last_tick["FWT-02"]["bad"] == 1
    assert store.outbox_stats()["rows"] == 2


def test_value_types_pass_through(settings, store):
    body = post(0)
    body["ticks"][0]["rows"] = [[1, 7], [2, True], [3, "Closed"], [4, 0.25]]
    Telemetry(settings, store).accept(body)
    assert messages(store)[0]["ticks"][0]["r"] == [[1, 7], [2, True], [3, "Closed"], [4, 0.25]]


def test_reading_is_much_smaller_than_column_form(settings, store):
    Telemetry(settings, store).accept(post(1000, value=41.25))
    (_, _, payload), = store.peek(1)
    assert len(payload) / 1000 < 20


def test_several_ticks_share_a_message(settings, store):
    body = post(3)
    body["ticks"].append({"ts": 1762223001000, "rows": [[9, 2.0]], "bad": 0, "total": 1})
    Telemetry(settings, store).accept(body)
    (msg,) = messages(store)
    assert [t["ts"] for t in msg["ticks"]] == [1762223000000, 1762223001000]


def test_large_tick_is_split_with_same_ts(settings, store):
    settings.max_message_bytes = 2000
    Telemetry(settings, store).accept(post(1000, bad=5))
    msgs = messages(store)
    assert len(msgs) > 1
    assert all(len(json.dumps(m, separators=(",", ":"))) <= 2000 for m in msgs)
    ticks = [t for m in msgs for t in m["ticks"]]
    assert {t["ts"] for t in ticks} == {1762223000000}
    assert [r[0] for t in ticks for r in t["r"]] == list(range(1, 1001))
    assert sum(t["bad"] for t in ticks) == 5  # counted once, not per split


def test_empty_tick_still_updates_health(settings, store):
    t = Telemetry(settings, store)
    assert t.accept(post(0, bad=4)) == 0
    assert store.peek(1) == []
    assert t.last_tick["FWT-02"]["bad"] == 4


def test_buffer_cap_drops_oldest(settings, store):
    settings.buffer_max_rows = 5
    t = Telemetry(settings, store)
    t.accept(post(3, ts=1762223000000))
    t.accept(post(3, ts=1762223001000))
    stats = store.outbox_stats()
    assert stats["rows"] == 3 and stats["dropped_rows_total"] == 3


def test_basic_ingest_topic(settings):
    settings.telemetry_rule = "hyper_telemetry"
    settings.telemetry_topic = "hyper/{thing}/telemetry"
    assert settings.telemetry_publish_topic == "$aws/rules/hyper_telemetry/hyper/gw-test/telemetry"
    settings.telemetry_rule = ""
    assert settings.telemetry_publish_topic == "hyper/gw-test/telemetry"
    assert settings.package_topic("request") == "hyper/gw-test/package/request"


@pytest.mark.parametrize("body", [
    {"ims": "x"},
    {"ims": "x", "ticks": [{"ts": 1762223000000, "rows": [[1, 2, 3]]}]},
    {"ims": "x", "ticks": [{"ts": 1762223000000, "rows": [["1", 2]]}]},
    {"ims": "x", "ticks": [{"ts": 1762223000000, "rows": [[1, {"nested": 1}]]}]},
    {"ims": "x", "ticks": [{"ts": "soon", "rows": []}]},
])
def test_malformed(settings, store, body):
    with pytest.raises(TelemetryError):
        Telemetry(settings, store).accept(body)
