"""Fleet console API: one Lambda behind API Gateway that serves the web UI and its JSON API.

Everything the console does is an AWS call on the gateways' cloud side; it never talks to a
gateway directly. A gateway is an AWS IoT thing named `<THING_PREFIX><serial>`, its config is
in named Device Shadows (`gateway` and one `ims-<key>` per IMS), and its packages are stored in
S3 and sent to it over MQTT by the package server (package_server.py) when it asks.

  GET    /api/config                              what this console is wired to
  GET    /api/gateways                            fleet list with status
  GET    /api/gateways/{thing}                    one gateway: shadows, IMSs, certificates
  PATCH  /api/gateways/{thing}                    site / customer labels
  POST   /api/gateways/{thing}/actions            pause, resume, rescan, restart, basic ingest
  DELETE /api/gateways/{thing}                    decommission (revoke certificate, delete thing)
  POST   /api/uploads                             presigned S3 PUT for an export file
  POST   /api/preview                             validate an export, diff it against a gateway
  POST   /api/gateways/{thing}/ims                add or update an IMS (PPM package)
  POST   /api/rollout                             the same package to several gateways
  PATCH  /api/gateways/{thing}/ims/{ims}          timer enabled / interval
  POST   /api/gateways/{thing}/ims/{ims}/retry    ask the gateway to try the install again
  DELETE /api/gateways/{thing}/ims/{ims}          remove an IMS
  GET    /api/hamf/hierarchy                      HAMF's asset tree (when HAMF_API_URL is set)
  GET    /api/audit                               recent console actions

There is no sign-in yet: API Gateway only accepts the allowlisted source IPs. Every request
that changes something must carry `X-Console-Request: 1`, which a cross-site form or image
can't send, so a page on another site can't drive the console from a browser on our network.
"""

from __future__ import annotations

import base64
import calendar
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

try:
    from gateway_agent import hamf
except ImportError:  # pragma: no cover - the Lambda bundle ships it next to this file
    import hamf  # type: ignore

log = logging.getLogger()
log.setLevel(logging.INFO)

THING_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
IMS_RE = re.compile(r"^[A-Za-z0-9 ._:-]{1,64}$")
UPLOAD_KEY_RE = re.compile(r"^uploads/[0-9a-f]{32}/[A-Za-z0-9 ._()-]{1,128}$")
MAX_PACKAGE_BYTES = 16 * 1024 * 1024  # over MQTT at ~360 KB/s this is ~45 s
MAX_BODY_BYTES = 256 * 1024
GATEWAY_SHADOW = "gateway"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def ims_shadow_name(ims: str) -> str:
    """Same mapping as the gateway agent (gateway_agent.shadow.ims_shadow_name)."""
    return "ims-" + re.sub(r"[^A-Za-z0-9:_-]", "_", ims)[:60]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------- AWS access


