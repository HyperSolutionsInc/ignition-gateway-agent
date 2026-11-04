# Test document

What gateway-agent claims to do, how each claim is tested, and what can't be tested without
a live gateway or AWS.

## Running the tests

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

No network access is needed. The suite stands in for the Ignition REST API with a local HTTP
server (`tests/conftest.py: FakeIgnition`) and for AWS IoT with an in-memory link
(`tests/test_shadow.py: FakeLink`).

## Acceptance criteria

| # | Claim | Evidence |
| --- | --- | --- |
| A1 | A HAMF export loads whether it is the API envelope, a bare Provider tree, or a zip of per-device files, and zip members are merged into one tree. | `test_hamf.py`: `test_bare_provider_tree`, `test_envelope_keeps_devices_and_timestamp`, `test_zip_of_per_device_files_is_merged` |
| A2 | An export that is invalid or ambiguous is rejected with a clear error, never partly imported: more than one IMS, missing identity fields, conflicting duplicate tags, a reused `tag_id`, or not a HAMF export at all. | `test_hamf.py`: `test_rejects_*` |
| A3 | The package version is a content hash, so the same export always gets the same version. | `test_version_is_content_hash` |
| A4 | Generated Ignition artifacts have the right tag paths, resource files and timer settings, and are rooted under `IGNITION_TAG_ROOT` when set. | `test_generator.py`: `test_paths_and_files`, `test_tag_root_prefix`, `test_timer_resource_settings` |
| A5 | Tag metadata modules hold thousands of tags without exceeding the JVM 64 KB constant limit, and round-trip exactly. | `test_large_meta_is_split_under_jvm_constant_limit`, `test_meta_module_round_trips` |
| A6 | The timer script's whole body sits inside `handleTimerEvent()`, with no module-level code. | `test_timer_script_has_everything_inside_handler` |
| A7 | Installing an IMS writes the project files, imports tags under the IMS's parent folder, rescans projects, and sends the API token. | `test_installer.py`: `test_install_writes_project_and_calls_gateway` |
| A8 | Re-importing the same export is a no-op, and IMSs install independently of each other. | `test_reimport_same_export_is_noop`, `test_second_ims_is_independent` |
| A9 | An update deletes tags dropped from the new version. Reconfiguring rewrites the timer. Removing an IMS deletes its resources and tag folder and leaves the shared publisher in place. | `test_update_deletes_dropped_tags`, `test_reconfigure_rewrites_timer`, `test_remove` |
| A10 | A failed gateway call is recorded as failed and can be retried. Two IMS keys can't claim the same tag folder. | `test_failed_import_is_recorded`, `test_same_folder_under_two_keys_is_refused` |
| A11 | Telemetry is packed into schema-v2 messages (`[tag_id, value]` readings), stays under the size limit, and a split tick keeps its `ts` and counts `bad` once. | `test_telemetry.py`: `test_compact_envelope`, `test_large_tick_is_split_with_same_ts`, `test_several_ticks_share_a_message` |
| A12 | The outbox drops the oldest data once over its row cap, and malformed posts are rejected. | `test_buffer_cap_drops_oldest`, `test_malformed` |
| A13 | Telemetry goes to `$aws/rules/hyper_telemetry/hyper/<thing>/telemetry` (Basic Ingest), or to plain pub/sub when the rule is set to `""`. Package topics are `hyper/<thing>/package/{request,chunk}`. | `test_basic_ingest_topic`, `test_gateway_shadow_can_switch_off_basic_ingest` |
| A14 | The shadow installs a package from an inline or MQTT-chunked source only when its SHA-256 matches, reports status, clears the single-use source, and doesn't reinstall when nothing changed. | `test_shadow.py`: `test_install_from_inline_source`, `test_mqtt_chunked_source`, `test_hash_mismatch_is_reported_and_not_installed` |
| A15 | The shadow can change timer settings, remove an IMS, request the listed IMS shadows, and refuses a gateway restart unless `ALLOW_REMOTE_RESTART` is on. | `test_timer_change_without_new_package`, `test_remove`, `test_gateway_shadow_requests_listed_ims_and_refuses_restart` |

## Verified outside the unit tests

Run once on the Kingdel lab gateway (Ignition 8.3.7):

* The real FWT-02 PPM edge-config export (25 device files) renders to one IMS with 3,901 tags.
* Every generated script compiles under the gateway's own Jython (`jython-ia-2.7.3.5.jar`), and
  the 358 KB metadata module loads.
* One full tick of 3,901 readings packs into a single 60 KB message.
* The agent container serves `/healthz`, accepts a telemetry post, and queues it.

## Not testable yet

* **The live Ignition REST endpoints** (`gateway_agent/ignition.py: ENDPOINTS`). The gateway
  has no API key yet, so the paths are unverified against its `/openapi` listing.
* **The live AWS IoT connection, shadows and Basic Ingest rule.** The available device
  certificate completes TLS but AWS closes the MQTT connection, and the rule doesn't exist yet.
* **The Jython publisher at runtime** (`gateway_agent/ignition_scripts/publisher.py`). It is
  compile-checked under Python 3 and Jython, but it only runs inside a live gateway.

## Security-relevant surface for review

* The local HTTP API (`server.py`) has no authentication. It is designed for the Docker
  network and a loopback-published port, and accepts package uploads up to 64 MB.
* Package download from shadow `source.url` (`shadow.py: _download`): HTTPS only unless
  `ALLOW_HTTP_PACKAGE_URLS=1`, 64 MB cap, content hash checked before install.
* Files are written into the Ignition data volume (`installer.py`). Paths are built from the
  IMS key passed through `module_name_for`.
* The device private key is read from a mounted path. TLS uses the system defaults with the
  Amazon root CA.
* Tag imports and deletes on the gateway go through its REST API with an API token from the
  environment.
