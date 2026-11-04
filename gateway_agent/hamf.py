"""Parse a HAMF edge-config export into one IMS package.

HAMF's `GET /v1/assets/:id/ignition-export` returns an envelope whose `config` field is an
Ignition Provider tree; the UI download saves only that tree, sometimes one file per device
zipped together. All three shapes are accepted:

  * the full envelope            {"generatedAt": ..., "devices": [...], "config": {Provider}}
  * a bare Provider tree         {"name": "", "tagType": "Provider", "tags": [...]}
  * a .zip of either, merged     (one file per device, as the edge-config download produces)

A package is exactly one IMS: every tag must sit under the same folder at IMS_PATH_DEPTH
(customer / campus / building / power cell by default). Anything that would silently produce
a partial or mixed config raises HamfError instead.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Iterable

ATOMIC = "AtomicTag"
FOLDER = "Folder"
PROVIDER = "Provider"

IDENTITY_FIELDS = ("tag_id", "asset_id", "asset_path", "signal_path")

# Input limits. An export is untrusted input (it can arrive over HTTP, the inbox or the shadow),
# so sizes are capped before parsing and zip members are read with bounded reads that don't
# trust the sizes the archive declares (zip-bomb protection). The real 25-device FWT-02 export
# is 2.8 MB uncompressed.
MAX_EXPORT_BYTES = 64 * 1024 * 1024
MAX_ZIP_MEMBERS = 500
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_NAME_LENGTH = 255
# A tag or folder name becomes one segment of an Ignition tag path, so it must not be able to
# add segments ("/"), name a provider ("[", "]"), or walk the tree ("." / "..").
_UNSAFE_NAME = re.compile(r"[/\[\]\x00-\x1f\x7f]")


class HamfError(ValueError):
    pass


@dataclass(frozen=True)
class Leaf:
    """One AtomicTag, addressed by its folder path from the provider root."""

    path: tuple[str, ...]  # folders + tag name
    tag_id: int
    asset_id: int
    asset_path: str
    signal_path: str
    data_type: str
    producer_id: int | None = None

    @property
    def device(self) -> str:
        # Last segment of the asset path: the short device label (CB01, PDU02, ENV, ...).
        return self.asset_path.rsplit(".", 1)[-1]


@dataclass
class Package:
    ims_key: str
    ims_folder: tuple[str, ...]  # provider-relative path of the IMS folder
    ims_node: dict[str, Any]  # the IMS Folder node, ready to import under its parent
    leaves: list[Leaf]
    devices: list[dict[str, Any]] = field(default_factory=list)
    generated_at: str | None = None
    source_names: list[str] = field(default_factory=list)

    @property
    def version(self) -> str:
        """Content hash of the IMS tree: the same HAMF export always yields the same version."""
        canonical = json.dumps(self.ims_node, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    @property
    def module_name(self) -> str:
        return module_name_for(self.ims_key)


def module_name_for(ims_key: str) -> str:
    """IMS key -> a valid Python identifier for the generated Ignition resources."""
    name = re.sub(r"[^0-9A-Za-z_]", "_", ims_key)
    if not name or name[0].isdigit():
        name = "ims_" + name
    return name


# ---------------------------------------------------------------- loading


def load(data: bytes, filename: str = "", ims_path_depth: int = 4, ims_key: str | None = None) -> Package:
    """Load a HAMF export (JSON or zip bytes) as a single-IMS package."""
    if len(data) > MAX_EXPORT_BYTES:
        raise HamfError("export is %d bytes; the limit is %d" % (len(data), MAX_EXPORT_BYTES))
    documents = list(_documents(data, filename))
    if not documents:
        raise HamfError("no HAMF export JSON found in %s" % (filename or "upload"))

    merged: list[dict[str, Any]] = []
    devices: list[dict[str, Any]] = []
    generated_at = None
    names = []
    for name, doc in documents:
        provider, envelope = _provider_of(doc, name)
        _merge_into(merged, provider.get("tags") or [], (), name)
        if envelope:
            devices.extend(envelope.get("devices") or [])
            generated_at = generated_at or envelope.get("generatedAt")
        names.append(name)

    leaves = list(_walk_leaves(merged, ()))
    if not leaves:
        raise HamfError("export contains no AtomicTags")
    _check_unique(leaves)

    ims_folder = _ims_folder(leaves, ims_path_depth)
    ims_node = _find_folder(merged, ims_folder)
    key = ims_key or ims_folder[-1]
    return Package(
        ims_key=key,
        ims_folder=ims_folder,
        ims_node=ims_node,
        leaves=leaves,
        devices=_dedupe_devices(devices),
        generated_at=generated_at,
        source_names=names,
    )


def _documents(data: bytes, filename: str) -> Iterable[tuple[str, Any]]:
    if data[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise HamfError("%s is not a valid zip: %s" % (filename or "upload", exc)) from exc
        with zf:
            members = zf.infolist()
            if len(members) > MAX_ZIP_MEMBERS:
                raise HamfError("zip has %d members; the limit is %d" % (len(members), MAX_ZIP_MEMBERS))
            budget = MAX_UNCOMPRESSED_BYTES
            for info in sorted(members, key=lambda i: i.filename):
                base = info.filename.rsplit("/", 1)[-1]
                if info.is_dir() or not base.lower().endswith(".json") or base.startswith("."):
                    continue
                raw = _read_member(zf, info, budget)
                budget -= len(raw)
                yield info.filename, _parse_json(raw, info.filename)
        return
    yield filename or "upload.json", _parse_json(data, filename)


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, budget: int) -> bytes:
    """Read one member, stopping as soon as the remaining uncompressed budget is exceeded."""
    try:
        with zf.open(info) as fh:
            raw = fh.read(budget + 1)
    except (RuntimeError, zipfile.BadZipFile, NotImplementedError, OSError) as exc:
        # RuntimeError: encrypted member; NotImplementedError: unsupported compression
        raise HamfError("cannot read %s from the zip: %s" % (info.filename, exc)) from exc
    if len(raw) > budget:
        raise HamfError("zip expands past %d bytes; refusing it" % MAX_UNCOMPRESSED_BYTES)
    return raw


def _parse_json(raw: bytes, name: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HamfError("%s is not valid JSON: %s" % (name, exc)) from exc


def _provider_of(doc: Any, name: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if isinstance(doc, dict) and isinstance(doc.get("config"), dict):
        provider = doc["config"]
        envelope = doc
    else:
        provider, envelope = doc, None
    if not isinstance(provider, dict) or provider.get("tagType") != PROVIDER:
        raise HamfError("%s is not a HAMF Ignition export (no Provider root)" % name)
    return provider, envelope


# ---------------------------------------------------------------- tree handling


def _merge_into(target: list[dict[str, Any]], nodes: list[dict[str, Any]], at: tuple[str, ...], src: str) -> None:
    """Merge Folder trees by name; identical duplicate tags are fine, conflicting ones are not."""
    index = {n["name"]: n for n in target}
    for node in nodes:
        kind = node.get("tagType")
        name = node.get("name")
        if not isinstance(name, str) or not name:
            raise HamfError("%s: unnamed node under /%s" % (src, "/".join(at)))
        _check_name(name, at, src)
        path = at + (name,)
        existing = index.get(name)
        if kind == FOLDER:
            if existing is None:
                existing = {"name": name, "tagType": FOLDER, "tags": []}
                target.append(existing)
                index[name] = existing
            elif existing.get("tagType") != FOLDER:
                raise HamfError("%s: /%s is both a tag and a folder" % (src, "/".join(path)))
            _merge_into(existing["tags"], node.get("tags") or [], path, src)
        elif kind == ATOMIC:
            if existing is None:
                leaf = copy.deepcopy(node)
                target.append(leaf)
                index[name] = leaf
            elif existing != node:
                raise HamfError("%s: conflicting definitions for tag /%s" % (src, "/".join(path)))
        else:
            raise HamfError("%s: unsupported tagType %r at /%s" % (src, kind, "/".join(path)))


def _check_name(name: str, at: tuple[str, ...], src: str) -> None:
    where = "/".join(at + (name,))
    if len(name) > MAX_NAME_LENGTH:
        raise HamfError("%s: name longer than %d characters under /%s" % (src, MAX_NAME_LENGTH, "/".join(at)))
    if _UNSAFE_NAME.search(name) or name in (".", "..") or name != name.strip():
        raise HamfError("%s: unsafe tag or folder name %r at /%s" % (src, name, where))


def _walk_leaves(nodes: list[dict[str, Any]], at: tuple[str, ...]) -> Iterable[Leaf]:
    for node in nodes:
        path = at + (node["name"],)
        if node["tagType"] == FOLDER:
            yield from _walk_leaves(node.get("tags") or [], path)
            continue
        missing = [f for f in IDENTITY_FIELDS if node.get(f) in (None, "")]
        if missing:
            raise HamfError("tag /%s is missing identity field(s): %s" % ("/".join(path), ", ".join(missing)))
        if not node.get("opcItemPath"):
            raise HamfError("tag /%s has no opcItemPath" % "/".join(path))
        try:
            tag_id = int(node["tag_id"])
            asset_id = int(node["asset_id"])
        except (TypeError, ValueError) as exc:
            raise HamfError("tag /%s has a non-integer tag_id/asset_id" % "/".join(path)) from exc
        producer = node.get("producer_id")
        yield Leaf(
            path=path,
            tag_id=tag_id,
            asset_id=asset_id,
            asset_path=str(node["asset_path"]),
            signal_path=str(node["signal_path"]),
            data_type=str(node.get("dataType") or "String"),
            producer_id=int(producer) if producer not in (None, "") else None,
        )


def _check_unique(leaves: list[Leaf]) -> None:
    seen: dict[int, tuple[str, ...]] = {}
    for leaf in leaves:
        other = seen.get(leaf.tag_id)
        if other is not None and other != leaf.path:
            raise HamfError(
                "tag_id %d is used by both /%s and /%s" % (leaf.tag_id, "/".join(other), "/".join(leaf.path))
            )
        seen[leaf.tag_id] = leaf.path


def _ims_folder(leaves: list[Leaf], depth: int) -> tuple[str, ...]:
    folders = set()
    for leaf in leaves:
        if len(leaf.path) <= depth:
            raise HamfError(
                "tag /%s sits above IMS depth %d; check IMS_PATH_DEPTH" % ("/".join(leaf.path), depth)
            )
        folders.add(leaf.path[:depth])
    if len(folders) != 1:
        listed = ", ".join("/".join(f) for f in sorted(folders))
        raise HamfError("export spans %d IMS folders (%s); import one IMS at a time" % (len(folders), listed))
    return folders.pop()


def _find_folder(nodes: list[dict[str, Any]], path: tuple[str, ...]) -> dict[str, Any]:
    node: dict[str, Any] = {"tags": nodes}
    for name in path:
        node = next(n for n in node["tags"] if n["name"] == name)
    return node


def _dedupe_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for d in devices:
        name = d.get("name")
        if name and (name not in out or (d.get("ipAddress") and not out[name].get("ipAddress"))):
            out[name] = {"name": name, "ipAddress": d.get("ipAddress")}
    return [out[k] for k in sorted(out)]
