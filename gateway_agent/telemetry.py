"""Turn timer-script posts into AWS IoT messages and queue them in the outbox.

Every message goes to the same topic; what it is about travels in the payload:

  {"v": 2, "gw": <thing>, "ims": <IMS key>, "pkg": <package version>,
   "ticks": [{"ts": <epoch ms>, "bad": <n>, "r": [[tag_id, value], ...]}, ...]}

A reading is just [tag_id, value], with one timestamp per tick. Everything else about a tag
(asset_id, producer_id, device, and which value column its data type goes in) is fixed in
HAMF and resolved from tag_id in the cloud. `bad` counts tags read with bad quality on that
tick (they are not sent).

Messages are packed up to IOT_MAX_MESSAGE_BYTES (AWS IoT caps a message at 128 KB); a tick
too large for one message is split across several, each carrying the same ts. The cloud rule
should take the gateway from the topic, which the IoT policy pins to the certificate, not
from `gw`.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from typing import Any

from .config import Settings
from .store import Store

SCHEMA_VERSION = 2
_SEP = (",", ":")

# Limits on what one timer-script post may contain. The FWT-02 IMS sends 3,901 readings per tick
# and the publisher batches at most 30 ticks per post, so these leave wide headroom.
MAX_TICKS_PER_POST = 1_000
MAX_READINGS_PER_TICK = 100_000
MAX_STRING_VALUE = 1_024
_IMS_RE = re.compile(r"[A-Za-z0-9:_.\-]{1,64}")
_VERSION_RE = re.compile(r"[A-Za-z0-9_\-]{1,64}")
_MIN_TS_MS = 946_684_800_000  # 2000-01-01T00:00:00Z
_MAX_FUTURE_MS = 24 * 3600 * 1000
_MAX_TAG_ID = 2**63 - 1


class TelemetryError(ValueError):
    pass


class Telemetry:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self._lock = threading.Lock()
        self.last_tick: dict[str, dict[str, Any]] = {}

    def accept(self, body: dict[str, Any]) -> int:
        """Accept one POST from hyper.publisher. Returns the number of readings queued."""
        if not isinstance(body, dict):
            raise TelemetryError("expected a JSON object")
        ims = body.get("ims")
        ticks = body.get("ticks")
        version = body.get("version")
        if not isinstance(ims, str) or not isinstance(ticks, list):
            raise TelemetryError("expected {ims, version, ticks: [...]}")
        if not _IMS_RE.fullmatch(ims):
            raise TelemetryError("ims must be 1-64 characters of A-Z a-z 0-9 : _ . -")
        if version is not None and not (isinstance(version, str) and _VERSION_RE.fullmatch(version)):
            raise TelemetryError("version must be 1-64 characters of A-Z a-z 0-9 _ -")
        if len(ticks) > MAX_TICKS_PER_POST:
            raise TelemetryError("at most %d ticks per post" % MAX_TICKS_PER_POST)

        now_ms = time.time() * 1000
        clean = [_tick(t, now_ms) for t in ticks]
        if clean:
            last = clean[-1]
            self.last_tick[ims] = {
                "at": time.time(),
                "ts": last["ts"],
                "good": len(last["r"]),
                "bad": last["bad"],
                "total": ticks[-1].get("total", 0),
                "version": body.get("version"),
                "dropped_in_gateway": body.get("dropped", 0),
            }
        readings = sum(len(t["r"]) for t in clean)
        if readings:
            self._enqueue(ims, body.get("version"), clean)
        return readings

    def _enqueue(self, ims: str, version: Any, ticks: list[dict[str, Any]]) -> None:
        envelope = {"v": SCHEMA_VERSION, "gw": self.settings.thing_name, "ims": ims, "pkg": version}
        limit = self.settings.max_message_bytes
        base = len(json.dumps({**envelope, "ticks": []}, separators=_SEP))
        with self._lock:
            out: list[dict[str, Any]] = []
            size = base
            for tick in ticks:
                if not tick["r"]:
                    continue
                head = {"ts": tick["ts"], "bad": tick["bad"], "r": []}
                head_size = len(json.dumps(head, separators=_SEP)) + 1
                if out and size + head_size > limit:
                    self._put(ims, envelope, out)
                    out, size = [], base
                current = dict(head, r=[])
                out.append(current)
                size += head_size
                for reading in tick["r"]:
                    n = len(json.dumps(reading, separators=_SEP)) + 1
                    if current["r"] and size + n > limit:
                        # Split this tick: close the message, continue the tick in a new one.
                        self._put(ims, envelope, out)
                        current = dict(head, r=[], bad=0)
                        out, size = [current], base + head_size
                    current["r"].append(reading)
                    size += n
            if out:
                self._put(ims, envelope, out)

    def _put(self, ims: str, envelope: dict[str, Any], ticks: list[dict[str, Any]]) -> None:
        payload = json.dumps({**envelope, "ticks": ticks}, separators=_SEP, allow_nan=False).encode("utf-8")
        readings = sum(len(t["r"]) for t in ticks)
        self.store.enqueue(ims, readings, payload, self.settings.buffer_max_rows)


def _tick(tick: Any, now_ms: float) -> dict[str, Any]:
    if not isinstance(tick, dict):
        raise TelemetryError("malformed tick %r" % (tick,))
    ts = tick.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
        raise TelemetryError("bad tick timestamp %r" % (ts,))
    if not _MIN_TS_MS <= ts <= now_ms + _MAX_FUTURE_MS:
        raise TelemetryError("tick timestamp %r is before 2000 or more than a day ahead" % (ts,))
    bad = tick.get("bad") or 0
    if isinstance(bad, bool) or not isinstance(bad, int) or bad < 0:
        raise TelemetryError("bad count must be a non-negative integer")
    rows = tick.get("rows") or []
    if not isinstance(rows, list) or len(rows) > MAX_READINGS_PER_TICK:
        raise TelemetryError("rows must be a list of at most %d readings" % MAX_READINGS_PER_TICK)
    readings = []
    for r in rows:
        # [tag_id, value] from hyper.publisher
        if not isinstance(r, list) or len(r) != 2:
            raise TelemetryError("malformed reading %r" % (r,))
        tag_id, value = r
        if isinstance(tag_id, bool) or not isinstance(tag_id, int) or not 0 < tag_id <= _MAX_TAG_ID:
            raise TelemetryError("tag_id must be a positive integer, got %r" % (tag_id,))
        if isinstance(value, float) and not math.isfinite(value):
            # NaN/Infinity are not valid JSON; a sensor reading like this is dropped, counted as bad.
            bad += 1
            continue
        if isinstance(value, str):
            if len(value) > MAX_STRING_VALUE:
                raise TelemetryError("string value for tag %d longer than %d characters" % (tag_id, MAX_STRING_VALUE))
        elif not isinstance(value, (bool, int, float)):
            raise TelemetryError("value for tag %d must be a number, boolean or string" % tag_id)
        readings.append([tag_id, value])
    return {"ts": int(ts), "bad": bad, "r": readings}
