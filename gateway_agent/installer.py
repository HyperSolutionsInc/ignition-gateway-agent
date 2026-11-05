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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import generator, hamf
from .config import Settings
from .ignition import IgnitionClient, IgnitionError
from .store import Store

log = logging.getLogger(__name__)

# Not a credential: a marker written into hand-import renders in place of the real token.
RENDER_PLACEHOLDER_TOKEN = "REPLACE_WITH_AGENT_INGEST_TOKEN"  # nosec B105


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
            if self._delete_tag_definitions([_strip_provider(folder)]):
                self.ignition.scan_config()
            self.ignition.scan_projects()
            self.store.record_package(ims, previous["version"], "removed", source, previous["manifest"], [])
            log.info("removed IMS %s (%s)", ims, source)
            return True

    def render_to(self, data: bytes, filename: str, out_dir: Path, ims_key: str | None = None) -> generator.Artifacts:
        """Write the artifacts for an export to a folder, without touching the gateway.

        Unless AGENT_INGEST_TOKEN is set, the publisher gets a placeholder token to replace
        before a hand import, since render must not create or reveal the live one.
        """
        pkg = hamf.load(data, filename, self.settings.ims_path_depth, ims_key)
        art = self._render(pkg, True, self.settings.timer_interval_ms,
                           ingest_token=self.settings.ingest_token or RENDER_PLACEHOLDER_TOKEN)
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

    def refresh_shared(self) -> bool:
        """Rewrite the shared project files if they are stale (e.g. a rotated ingest token).

        Returns True when files changed and the gateway was asked to rescan.
        """
        if not any(p["status"] == "installed" for p in self.store.packages()):
            return False
        with self._lock:
            changed = False
            for rel, content in generator.shared_project_files(
                self.settings.agent_url_for_ignition, self.settings.ingest_token
            ).items():
                path = self.settings.project_dir / rel
                if not path.exists() or path.read_bytes() != content:
                    _write_atomic(path, content)
                    changed = True
            if changed:
                self.ignition.scan_projects()
                log.info("shared project files refreshed")
            return changed

    def _render(self, pkg: hamf.Package, enabled: bool, interval_ms: int, ingest_token: str | None = None) -> generator.Artifacts:
        return generator.render(
            pkg,
            provider=self.settings.tag_provider,
            tag_root=self.settings.tag_root,
            agent_url=self.settings.agent_url_for_ignition,
            ingest_token=ingest_token if ingest_token is not None else self.settings.ingest_token,
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

        # Import with Overwrite updates tags in place but keeps tags the new version dropped,
        # and 8.3 has no REST delete, so those are removed from the tag-definition files first.
        if removed and self._delete_tag_definitions(removed):
            self.ignition.scan_config()
        self.ignition.import_tags(self.settings.tag_provider, _strip_provider(art.tag_base_path), art.tags[0])
        self.ignition.scan_projects()
        self._confirm_project_loaded()

    def _confirm_project_loaded(self, timeout_s: float = 20.0) -> None:
        """A rescan returns 200 even when Ignition rejects the project files, so check it loaded."""
        deadline = time.monotonic() + timeout_s
        while True:
            if self.settings.ignition_project in self.ignition.project_names():
                return
            if time.monotonic() > deadline:
                raise IgnitionError("project %s did not load after the rescan; see the gateway log"
                                    % self.settings.ignition_project)
            time.sleep(1)

    def _tag_definition_root(self) -> Path:
        return (self.settings.ignition_data_dir / "config" / "resources" / "core" / "ignition" / "tag-definition"
                / self.settings.tag_provider)

    def _delete_tag_definitions(self, paths: list[str]) -> bool:
        """Delete tags or whole folders (provider-relative paths) from the stored tag definitions.

        Each folder is a directory holding `tags.json` for the tags directly inside it. A path
        naming a folder removes the directory; a path naming a tag removes its entry from its
        folder's `tags.json`. Returns True if anything changed (the caller then scans config).
        """
        root = self._tag_definition_root().resolve()
        changed = False
        by_folder: dict[Path, set[str]] = {}
        for rel in paths:
            parts = [p for p in rel.split("/") if p]
            if not parts:
                continue
            target = root.joinpath(*parts).resolve()
            if root not in target.parents:
                raise ValueError("tag path %r escapes the tag definitions" % rel)
            if target.is_dir():
                shutil.rmtree(target)
                changed = True
            else:
                by_folder.setdefault(target.parent, set()).add(parts[-1])
        for folder, names in by_folder.items():
            tags_file = folder / "tags.json"
            if not tags_file.exists():
                continue
            entries = json.loads(tags_file.read_text())
            kept = [e for e in entries if e.get("name") not in names]
            if len(kept) != len(entries):
                _write_atomic(tags_file, (json.dumps(kept, indent=2) + "\n").encode("utf-8"))
                changed = True
        return changed

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
