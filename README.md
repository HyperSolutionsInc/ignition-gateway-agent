# hyper gateway (gateway-agent)

One container image: **Ignition 8.3 with gateway-agent built in**. Start it and it sets itself
up, with nothing to configure on site:

* **Ignition commissions itself.** The EULA is accepted and the admin account is created, with
  a generated password kept in the agent's state volume.
* **The agent mints its own Ignition API key** at first boot, under its own `GatewayAgent`
  security level. No one has to visit the gateway webpage.
* **It gets its own AWS IoT identity** through fleet provisioning, from the claim certificate
  baked into the image.
* **IMSs arrive by themselves.** The cloud assigns them through Device Shadows; the agent
  downloads, checks and installs each one, generating its Ignition tags and timer script with
  no gateway restart.
* **Telemetry flows**: timer scripts read each IMS's tags, and the agent buffers the readings
  on disk and publishes them to AWS IoT Core over MQTT/TLS, through Basic Ingest to the
  `hyper_telemetry` rule.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design,
[docs/SHADOW.md](docs/SHADOW.md) for the shadow contract, and
[docs/aws-fleet-provisioning.md](docs/aws-fleet-provisioning.md) for the one-time AWS setup.

## Build the image (once per release)

Put the fleet claim certificate in `docker/claim/` (see its README), then:

```bash
docker build -t hyper-gateway:0.2.1 \
  --build-arg IOT_ENDPOINT=<account>-ats.iot.us-east-2.amazonaws.com \
  --build-arg IOT_PROVISIONING_TEMPLATE=hyper-gateway-fleet .
```

Push it to the private registry the gateways pull from.

## Install a gateway

```bash
docker compose -f deploy/docker-compose.yml up -d
```

or, without compose:

```bash
docker run -d --name gateway --restart unless-stopped -p 8088:8088 -p 8043:8043 \
  -v gateway-data:/usr/local/bin/ignition/data -v gateway-agent:/var/lib/gateway-agent hyper-gateway:0.2.1
```

First boot takes about a minute: Ignition starts once to create its configuration, the agent
grants itself API access, and Ignition restarts for good. After that, `docker logs gateway`
shows `inbox ...` and `AWS IoT link starting as hyper-gw-<serial>`. Check it with:

```bash
docker exec gateway gateway-agent status
docker exec gateway cat /var/lib/gateway-agent/gateway-admin-password   # Ignition admin sign-in
```

The two named volumes hold everything that must survive an upgrade: Ignition's data, and
the agent's state (tokens, identity, outbox).

## Adding an IMS

Normally the cloud assigns IMSs and the gateway installs them by itself. The
[fleet console](docs/CONSOLE.md) does this from a browser: add, update, roll out, timers,
decommission. For scripts there is `tools/publish_ims.py` (see
[docs/aws-fleet-provisioning.md](docs/aws-fleet-provisioning.md)).
On site, any of these works too:

```bash
# 1. CLI (inside the container)
docker cp FWT-02.edge-config.zip gateway:/tmp/
docker exec gateway gateway-agent import /tmp/FWT-02.edge-config.zip

# 2. HTTP (admin token: AGENT_API_TOKEN, or generated into the state volume on first start)
TOKEN=$(docker exec gateway cat /var/lib/gateway-agent/api_token)
curl -H "X-Agent-Token: $TOKEN" --data-binary @FWT-02.edge-config.zip \
  "http://127.0.0.1:8600/v1/packages?filename=FWT-02.edge-config.zip"

# 3. Drop folder: anything copied into <state>/inbox is installed, then moved to inbox/done or inbox/failed
docker cp FWT-02.edge-config.zip gateway:/var/lib/gateway-agent/inbox/

# 4. Device Shadow: tools/publish_ims.py --thing <thing> --bucket <bucket> FWT-02.export.json
```

Re-importing the same export does nothing. Importing a newer export for the same IMS updates
it in place: changed tags are overwritten, tags no longer in the export are deleted, and other
IMSs are untouched.

Other operations:

```bash
gateway-agent list
gateway-agent set FWT-02 --disable          # or --enable, --interval-ms 5000
gateway-agent remove FWT-02                 # timer, library script and tag folder
gateway-agent status                        # same health the shadow reports
gateway-agent render export.zip out/        # write the generated artifacts only, no gateway
gateway-agent rotate-tokens                 # new generated API tokens; restart to apply
```

