"""Turn timer-script posts into AWS IoT messages and queue them in the outbox.

Each published message carries rows in the ts.telemetry shape the cloud already ingests:
ts, tag_id, asset_id, producer_id, ims_id, value_double, value_int, value_bool, value_string.
Messages are packed up to IOT_MAX_MESSAGE_BYTES (AWS IoT caps a message at 128 KB).
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .store import Store

_VALUE_COLUMN = {"d": "value_double", "i": "value_int", "b": "value_bool", "s": "value_string"}


class TelemetryError(ValueError):
    pass


class Telemetry:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self._lock = threading.Lock()
        self.last_tick: dict[str, dict[str, Any]] = {}

    def accept(self, body: dict[str, Any]) -> int:
        """Accept one POST from hyper.publisher. Returns the number of rows queued."""
        ims = body.get("ims")
        ticks = body.get("ticks")
        if not isinstance(ims, str) or not isinstance(ticks, list):
            raise TelemetryError("expected {ims, version, ticks: [...]}")

        rows: list[dict[str, Any]] = []
        for tick in ticks:
            ts = _iso(tick.get("ts"))
            for r in tick.get("rows") or []:
                rows.append(_row(ts, r))
        if ticks:
            last = ticks[-1]
            self.last_tick[ims] = {
                "at": time.time(),
                "ts": _iso(last.get("ts")),
                "good": len(last.get("rows") or []),
                "bad": last.get("bad", 0),
                "total": last.get("total", 0),
                "version": body.get("version"),
                "dropped_in_gateway": body.get("dropped", 0),
            }
        if rows:
            self._enqueue(ims, body.get("version"), rows)
        return len(rows)

    def _enqueue(self, ims: str, version: Any, rows: list[dict[str, Any]]) -> None:
        envelope = {"gateway": self.settings.thing_name, "ims": ims, "version": version}
        overhead = len(json.dumps({**envelope, "rows": []}))
        limit = self.settings.max_message_bytes
        batch: list[dict[str, Any]] = []
        size = overhead
        with self._lock:
            for row in rows:
                encoded = len(json.dumps(row, separators=(",", ":"))) + 1
                if batch and size + encoded > limit:
                    self._put(ims, envelope, batch)
                    batch, size = [], overhead
                batch.append(row)
                size += encoded
            if batch:
                self._put(ims, envelope, batch)

    def _put(self, ims: str, envelope: dict[str, Any], batch: list[dict[str, Any]]) -> None:
        payload = json.dumps({**envelope, "rows": batch}, separators=(",", ":")).encode("utf-8")
        self.store.enqueue(ims, len(batch), payload, self.settings.buffer_max_rows)


def _row(ts: str, r: list[Any]) -> dict[str, Any]:
    # [tag_id, asset_id, producer_id, device, kind, value] from hyper.publisher
    if not isinstance(r, list) or len(r) != 6 or r[4] not in _VALUE_COLUMN:
        raise TelemetryError("malformed row %r" % (r,))
    row = {
        "ts": ts,
        "tag_id": r[0],
        "asset_id": r[1],
        "producer_id": r[2],
        "ims_id": r[3],
        "value_double": None,
        "value_int": None,
        "value_bool": None,
        "value_string": None,
    }
    row[_VALUE_COLUMN[r[4]]] = r[5]
    return row


def _iso(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).isoformat(timespec="milliseconds")
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise TelemetryError("bad tick timestamp %r" % (ms,)) from exc
