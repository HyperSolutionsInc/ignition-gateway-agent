"""Controls confirmed or added after review round 4 (R-0014, R-0015): TLS, SQL, telemetry input."""

import ast
import os
import json
import math
import shutil
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from gateway_agent import shadow, store as store_mod
from gateway_agent.iot import tls_context
from gateway_agent.telemetry import Telemetry, TelemetryError

NOW_MS = int(time.time() * 1000)


# ---------------------------------------------------------------- TLS verification (R4-1, R4-2, R4-3)

def _assert_verifying(ctx):
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_package_download_context_verifies_certificates():
    _assert_verifying(shadow.download_tls_context())


@pytest.fixture
def self_signed(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    return key, cert


def test_mqtt_context_verifies_server_and_pins_ca(self_signed, settings):
    key, cert = self_signed
    settings.iot_ca_path, settings.iot_cert_path, settings.iot_key_path = str(cert), str(cert), str(key)
    ctx = tls_context(settings)
    _assert_verifying(ctx)
    # Only the configured CA is trusted (the Amazon root CA in production), not the system store.
    assert len(ctx.get_ca_certs()) == 1


def test_package_download_rejects_untrusted_certificate(self_signed):
    key, cert = self_signed

    class Ok(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Ok)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(str(cert), str(key))
    srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(Exception) as exc:
            shadow._download("https://localhost:%d/p.zip" % srv.server_address[1])
        assert "CERTIFICATE_VERIFY_FAILED" in str(exc.value) or "certificate verify failed" in str(exc.value)
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- SQL (R4-4)

def test_store_never_builds_sql_from_values():
    """Every execute() call's SQL argument is a constant string: values only go in as parameters."""
    tree = ast.parse(Path(store_mod.__file__).read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in ("execute", "executescript")]
    assert calls
    for call in calls:
        sql = call.args[0]
        assert isinstance(sql, ast.Constant) or (isinstance(sql, ast.Name) and sql.id == "SCHEMA"), ast.dump(sql)[:120]


@pytest.mark.parametrize("payload", ["x'); DROP TABLE outbox;--", "x\" OR 1=1 --", "'; DELETE FROM packages; --"])
def test_injection_payloads_are_stored_as_data(store, payload):
    store.enqueue(payload, 1, b"{}", 100)
    store.record_package(payload, "v1", "installed", "cli", {"tag_folder": payload}, [])
    store.put(payload, payload)
    assert store.peek(10)[0][1] == payload
    assert store.package(payload)["manifest"]["tag_folder"] == payload
    assert store.get(payload) == payload
    assert store.outbox_stats()["messages"] == 1  # outbox table still there and intact


# ---------------------------------------------------------------- telemetry input (R4-5)

def body(rows=None, ts=NOW_MS, ims="FWT-02", version="abc123", **tick):
    return {"ims": ims, "version": version,
            "ticks": [dict({"ts": ts, "rows": rows if rows is not None else [[1, 2.5]]}, **tick)]}


def test_nan_and_infinity_are_dropped_and_counted_bad(settings, store):
    t = Telemetry(settings, store)
    # What the HTTP server's json.loads produces from a publisher post containing NaN/Infinity.
    raw = json.loads('{"ims":"FWT-02","version":"v1","ticks":[{"ts":%d,"bad":2,"rows":[[1,NaN],[2,Infinity],[3,-Infinity],[4,1.5]]}]}' % NOW_MS)
    assert t.accept(raw) == 1
    (_, _, payload), = store.peek(1)
    text = payload.decode()
    assert "NaN" not in text and "Infinity" not in text
    msg = json.loads(text)
    assert msg["ticks"][0]["r"] == [[4, 1.5]] and msg["ticks"][0]["bad"] == 5


def test_valid_post_is_accepted(settings, store):
    assert Telemetry(settings, store).accept(body([[1, 2.5], [2, True], [3, "Closed"], [4, 7]])) == 4


@pytest.mark.parametrize("bad", [
    body(ims="has space"), body(ims="x" * 65), body(ims="../etc"), body(ims=""),
    body(version="v 1"), body(version=123),
    body(ts=1), body(ts=NOW_MS + 2 * 24 * 3600 * 1000), body(ts=float("nan")), body(ts=True),
    body([[0, 1.0]]), body([[-5, 1.0]]), body([[True, 1.0]]), body([[2**63, 1.0]]),
    body([[1, "x" * 1025]]), body([[1, None]]), body([[1, [1, 2]]]),
    body(bad=-1), body(bad="3"),
    {"ims": "FWT-02", "ticks": "nope"}, ["not", "an", "object"],
])
def test_invalid_telemetry_is_rejected(settings, store, bad):
    with pytest.raises(TelemetryError):
        Telemetry(settings, store).accept(bad)
    assert store.peek(1) == []


def test_post_size_limits(settings, store, monkeypatch):
    import gateway_agent.telemetry as tm
    monkeypatch.setattr(tm, "MAX_TICKS_PER_POST", 2)
    monkeypatch.setattr(tm, "MAX_READINGS_PER_TICK", 3)
    t = Telemetry(settings, store)
    many_ticks = {"ims": "FWT-02", "ticks": [{"ts": NOW_MS, "rows": [[1, 1.0]]}] * 3}
    with pytest.raises(TelemetryError, match="ticks"):
        t.accept(many_ticks)
    with pytest.raises(TelemetryError, match="readings"):
        t.accept(body([[i + 1, 1.0] for i in range(4)]))


def test_output_encoder_refuses_nan():
    with pytest.raises(ValueError):
        json.dumps({"x": math.nan}, allow_nan=False)


# ---------------------------------------------------------------- token exposure (R4-6, R4-7)

def test_rotate_tokens_never_prints_token_values(tmp_path, monkeypatch, capsys):
    from gateway_agent import cli
    for name in ("AGENT_API_TOKEN", "AGENT_INGEST_TOKEN"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name + "_FILE", raising=False)
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    assert cli.main(["rotate-tokens"]) == 0
    out = capsys.readouterr()
    for name in ("api_token", "ingest_token"):
        value = (tmp_path / name).read_text().strip()
        assert value and value not in out.out and value not in out.err


def test_secret_redaction_masks_messages_args_and_tracebacks():
    import io
    import logging
    from gateway_agent.cli import SecretRedactor

    secret = "super-secret-token-0123456789"
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretRedactor([secret, "", "short"]))
    log = logging.getLogger("redaction-test")
    log.addHandler(handler)
    log.propagate = False
    try:
        log.warning("token is %s", secret)
        log.warning("inline " + secret)
        try:
            raise RuntimeError("failed with " + secret)
        except RuntimeError:
            log.exception("boom")
    finally:
        log.removeHandler(handler)
    text = stream.getvalue()
    assert secret not in text
    assert text.count("***") >= 3


