"""Controls added after review round 3 (R-0007, R-0010): input limits, inbox safety, secrets."""

import io
import json
import os
import stat
import zipfile

import pytest

from gateway_agent import hamf
from gateway_agent.config import Settings
from gateway_agent.ignition import IgnitionClient
from gateway_agent.inbox import Inbox
from conftest import folder, ims_tree, leaf, provider, to_bytes, zipped


# ---------------------------------------------------------------- HAMF input limits (R3-1)

def test_export_over_size_limit_is_refused(monkeypatch):
    monkeypatch.setattr(hamf, "MAX_EXPORT_BYTES", 100)
    with pytest.raises(hamf.HamfError, match="limit"):
        hamf.load(b" " * 101, "big.json")


def test_zip_bomb_is_stopped_by_bounded_read(monkeypatch):
    monkeypatch.setattr(hamf, "MAX_UNCOMPRESSED_BYTES", 10_000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.json", b" " * 5_000_000)  # compresses to a few KB
    assert len(buf.getvalue()) < 20_000
    with pytest.raises(hamf.HamfError, match="expands past"):
        hamf.load(buf.getvalue(), "bomb.zip")


def test_uncompressed_budget_is_shared_across_members(monkeypatch):
    one = to_bytes(ims_tree())
    monkeypatch.setattr(hamf, "MAX_UNCOMPRESSED_BYTES", int(len(one) * 1.5))
    with pytest.raises(hamf.HamfError, match="expands past"):
        hamf.load(zipped({"a.json": ims_tree(), "b.json": ims_tree()}), "x.zip")


def test_too_many_zip_members(monkeypatch):
    monkeypatch.setattr(hamf, "MAX_ZIP_MEMBERS", 3)
    with pytest.raises(hamf.HamfError, match="members"):
        hamf.load(zipped({"%d.json" % i: ims_tree() for i in range(4)}), "x.zip")


def test_corrupt_zip_is_a_clean_error():
    with pytest.raises(hamf.HamfError, match="not a valid zip"):
        hamf.load(b"PK\x03\x04garbage", "x.zip")


@pytest.mark.parametrize("bad", ["a/b", "..", ".", "[other]x", "x]", " lead", "trail ", "ctl\x07", "n" * 256])
def test_unsafe_names_are_refused(bad):
    ap = "Switch.Petersburg.ACI.FWT-02.PPM.ENV"
    tree = provider(folder("Switch", folder("Petersburg", folder("ACI", folder("FWT-02", folder("ENV", leaf(bad, 1, 1, ap)))))))
    with pytest.raises(hamf.HamfError, match="name"):
        hamf.load(to_bytes(tree), "x.json")


def test_unsafe_folder_name_is_refused():
    tree = ims_tree()
    tree["tags"][0]["tags"][0]["name"] = "Peters/burg"
    with pytest.raises(hamf.HamfError, match="unsafe"):
        hamf.load(to_bytes(tree), "x.json")


def test_real_world_names_are_accepted():
    ap = "Switch.Petersburg.ACI.FWT-02.PPM.3WA-MainU"
    names = ["3WA-MainU", "Current_Phase_A", "Temp (C)", "Source 1 Voltage AN", "UMG801-PDU01"]
    tree = provider(folder("Switch", folder("Petersburg", folder("ACI", folder("FWT-02",
        folder("3WA-MainU", *[leaf(n, i + 1, 1, ap) for i, n in enumerate(names)]))))))
    assert len(hamf.load(to_bytes(tree), "x.json").leaves) == len(names)


# ---------------------------------------------------------------- inbox (R3-1)

def _failed(inbox_dir):
    return sorted(p.name for p in (inbox_dir / "failed").iterdir() if not p.name.endswith(".error"))


def test_inbox_refuses_symlink_without_reading_target(tmp_path, installer):
    secret = tmp_path / "outside.json"
    secret.write_text(json.dumps(ims_tree()))
    inbox = Inbox(tmp_path / "inbox", installer)
    link = inbox.dir / "link.json"
    link.symlink_to(secret)
    inbox.scan_once()
    assert installer.store.package("FWT-02") is None
    assert _failed(inbox.dir)[0].endswith("link.json")
    assert secret.exists()  # target untouched
    assert "regular file" in next((inbox.dir / "failed").glob("*.error")).read_text()


def test_inbox_refuses_oversize_file(tmp_path, installer, monkeypatch):
    import gateway_agent.inbox as inbox_mod
    monkeypatch.setattr(inbox_mod, "MAX_EXPORT_BYTES", 50)
    inbox = Inbox(tmp_path / "inbox", installer)
    (inbox.dir / "big.json").write_bytes(to_bytes(ims_tree()))
    inbox.scan_once()
    inbox.scan_once()
    assert installer.store.package("FWT-02") is None
    assert _failed(inbox.dir)[0].endswith("big.json")


def test_inbox_installs_regular_file_after_it_settles(tmp_path, installer):
    inbox = Inbox(tmp_path / "inbox", installer)
    (inbox.dir / "ok.json").write_bytes(to_bytes(ims_tree()))
    inbox.scan_once()
    assert installer.store.package("FWT-02") is None  # waits one poll for the size to settle
    inbox.scan_once()
    assert installer.store.package("FWT-02")["status"] == "installed"
    assert stat.S_IMODE(os.stat(inbox.dir).st_mode) == 0o700


# ---------------------------------------------------------------- tokens and secrets (R3-2..R3-4)

@pytest.mark.parametrize("token", ["has space in it", "line\r\nX-Injected: 1", "short", "tab\there12345"])
def test_ignition_token_that_could_break_a_header_is_refused(token):
    with pytest.raises(ValueError, match="IGNITION_API_TOKEN"):
        IgnitionClient("http://ignition:8088", token)


def test_secrets_can_come_from_files(tmp_path, monkeypatch):
    for name, value in (("AGENT_API_TOKEN", "admin-from-file-0123456789"),
                        ("AGENT_INGEST_TOKEN", "ingest-from-file-0123456789"),
                        ("IGNITION_API_TOKEN", "ign-from-file-0123456789")):
        path = tmp_path / name.lower()
        path.write_text(value + "\n")
        monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(name + "_FILE", str(path))
    s = Settings()
    assert (s.api_token, s.ingest_token, s.ignition_api_token) == (
        "admin-from-file-0123456789", "ingest-from-file-0123456789", "ign-from-file-0123456789")


def test_setting_both_value_and_file_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_API_TOKEN", "admin-token-0123456789abc")
    monkeypatch.setenv("AGENT_API_TOKEN_FILE", str(tmp_path / "x"))
    with pytest.raises(ValueError, match="not both"):
        Settings()


def test_malformed_operator_token_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_API_TOKEN", "short")
    s = Settings()
    s.state_dir = tmp_path
    with pytest.raises(ValueError, match="AGENT_API_TOKEN"):
        s.ensure_tokens()


def test_rotate_tokens_replaces_generated_tokens(tmp_path, monkeypatch):
    for name in ("AGENT_API_TOKEN", "AGENT_INGEST_TOKEN"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name + "_FILE", raising=False)
    s = Settings()
    s.state_dir = tmp_path
    s.ensure_tokens()
    old_admin, old_ingest = s.api_token, s.ingest_token
    assert s.rotate_tokens(admin=False, ingest=True) == ["ingest_token"]
    fresh = Settings()
    fresh.state_dir = tmp_path
    fresh.ensure_tokens()
    assert fresh.api_token == old_admin and fresh.ingest_token != old_ingest
    assert stat.S_IMODE(os.stat(tmp_path / "ingest_token").st_mode) == 0o600
    assert not list(tmp_path.glob(".*.tmp"))


def test_rotate_refuses_externally_managed_token(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_API_TOKEN", "admin-token-0123456789abc")
    s = Settings()
    s.state_dir = tmp_path
    with pytest.raises(ValueError, match="externally"):
        s.rotate_tokens(admin=True, ingest=False)
