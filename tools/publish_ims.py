"""Assign an IMS to a gateway: the cloud side of automatic IMS delivery.

Uploads one HAMF export to S3, sets the IMS's Device Shadow (`ims-<key>`) to it, and adds the
IMS to the gateway's `gateway` shadow list. The gateway then downloads, checks and installs it
by itself, and reports the result on `ims-<key>`. Re-running with a changed export updates
the IMS; `--remove` takes it off the gateway. HAMF (or a Lambda) can call this, or port it.

  pip install -r tools/requirements.txt
  python tools/publish_ims.py --thing <thing> --bucket <bucket> FWT-03.edge-config.zip
  python tools/publish_ims.py --thing <thing> --remove FWT-03

Credentials come from the usual AWS sources (profile, environment, role).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # for gateway_agent.hamf

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402

from gateway_agent import hamf  # noqa: E402
from gateway_agent.shadow import ims_shadow_name  # noqa: E402


def _shadow(iot, thing: str, name: str) -> dict:
    try:
        return json.loads(iot.get_thing_shadow(thingName=thing, shadowName=name)["payload"].read())
    except iot.exceptions.ResourceNotFoundException:
        return {}


def _update(iot, thing: str, name: str, desired: dict) -> None:
    iot.update_thing_shadow(thingName=thing, shadowName=name,
                            payload=json.dumps({"state": {"desired": desired}}).encode())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--thing", required=True, help="the gateway's AWS IoT thing name")
    ap.add_argument("--region", default="us-east-2")
    ap.add_argument("--bucket", help="S3 bucket for packages (required unless --remove)")
    ap.add_argument("--prefix", default="gateways", help="S3 key prefix")
    ap.add_argument("--ims", help="IMS key (default: the IMS folder name in the export)")
    ap.add_argument("--depth", type=int, default=4, help="IMS folder depth in the HAMF tree")
    ap.add_argument("--interval-ms", type=int)
    ap.add_argument("--url-ttl", type=int, default=24 * 3600, help="presigned URL lifetime, seconds")
    ap.add_argument("--remove", metavar="IMS", help="remove this IMS from the gateway instead")
    ap.add_argument("export", nargs="?", type=Path, help="HAMF export (.json or .zip)")
    args = ap.parse_args()

    iot = boto3.client("iot-data", region_name=args.region)
    gateway = _shadow(iot, args.thing, "gateway")
    listed = list(((gateway.get("state") or {}).get("desired") or {}).get("ims") or [])

    if args.remove:
        _update(iot, args.thing, ims_shadow_name(args.remove), {"remove": True})
        _update(iot, args.thing, "gateway", {"ims": [k for k in listed if k != args.remove]})
        print("removal of %s requested on %s" % (args.remove, args.thing))
        return 0

    if not (args.export and args.bucket):
        ap.error("an export file and --bucket are needed to publish an IMS")
    data = args.export.read_bytes()
    pkg = hamf.load(data, args.export.name, args.depth, args.ims)  # refuse a bad export before it ships
    sha = hashlib.sha256(data).hexdigest()
    key = "%s/%s/%s/%s%s" % (args.prefix, args.thing, pkg.ims_key, sha, "".join(args.export.suffixes[-1:]))

    # Sign against the regional endpoint. A URL on the global one (bucket.s3.amazonaws.com) is
    # redirected to the region for a new bucket, and the redirect breaks the signature (403).
    s3 = boto3.client("s3", region_name=args.region, endpoint_url="https://s3.%s.amazonaws.com" % args.region,
                      config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
    s3.put_object(Bucket=args.bucket, Key=key, Body=data, ServerSideEncryption="AES256")
    url = s3.generate_presigned_url("get_object", Params={"Bucket": args.bucket, "Key": key}, ExpiresIn=args.url_ttl)

    desired = {"version": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sha256": sha,
               "filename": args.export.name, "source": {"type": "url", "url": url}, "remove": False}
    if args.interval_ms:
        desired["interval_ms"] = args.interval_ms
    _update(iot, args.thing, ims_shadow_name(pkg.ims_key), desired)
    if pkg.ims_key not in listed:
        _update(iot, args.thing, "gateway", {"ims": listed + [pkg.ims_key]})
    print("published IMS %s (%d tags, sha256 %s) to %s" % (pkg.ims_key, len(pkg.leaves), sha[:12], args.thing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
