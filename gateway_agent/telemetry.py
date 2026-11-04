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
import threading
import time
from typing import Any

from .config import Settings
from .store import Store

SCHEMA_VERSION = 2
_SEP = (",", ":")


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
        ims = body.get("ims")
        ticks = body.get("ticks")
        if not isinstance(ims, str) or not isinstance(ticks, list):
            raise TelemetryError("expected {ims, version, ticks: [...]}")

        clean = [_tick(t) for t in ticks]
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
        payload = json.dumps({**envelope, "ticks": ticks}, separators=_SEP).encode("utf-8")
        readings = sum(len(t["r"]) for t in ticks)
        self.store.enqueue(ims, readings, payload, self.settings.buffer_max_rows)


def _tick(tick: Any) -> dict[str, Any]:
    if not isinstance(tick, dict):
        raise TelemetryError("malformed tick %r" % (tick,))
    ts = tick.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0:
        raise TelemetryError("bad tick timestamp %r" % (ts,))
    readings = []
    for r in tick.get("rows") or []:
        # [tag_id, value] from hyper.publisher
        if (
            not isinstance(r, list)
            or len(r) != 2
            or isinstance(r[0], bool)
            or not isinstance(r[0], int)
            or not isinstance(r[1], (bool, int, float, str))
        ):
            raise TelemetryError("malformed reading %r" % (r,))
        readings.append(r)
    return {"ts": int(ts), "bad": int(tick.get("bad") or 0), "r": readings}
