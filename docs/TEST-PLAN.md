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
| A16 | Every HTTP endpoint except `/healthz` needs a token. The ingest token grants only `/v1/telemetry`, and the admin token doesn't grant telemetry. Tokens are compared in constant time, and the server won't start without both. | `test_security.py`: `test_everything_else_needs_a_token`, `test_ingest_token_only_grants_telemetry`, `test_admin_token_does_not_grant_telemetry`, `test_admin_token_works_both_header_styles`, `test_server_refuses_to_start_without_tokens` |
| A17 | Missing tokens are generated once (32 random bytes, file mode 0600) and reused on restart. A rotated ingest token is rewritten into the publisher and the gateway is rescanned. `render` never embeds the live token. | `test_tokens_are_generated_once_with_private_permissions`, `test_refresh_shared_rewrites_publisher_after_token_rotation`, `test_render_does_not_embed_the_live_token` |
| A18 | Values written into generated Jython (agent URL, ingest token) are validated and inserted as escaped literals, so they can't inject code. | `test_generator.py`: `test_publisher_rejects_unsafe_agent_url`, `test_publisher_rejects_unsafe_token` |
| A19 | URL fetches only use http(s). The Ignition URL must be http(s). Package downloads must be HTTPS (unless `ALLOW_HTTP_PACKAGE_URLS=1`) and won't follow a redirect to any other scheme. | `test_ignition_client_rejects_non_http_schemes`, `test_package_download_requires_https`, `test_package_download_refuses_redirect_to_other_scheme` |

## Verified outside the unit tests

Run once on the Kingdel lab gateway (Ignition 8.3.7):

* The real FWT-02 PPM edge-config export (25 device files) renders to one IMS with 3,901 tags.
* Every generated script compiles under the gateway's own Jython (`jython-ia-2.7.3.5.jar`), and
  the 358 KB metadata module loads.
* One full tick of 3,901 readings packs into a single 60 KB message.
* The agent container serves `/healthz`, accepts a telemetry post, and queues it. *(This was
  before round 1. Telemetry now also needs the ingest token, which is covered by
  `test_security.py`.)*

## Not testable yet

* **The live Ignition REST endpoints** (`gateway_agent/ignition.py: ENDPOINTS`). The gateway
  has no API key yet, so the paths are unverified against its `/openapi` listing.
* **The live AWS IoT connection, shadows and Basic Ingest rule.** The available device
  certificate completes TLS but AWS closes the MQTT connection, and the rule doesn't exist yet.
* **The Jython publisher at runtime** (`gateway_agent/ignition_scripts/publisher.py`). It is
  compile-checked under Python 3 and Jython, but it only runs inside a live gateway.

## Static checks

Run with the ARES Grid's toolchain (Bandit 1.8.6, pip-audit):

```bash
bandit -q -r gateway_agent          # no issues; 5 justified `# nosec` markers listed below
pip-audit -r requirements.txt       # no known vulnerabilities (paho-mqtt 2.1.0)
```

| Marker | Why it's safe |
| --- | --- |
| `config.py` B104 (bind 0.0.0.0) | It must listen on the container interface for the Ignition container to reach it. Compose publishes it on loopback only, and every endpoint but `/healthz` needs a token. |
| `ignition.py` B310 | The scheme is validated as http(s) in `IgnitionClient.__init__`, and the path is built internally. |
| `cli.py` B310 | `--agent-url` is validated as http(s); fixed endpoint paths. |
| `shadow.py` B310 | HTTPS is enforced on the initial URL and on every redirect. |
| `installer.py` B105 | A placeholder marker, not a credential. |

## Changes from review round 1 (R-0004)

* The local HTTP API now requires tokens (admin and ingest, split by privilege). *(AEGIS)*
* http(s)-only URL handling, and redirects of package downloads are scheme-checked. *(Bandit B310)*
* The generated Jython escapes and validates every inserted value.
* Added a pinned `requirements.txt`, so pip-audit scans the runtime dependencies (the Grid
  scan skips `pyproject.toml`). The image installs from it.
* This test document is filed with the review. *(PROBE)*

## Security-relevant surface for review

* The local HTTP API (`server.py`) needs an admin or ingest token on every endpoint except
  `/healthz`. It is designed for the Docker network and a loopback-published port, and accepts
  package uploads up to 64 MB.
* Package download from shadow `source.url` (`shadow.py: _download`): HTTPS only unless
  `ALLOW_HTTP_PACKAGE_URLS=1`, 64 MB cap, content hash checked before install.
* Files are written into the Ignition data volume (`installer.py`). Paths are built from the
  IMS key passed through `module_name_for`.
* The device private key is read from a mounted path. TLS uses the system defaults with the
  Amazon root CA.
* Tag imports and deletes on the gateway go through its REST API with an API token from the
  environment.
