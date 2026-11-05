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
| A20 | HAMF input is size-limited before parsing: 64 MB per export, at most 500 zip members, and 256 MB of uncompressed content across all members, enforced by bounded reads that ignore the sizes the zip declares (zip-bomb protection). A corrupt zip is a clean error. Tag and folder names that could alter a tag path (`/`, `[`, `]`, `.`, `..`, control characters, edge whitespace, more than 255 characters) are refused, and real-world names are accepted. | `test_hardening.py`: `test_export_over_size_limit_is_refused`, `test_zip_bomb_is_stopped_by_bounded_read`, `test_uncompressed_budget_is_shared_across_members`, `test_too_many_zip_members`, `test_corrupt_zip_is_a_clean_error`, `test_unsafe_names_are_refused`, `test_unsafe_folder_name_is_refused`, `test_real_world_names_are_accepted` |
| A21 | The inbox only reads regular files. Symlinks are refused without reading their target (opened with `O_NOFOLLOW` and re-checked with `fstat`). Files over the export limit are refused. The inbox directory is mode 0700. | `test_inbox_refuses_symlink_without_reading_target`, `test_inbox_refuses_oversize_file`, `test_inbox_installs_regular_file_after_it_settles` |
| A22 | Secrets can come from `<NAME>_FILE` (Docker or Kubernetes secrets) instead of the environment, but not both at once. Operator-supplied agent tokens must be 16–128 URL-safe characters. The Ignition API token is refused if it could break out of a request header. | `test_secrets_can_come_from_files`, `test_setting_both_value_and_file_is_an_error`, `test_malformed_operator_token_is_refused`, `test_ignition_token_that_could_break_a_header_is_refused` |
| A23 | `rotate-tokens` replaces generated tokens atomically (mode 0600) and refuses tokens managed outside the agent. On restart, the new ingest token is written into the publisher (A17). | `test_rotate_tokens_replaces_generated_tokens`, `test_rotate_refuses_externally_managed_token` |
| A24 | TLS is verified explicitly everywhere: certificate required, hostname checked, TLS 1.2 or later. MQTT trusts only the configured CA (the Amazon root CA). Package downloads use the system CA store and reject an untrusted certificate. | `test_round4.py`: `test_package_download_context_verifies_certificates`, `test_mqtt_context_verifies_server_and_pins_ca`, `test_package_download_rejects_untrusted_certificate` |
| A25 | The SQLite store never builds SQL from values. Every `execute` takes a constant SQL string with `?` parameters, which is checked over the source's syntax tree, and injection payloads round-trip as plain data. | `test_store_never_builds_sql_from_values`, `test_injection_payloads_are_stored_as_data` |
| A26 | Telemetry input is fully validated: IMS key and version format, timestamp between 2000 and one day ahead, positive 64-bit `tag_id`, value a number, boolean or string of at most 1,024 characters, at most 1,000 ticks per post and 100,000 readings per tick, non-negative `bad` count. `NaN` and `Infinity` (not valid JSON) are dropped and counted as bad, and the encoder refuses to write them. | `test_nan_and_infinity_are_dropped_and_counted_bad`, `test_invalid_telemetry_is_rejected`, `test_post_size_limits`, `test_valid_post_is_accepted` |
| A27 | No token value is ever printed by the CLI, and a logging filter masks every configured secret in log messages, arguments and tracebacks. | `test_rotate_tokens_never_prints_token_values`, `test_secret_redaction_masks_messages_args_and_tracebacks` |
| A28 | Secret writes are atomic and verified: on failure the old secret stays and no temp file is left, and after writing, the content and 0600 mode are checked. `IOT_ENDPOINT` must be a bare DNS hostname (no scheme, port or path), `IOT_PORT` must be 1–65535, and the thing name/client id must be AWS-valid, checked before any connection is made. | `test_round4.py`: `test_failed_secret_write_leaves_old_secret_and_no_temp_file`, `test_secret_write_is_verified`, `test_iot_endpoint_must_be_a_bare_hostname`, `test_iot_settings_validation` |
| A29 | First boot needs no hands: the agent mints its Ignition API key (`gateway-agent:<key>`, hash = base64url(SHA-256(key bytes)), checked against Ignition's own example), writes the key and a `GatewayAgent` security level into `external`, and grants that level API read and write in whichever collection holds the security properties (local before core), refusing an `AllOf` rule it can't satisfy. `bootstrap prepare` exits 3 until the gateway has started once. | `test_bootstrap.py` |
| A30 | Fleet provisioning by claim: creates a certificate, provisions it through the template with the serial and any extra parameters, and stores the certificate, key and thing name with mode 0600. Refusals store nothing. A configured identity beats a stored one, which beats provisioning. Provisioning retries until it works or the agent stops, and a generated serial is kept. | `test_provisioning.py` |
| A31 | The REST client matches Ignition 8.3.7's API. Tags are imported as the IMS folder node, sent as `application/octet-stream`, and a reported `failureCount` is an error. Restart uses `restart-tasks/restart?confirm=true`. | `test_round4.py`: `test_import_reports_gateway_side_failures`, `test_restart_uses_restart_tasks_with_confirm`; `test_installer.py`: `test_install_writes_project_and_calls_gateway` |
| A32 | Tags dropped by an update, and a removed IMS's folder, are deleted from the tag-definition files (8.3.7 has no REST delete, and Overwrite keeps them), followed by a config scan. Deletion can't leave the tag-definition tree. | `test_update_deletes_dropped_tags`, `test_update_that_drops_one_tag_keeps_its_neighbours`, `test_remove`, `test_tag_deletion_cannot_escape_the_tag_definitions` |
| A33 | An install fails, and is recorded as failed, if Ignition doesn't load the project after the rescan. Generated resources carry no `lastModification` (8.3.7 skips a project whose `lastModification` has no timestamp). A tag with a null value isn't sent. | `test_install_fails_if_ignition_does_not_load_the_project`, `test_generated_resources_carry_no_lastmodification`, `test_publisher_parses_and_has_agent_url_and_token` |
| A34 | MQTT message handlers run on their own dispatch thread, never on paho's network thread, so a handler that publishes and waits for its PUBACK can't stall the link. | `test_shadow.py`: `test_iot_handlers_run_off_the_network_thread` |
| A35 | The interim `downloading` report is pinned to the shadow document version it was read at, so if AWS applies it after the final report it is rejected (409, not an error) instead of overwriting the final status. Every desired key the cloud sets, `remove: false` included, is mirrored so the delta clears. | `test_interim_report_is_pinned_to_document_version`, `test_version_conflict_on_update_is_not_an_error`, `test_install_from_inline_source` |
| A36 | A generated gateway serial is `auto-<12 hex>`, so the thing is `hyper-gw-auto-…`. | `test_provisioning.py` |
| A37 | The agent never answers a delta with a report the shadow already holds (`applied_at` ignored), so a delta that stays open (a refused restart, a removed IMS) can't start a delta→report loop. | `test_open_deltas_do_not_cause_a_report_loop`, `test_install_from_inline_source` |
| A38 | Fleet console API: serves the UI with a strict CSP; every change needs `X-Console-Request`; it only touches `hyper-gw-` things; publishing stores the package and manifest under the gateway's own prefix and sets an MQTT source; preview diffs against the current package; bad uploads, exports, timers and actions are refused; stale gateways show offline; presigned URLs are never echoed; labels, remove, retry, timer, rollout (one failure doesn't stop the rest), decommission (confirm, certificate revoked) and audit filtering all work. | `test_console.py` |
| A39 | Package server: the chunks reassemble exactly; it serves only packages stored under the requesting gateway's thing name, refuses malformed requests and objects that don't match their hash; the real agent installs a console package fetched over MQTT through it. | `test_package_server_*`, `test_agent_fetches_a_console_package_over_mqtt` |

