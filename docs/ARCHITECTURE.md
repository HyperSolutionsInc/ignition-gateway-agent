# Architecture

```
            HAMF (hyper-sense)                                   AWS IoT Core
   GET /v1/assets/:id/ignition-export                  ┌───────────────────────────────┐
                    │                                  │ thing: <gateway>              │
                    │ one IMS export (.json / .zip)    │  shadow "gateway"             │
                    │                                  │  shadow "ims-<key>" (per IMS) │
      ┌─────────────┼──────────────┐                   │  rule hyper_telemetry         │
      │ inbox drop  │ HTTP upload  │ shadow source ◄───┤   (Basic Ingest)              │
      ▼             ▼              ▼                   └──────────────▲────────────────┘
 ┌──────────────────────────────────────────┐   MQTT over TLS (8883) │
 │ gateway-agent container                   │   X.509 device cert    │
 │                                           │────────────────────────┘
 │  hamf.py       parse + validate one IMS   │
 │  generator.py  tag import + Jython        │
 │  installer.py  write project, REST import │
 │  telemetry.py  timer posts -> outbox      │
 │  store.py      SQLite: packages, outbox   │
 │  iot.py        MQTT link + publisher      │
 │  shadow.py     config in / health out     │
 └───────┬───────────────────────▲───────────┘
         │ project files          │ POST /v1/telemetry
         │ (shared data volume)   │ (Docker network)
         │ REST: tags/import,     │
         │       scan/projects    │
 ┌───────▼───────────────────────┴───────────┐
 │ ignition container (8.3)                   │
 │  project hyper-gateway                     │
 │   script-python/hyper/publisher   shared   │
 │   script-python/hyper/ims/<mod>   per IMS  │
 │   timer/ims_<mod>                 per IMS  │
 │  [default]<customer>/<campus>/<building>/  │
 │            <IMS>/...  tags from HAMF        │
 └────────────────────────────────────────────┘
```

## Why the work is split this way

**Timer scripts read; the agent sends.** Ignition's Jython 2.7 can't reliably hold a modern
mutual-TLS MQTT session, and certificates shouldn't live inside the gateway project. Each IMS
timer reads its tags every interval and posts good-quality values to the agent over the Docker
network as `[tag_id, value]` pairs. The agent owns the single AWS IoT connection, the device certificate, and a
disk-backed outbox. Readings survive AWS outages (up to `BUFFER_MAX_ROWS`) and agent restarts:
the timer keeps up to 10 minutes of ticks in gateway globals until the agent answers.

**One IMS per package.** A customer gateway carries up to 24 IMSs, and they come online one at
a time. Each IMS gets its own tag folder, its own library script and its own timer resource.
Installing, updating, disabling or removing one IMS never rewrites another's. A package that
spans more than one IMS folder is rejected.

**Generated, not hand-edited.** The tag metadata the timer needs (tag path → `tag_id`) is
generated from HAMF's identity block on every leaf, so it can't drift from the export. It's stored as JSON split into string constants under 30 KB each.
A literal list of thousands of entries exceeds the JVM's 64 KB method limit when Jython
compiles it. The timer script body is three lines inside `handleTimerEvent()`, and nothing
runs at module level.

**REST, not raw file edits plus a restart.** Project files are written in place on the shared
volume. Then the agent calls the gateway's `scan/projects` endpoint, and tags go in through
`tags/import`. That replaces the earlier pattern of editing resources on disk and restarting
the gateway to make them take effect.

## Install sequence (one IMS)

1. Parse the export: merge per-device files, check every leaf has `tag_id`, `asset_id`,
   `asset_path`, `signal_path` and `opcItemPath`, check that no `tag_id` is reused, and find
   the single IMS folder at `IMS_PATH_DEPTH`.
2. Content-hash the IMS tree. If the same hash is already installed with the same timer
   settings, stop; re-imports are idempotent.
3. Write `project.json`, `hyper.publisher`, `hyper.ims.<mod>` and `timer/ims_<mod>` into the
   managed project (atomic file replace).
4. Delete tags that existed in the previous version of this IMS and are gone in the new one.
5. Import the IMS folder under its parent path with `collisionPolicy=Overwrite`.
6. Trigger a project rescan.
7. Record the result (installed or failed with the error) in the agent's store.

