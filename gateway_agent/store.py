"""SQLite-backed state: installed IMS packages and the telemetry store-and-forward buffer."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    ims          TEXT PRIMARY KEY,
    version      TEXT NOT NULL,
    status       TEXT NOT NULL,            -- installed | failed | removed
    source       TEXT NOT NULL,            -- cli | http | inbox | shadow
    manifest     TEXT NOT NULL,            -- generator manifest (JSON)
    files        TEXT NOT NULL,            -- project-relative files owned by this IMS (JSON list)
    error        TEXT,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS package_blobs (
    ims          TEXT PRIMARY KEY,
    version      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    data         BLOB NOT NULL             -- the original HAMF export, kept for re-render and audit
);
CREATE TABLE IF NOT EXISTS outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ims          TEXT NOT NULL,
    rows         INTEGER NOT NULL,
    payload      BLOB NOT NULL,            -- one ready-to-publish MQTT message (JSON)
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_ims ON outbox (ims, id);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------ packages

    def record_package(
        self,
        ims: str,
        version: str,
        status: str,
        source: str,
        manifest: dict[str, Any],
        files: list[str],
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO packages (ims, version, status, source, manifest, files, error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ims) DO UPDATE SET version=excluded.version, status=excluded.status, "
                "source=excluded.source, manifest=excluded.manifest, files=excluded.files, "
                "error=excluded.error, updated_at=excluded.updated_at",
                (ims, version, status, source, json.dumps(manifest), json.dumps(files), error, time.time()),
            )

    def save_blob(self, ims: str, version: str, filename: str, data: bytes) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO package_blobs (ims, version, filename, data) VALUES (?, ?, ?, ?)",
                (ims, version, filename, data),
            )

    def blob(self, ims: str) -> tuple[str, str, bytes] | None:
        row = self._db.execute("SELECT version, filename, data FROM package_blobs WHERE ims=?", (ims,)).fetchone()
        return (row[0], row[1], bytes(row[2])) if row else None

    def package(self, ims: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT ims, version, status, source, manifest, files, error, updated_at FROM packages WHERE ims=?",
            (ims,),
        ).fetchone()
        return _package_row(row) if row else None

    def packages(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT ims, version, status, source, manifest, files, error, updated_at FROM packages ORDER BY ims"
        ).fetchall()
        return [_package_row(r) for r in rows]

    # ------------------------------------------------------------ outbox

    def enqueue(self, ims: str, rows: int, payload: bytes, max_rows: int) -> int:
        """Append one message; trims oldest messages if the buffer exceeds max_rows. Returns rows dropped."""
        with self._lock:
            self._db.execute(
                "INSERT INTO outbox (ims, rows, payload, created_at) VALUES (?, ?, ?, ?)",
                (ims, rows, payload, time.time()),
            )
            total = self._db.execute("SELECT COALESCE(SUM(rows), 0) FROM outbox").fetchone()[0]
            dropped = 0
            while total > max_rows:
                oldest = self._db.execute("SELECT id, rows FROM outbox ORDER BY id LIMIT 1").fetchone()
                if oldest is None:
                    break
                self._db.execute("DELETE FROM outbox WHERE id=?", (oldest[0],))
                total -= oldest[1]
                dropped += oldest[1]
            if dropped:
                self._bump("outbox_dropped_rows", dropped)
            return dropped

    def peek(self, limit: int) -> list[tuple[int, str, bytes]]:
        rows = self._db.execute("SELECT id, ims, payload FROM outbox ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [(r[0], r[1], bytes(r[2])) for r in rows]

    def ack(self, message_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM outbox WHERE id=?", (message_id,))

    def outbox_stats(self) -> dict[str, Any]:
        msgs, rows, oldest = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(rows), 0), MIN(created_at) FROM outbox"
        ).fetchone()
        return {
            "messages": msgs,
            "rows": rows,
            "oldest_age_s": round(time.time() - oldest, 1) if oldest else 0,
            "dropped_rows_total": int(self.get("outbox_dropped_rows", "0")),
        }

    # ------------------------------------------------------------ kv

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (key, value))

    def _bump(self, key: str, by: int) -> None:
        current = int(self.get(key, "0") or 0)
        self._db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (key, str(current + by)))


def _package_row(row: tuple) -> dict[str, Any]:
    return {
        "ims": row[0],
        "version": row[1],
        "status": row[2],
        "source": row[3],
        "manifest": json.loads(row[4]),
        "files": json.loads(row[5]),
        "error": row[6],
        "updated_at": row[7],
    }