Exports are limited to 64 MB, 500 zip members and 256 MB uncompressed, and tag or folder names
that could change a tag path (`/`, `[`, `]`, `..`) are refused.

`render` output can also be imported by hand: `out/project` is an Ignition project folder,
and `out/tags/<mod>.json` is a Designer tag import for the `importPath` it names. Unless
`AGENT_INGEST_TOKEN` is set, the rendered publisher holds the placeholder
`REPLACE_WITH_AGENT_INGEST_TOKEN`; replace it with the agent's ingest token before importing.

## API authentication

Every endpoint except `/healthz` needs a token, sent as `X-Agent-Token` or
`Authorization: Bearer`:

* **Admin token** (`AGENT_API_TOKEN`): package import, list, set, remove, and status. The CLI
  inside the container reads it from the state volume.
* **Ingest token** (`AGENT_INGEST_TOKEN`): `POST /v1/telemetry` only. It is written into the
  generated `hyper.publisher` script, so anyone who can read the gateway project can see it.
  That is why it grants nothing else.

Either one is generated (32 random bytes, mode 0600) in the state volume on first start if it
isn't set. Any of `AGENT_API_TOKEN`, `AGENT_INGEST_TOKEN` and `IGNITION_API_TOKEN` can instead
be read from a file with the `_FILE` suffix (for example `AGENT_API_TOKEN_FILE=/run/secrets/agent_api_token`
with Docker secrets).

To rotate generated tokens:

```bash
docker exec gateway gateway-agent rotate-tokens      # or --admin / --ingest
docker restart gateway
```

On startup the agent writes the new ingest token into the publisher and asks the gateway to
rescan. Tokens supplied through the environment or `_FILE` are rotated where they are managed.

## Configuration

A standard site needs none: the image carries its defaults. Everything can still be overridden
by environment variable (the full list is in `gateway_agent/config.py`). The ones worth knowing:

| Variable | Default in the image | Purpose |
| --- | --- | --- |
| `GATEWAY_SERIAL` | generated on first boot | Names the thing (`hyper-gw-<serial>`); set it to an asset tag if you have one |
| `IOT_ENDPOINT`, `IOT_PROVISIONING_TEMPLATE` | set at build time | Where and how the gateway provisions itself |
| `IOT_THING_NAME`, `IOT_CERT_PATH`, `IOT_KEY_PATH` | unset | Use an existing thing and certificate instead of provisioning |
| `GATEWAY_ADMIN_PASSWORD` (or `_FILE`) | generated | Ignition admin password at first boot |
| `IGNITION_TAG_ROOT` | empty | Optional folder under the provider to place every IMS beneath |
| `IMS_PATH_DEPTH` | `4` | How deep the IMS folder sits in the HAMF tree (customer / campus / building / IMS) |
| `TIMER_INTERVAL_MS` | `1000` | Default read interval per IMS |
| `ALLOW_REMOTE_RESTART` | `false` | Whether the shadow may restart Ignition |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

The test suite runs against a stand-in for the gateway REST API and a fake MQTT link, so it
needs neither Ignition nor AWS.

## Status

Verified on a live Ignition 8.3.7 gateway (Kingdel lab, a throwaway container):
* The image boots hands-free from empty volumes in about 55 s: Ignition commissions itself,
  the agent mints its API key, grants itself access, and comes up.
* The real FWT-02 export (25 device files, 3,901 tags) installs. The project loads, the
  timer runs every second, and readings queue in the outbox.
* Tag deletion through the tag-definition files plus `scan/config`, and project hot-load
  through `scan/projects`, both work with no gateway restart.
* Device connections can be created through the REST API (`ModbusTcp` with full settings).

Not yet verified:
* **AWS**: fleet provisioning, shadows and the Basic Ingest rule are covered by tests only;
  the end-to-end run in the AWS sandbox is next.
* **Modbus data from HAMF exports.** HAMF's Modbus tags (`[device]3/<register>`, no
  `valueSource`) aren't valid Ignition 8.3 Modbus addresses, so they become memory tags with
  no value. They need Ignition's own addressing (for example `ns=1;s=[device]1.HRF39`), the
  unit ID and the addressing base, and a device connection per device. That is a change to
  HAMF's export. OPC UA (PLCnext) tags are unaffected.