class Aws:
    """The AWS clients and settings the console uses; tests pass fakes."""

    def __init__(self, s3=None, iot=None, iot_data=None, ddb=None, env: dict[str, str] | None = None):
        env = dict(os.environ if env is None else env)
        self.region = env.get("AWS_REGION", "us-east-2")
        self.bucket = env.get("BUCKET", "")
        self.audit_table = env.get("AUDIT_TABLE", "")
        self.prefix = env.get("THING_PREFIX", "hyper-gw-")
        self.hamf_url = env.get("HAMF_API_URL", "").rstrip("/")
        self.stale_s = int(env.get("HEALTH_STALE_S", "180"))
        if s3 is None:
            import boto3
            from botocore.config import Config

            # Regional endpoint: a presigned URL on the global one breaks on S3's redirect.
            s3 = boto3.client("s3", region_name=self.region, endpoint_url="https://s3.%s.amazonaws.com" % self.region,
                              config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
            iot = boto3.client("iot", region_name=self.region)
            endpoint = env.get("IOT_DATA_ENDPOINT") or iot.describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"]
            iot_data = boto3.client("iot-data", region_name=self.region, endpoint_url="https://" + endpoint)
            ddb = boto3.client("dynamodb", region_name=self.region)
        self.s3, self.iot, self.iot_data, self.ddb = s3, iot, iot_data, ddb

    # shadows

    def shadow(self, thing: str, name: str) -> dict[str, Any]:
        try:
            return json.loads(self.iot_data.get_thing_shadow(thingName=thing, shadowName=name)["payload"].read())
        except self.iot_data.exceptions.ResourceNotFoundException:
            return {}

    def set_desired(self, thing: str, name: str, desired: dict[str, Any]) -> None:
        self.iot_data.update_thing_shadow(thingName=thing, shadowName=name,
                                          payload=json.dumps({"state": {"desired": desired}}).encode())

    def shadow_names(self, thing: str) -> list[str]:
        names, token = [], None
        while True:
            kw = {"thingName": thing, "pageSize": 100}
            if token:
                kw["nextToken"] = token
            page = self.iot_data.list_named_shadows_for_thing(**kw)
            names += page.get("results", [])
            token = page.get("nextToken")
            if not token:
                return names

    # S3

    def get_object(self, key: str, limit: int = MAX_PACKAGE_BYTES) -> bytes:
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"]
        except self.s3.exceptions.NoSuchKey:
            raise ApiError(404, "no such object: %s" % key)
        data = body.read(limit + 1)
        if len(data) > limit:
            raise ApiError(413, "file is over %d MB" % (limit // (1024 * 1024)))
        return data

    def put_object(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
                           ServerSideEncryption="AES256")

    # audit

    def audit(self, ctx: "Request", action: str, thing: str | None = None, **detail: Any) -> None:
        if not self.audit_table:
            return
        item = {"pk": {"S": "audit"}, "sk": {"S": "%s#%s" % (_now(), uuid.uuid4().hex[:8])},
                "action": {"S": action}, "ip": {"S": ctx.source_ip or "-"},
                "detail": {"S": json.dumps(detail, default=str)[:2000]},
                "expires": {"N": str(int(time.time()) + 400 * 86400)}}
        if thing:
            item["thing"] = {"S": thing}
        self.ddb.put_item(TableName=self.audit_table, Item=item)


# ---------------------------------------------------------------------------- requests


class Request:
    def __init__(self, event: dict[str, Any]):
        self.method = event.get("httpMethod", "GET").upper()
        self.path = event.get("path") or "/"
        self.headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        self.query = event.get("queryStringParameters") or {}
        self.source_ip = ((event.get("requestContext") or {}).get("identity") or {}).get("sourceIp")
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        if len(raw) > MAX_BODY_BYTES:
            raise ApiError(413, "request body too large")
        self.raw_body = raw

    def json(self) -> dict[str, Any]:
        if not self.raw_body:
            return {}
        try:
            body = json.loads(self.raw_body)
        except ValueError:
            raise ApiError(400, "body is not JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        return body


def _response(status: int, body: Any, content_type: str = "application/json") -> dict[str, Any]:
    headers = {
        "Content-Type": content_type,
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if content_type.startswith("text/html"):
        headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self' https://*.amazonaws.com; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        headers["X-Frame-Options"] = "DENY"
    text = body if isinstance(body, str) else json.dumps(body, default=str)
    return {"statusCode": status, "headers": headers, "body": text}


# ---------------------------------------------------------------------------- helpers


def _thing(aws: Aws, value: str) -> str:
    if not THING_RE.fullmatch(value or "") or not value.startswith(aws.prefix):
        raise ApiError(400, "not a gateway thing name: %r" % value)
    return value


def _ims(value: str) -> str:
    if not IMS_RE.fullmatch(value or ""):
        raise ApiError(400, "not an IMS key: %r" % value)
    return value


def _age_s(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return max(0.0, time.time() - calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def _summary(aws: Aws, thing: dict[str, Any]) -> dict[str, Any]:
    name = thing["thingName"]
    doc = aws.shadow(name, GATEWAY_SHADOW)
    state = doc.get("state") or {}
    reported = state.get("reported") or {}
    desired = state.get("desired") or {}
    health = reported.get("health") or {}
    age = _age_s(health.get("reported_at"))
    packages = health.get("packages") or {}
    attrs = thing.get("attributes") or {}
    return {
        "thing": name,
        "serial": attrs.get("serial"),
        "site": attrs.get("site"),
        "customer": attrs.get("customer"),
        "online": age is not None and age < aws.stale_s,
        "last_seen": health.get("reported_at"),
        "last_seen_age_s": None if age is None else round(age),
        "agent_version": health.get("agent_version"),
        "ignition": (health.get("ignition") or {}).get("state"),
        "outbox_rows": (health.get("outbox") or {}).get("rows"),
        "last_error": health.get("last_error"),
        "ims": sorted(set(desired.get("ims") or []) | set(packages)),
        "ims_status": {k: v.get("status") for k, v in packages.items()},
        "pending": sorted((state.get("delta") or {}).keys()),
    }


def _things(aws: Aws) -> list[dict[str, Any]]:
    out, token = [], None
    while True:
        kw: dict[str, Any] = {"maxResults": 250}
        if token:
            kw["nextToken"] = token
        page = aws.iot.list_things(**kw)
        out += [t for t in page.get("things", []) if t["thingName"].startswith(aws.prefix)]
        token = page.get("nextToken")
        if not token:
            return out


def _manifest(pkg: Any, data: bytes, filename: str) -> dict[str, Any]:
    return {
        "ims_key": pkg.ims_key,
        "version": pkg.version,
        "sha256": hashlib.sha256(data).hexdigest(),
        "filename": filename,
        "size": len(data),
        "tag_count": len(pkg.leaves),
        "tag_folder": "/".join(pkg.ims_folder),
        "devices": sorted({leaf.device for leaf in pkg.leaves}),
        "tags": {str(leaf.tag_id): "/".join(leaf.path) for leaf in pkg.leaves},
    }


def _package_key(thing: str, ims: str, sha: str) -> str:
    """Where the package server finds a gateway's package; scoped by thing, so a gateway can only
    ever be sent its own packages."""
    return "packages/%s/%s/%s" % (thing, ims_shadow_name(ims), sha)


def _diff(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any] | None:
    if not old:
        return None
    a, b = old.get("tags") or {}, new["tags"]
    added = sorted(set(b) - set(a), key=int)
    removed = sorted(set(a) - set(b), key=int)
    moved = sorted((k for k in set(a) & set(b) if a[k] != b[k]), key=int)
    return {
        "from_version": old.get("version"),
        "same": not (added or removed or moved),
        "added": len(added), "removed": len(removed), "moved": len(moved),
        "added_sample": [b[k] for k in added[:20]],
        "removed_sample": [a[k] for k in removed[:20]],
        "moved_sample": [{"from": a[k], "to": b[k]} for k in moved[:20]],
        "devices_added": sorted(set(new["devices"]) - set(old.get("devices") or [])),
        "devices_removed": sorted(set(old.get("devices") or []) - set(new["devices"])),
    }


def _http_get(url: str, limit: int) -> bytes:
    if urllib.parse.urlsplit(url).scheme not in ("https", "http"):
        raise ApiError(500, "HAMF_API_URL must be http(s)")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - scheme checked above
            data = resp.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise ApiError(502, "HAMF answered %d for %s" % (exc.code, urllib.parse.urlsplit(url).path))
    except OSError as exc:
        raise ApiError(502, "HAMF unreachable: %s" % exc)
    if len(data) > limit:
        raise ApiError(413, "HAMF export is over %d MB" % (limit // (1024 * 1024)))
    return data


def _load_source(aws: Aws, body: dict[str, Any]) -> tuple[bytes, str, dict[str, Any]]:
    """The export bytes, a filename, and where they came from (an upload or a HAMF asset)."""
    if body.get("upload_key"):
        key = str(body["upload_key"])
        if not UPLOAD_KEY_RE.fullmatch(key):
            raise ApiError(400, "bad upload_key")
        return aws.get_object(key), key.rsplit("/", 1)[-1], {"upload_key": key}
    if body.get("hamf_asset_id") is not None:
        if not aws.hamf_url:
            raise ApiError(400, "HAMF pull is not configured (HAMF_API_URL)")
        asset = str(body["hamf_asset_id"])
        if not re.fullmatch(r"[0-9]{1,18}", asset):
            raise ApiError(400, "bad hamf_asset_id")
        url = "%s/v1/assets/%s/ignition-export" % (aws.hamf_url, asset)
        return _http_get(url, MAX_PACKAGE_BYTES), "hamf-asset-%s.json" % asset, {"hamf_asset_id": asset}
    raise ApiError(400, "give upload_key or hamf_asset_id")


def _parse(data: bytes, filename: str, body: dict[str, Any]) -> Any:
    try:
        return hamf.load(data, filename, int(body.get("depth") or 4), body.get("ims") or None)
    except (hamf.HamfError, ValueError) as exc:
        raise ApiError(422, "not a valid HAMF export: %s" % exc)


def _current_manifest(aws: Aws, thing: str, ims: str) -> dict[str, Any] | None:
    desired = (aws.shadow(thing, ims_shadow_name(ims)).get("state") or {}).get("desired") or {}
    sha = desired.get("sha256")
    if not sha or desired.get("remove"):
        return None
    try:
        return json.loads(aws.get_object(_package_key(thing, ims, sha) + ".manifest.json"))
    except ApiError:
        return None  # published by another tool (publish_ims.py): no manifest to diff against


def _timer_options(body: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if body.get("interval_ms") is not None:
        ms = int(body["interval_ms"])
        if not 100 <= ms <= 3_600_000:
            raise ApiError(400, "interval_ms must be 100-3600000")
        out["interval_ms"] = ms
    if body.get("enabled") is not None:
        out["enabled"] = bool(body["enabled"])
    return out


def _publish_to(aws: Aws, thing: str, pkg: Any, data: bytes, manifest: dict[str, Any],
                options: dict[str, Any]) -> dict[str, Any]:
    sha = manifest["sha256"]
    key = _package_key(thing, pkg.ims_key, sha)
    aws.put_object(key, data)
    aws.put_object(key + ".manifest.json", json.dumps(manifest).encode(), "application/json")
    desired = {"version": _now(), "sha256": sha, "filename": manifest["filename"],
               "source": {"type": "mqtt"}, "remove": False, **options}
    aws.set_desired(thing, ims_shadow_name(pkg.ims_key), desired)
    listed = list(((aws.shadow(thing, GATEWAY_SHADOW).get("state") or {}).get("desired") or {}).get("ims") or [])
    if pkg.ims_key not in listed:
        aws.set_desired(thing, GATEWAY_SHADOW, {"ims": listed + [pkg.ims_key]})
    return {"thing": thing, "ims": pkg.ims_key, "sha256": sha, "version": desired["version"]}


# ---------------------------------------------------------------------------- handlers


def get_config(aws: Aws, req: Request) -> Any:
    return {"region": aws.region, "bucket": aws.bucket, "thing_prefix": aws.prefix,
            "hamf_enabled": bool(aws.hamf_url), "max_package_mb": MAX_PACKAGE_BYTES // (1024 * 1024),
            "health_stale_s": aws.stale_s}


def list_gateways(aws: Aws, req: Request) -> Any:
    things = _things(aws)
    with ThreadPoolExecutor(max_workers=16) as pool:
        rows = list(pool.map(lambda t: _summary(aws, t), things))
    return {"gateways": sorted(rows, key=lambda r: r["thing"])}


def get_gateway(aws: Aws, req: Request, thing: str) -> Any:
    thing = _thing(aws, thing)
    try:
        described = aws.iot.describe_thing(thingName=thing)
    except aws.iot.exceptions.ResourceNotFoundException:
        raise ApiError(404, "no gateway %s" % thing)
    summary = _summary(aws, {"thingName": thing, "attributes": described.get("attributes") or {}})
    gateway = aws.shadow(thing, GATEWAY_SHADOW)
    names = [n for n in aws.shadow_names(thing) if n.startswith("ims-")]
    with ThreadPoolExecutor(max_workers=8) as pool:
        docs = dict(zip(names, pool.map(lambda n: aws.shadow(thing, n), names)))
    ims = []
    for name, doc in sorted(docs.items()):
        state = doc.get("state") or {}
        desired = dict(state.get("desired") or {})
        if isinstance(desired.get("source"), dict):
            desired["source"] = {"type": desired["source"].get("type")}  # never echo presigned URLs
        ims.append({"shadow": name, "desired": desired, "reported": state.get("reported") or {},
                    "pending": sorted((state.get("delta") or {}).keys()), "shadow_version": doc.get("version")})
    certs = []
    for arn in aws.iot.list_thing_principals(thingName=thing).get("principals", []):
        if ":cert/" not in arn:
            continue
        desc = aws.iot.describe_certificate(certificateId=arn.rsplit("/", 1)[-1])["certificateDescription"]
        validity = desc.get("validity") or {}
        certs.append({"id": desc["certificateId"][:12], "status": desc.get("status"),
                      "not_after": validity.get("notAfter"), "created": desc.get("creationDate")})
    gstate = gateway.get("state") or {}
    return {**summary, "attributes": described.get("attributes") or {},
            "gateway_shadow": {"desired": gstate.get("desired") or {}, "reported": gstate.get("reported") or {},
                               "pending": sorted((gstate.get("delta") or {}).keys())},
            "ims_shadows": ims, "certificates": certs}


def patch_gateway(aws: Aws, req: Request, thing: str) -> Any:
    thing = _thing(aws, thing)
    body = req.json()
    attrs = {}
    for key in ("site", "customer"):
        if key in body:
            value = str(body[key] or "").strip()
            if value and not re.fullmatch(r"[A-Za-z0-9 _.,@:/#=\[\]-]{1,800}", value):
                raise ApiError(400, "%s has characters AWS IoT attributes don't allow" % key)
            attrs[key] = value
    if not attrs:
        raise ApiError(400, "nothing to change (site, customer)")
    # An empty value removes the attribute.
    aws.iot.update_thing(thingName=thing, attributePayload={"attributes": attrs, "merge": True})
    aws.audit(req, "label", thing, **attrs)
    return {"ok": True}


ACTIONS: dict[str, Callable[[], dict[str, Any]]] = {
    "pause": lambda: {"publish_paused": True},
    "resume": lambda: {"publish_paused": False},
    "rescan": lambda: {"rescan": _now()},
    "restart": lambda: {"restart": _now()},
    "basic_ingest_on": lambda: {"telemetry_rule": "hyper_telemetry"},
    "basic_ingest_off": lambda: {"telemetry_rule": ""},
}


def gateway_action(aws: Aws, req: Request, thing: str) -> Any:
    thing = _thing(aws, thing)
    action = str(req.json().get("action") or "")
    if action not in ACTIONS:
        raise ApiError(400, "action must be one of %s" % ", ".join(sorted(ACTIONS)))
    desired = ACTIONS[action]()
    aws.set_desired(thing, GATEWAY_SHADOW, desired)
    aws.audit(req, "action:" + action, thing)
    return {"ok": True, "desired": desired}


def decommission(aws: Aws, req: Request, thing: str) -> Any:
    thing = _thing(aws, thing)
    if req.json().get("confirm") != thing:
        raise ApiError(400, "type the thing name in confirm to decommission it")
    revoked = []
    for arn in aws.iot.list_thing_principals(thingName=thing).get("principals", []):
        aws.iot.detach_thing_principal(thingName=thing, principal=arn)
        if ":cert/" in arn:
            # INACTIVE, not deleted: the gateway can never connect again, and the record stays.
            aws.iot.update_certificate(certificateId=arn.rsplit("/", 1)[-1], newStatus="INACTIVE")
            revoked.append(arn.rsplit("/", 1)[-1][:12])
    for name in aws.shadow_names(thing):
        aws.iot_data.delete_thing_shadow(thingName=thing, shadowName=name)
    aws.iot.delete_thing(thingName=thing)
    aws.audit(req, "decommission", thing, revoked=revoked)
    return {"ok": True, "revoked_certificates": revoked}


def create_upload(aws: Aws, req: Request) -> Any:
    body = req.json()
    name = re.sub(r"[^A-Za-z0-9 ._()-]", "_", str(body.get("filename") or "export.json"))[:128] or "export.json"
    size = int(body.get("size") or 0)
    if size > MAX_PACKAGE_BYTES:
        raise ApiError(413, "file is over %d MB" % (MAX_PACKAGE_BYTES // (1024 * 1024)))
    key = "uploads/%s/%s" % (uuid.uuid4().hex, name)
    url = aws.s3.generate_presigned_url("put_object", Params={"Bucket": aws.bucket, "Key": key}, ExpiresIn=900)
    return {"upload_key": key, "url": url}


def preview(aws: Aws, req: Request) -> Any:
    body = req.json()
    data, filename, origin = _load_source(aws, body)
    pkg = _parse(data, filename, body)
    manifest = _manifest(pkg, data, filename)
    out = {k: v for k, v in manifest.items() if k != "tags"}
    out["origin"] = origin
    if body.get("thing"):
        thing = _thing(aws, str(body["thing"]))
        out["diff"] = _diff(_current_manifest(aws, thing, pkg.ims_key), manifest)
    return out


def publish_ims(aws: Aws, req: Request, thing: str) -> Any:
    thing = _thing(aws, thing)
    body = req.json()
    data, filename, origin = _load_source(aws, body)
    pkg = _parse(data, filename, body)
    result = _publish_to(aws, thing, pkg, data, _manifest(pkg, data, filename), _timer_options(body))
    aws.audit(req, "publish", thing, ims=pkg.ims_key, sha256=result["sha256"][:12], tags=len(pkg.leaves), **origin)
    return result


def rollout(aws: Aws, req: Request) -> Any:
    body = req.json()
    things = [_thing(aws, str(t)) for t in body.get("things") or []]
    if not things:
        raise ApiError(400, "pick at least one gateway")
    if len(things) > 100:
        raise ApiError(400, "at most 100 gateways per rollout")
    data, filename, origin = _load_source(aws, body)
    pkg = _parse(data, filename, body)
    manifest = _manifest(pkg, data, filename)
    options = _timer_options(body)
    results = []
    for thing in things:
        try:
            results.append({"ok": True, **_publish_to(aws, thing, pkg, data, manifest, options)})
        except Exception as exc:  # one bad gateway shouldn't stop the rest
            results.append({"ok": False, "thing": thing, "error": str(exc)[:300]})
    aws.audit(req, "rollout", None, ims=pkg.ims_key, sha256=manifest["sha256"][:12],
              things=things, failed=[r["thing"] for r in results if not r["ok"]], **origin)
    return {"ims": pkg.ims_key, "results": results}


def patch_ims(aws: Aws, req: Request, thing: str, ims: str) -> Any:
    thing, ims = _thing(aws, thing), _ims(ims)
    options = _timer_options(req.json())
    if not options:
        raise ApiError(400, "nothing to change (enabled, interval_ms)")
    aws.set_desired(thing, ims_shadow_name(ims), options)
    aws.audit(req, "timer", thing, ims=ims, **options)
    return {"ok": True, "desired": options}


def retry_ims(aws: Aws, req: Request, thing: str, ims: str) -> Any:
    thing, ims = _thing(aws, thing), _ims(ims)
    desired = (aws.shadow(thing, ims_shadow_name(ims)).get("state") or {}).get("desired") or {}
    if not desired.get("sha256") or desired.get("remove"):
        raise ApiError(409, "%s has no package to retry" % ims)
    # A new version label and source make a fresh delta, which is what the gateway acts on.
    aws.set_desired(thing, ims_shadow_name(ims), {"version": _now(), "source": {"type": "mqtt"}})
    aws.audit(req, "retry", thing, ims=ims)
    return {"ok": True}


def remove_ims(aws: Aws, req: Request, thing: str, ims: str) -> Any:
    thing, ims = _thing(aws, thing), _ims(ims)
    aws.set_desired(thing, ims_shadow_name(ims), {"remove": True})
    listed = list(((aws.shadow(thing, GATEWAY_SHADOW).get("state") or {}).get("desired") or {}).get("ims") or [])
    aws.set_desired(thing, GATEWAY_SHADOW, {"ims": [k for k in listed if k != ims]})
    aws.audit(req, "remove", thing, ims=ims)
    return {"ok": True}


def hamf_hierarchy(aws: Aws, req: Request) -> Any:
    if not aws.hamf_url:
        raise ApiError(404, "HAMF pull is not configured (HAMF_API_URL)")
    return json.loads(_http_get(aws.hamf_url + "/v1/hierarchy", 8 * 1024 * 1024))


def get_audit(aws: Aws, req: Request) -> Any:
    if not aws.audit_table:
        return {"entries": []}
    kw: dict[str, Any] = {"TableName": aws.audit_table, "KeyConditionExpression": "pk = :p",
                          "ExpressionAttributeValues": {":p": {"S": "audit"}}, "ScanIndexForward": False,
                          "Limit": 200}
    thing = req.query.get("thing")
    if thing:
        kw["FilterExpression"] = "thing = :t"
        kw["ExpressionAttributeValues"][":t"] = {"S": _thing(aws, thing)}
    items = aws.ddb.query(**kw).get("Items", [])
    return {"entries": [{"at": i["sk"]["S"].split("#")[0], "action": i["action"]["S"],
                         "thing": (i.get("thing") or {}).get("S"), "ip": i["ip"]["S"],
                         "detail": json.loads(i["detail"]["S"])} for i in items]}


SEG = r"([^/]+)"
ROUTES: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = [
    ("GET", re.compile(r"^/api/config$"), get_config),
    ("GET", re.compile(r"^/api/gateways$"), list_gateways),
    ("GET", re.compile(r"^/api/gateways/%s$" % SEG), get_gateway),
    ("PATCH", re.compile(r"^/api/gateways/%s$" % SEG), patch_gateway),
    ("DELETE", re.compile(r"^/api/gateways/%s$" % SEG), decommission),
    ("POST", re.compile(r"^/api/gateways/%s/actions$" % SEG), gateway_action),
    ("POST", re.compile(r"^/api/uploads$"), create_upload),
    ("POST", re.compile(r"^/api/preview$"), preview),
    ("POST", re.compile(r"^/api/gateways/%s/ims$" % SEG), publish_ims),
    ("POST", re.compile(r"^/api/rollout$"), rollout),
    ("PATCH", re.compile(r"^/api/gateways/%s/ims/%s$" % (SEG, SEG)), patch_ims),
    ("POST", re.compile(r"^/api/gateways/%s/ims/%s/retry$" % (SEG, SEG)), retry_ims),
    ("DELETE", re.compile(r"^/api/gateways/%s/ims/%s$" % (SEG, SEG)), remove_ims),
    ("GET", re.compile(r"^/api/hamf/hierarchy$"), hamf_hierarchy),
    ("GET", re.compile(r"^/api/audit$"), get_audit),
]

_HERE = Path(__file__).resolve().parent
# In the Lambda bundle the UI sits in web/ beside this file; in the repo it is console/web.
WEB_DIR = Path(os.environ.get("WEB_DIR") or (_HERE / "web" if (_HERE / "web").is_dir() else _HERE.parent / "web"))
_aws: Aws | None = None


def handle(event: dict[str, Any], aws: Aws) -> dict[str, Any]:
    try:
        req = Request(event)
        if req.method == "GET" and req.path in STATIC:
            filename, ctype = STATIC[req.path]
            return _response(200, (WEB_DIR / filename).read_text(), ctype)
        if req.method not in ("GET", "HEAD") and req.headers.get("x-console-request") != "1":
            raise ApiError(403, "missing X-Console-Request header")
        for method, pattern, fn in ROUTES:
            m = pattern.match(req.path)
            if m and method == req.method:
                args = [urllib.parse.unquote(g) for g in m.groups()]
                return _response(200, fn(aws, req, *args))
        raise ApiError(404, "no route %s %s" % (req.method, req.path))
    except ApiError as exc:
        return _response(exc.status, {"error": exc.message})
    except Exception:
        log.exception("unhandled error")
        return _response(500, {"error": "internal error; see the console's CloudWatch log"})


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _aws
    if _aws is None:
        _aws = Aws()
    return handle(event, _aws)