## Verified outside the unit tests

Run once on the Kingdel lab gateway (Ignition 8.3.7):

* The real FWT-02 PPM edge-config export (25 device files) renders to one IMS with 3,901 tags.
* Every generated script compiles under the gateway's own Jython (`jython-ia-2.7.3.5.jar`), and
  the 358 KB metadata module loads.
* One full tick of 3,901 readings packs into a single 60 KB message.
* The agent container serves `/healthz`, accepts a telemetry post, and queues it. *(This was
  before round 1. Telemetry now also needs the ingest token, which is covered by
  `test_security.py`.)*

## End to end on AWS (sandbox, 2026-11-05)

Run on Kingdel against the sandbox account (379549361391, us-east-2), with the AWS side built by
the steps now in [`tools/aws_setup.sh`](../tools/aws_setup.sh) (the run named its roles and bucket
`hyper-gw-e2e-…`). For the test, the `hyper_telemetry` rule
writes each message to S3 (`telemetry/<thing>/…`), standing in for the consumer. The test IMSs
are synthetic exports whose tags are Ignition expression and memory tags, so they have values
without a PLC.

| # | Step | Result |
|---|---|---|
| E1 | Start a fresh container with only `GATEWAY_SERIAL=E2E-0001`. | Pass. First boot, API key and permissions took 32 s. The gateway provisioned itself as `hyper-gw-E2E-0001` (certificate ACTIVE, policy `hyper-gateway`) and connected about 55 s after start. |
| E2 | Health on the `gateway` shadow. | Pass. Reported every 60 s: Ignition RUNNING, link, outbox, certificate expiry, disk. |
| E3 | Assign IMS E2E-01 with `publish_ims.py` (S3 plus shadows). | Pass after fixes F1–F3. Installed 3 tags under `[default]Switch/Petersburg/ACI/E2E-01`, and the shadow reads `installed` with an empty delta. |
| E4 | Telemetry reaches the cloud through Basic Ingest. | Pass after F4. Payload-v2 messages arrive in S3 with live values, about one per second per IMS, and the outbox stays at 0. |
| E5 | Update E2E-01 (adds a tag) and add a second IMS, E2E-02. | Pass. Both installed within 2 s, and telemetry carries the new tag (9003) and the second IMS. |
| E6 | Network outage: container disconnected for 60 s. | Pass. 120 readings buffered, then replayed with no gap: 70 ticks in the 70 s window around the outage. |
| E7 | Remove E2E-02. | Pass. Its tags and timer are gone from Ignition, and its shadow reads `removed`. |
| E8 | Remote restart request with `ALLOW_REMOTE_RESTART` off. | Pass (refused). Reported as `restart_refused`; the desired key stays in the delta until the cloud clears it. |
| E9 | Image upgrade: new image, same volumes. | Pass. No first-boot steps and no re-provisioning; it reconnected as the same thing, and IMSs and tokens were kept. |
| E10 | Second container with no serial. | Pass after F5. It generated a serial and provisioned itself. |

