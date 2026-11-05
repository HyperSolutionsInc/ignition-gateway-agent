"""Package server: answers a gateway's package request with the package, in MQTT chunks.

The console sets an IMS shadow's source to {"type": "mqtt"}. The gateway then publishes
`{"ims", "request_id", "sha256"}` to `hyper/<thing>/package/request`, and an IoT rule hands it
here with the thing name from the topic. The package is read from
`packages/<thing>/ims-<key>/<sha256>` in S3, so a gateway can only ever get its own packages,
checked against the hash, and sent to `hyper/<thing>/package/chunk` as
`{"request_id", "seq", "total", "data"}` (base64), which the agent reassembles and checks again.

Unlike a presigned URL, this never expires: a gateway that was offline for a week still gets
its package when it comes back.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import time
from typing import Any

log = logging.getLogger()
log.setLevel(logging.INFO)

CHUNK_BYTES = 90_000  # base64 + JSON stays under AWS IoT's 128 KB message limit
CHUNK_PAUSE_S = 0.25  # ~360 KB/s, under the 512 KB/s a device connection can receive
MAX_PACKAGE_BYTES = 16 * 1024 * 1024
THING_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def ims_shadow_name(ims: str) -> str:
    return "ims-" + re.sub(r"[^A-Za-z0-9:_-]", "_", ims)[:60]


class Clients:
    def __init__(self, s3=None, iot_data=None, env: dict[str, str] | None = None, sleep=time.sleep):
        env = dict(os.environ if env is None else env)
        self.bucket = env.get("BUCKET", "")
        self.prefix = env.get("THING_PREFIX", "hyper-gw-")
        self.sleep = sleep
        if s3 is None:
            import boto3

            region = env.get("AWS_REGION", "us-east-2")
            s3 = boto3.client("s3", region_name=region)
            endpoint = env.get("IOT_DATA_ENDPOINT") or boto3.client("iot", region_name=region).describe_endpoint(
                endpointType="iot:Data-ATS")["endpointAddress"]
            iot_data = boto3.client("iot-data", region_name=region, endpoint_url="https://" + endpoint)
        self.s3, self.iot_data = s3, iot_data


def serve(event: dict[str, Any], c: Clients) -> dict[str, Any]:
    thing = str(event.get("thing") or "")
    request_id = str(event.get("request_id") or "")
    sha = str(event.get("sha256") or "")
    ims = str(event.get("ims") or "")
    if not (THING_RE.fullmatch(thing) and thing.startswith(c.prefix) and REQUEST_ID_RE.fullmatch(request_id)
            and SHA_RE.fullmatch(sha) and 0 < len(ims) <= 64):
        log.warning("ignored malformed package request: %s", json.dumps(event)[:300])
        return {"ok": False, "reason": "malformed"}
    key = "packages/%s/%s/%s" % (thing, ims_shadow_name(ims), sha)
    try:
        body = c.s3.get_object(Bucket=c.bucket, Key=key)["Body"]
    except c.s3.exceptions.NoSuchKey:
        log.warning("%s asked for %s %s, which isn't stored", thing, ims, sha[:12])
        return {"ok": False, "reason": "not found"}
    data = body.read(MAX_PACKAGE_BYTES + 1)
    if len(data) > MAX_PACKAGE_BYTES or hashlib.sha256(data).hexdigest() != sha:
        log.error("stored package %s is oversize or doesn't match its hash", key)
        return {"ok": False, "reason": "bad object"}
    total = max(1, math.ceil(len(data) / CHUNK_BYTES))
    topic = "hyper/%s/package/chunk" % thing
    for seq in range(total):
        part = data[seq * CHUNK_BYTES:(seq + 1) * CHUNK_BYTES]
        msg = {"request_id": request_id, "seq": seq, "total": total, "data": base64.b64encode(part).decode()}
        c.iot_data.publish(topic=topic, qos=1, payload=json.dumps(msg).encode())
        if seq + 1 < total:
            c.sleep(CHUNK_PAUSE_S)
    log.info("sent %s %s to %s in %d chunks (%d bytes)", ims, sha[:12], thing, total, len(data))
    return {"ok": True, "chunks": total}


_clients: Clients | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _clients
    if _clients is None:
        _clients = Clients()
    return serve(event, _clients)
