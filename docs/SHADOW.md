# Device Shadow contract

The gateway agent receives Ignition configuration and reports gateway health through
**named** Device Shadows on the gateway's AWS IoT thing. One thing per gateway; the thing
name is also the MQTT client id (`IOT_THING_NAME`).

| Shadow | Who writes `desired` | Purpose |
| --- | --- | --- |
| `gateway` | cloud | Which IMSs this gateway should have, plus gateway-wide actions and settings. The agent writes `reported.health` every `HEALTH_INTERVAL_S`. |
| `ims-<key>` | cloud | One per IMS: which HAMF package version to run, and its timer settings. |

A shadow document is limited to 8 KB, which is why each IMS has its own shadow and why
package content is never placed in the shadow directly; `source` tells the agent where to
fetch it.

`<key>` is the IMS key (by default the IMS folder name from the HAMF export, such as
`FWT-02`), with any character outside `A-Z a-z 0-9 : _ -` replaced by `_`.

## `gateway` shadow

```json
{
  "state": {
    "desired": {
      "ims": ["FWT-01", "FWT-02"],
      "publish_paused": false,
      "telemetry_topic": "hyper/gateways/{thing}/ims/{ims}/telemetry",
      "rescan": 3,
      "restart": 1
    }
  }
}
```

| Key | Effect |
| --- | --- |
| `ims` | IMS keys whose `ims-<key>` shadows the agent should fetch. Deltas are not replayed after a disconnect, so the agent re-reads every listed shadow on each connect. Adding a key here is how a new IMS is announced to the gateway. |
| `publish_paused` | Stop or resume uplink. Readings keep buffering locally while paused. |
| `telemetry_topic` | Override the telemetry topic template (`{thing}`, `{ims}`). |
| `rescan` | Any new value triggers an Ignition project rescan. |
| `restart` | Any new value restarts the Ignition gateway, only if the agent runs with `ALLOW_REMOTE_RESTART=true`; otherwise the agent reports `restart_refused`. |

Reported (in addition to mirroring the keys above):

```json
"health": {
  "agent_version": "0.1.0",
  "reported_at": "2026-11-04T02:23:32Z",
  "ignition": {"state": "RUNNING"},
  "link": {"connected_since": "...", "last_disconnect": null},
  "outbox": {"messages": 0, "rows": 0, "oldest_age_s": 0, "dropped_rows_total": 0},
  "published_messages": 18233,
  "last_publish": "...",
  "packages": {
    "FWT-02": {"v": "f132213ca2cd0b7f", "status": "installed", "enabled": true,
               "tags": 3901, "good": 3860, "bad": 41, "tick_age_s": 0.7}
  },
  "cert_not_after": "Dec 31 23:59:59 2049 GMT",
  "disk_free_mb": 808293,
  "last_error": null
}
```

`good` / `bad` are the tag-quality counts from the IMS's most recent timer tick. A high
`bad` count usually means a device is unreachable or a register map is wrong. A growing
`tick_age_s` means the timer script has stopped running.

## `ims-<key>` shadow

```json
{
  "state": {
    "desired": {
      "version": "2026-11-03.1",
      "sha256": "9f2c...e1",
      "filename": "FWT-02.edge-config.zip",
      "source": {"type": "url", "url": "https://<bucket>.s3.us-east-2.amazonaws.com/...presigned..."},
      "enabled": true,
      "interval_ms": 1000
    }
  }
}
```

| Key | Meaning |
| --- | --- |
| `version` | Free-form label set by the cloud; mirrored back so the delta clears. |
| `sha256` | SHA-256 of the package bytes. Required. A change here is what triggers a download and install. |
| `filename` | Name of the HAMF export (`.json` or `.zip`); `.zip` is detected from content as well. |
| `source` | Where to get the bytes (see below). The agent sets `desired.source` to `null` after a successful install, because presigned URLs and inline blobs are single-use. |
| `enabled` | Enable or disable this IMS's timer script without reinstalling. |
| `interval_ms` | Timer interval for this IMS. |
| `remove` | `true` removes the IMS's timer, library script and tag folder from the gateway. |

The package itself is exactly what HAMF exports for one IMS: the `ignition-export` envelope,
the bare Provider tree, or the per-device `.zip`. The agent generates the Ignition tag import
and timer scripts from it on the gateway, so the cloud never has to ship Jython.

### Package sources

* **URL**: `{"type": "url", "url": "https://..."}`. HTTPS only. An S3 presigned GET URL is the
  expected use. Up to 64 MB.
* **Inline**: `{"type": "inline", "encoding": "gzip+base64", "data": "..."}`. Only for small
  packages, since the whole shadow document must stay under 8 KB.
* **MQTT**: `"mqtt"`. The agent publishes
  `hyper/gateways/<thing>/packages/<key>/request` with `{"request_id", "sha256"}`. The cloud
  answers on `hyper/gateways/<thing>/packages/<key>/chunk` with
  `{"request_id", "seq", "total", "data": "<base64>"}`, sequence numbers `0..total-1`, each
  message under 128 KB. The transfer must complete within 120 s.

Whatever the source, the bytes must hash to `desired.sha256` or nothing is installed.

### Reported

```json
"reported": {
  "version": "2026-11-03.1", "sha256": "9f2c...e1", "filename": "FWT-02.edge-config.zip",
  "enabled": true, "interval_ms": 1000,
  "status": "installed",
  "package_version": "f132213ca2cd0b7f",
  "tag_count": 3901,
  "tag_folder": "[default]Switch/Petersburg/ACI/FWT-02",
  "error": null,
  "applied_at": "2026-11-04T02:30:11Z"
}
```

`status` moves through `downloading` → `installed`, or to `failed` with `error` set
(bad hash, invalid export, gateway API error). `package_version` is the content hash of the
IMS tag tree: two uploads of the same HAMF export yield the same value.

## Pushing a package by hand (AWS CLI)

```bash
sha=$(shasum -a 256 FWT-02.edge-config.zip | cut -d' ' -f1)
aws s3 cp FWT-02.edge-config.zip s3://<bucket>/gateways/<thing>/FWT-02/$sha.zip
url=$(aws s3 presign s3://<bucket>/gateways/<thing>/FWT-02/$sha.zip --expires-in 3600)

cat > desired.json <<EOF
{"state": {"desired": {"version": "$(date -u +%F.%H%M)", "sha256": "$sha",
  "filename": "FWT-02.edge-config.zip", "source": {"type": "url", "url": "$url"},
  "enabled": true, "interval_ms": 1000}}}
EOF
aws iot-data update-thing-shadow --thing-name <thing> --shadow-name ims-FWT-02 \
  --cli-binary-format raw-in-base64-out --payload file://desired.json /dev/stdout

# announce it on the gateway shadow (merge with the existing list)
aws iot-data update-thing-shadow --thing-name <thing> --shadow-name gateway \
  --cli-binary-format raw-in-base64-out \
  --payload '{"state":{"desired":{"ims":["FWT-02"]}}}' /dev/stdout
```

## IoT policy

`docs/aws-iot-policy.json` is the least-privilege policy the agent needs, scoped to the
connecting thing's own name. Attach it to each gateway's certificate.
