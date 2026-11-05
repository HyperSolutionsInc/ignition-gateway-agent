import json
from urllib.parse import parse_qs, urlsplit

import pytest

from gateway_agent import hamf
from gateway_agent.ignition import IgnitionError
from conftest import ims_tree, stored_tag_paths, to_bytes


def test_install_writes_project_and_calls_gateway(installer, settings, fake_ignition):
    result = installer.install(to_bytes(ims_tree()), "x.json", "cli")
    assert result.changed and result.ims == "FWT-02" and result.tag_count == 2

    project = settings.project_dir
    assert (project / "project.json").exists()
    assert (project / "ignition/script-python/hyper/publisher/code.py").exists()
    assert (project / "ignition/timer/ims_FWT_02/handleTimerEvent.py").exists()

    assert fake_ignition.paths() == ["/data/api/v1/tags/import", "/data/api/v1/scan/projects"]
    method, path, body, token, content_type = fake_ignition.calls[0]
    query = parse_qs(urlsplit(path).query)
    assert query["path"] == ["Switch/Petersburg/ACI"] and query["provider"] == ["default"]
    assert query["collisionPolicy"] == ["Overwrite"] and query["type"] == ["json"]
    # The body is the IMS folder node itself, as an export file (verified against 8.3.7)
    assert json.loads(body)["name"] == "FWT-02" and json.loads(body)["tagType"] == "Folder"
    assert content_type == "application/octet-stream"
    assert token == "test-token"
    assert stored_tag_paths(fake_ignition.tag_root / "default") == [
        "Switch/Petersburg/ACI/FWT-02/PPM/ENV/Leak", "Switch/Petersburg/ACI/FWT-02/PPM/ENV/Temperature"]

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
    root = fake_ignition.tag_root / "default"
    assert len(stored_tag_paths(root)) == 4
    fake_ignition.calls.clear()
    installer.install(to_bytes(ims_tree(devices=("ENV",))), "v2.json", "cli")
    # Overwrite alone would keep PDU01's tags; the installer removes them from the files, then
    # asks for a config scan before importing.
    assert fake_ignition.paths() == ["/data/api/v1/scan/config", "/data/api/v1/tags/import", "/data/api/v1/scan/projects"]
    assert stored_tag_paths(root) == [
        "Switch/Petersburg/ACI/FWT-02/PPM/ENV/Leak", "Switch/Petersburg/ACI/FWT-02/PPM/ENV/Temperature"]


def test_update_that_drops_one_tag_keeps_its_neighbours(installer, fake_ignition):
    tree = ims_tree(devices=("ENV",))
    installer.install(to_bytes(tree), "v1.json", "cli")
    env = tree["tags"][0]["tags"][0]["tags"][0]["tags"][0]["tags"][0]["tags"][0]
    env["tags"] = [t for t in env["tags"] if t["name"] != "Leak"]
    installer.install(to_bytes(tree), "v2.json", "cli")
    assert stored_tag_paths(fake_ignition.tag_root / "default") == ["Switch/Petersburg/ACI/FWT-02/PPM/ENV/Temperature"]


def test_tag_deletion_cannot_escape_the_tag_definitions(installer):
    with pytest.raises(ValueError, match="escapes"):
        installer._delete_tag_definitions(["../../../../etc/passwd"])


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
    assert not (fake_ignition.tag_root / "default/Switch/Petersburg/ACI/FWT-02").exists()
    assert fake_ignition.paths() == ["/data/api/v1/scan/config", "/data/api/v1/scan/projects"]
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


def test_install_fails_if_ignition_does_not_load_the_project(installer, fake_ignition, monkeypatch):
    # Seen on a live 8.3.7 gateway: a scan returns 200 while Ignition skips a project it can't parse.
    fake_ignition.projects = []
    monkeypatch.setattr(installer, "_confirm_project_loaded",
                        lambda timeout_s=0.5, f=installer._confirm_project_loaded: f(timeout_s))
    with pytest.raises(IgnitionError, match="did not load"):
        installer.install(to_bytes(ims_tree()), "x.json", "cli")
    assert installer.store.package("FWT-02")["status"] == "failed"


def test_generated_resources_carry_no_lastmodification(installer, settings):
    # 8.3.7 needs a timestamp on lastModification and skips the project without one; omit it.
    installer.install(to_bytes(ims_tree()), "x.json", "cli")
    for res in settings.project_dir.rglob("resource.json"):
        assert "lastModification" not in json.loads(res.read_text())["attributes"], res
