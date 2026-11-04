"""Install, update and remove IMS packages on the local Ignition gateway.

An install is: parse the HAMF export -> render artifacts -> write project resources into the
managed project -> delete tags the new version dropped -> import the IMS tag folder ->
ask the gateway to rescan projects. Re-importing the same export is a no-op, so the same
package can arrive from the CLI, the inbox folder or the shadow without double work.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import generator, hamf
from .config import Settings
from .ignition import IgnitionClient
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class InstallResult:
    ims: str
    version: str
    changed: bool
    tag_count: int
    tag_folder: str


class Installer:
    def __init__(self, settings: Settings, store: Store, ignition: IgnitionClient):
        self.settings = settings
        self.store = store
        self.ignition = ignition
        self._lock = threading.Lock()

    # ------------------------------------------------------------ public

    def install(
        self,
        data: bytes,
        filename: str,
        source: str,
        *,
        ims_key: str | None = None,
        enabled: bool | None = None,
        interval_ms: int | None = None,
    ) -> InstallResult:
        pkg = hamf.load(data, filename, self.settings.ims_path_depth, ims_key)
        with self._lock:
            previous = self.store.package(pkg.ims_key)
            prev_manifest = previous["manifest"] if previous else {}
            if enabled is None:
                enabled = prev_manifest.get("enabled", True)
            if interval_ms is None:
                interval_ms = prev_manifest.get("interval_ms", self.settings.timer_interval_ms)
            art = self._render(pkg, enabled, interval_ms)

            if (
                previous
                and previous["status"] == "installed"
                and previous["version"] == art.version
                and prev_manifest.get("enabled") == enabled
                and prev_manifest.get("interval_ms") == interval_ms
            ):
                return InstallResult(pkg.ims_key, art.version, False, len(pkg.leaves), art.tag_folder_path)

            self._check_folder_not_owned_elsewhere(pkg.ims_key, art.tag_folder_path)
            removed = self._removed_tag_paths(pkg, previous)
            self.store.save_blob(pkg.ims_key, art.version, filename, data)
            try:
                self._apply(art, previous, removed)
            except Exception as exc:
                self.store.record_package(
                    pkg.ims_key, art.version, "failed", source, art.manifest, sorted(art.ims_files), str(exc)
                )
                raise
            self.store.record_package(pkg.ims_key, art.version, "installed", source, art.manifest, sorted(art.ims_files))
            log.info("installed IMS %s version %s (%d tags) from %s", pkg.ims_key, art.version, len(pkg.leaves), source)
            return InstallResult(pkg.ims_key, art.version, True, len(pkg.leaves), art.tag_folder_path)

    def reconfigure(self, ims: str, *, enabled: bool | None = None, interval_ms: int | None = None) -> InstallResult:
        """Re-render an installed IMS from its stored export with new timer settings."""
        blob = self.store.blob(ims)
        if blob is None:
            raise KeyError("IMS %s is not installed" % ims)
        _, filename, data = blob
        return self.install(data, filename, "reconfigure", ims_key=ims, enabled=enabled, interval_ms=interval_ms)

    def remove(self, ims: str, source: str) -> bool:
        with self._lock:
            previous = self.store.package(ims)
            if previous is None or previous["status"] == "removed":
                return False
            module = previous["manifest"]["module"]
            for rel in generator.ims_resource_dirs(module):
                shutil.rmtree(self.settings.project_dir / rel, ignore_errors=True)
            folder = previous["manifest"]["tag_folder"]
            self.ignition.delete_tags(self.settings.tag_provider, [_strip_provider(folder)])
            self.ignition.scan_projects()
            self.store.record_package(ims, previous["version"], "removed", source, previous["manifest"], [])
            log.info("removed IMS %s (%s)", ims, source)
            return True

    def render_to(self, data: bytes, filename: str, out_dir: Path, ims_key: str | None = None) -> generator.Artifacts:
        """Write the artifacts for an export to a folder, without touching the gateway."""
        pkg = hamf.load(data, filename, self.settings.ims_path_depth, ims_key)
        art = self._render(pkg, True, self.settings.timer_interval_ms)
        project = out_dir / "project"
        for rel, content in {**art.shared_files, **art.ims_files}.items():
            _write_atomic(project / rel, content)
        _write_atomic(
            out_dir / "tags" / ("%s.json" % pkg.module_name),
            json.dumps({"importPath": art.tag_base_path, "tags": art.tags}, indent=2).encode("utf-8"),
        )
        _write_atomic(out_dir / "manifest.json", json.dumps(art.manifest, indent=2).encode("utf-8"))
        return art

    # ------------------------------------------------------------ internals

    def _render(self, pkg: hamf.Package, enabled: bool, interval_ms: int) -> generator.Artifacts:
        return generator.render(
            pkg,
            provider=self.settings.tag_provider,
            tag_root=self.settings.tag_root,
            agent_url=self.settings.agent_url_for_ignition,
            interval_ms=interval_ms,
            enabled=enabled,
        )

    def _apply(self, art: generator.Artifacts, previous: dict[str, Any] | None, removed: list[str]) -> None:
        project = self.settings.project_dir
        for rel, content in art.shared_files.items():
            _write_atomic(project / rel, content)
        if previous:
            for rel in set(previous["files"]) - set(art.ims_files):
                (project / rel).unlink(missing_ok=True)
        for rel, content in art.ims_files.items():
            _write_atomic(project / rel, content)

        if removed:
            self.ignition.delete_tags(self.settings.tag_provider, removed)
        self.ignition.import_tags(self.settings.tag_provider, _strip_provider(art.tag_base_path), art.tags)
        self.ignition.scan_projects()

    def _removed_tag_paths(self, pkg: hamf.Package, previous: dict[str, Any] | None) -> list[str]:
        """Tags present in the installed version but absent from the new one."""
        if not previous or previous["status"] != "installed":
            return []
        blob = self.store.blob(pkg.ims_key)
        if blob is None:
            return []
        try:
            old = hamf.load(blob[2], blob[1], self.settings.ims_path_depth, pkg.ims_key)
        except hamf.HamfError:
            return []
        root = [p for p in self.settings.tag_root.strip("/").split("/") if p]
        new_paths = {leaf.path for leaf in pkg.leaves}
        return sorted("/".join(root + list(leaf.path)) for leaf in old.leaves if leaf.path not in new_paths)

    def _check_folder_not_owned_elsewhere(self, ims: str, folder: str) -> None:
        for other in self.store.packages():
            if other["ims"] != ims and other["status"] == "installed" and other["manifest"].get("tag_folder") == folder:
                raise hamf.HamfError("tag folder %s already belongs to IMS %s" % (folder, other["ims"]))


def _strip_provider(path: str) -> str:
    return path.split("]", 1)[1] if path.startswith("[") else path


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