Found and fixed during the run:

* **F1.** `publish_ims.py` presigned URLs on S3's global hostname. For a new bucket S3 redirects
  to the regional host, which breaks the signature (403). The tool now signs against
  `s3.<region>.amazonaws.com`.
* **F2.** A fast failure was overwritten by the interim `downloading` report, because AWS
  applied the two updates out of order. That report is now version-pinned (A35).
* **F3.** `remove: false` was never mirrored, so a delta stayed open forever (A35).
* **F4.** MQTT handlers ran on paho's network thread. A shadow delta handler waited there for
  its own PUBACK, which stalled the whole link for 10 s at a time. The outbox backed up (94
  messages) and timed-out publishes were sent twice. Handlers now have their own thread (A34).
* **F5.** A generated serial started with `gw-`, giving `hyper-gw-gw-…` (A36).

Fleet console, deployed to the same sandbox (stack `hyper-console`, allowlist 50.154.200.106/32):

| # | Step | Result |
|---|---|---|
| E11 | `tools/aws_setup.sh`, then `console/deploy.sh`, on a clean account. | Pass after F6. |
| E12 | The fleet list shows two fresh gateways, their labels and health. | Pass. |
| E13 | Publish two IMSs through the console: upload, then MQTT delivery. | Pass. Installed about 3 s after publishing. |
| E14 | Publish the real FWT-02 export (2.7 MB). | Pass. 31 chunks, 3,901 tags, installed 11 s after publishing. |
| E15 | In the UI: update with a diff, add, timer, rollout to two gateways, activity, decommission dialog (cancelled). | Pass, with screenshots in [CONSOLE.md](CONSOLE.md). |
| E16 | Refused remote restart. | Pass after F7. |

* **F6.** `aws cloudformation package` ignores `Globals.CodeUri`, so the Lambdas deployed
  without code. `CodeUri` is now set on each function.
* **F7.** A refused restart (and likewise a removed IMS) leaves a delta open, and AWS sends a
  delta after every accepted update. The agent answered each delta with an unchanged report,
  so it looped: about 4 shadow updates a second, measured live. It now reports only changes
  (A37), and the version stayed flat afterwards.

For the consumer: delivery is at least once, and the run saw duplicate ticks before F4. De-duplicate
on `(gw, ims, ts)`.

## Still open

* **HAMF Modbus addressing.** Real HAMF tags (`[device]3/<reg>` with no `valueSource`) become
  memory tags with null values in Ignition. The export needs OPC tags on a Modbus device
  connection. See the handbook.
* **The telemetry consumer** (rule action to `ts.telemetry`). The test used S3.
* **Presigned URL lifetime.** A URL signed with temporary (SSO or role) credentials dies when
  those credentials expire, whatever `--url-ttl` says. Whatever runs `publish_ims.py` in production
  should sign with a long-lived role. The gateway also retries only when the shadow changes, so
  an expired URL needs a re-publish.

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

