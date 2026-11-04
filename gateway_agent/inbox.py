"""Drop-folder import: copy a HAMF export into <state>/inbox and it is installed.

Processed files move to inbox/done, rejected ones to inbox/failed with a .error note beside
them. A file is only picked up once its size has stopped changing, so a slow copy is safe.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .installer import Installer

log = logging.getLogger(__name__)


class Inbox(threading.Thread):
    def __init__(self, directory: Path, installer: Installer, poll_s: float = 5.0):
        super().__init__(name="inbox", daemon=True)
        self.dir = directory
        self.installer = installer
        self.poll_s = poll_s
        self._sizes: dict[Path, int] = {}
        for sub in ("", "done", "failed"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)

    def run(self) -> None:
        while True:
            try:
                self.scan_once()
            except Exception:
                log.exception("inbox scan failed")
            time.sleep(self.poll_s)

    def scan_once(self) -> None:
        for path in sorted(self.dir.iterdir()):
            if not path.is_file() or path.name.startswith(".") or path.suffix.lower() not in (".json", ".zip"):
                continue
            size = path.stat().st_size
            if self._sizes.get(path) != size:
                self._sizes[path] = size  # wait one more poll for the copy to settle
                continue
            self._sizes.pop(path, None)
            self._process(path)

    def _process(self, path: Path) -> None:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        try:
            result = self.installer.install(path.read_bytes(), path.name, "inbox")
        except Exception as exc:
            log.warning("inbox: %s rejected: %s", path.name, exc)
            target = self.dir / "failed" / ("%s-%s" % (stamp, path.name))
            path.replace(target)
            target.with_name(target.name + ".error").write_text(str(exc) + "\n")
            return
        log.info("inbox: %s -> IMS %s %s", path.name, result.ims, "installed" if result.changed else "unchanged")
        path.replace(self.dir / "done" / ("%s-%s" % (stamp, path.name)))
