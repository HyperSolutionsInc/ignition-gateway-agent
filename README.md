# gateway-agent

A sidecar container for an Ignition 8.3 gateway. It:

* **imports HAMF IMS exports**: turns each HAMF `ignition-export` (one IMS) into an Ignition
  tag import plus generated timer and library scripts, and installs them without restarting
  the gateway. IMSs can be added one at a time as they are stood up;
* **uplinks telemetry to AWS IoT Core**: the generated timer scripts read each IMS's tags
  and hand the values to the agent, which buffers them on disk and publishes over MQTT/TLS
  through Basic Ingest to the `hyper_telemetry` rule;
* **takes configuration from Device Shadows**: new or updated IMS packages, timer settings,
  removals and gateway actions arrive through named shadows, and gateway health is reported
  back.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and
[docs/SHADOW.md](docs/SHADOW.md) for the shadow contract.

## Requirements

* Ignition 8.3 in Docker, with an **API key** (Gateway > Security > API Keys) that can import
  tags and scan projects.
* An AWS IoT thing for the gateway, with an active X.509 certificate that has
  [docs/aws-iot-policy.json](docs/aws-iot-policy.json) attached.
* An AWS IoT rule named `hyper_telemetry` (or whatever `IOT_TELEMETRY_RULE` is set to) with
  `FROM 'hyper/+/telemetry'`. Telemetry sent to a rule that doesn't exist is dropped. See
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#topics).

## Deploy next to Ignition

```bash
cp deploy/.env.example deploy/.env           # fill in IGNITION_API_TOKEN, IOT_THING_NAME, ...
mkdir -p deploy/certs
cp AmazonRootCA1.pem device.cert.pem device.private.key deploy/certs/
sudo chown -R 2003:2003 deploy/certs         # the agent runs as the Ignition uid
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
docker exec gateway-agent gateway-agent check-iot
```

The agent joins the Ignition stack's Docker network and mounts its data volume. Set
`IGNITION_NETWORK` and `IGNITION_DATA_VOLUME` to match the stack (`docker network ls`,
`docker volume ls`).

## Adding an IMS

Choose whichever path suits the site:

```bash
# 1. CLI (inside the container)
docker cp FWT-02.edge-config.zip gateway-agent:/tmp/
docker exec gateway-agent gateway-agent import /tmp/FWT-02.edge-config.zip

# 2. HTTP
curl --data-binary @FWT-02.edge-config.zip "http://127.0.0.1:8600/v1/packages?filename=FWT-02.edge-config.zip"

# 3. Drop folder: anything copied into <state>/inbox is installed, then moved to inbox/done or inbox/failed
docker cp FWT-02.edge-config.zip gateway-agent:/var/lib/gateway-agent/inbox/

# 4. Device Shadow: set desired on ims-FWT-02 (see docs/SHADOW.md)
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
```

`render` output can also be imported by hand: `out/project` is an Ignition project folder,
and `out/tags/<mod>.json` is a Designer tag import for the `importPath` it names.

## Configuration

Everything is set by environment variable; [deploy/.env.example](deploy/.env.example) lists
them all with defaults. The ones that differ per site:

| Variable | Purpose |
| --- | --- |
| `IGNITION_API_TOKEN` | Gateway API key. |
| `IOT_THING_NAME` | The gateway's thing name, also its MQTT client id. |
| `IGNITION_TAG_ROOT` | Optional folder under the provider to place every IMS beneath. |
| `IMS_PATH_DEPTH` | How deep the IMS folder sits in the HAMF tree (default 4: customer / campus / building / power cell). |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

The test suite runs against a stand-in for the gateway REST API and a fake MQTT link, so it
needs neither Ignition nor AWS.

## Status

* Verified: rendering the real FWT-02 PPM edge-config (25 device files, 3,901 tags), and
  compiling every generated script with the gateway's own Jython (`jython-ia-2.7.3.5`).
* Not yet verified against a live gateway: the 8.3 REST endpoint paths in
  `gateway_agent/ignition.py` (`ENDPOINTS`). The gateway needs an API key before they can be
  checked against its `/openapi` listing.
* Not yet verified against AWS: the hackathon device certificate completes TLS but AWS closes
  every MQTT connection, which means the certificate is inactive or has no policy that
  allows connecting. A gateway thing and certificate need to be provisioned.