# ---------------------------------------------------------------- round 5 (R-0024): rotation storage, IoT endpoint

def test_failed_secret_write_leaves_old_secret_and_no_temp_file(tmp_path, monkeypatch):
    from gateway_agent import config
    target = tmp_path / "api_token"
    config._write_secret(target, "old-token-0123456789abcdef")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(config.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        config._write_secret(target, "new-token-0123456789abcdef")
    assert target.read_text().strip() == "old-token-0123456789abcdef"
    assert not list(tmp_path.glob(".*.tmp"))


def test_secret_write_is_verified(tmp_path, monkeypatch):
    from gateway_agent import config
    real_replace = config.os.replace

    def replace_and_loosen(src, dst):
        real_replace(src, dst)
        os.chmod(dst, 0o644)  # simulate a filesystem that ignores the requested mode

    monkeypatch.setattr(config.os, "replace", replace_and_loosen)
    with pytest.raises(OSError, match="did not verify"):
        config._write_secret(tmp_path / "t", "token-0123456789abcdef")


@pytest.mark.parametrize("endpoint", [
    "https://abc-ats.iot.us-east-2.amazonaws.com", "abc-ats.iot.us-east-2.amazonaws.com:8883",
    "abc-ats.iot.us-east-2.amazonaws.com/path", "", "bad host", "-leading.example.com", "a..b",
])
def test_iot_endpoint_must_be_a_bare_hostname(settings, endpoint):
    settings.iot_endpoint = endpoint
    with pytest.raises(ValueError, match="IOT_ENDPOINT"):
        settings.validate_iot()


def test_iot_settings_validation(settings):
    settings.iot_endpoint = "a27n1c51bedgc5-ats.iot.us-east-2.amazonaws.com"
    settings.validate_iot()
    settings.iot_port = 0
    with pytest.raises(ValueError, match="IOT_PORT"):
        settings.validate_iot()
    settings.iot_port = 8883
    settings.thing_name, settings.client_id = "gw/../x", ""
    with pytest.raises(ValueError, match="IOT_THING_NAME"):
        settings.validate_iot()