## Topics

The topic structure is kept shallow on purpose: the topic says *who* sent a message and
*what kind* it is, and everything else travels in the payload. Adding an IMS, a tag or a
payload field never needs a new topic, policy change or rule change.

| Direction | Topic | Payload |
| --- | --- | --- |
| gateway → cloud | `$aws/rules/hyper_telemetry/hyper/<thing>/telemetry` | telemetry (below) |
| gateway → cloud | `hyper/<thing>/package/request` | `{ims, request_id, sha256}` |
| cloud → gateway | `hyper/<thing>/package/chunk` | `{ims, request_id, seq, total, data}` |
| both | `$aws/things/<thing>/shadow/name/<name>/...` | AWS Device Shadow (see SHADOW.md) |

The thing name stays in every topic because the IoT policy uses it
(`${iot:Connection.Thing.ThingName}`) to stop one gateway's certificate from publishing
as another gateway.

### Basic Ingest

Telemetry is published with AWS IoT **Basic Ingest**: a topic beginning
`$aws/rules/<rule name>/` is delivered straight to that rule, bypassing the message broker.
There's no messaging charge for it, but nothing can subscribe to it either. The rule sees
the topic with the `$aws/rules/hyper_telemetry/` prefix removed (`hyper/<thing>/telemetry`),
so it can still read the gateway from the topic:

```sql
SELECT *, topic(2) AS gateway_id FROM 'hyper/+/telemetry'
```

The rule **must exist** under the name in `IOT_TELEMETRY_RULE` (default `hyper_telemetry`).
Messages sent to a rule name that doesn't exist are dropped.

To watch a gateway's telemetry live while debugging, set `telemetry_rule` to `""` on the
`gateway` shadow. The agent then publishes to plain `hyper/<thing>/telemetry`, which the
MQTT test client can subscribe to. Set it back to `hyper_telemetry` when you're done; the
policy allows both topics.

## Telemetry message

```json
{
  "v": 2,
  "gw": "<thing>",
  "ims": "FWT-02",
  "pkg": "f132213ca2cd0b7f",
  "ticks": [
    {"ts": 1762223000000, "bad": 41, "r": [[12710, 41.2], [12711, true], [12733, 3], [12790, "Closed"]]}
  ]
}
```

| Field | Meaning |
| --- | --- |
| `v` | Payload schema version (2). |
| `gw` | Thing name. Informational only: the rule should trust `topic(2)`, which the policy pins to the certificate. |
| `ims` | IMS key. |
| `pkg` | Content hash of the IMS package that produced the readings (matches `package_version` in the IMS shadow). |
| `ticks[].ts` | Read time, epoch milliseconds UTC. All readings in a tick share it. |
| `ticks[].bad` | Tags read with bad quality on that tick. They aren't sent. |
| `ticks[].r` | Readings, each `[tag_id, value]`. |

A reading carries only `tag_id` and the value. Everything else about a tag is fixed in HAMF
and is looked up from `tag_id` in the cloud: `asset_id`, `producer_id`, the device, and which
`ts.telemetry` value column the tag's data type goes in (`value_double`, `value_int`,
`value_bool` or `value_string`). Values are plain JSON numbers, booleans or strings. Don't
rely on JSON number formatting to tell int from double; use the tag's HAMF data type. Measured
on the real FWT-02 export: a full 3,901-tag tick is one 60 KB message (15.4 bytes per
reading), against 708 KB for the same tick in column-per-key form.

Messages are packed up to `IOT_MAX_MESSAGE_BYTES` (default 100 KB; AWS caps messages at
128 KB). A tick too large for one message is split across several, each repeating the same
`ts`. Each message is removed from the outbox only after AWS acknowledges it (QoS 1).

### Writing it to `ts.telemetry`

An IoT rule can't fan one message out into many database rows, so the `hyper_telemetry`
rule's action should hand the message to something that can: a Lambda, or Kinesis/Firehose
feeding a consumer. That consumer:

1. takes the gateway from the rule's `topic(2)`;
2. for each tick and reading, looks up the tag by `tag_id` (from HAMF's `asset.tags` or a
   cache of it) to get `asset_id`, `producer_id`, device and value type;
3. writes `ts = to_timestamp(ts / 1000)` and the value into the column for that type.
