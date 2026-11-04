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
network. The agent owns the single AWS IoT connection, the device certificate, and a
disk-backed outbox. Readings survive AWS outages (up to `BUFFER_MAX_ROWS`) and agent restarts:
the timer keeps up to 10 minutes of ticks in gateway globals until the agent answers.

**One IMS per package.** A customer gateway carries up to 24 IMSs, and they come online one at
a time. Each IMS gets its own tag folder, its own library script and its own timer resource.
Installing, updating, disabling or removing one IMS never rewrites another's. A package that
spans more than one IMS folder is rejected.

**Generated, not hand-edited.** The tag metadata the timer needs (tag path → `tag_id`,
`asset_id`, `producer_id`, device) is generated from HAMF's identity block on every leaf, so it
can't drift from the export. It's stored as JSON split into string constants under 30 KB each.
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
  "v": 1,
  "gw": "<thing>",
  "ims": "FWT-02",
  "pkg": "f132213ca2cd0b7f",
  "rows": [
    {"ts": "2026-11-04T02:23:20.000+00:00", "tag_id": 12710, "asset_id": 116, "producer_id": null,
     "ims_id": "CB08", "value_double": 41.2, "value_int": null, "value_bool": null, "value_string": null}
  ]
}
```

`v` is the payload schema version; `pkg` is the content hash of the IMS package that produced
the readings. `gw` is informational: the rule should trust `topic(2)`, which the policy pins
to the certificate.

Rows have the same columns as `ts.telemetry`, so a cloud rule can insert them without
reshaping. Messages are packed up to `IOT_MAX_MESSAGE_BYTES` (default 100 KB; AWS caps
messages at 128 KB). Each message is removed from the outbox only after AWS acknowledges it
(QoS 1).

`producer_id` is carried through when HAMF includes it on the tag. The current HAMF export
does not, so it is `null`.
