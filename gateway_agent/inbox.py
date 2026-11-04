"""Drop-folder import: copy a HAMF export into <state>/inbox and it is installed.

Processed files move to inbox/done, rejected ones to inbox/failed with a .error note beside
them. A file is only picked up once its size has stopped changing, so a slow copy is safe.

Files here are only ever parsed as HAMF JSON/zip (never executed), and with the same limits as
any other import (hamf.MAX_EXPORT_BYTES, zip-bomb checks). Symlinks and anything that isn't a
regular file are rejected without being read, so the inbox can't be used to make the agent
read files elsewhere on disk.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
import time
from pathlib import Path

from .hamf import MAX_EXPORT_BYTES
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
            (self.dir / sub).mkdir(mode=0o700, parents=True, exist_ok=True)

    def run(self) -> None:
        while True:
            try:
                self.scan_once()
            except Exception:
                log.exception("inbox scan failed")
            time.sleep(self.poll_s)

    def scan_once(self) -> None:
        for path in sorted(self.dir.iterdir()):
            if path.name.startswith(".") or path.suffix.lower() not in (".json", ".zip"):
                continue
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode):
                if not stat.S_ISDIR(st.st_mode):
                    self._reject(path, "not a regular file (symlinks and devices are refused)")
                continue
            if self._sizes.get(path) != st.st_size:
                self._sizes[path] = st.st_size  # wait one more poll for the copy to settle
                continue
            self._sizes.pop(path, None)
            if st.st_size > MAX_EXPORT_BYTES:
                self._reject(path, "file is %d bytes; the limit is %d" % (st.st_size, MAX_EXPORT_BYTES))
                continue
            self._process(path)

    def _process(self, path: Path) -> None:
        try:
            data = _read_regular_file(path, MAX_EXPORT_BYTES)
            result = self.installer.install(data, path.name, "inbox")
        except Exception as exc:
            self._reject(path, str(exc))
            return
        log.info("inbox: %s -> IMS %s %s", path.name, result.ims, "installed" if result.changed else "unchanged")
        path.replace(self.dir / "done" / ("%s-%s" % (_stamp(), path.name)))

    def _reject(self, path: Path, reason: str) -> None:
        log.warning("inbox: %s rejected: %s", path.name, reason)
        target = self.dir / "failed" / ("%s-%s" % (_stamp(), path.name))
        path.replace(target)  # moves a symlink itself, never its target
        target.with_name(target.name + ".error").write_text(reason + "\n")


def _read_regular_file(path: Path, limit: int) -> bytes:
    """Read a file without following a symlink swapped in after the scan, and at most limit bytes."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags)
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise ValueError("not a regular file")
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file exceeds %d bytes" % limit)
    return data


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S")