## Changes from review round 3 (R-0006 to R-0011)

* **R3-1** (AEGIS, PR #3, Medium, inbox file handling). Export files are only parsed, never
  executed. The real gaps were unbounded reads and zip decompression and followed symlinks, and
  all three are closed (A20, A21). Tag and folder names that could change a tag path are
  refused (A20).
* **R3-2** (AEGIS, PR #3, Medium, API token validation). The Ignition API token is validated so
  it can't inject a header. Operator-supplied agent tokens are validated too (A22).
* **R3-3** (AEGIS, PR #6, Medium, tokens stored unencrypted). Tokens can now come from a
  secret store through `*_FILE` (A22). They aren't encrypted at rest inside the state volume:
  the key would have to sit beside them in that same volume, so it would add no protection. The
  volume is private to the agent's uid and the files are 0600.
* **R3-4** (AEGIS, PR #6, Low, no rotation). Added `gateway-agent rotate-tokens` (A23).

## Changes from review round 4 (R-0012 to R-0017)

* **R4-1, R4-2** (AEGIS, PR #4, Medium, package download TLS not verified). **No defect.** The
  download already used `ssl.create_default_context()`, which verifies certificates and
  hostnames. It is now set explicitly (and TLS 1.2 or later is required) in
  `download_tls_context()`, and a test shows a download from an untrusted certificate fails (A24).
* **R4-3** (AEGIS, PR #5, Medium, MQTT TLS not explicit). **No defect**, same default. It is now
  explicit in `iot.tls_context()`, requires TLS 1.2 or later, trusts only the configured CA, and
  is tested (A24).
* **R4-4** (AEGIS, PR #5, Medium, possible SQL injection). **No defect.** Every query was already
  parameterised. A syntax-tree test now enforces it, and injection payloads are tested as data (A25).
* **R4-5** (AEGIS, PR #5, Medium, telemetry input validation). **A real defect:** a `NaN` or
  `Infinity` reading was accepted and written out as invalid JSON, which would make the cloud
  reject the whole message. Such readings are now dropped and counted as bad, the encoder refuses
  non-finite values, and every field is range- and format-checked (A26).
* **R4-6** (AEGIS, PR #7, Medium, `rotate-tokens` may expose tokens). **No defect:** only token
  names are printed. Now tested (A27).
* **R4-7** (AEGIS, PR #7, Low, secrets in logs). Nothing logged a token, but there was no
  safeguard. A redaction filter now masks every configured secret in all logs (A27).
* **Low items on PR #2 and PR #6** described existing controls (escaped literals, name
  validation, token format check, auth check, body-size limit) with "no action required". They
  are recorded as confirmations.
* The PR #3 security review stalled on an escalation claiming its test document was missing.
  The document was at the stated path, and the escalation was answered so the review resumed.

## Changes in 0.2.0: single hands-free image

* One image (Ignition 8.3.7 + agent) that installs itself (A29), provisions its AWS identity
  (A30), and takes IMSs from the cloud through Device Shadows (`tools/publish_ims.py`).
* REST calls checked against the gateway's real `/openapi.json`. Tag deletion is reworked
  because 8.3.7 has no REST delete (A31, A32).
* Three defects found by booting the image on a live gateway, all fixed (A33):
  1. Generated `resource.json` files without a `lastModification` timestamp made Ignition
     skip the project. A rescan still returned 200, so the installer now verifies the project
     loaded.
  2. Null tag values were sent as the string `"None"`.
  3. The import body format was wrong for 8.3.7.
* Verified live on Kingdel, in a throwaway container from empty volumes:
  * hands-free boot in about 55 s;
  * the real FWT-02 export installs, its timer runs every second, and readings queue;
  * tag deletion by file plus config scan, and project hot-load, both work.

## Changes from review round 5 (R-0019 to R-0024)

All twelve reviews passed. AEGIS's pass on PR #7 still listed two Medium items, which were
addressed:

* **R5-1** (`rotate-tokens` storage). Tokens were already written atomically with mode 0600
  (A23). Now a failed write removes its temp file and keeps the old secret, and every write is
  read back and its mode checked before success is reported (A28).
* **R5-2** (`check-iot` "URL scheme"). `IOT_ENDPOINT` is a hostname, not a URL, so CWE-601 doesn't
  apply. It is now validated as a bare hostname, along with the port and the client id, before
  any connection (A28).
* The other Low items on PR #3 and PR #5 confirmed existing controls ("no action required").

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
