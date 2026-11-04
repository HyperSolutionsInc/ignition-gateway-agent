import json
from urllib.parse import parse_qs, urlsplit

import pytest

from gateway_agent import hamf
from gateway_agent.ignition import IgnitionError
from conftest import ims_tree, to_bytes


def test_install_writes_project_and_calls_gateway(installer, settings, fake_ignition):
    result = installer.install(to_bytes(ims_tree()), "x.json", "cli")
    assert result.changed and result.ims == "FWT-02" and result.tag_count == 2

    project = settings.project_dir
    assert (project / "project.json").exists()
    assert (project / "ignition/script-python/hyper/publisher/code.py").exists()
    assert (project / "ignition/timer/ims_FWT_02/handleTimerEvent.py").exists()

    assert fake_ignition.paths() == ["/data/api/v1/tags/import", "/data/api/v1/scan/projects"]
    method, path, body, token = fake_ignition.calls[0]
    query = parse_qs(urlsplit(path).query)
    assert query["path"] == ["Switch/Petersburg/ACI"] and query["provider"] == ["default"]
    assert json.loads(body)["tags"][0]["name"] == "FWT-02"
    assert token == "test-token"

    pkg = installer.store.package("FWT-02")
    assert pkg["status"] == "installed" and pkg["version"] == result.version


def test_reimport_same_export_is_noop(installer, fake_ignition):
    installer.install(to_bytes(ims_tree()), "x.json", "cli")
    calls = len(fake_ignition.calls)
    assert installer.install(to_bytes(ims_tree()), "again.json", "inbox").changed is False
    assert len(fake_ignition.calls) == calls


def test_second_ims_is_independent(installer, settings):
    installer.install(to_bytes(ims_tree("FWT-01", base_id=100)), "a.json", "cli")
    installer.install(to_bytes(ims_tree("FWT-02", base_id=200)), "b.json", "cli")
    assert [p["ims"] for p in installer.store.packages()] == ["FWT-01", "FWT-02"]
    assert (settings.project_dir / "ignition/timer/ims_FWT_01").is_dir()
    assert (settings.project_dir / "ignition/timer/ims_FWT_02").is_dir()


def test_update_deletes_dropped_tags(installer, fake_ignition):
    installer.install(to_bytes(ims_tree(devices=("ENV", "PDU01"))), "v1.json", "cli")
    fake_ignition.calls.clear()
    installer.install(to_bytes(ims_tree(devices=("ENV",))), "v2.json", "cli")
    assert fake_ignition.paths()[0] == "/data/api/v1/tags/delete"
    deleted = json.loads(fake_ignition.calls[0][2])["paths"]
    assert deleted == [
        "Switch/Petersburg/ACI/FWT-02/PPM/PDU01/Leak",
        "Switch/Petersburg/ACI/FWT-02/PPM/PDU01/Temperature",
    ]


def test_reconfigure_rewrites_timer(installer, settings):
    installer.install(to_bytes(ims_tree()), "x.json", "cli")
    installer.reconfigure("FWT-02", enabled=False, interval_ms=10000)
    res = json.loads((settings.project_dir / "ignition/timer/ims_FWT_02/resource.json").read_text())
    assert res["attributes"]["enabled"] is False and res["attributes"]["delay"] == 10000


def test_remove(installer, settings, fake_ignition):
    installer.install(to_bytes(ims_tree()), "x.json", "cli")
    fake_ignition.calls.clear()
    assert installer.remove("FWT-02", "cli") is True
    assert not (settings.project_dir / "ignition/timer/ims_FWT_02").exists()
    assert (settings.project_dir / "ignition/script-python/hyper/publisher").exists()
    assert json.loads(fake_ignition.calls[0][2])["paths"] == ["Switch/Petersburg/ACI/FWT-02"]
    assert installer.store.package("FWT-02")["status"] == "removed"
    assert installer.remove("FWT-02", "cli") is False


def test_failed_import_is_recorded(installer, fake_ignition):
    fake_ignition.fail_import = True
    with pytest.raises(IgnitionError):
        installer.install(to_bytes(ims_tree()), "x.json", "cli")
    pkg = installer.store.package("FWT-02")
    assert pkg["status"] == "failed" and "HTTP 500" in pkg["error"]
    # a retry after the gateway recovers goes through even though the version is unchanged
    fake_ignition.fail_import = False
    assert installer.install(to_bytes(ims_tree()), "x.json", "cli").changed


def test_same_folder_under_two_keys_is_refused(installer):
    installer.install(to_bytes(ims_tree()), "x.json", "cli")
    with pytest.raises(hamf.HamfError, match="already belongs"):
        installer.install(to_bytes(ims_tree()), "x.json", "cli", ims_key="OTHER")


def test_render_to_folder_does_not_touch_gateway(installer, tmp_path, fake_ignition):
    art = installer.render_to(to_bytes(ims_tree()), "x.json", tmp_path / "out")
    assert (tmp_path / "out/project/ignition/timer/ims_FWT_02/handleTimerEvent.py").exists()
    assert json.loads((tmp_path / "out/tags/FWT_02.json").read_text())["importPath"] == art.tag_base_path
    assert fake_ignition.calls == []
