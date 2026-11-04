"""Turn an IMS package into Ignition artifacts.

For each IMS the agent generates:

  * a tag import: the IMS folder node, imported under its parent folder in the tag provider,
    so installing or updating one IMS never touches the others;
  * a project library script `hyper.ims.<module>` holding that IMS's tag metadata;
  * a gateway timer script `ims_<module>` that calls the shared publisher every interval.

Shared across all IMSs (rewritten on every install so they stay current):

  * project.json for the managed project;
  * the publisher library `hyper.publisher`.

The tag metadata is stored as JSON split across several string constants. A single large
literal list would exceed the JVM's 64 KB method limit once Jython compiles the module, which
is the failure the hand-written timer scripts had to work around.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any

from .hamf import Package

META_CHUNK_CHARS = 30_000
MANAGED_BY = "gateway-agent"


@dataclass
class Artifacts:
    ims_key: str
    version: str
    tag_base_path: str  # where the IMS folder node is imported, e.g. "[default]Switch/Petersburg/ACI"
    tag_folder_path: str  # the IMS folder itself, e.g. "[default]Switch/Petersburg/ACI/FWT-02"
    tags: list[dict[str, Any]]  # import payload: [IMS folder node]
    ims_files: dict[str, bytes]  # project-relative path -> content, owned by this IMS
    shared_files: dict[str, bytes]  # project-relative path -> content, shared by all IMSs
    manifest: dict[str, Any]


def ims_resource_dirs(module: str) -> list[str]:
    """Project-relative resource folders owned by one IMS (used for removal)."""
    return ["ignition/script-python/hyper/ims/%s" % module, "ignition/timer/ims_%s" % module]


def render(
    pkg: Package,
    *,
    provider: str,
    tag_root: str,
    agent_url: str,
    ingest_token: str,
    interval_ms: int,
    enabled: bool = True,
) -> Artifacts:
    root = [p for p in tag_root.strip("/").split("/") if p]
    folder = tuple(root) + pkg.ims_folder
    base_path = "[%s]%s" % (provider, "/".join(folder[:-1]))
    folder_path = "[%s]%s" % (provider, "/".join(folder))
    module = pkg.module_name

    # The gateway only needs to know which tag_id each path is; everything else about a tag
    # (asset, producer, device, value type) is resolved from tag_id in the cloud.
    meta_rows = [["[%s]%s" % (provider, "/".join(tuple(root) + leaf.path)), leaf.tag_id] for leaf in pkg.leaves]

    ims_files = {
        "ignition/script-python/hyper/ims/%s/code.py" % module: _meta_module(pkg, meta_rows).encode("utf-8"),
        "ignition/script-python/hyper/ims/%s/resource.json" % module: _library_resource(),
        "ignition/timer/ims_%s/handleTimerEvent.py" % module: _timer_script(module).encode("utf-8"),
        "ignition/timer/ims_%s/resource.json" % module: _timer_resource(interval_ms, enabled),
    }
    shared_files = shared_project_files(agent_url, ingest_token)

    manifest = {
        "ims": pkg.ims_key,
        "version": pkg.version,
        "module": module,
        "tag_count": len(pkg.leaves),
        "tag_folder": folder_path,
        "devices": pkg.devices,
        "hamf_generated_at": pkg.generated_at,
        "sources": pkg.source_names,
        "interval_ms": interval_ms,
        "enabled": enabled,
    }
    return Artifacts(
        ims_key=pkg.ims_key,
        version=pkg.version,
        tag_base_path=base_path,
        tag_folder_path=folder_path,
        tags=[pkg.ims_node],
        ims_files=ims_files,
        shared_files=shared_files,
        manifest=manifest,
    )


def shared_project_files(agent_url: str, ingest_token: str) -> dict[str, bytes]:
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:[0-9]{1,5})?/?", agent_url):
        raise ValueError("AGENT_URL_FOR_IGNITION must be http(s)://host[:port], got %r" % agent_url)
    if not re.fullmatch(r"[A-Za-z0-9_\-]{16,128}", ingest_token):
        raise ValueError("the ingest token must be 16-128 URL-safe characters")
    publisher = resources.files("gateway_agent.ignition_scripts").joinpath("publisher.py").read_text("utf-8")
    # Inserted as escaped literals so no value can alter the generated code.
    publisher = publisher.replace("__AGENT_BASE_URL__", json.dumps(agent_url.rstrip("/")))
    publisher = publisher.replace("__INGEST_TOKEN__", json.dumps(ingest_token))
    return {
        "project.json": _json(
            {
                "title": "Hyper Gateway",
                "description": "Managed by %s. Edits made in Designer are overwritten on the next import." % MANAGED_BY,
                "enabled": True,
                "inheritable": False,
                "parent": "",
            }
        ),
        "ignition/script-python/hyper/publisher/code.py": publisher.encode("utf-8"),
        "ignition/script-python/hyper/publisher/resource.json": _library_resource(),
    }


# ---------------------------------------------------------------- resource bodies


def _meta_module(pkg: Package, rows: list[list[Any]]) -> str:
    encoded = json.dumps(rows, separators=(",", ":"), ensure_ascii=True)
    chunks = [encoded[i : i + META_CHUNK_CHARS] for i in range(0, len(encoded), META_CHUNK_CHARS)]
    # json.dumps of an ASCII string is also a valid Python 2 and 3 string literal.
    literals = ",\n".join("\t" + json.dumps(c) for c in chunks)
    return (
        "# Generated by %(by)s from a HAMF export. Do not edit; re-import the export instead.\n"
        "# IMS %(ims)s, %(count)d tags, sources: %(sources)s\n"
        "# Rows: [tag path, tag_id]\n"
        "import json\n"
        "\n"
        "IMS_KEY = %(ims_lit)s\n"
        "VERSION = %(version_lit)s\n"
        "\n"
        "_META_JSON = [\n%(literals)s\n]\n"
        "_meta = None\n"
        "\n"
        "\n"
        "def meta():\n"
        "\tglobal _meta\n"
        "\tif _meta is None:\n"
        "\t\t_meta = json.loads(\"\".join(_META_JSON))\n"
        "\treturn _meta\n"
    ) % {
        "by": MANAGED_BY,
        "ims": pkg.ims_key,
        "count": len(rows),
        "sources": ", ".join(pkg.source_names)[:200],
        "ims_lit": json.dumps(pkg.ims_key),
        "version_lit": json.dumps(pkg.version),
        "literals": literals,
    }


def _timer_script(module: str) -> str:
    # The whole body sits inside handleTimerEvent(); nothing runs at module level.
    return (
        "def handleTimerEvent():\n"
        "\t# Generated by %s. Do not edit; re-import the HAMF export instead.\n"
        "\timport hyper.publisher\n"
        "\timport hyper.ims.%s as ims\n"
        "\thyper.publisher.tick(ims.IMS_KEY, ims.VERSION, ims.meta())\n"
    ) % (MANAGED_BY, module)


def _library_resource() -> bytes:
    return _json(
        {
            "scope": "A",
            "version": 1,
            "restricted": False,
            "overridable": True,
            "files": ["code.py"],
            "attributes": {"hintScope": 2, "lastModification": {"actor": MANAGED_BY}},
        }
    )


def _timer_resource(interval_ms: int, enabled: bool) -> bytes:
    return _json(
        {
            "scope": "G",
            "version": 1,
            "restricted": False,
            "overridable": True,
            "files": ["handleTimerEvent.py"],
            "attributes": {
                # Each IMS gets its own thread so a slow read on one IMS can't stall the rest.
                "sharedThread": False,
                "delay": interval_ms,
                "fixedDelay": True,
                "enabled": enabled,
                "lastModification": {"actor": MANAGED_BY},
            },
        }
    )


def _json(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2) + "\n").encode("utf-8")
