import ast
import json

import pytest

from gateway_agent import generator, hamf
from conftest import ims_tree, to_bytes


def render(pkg, **kw):
    opts = dict(provider="default", tag_root="", agent_url="http://gateway-agent:8600/",
                ingest_token="ingest-token-0123456789abcdef", interval_ms=1000)
    opts.update(kw)
    return generator.render(pkg, **opts)


def _exec_meta(source):
    ns = {}
    exec(compile(source, "code.py", "exec"), ns)
    return ns


def test_paths_and_files():
    art = render(hamf.load(to_bytes(ims_tree()), "x.json"))
    assert art.tag_base_path == "[default]Switch/Petersburg/ACI"
    assert art.tag_folder_path == "[default]Switch/Petersburg/ACI/FWT-02"
    assert sorted(art.ims_files) == [
        "ignition/script-python/hyper/ims/FWT_02/code.py",
        "ignition/script-python/hyper/ims/FWT_02/resource.json",
        "ignition/timer/ims_FWT_02/handleTimerEvent.py",
        "ignition/timer/ims_FWT_02/resource.json",
    ]
    assert art.tags[0]["name"] == "FWT-02"


def test_tag_root_prefix():
    art = render(hamf.load(to_bytes(ims_tree()), "x.json"), tag_root="ACI/")
    assert art.tag_base_path == "[default]ACI/Switch/Petersburg/ACI"
    ns = _exec_meta(art.ims_files["ignition/script-python/hyper/ims/FWT_02/code.py"].decode())
    assert ns["meta"]()[0][0].startswith("[default]ACI/Switch/")


def test_meta_module_round_trips():
    pkg = hamf.load(to_bytes(ims_tree(devices=("ENV", "PDU01"))), "x.json")
    ns = _exec_meta(render(pkg).ims_files["ignition/script-python/hyper/ims/FWT_02/code.py"].decode())
    assert ns["IMS_KEY"] == "FWT-02"
    assert ns["VERSION"] == pkg.version
    rows = ns["meta"]()
    assert rows[0] == ["[default]Switch/Petersburg/ACI/FWT-02/PPM/ENV/Temperature", 100]
    assert len(rows) == 4


def test_large_meta_is_split_under_jvm_constant_limit():
    devices = tuple("CB%02d" % i for i in range(400))
    pkg = hamf.load(to_bytes(ims_tree(devices=devices)), "x.json")
    source = render(pkg).ims_files["ignition/script-python/hyper/ims/FWT_02/code.py"].decode()
    tree = ast.parse(source)
    chunks = next(n.value for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "_META_JSON")
    assert len(chunks.elts) > 1
    assert all(len(c.value.encode()) < 65535 for c in chunks.elts)
    assert len(_exec_meta(source)["meta"]()) == 800


def test_timer_script_has_everything_inside_handler():
    art = render(hamf.load(to_bytes(ims_tree()), "x.json"))
    tree = ast.parse(art.ims_files["ignition/timer/ims_FWT_02/handleTimerEvent.py"].decode())
    assert len(tree.body) == 1 and isinstance(tree.body[0], ast.FunctionDef)
    assert tree.body[0].name == "handleTimerEvent"


def test_timer_resource_settings():
    art = render(hamf.load(to_bytes(ims_tree()), "x.json"), interval_ms=5000, enabled=False)
    res = json.loads(art.ims_files["ignition/timer/ims_FWT_02/resource.json"])
    assert res["attributes"]["delay"] == 5000
    assert res["attributes"]["enabled"] is False
    assert res["attributes"]["sharedThread"] is False


def test_publisher_parses_and_has_agent_url_and_token():
    files = generator.shared_project_files("http://gateway-agent:8600/", "ingest-token-0123456789abcdef")
    source = files["ignition/script-python/hyper/publisher/code.py"].decode()
    ast.parse(source)
    assert 'AGENT_URL = "http://gateway-agent:8600" + "/v1/telemetry"' in source
    assert 'INGEST_TOKEN = "ingest-token-0123456789abcdef"' in source
    assert '"X-Agent-Token": INGEST_TOKEN' in source
    assert json.loads(files["project.json"])["enabled"] is True


@pytest.mark.parametrize("url", [
    'http://x"+__import__("os").system("id")+"',
    "file:///etc/passwd",
    "http://host/path?q=1",
    "ftp://host",
])
def test_publisher_rejects_unsafe_agent_url(url):
    with pytest.raises(ValueError):
        generator.shared_project_files(url, "ingest-token-0123456789abcdef")


@pytest.mark.parametrize("token", ['abc"+os.system("id")+"0123456789', "short", "has space 0123456789abc"])
def test_publisher_rejects_unsafe_token(token):
    with pytest.raises(ValueError):
        generator.shared_project_files("http://gateway-agent:8600", token)
