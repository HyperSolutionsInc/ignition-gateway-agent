import pytest

from gateway_agent import hamf
from conftest import folder, ims_tree, leaf, provider, to_bytes, zipped


def test_bare_provider_tree():
    pkg = hamf.load(to_bytes(ims_tree()), "x.json")
    assert pkg.ims_key == "FWT-02"
    assert pkg.ims_folder == ("Switch", "Petersburg", "ACI", "FWT-02")
    assert len(pkg.leaves) == 2
    assert pkg.leaves[0].device == "ENV"
    assert pkg.module_name == "FWT_02"


def test_envelope_keeps_devices_and_timestamp():
    env = {"generatedAt": "2026-11-01T00:00:00Z", "devices": [{"name": "dev", "ipAddress": "10.0.0.5"}], "config": ims_tree()}
    pkg = hamf.load(to_bytes(env), "x.json")
    assert pkg.generated_at == "2026-11-01T00:00:00Z"
    assert pkg.devices == [{"name": "dev", "ipAddress": "10.0.0.5"}]


def test_zip_of_per_device_files_is_merged():
    data = zipped({
        "ENV.ignition.json": ims_tree(devices=("ENV",), base_id=100),
        "PDU01.ignition.json": ims_tree(devices=("PDU01",), base_id=200),
        "__MACOSX/._junk.json": {"ignored": True},
    })
    pkg = hamf.load(data, "edge-config.zip")
    assert len(pkg.leaves) == 4
    ppm = pkg.ims_node["tags"][0]
    assert sorted(n["name"] for n in ppm["tags"]) == ["ENV", "PDU01"]


def test_version_is_content_hash():
    a = hamf.load(to_bytes(ims_tree()), "a.json").version
    b = hamf.load(to_bytes(ims_tree()), "b.json").version
    c = hamf.load(to_bytes(ims_tree(base_id=900)), "c.json").version
    assert a == b != c


def test_rejects_export_spanning_two_ims():
    tree = ims_tree("FWT-01")
    other = ims_tree("FWT-02", base_id=500)
    tree["tags"][0]["tags"][0]["tags"][0]["tags"].append(other["tags"][0]["tags"][0]["tags"][0]["tags"][0])
    with pytest.raises(hamf.HamfError, match="2 IMS folders"):
        hamf.load(to_bytes(tree), "x.json")


def test_rejects_missing_identity():
    bad = provider(folder("A", folder("B", folder("C", folder("D", {"name": "t", "tagType": "AtomicTag", "opcItemPath": "x"})))))
    with pytest.raises(hamf.HamfError, match="identity"):
        hamf.load(to_bytes(bad), "x.json")


def test_rejects_conflicting_duplicate_tag():
    a = ims_tree()
    b = ims_tree()
    b["tags"][0]["tags"][0]["tags"][0]["tags"][0]["tags"][0]["tags"][0]["tags"][0]["opcItemPath"] = "[dev]3/9999"
    with pytest.raises(hamf.HamfError, match="conflicting"):
        hamf.load(zipped({"a.json": a, "b.json": b}), "x.zip")


def test_rejects_reused_tag_id():
    ap = "Switch.Petersburg.ACI.FWT-02.PPM.ENV"
    tree = provider(folder("Switch", folder("Petersburg", folder("ACI", folder("FWT-02",
        folder("ENV", leaf("a", 1, 1, ap), leaf("b", 1, 1, ap)))))))
    with pytest.raises(hamf.HamfError, match="tag_id 1"):
        hamf.load(to_bytes(tree), "x.json")


def test_rejects_non_export():
    with pytest.raises(hamf.HamfError, match="Provider"):
        hamf.load(b'{"hello": 1}', "x.json")


def test_ims_key_override():
    assert hamf.load(to_bytes(ims_tree()), "x.json", ims_key="IMS-07").ims_key == "IMS-07"
