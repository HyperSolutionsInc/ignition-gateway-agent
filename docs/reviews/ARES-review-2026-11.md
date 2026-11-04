# gateway-agent: ARES review report

Test and security review by ARES//GRID of **gateway-agent**
(`HyperSolutionsInc/ignition-gateway-agent`), the sidecar that turns HAMF IMS exports into
Ignition tag imports and timer scripts, sends telemetry to AWS IoT Core, and takes
configuration from Device Shadows.

## Outcome

**Accepted as clean after six rounds (2026-11-04).**
* Round 5 was the first where all twelve reviews passed (QA and security, six areas).
* Round 6, a full re-review of the round 5 fixes, had **PROBE pass all six areas** (147 tests,
  no defects, no coverage gaps) and raised **no actionable findings**. Its
  remaining items restate controls that are already in place and tested; each is assessed in
  the round 6 section.
* Jack accepted the result as clean on that basis.

| | Start (round 1) | Final (`0aee0c2`) |
| --- | --- | --- |
| Automated tests | 47 | **147**, all passing |
| Bandit | 4 Medium | **no issues** (5 `# nosec` markers, each justified in `docs/TEST-PLAN.md`) |
| pip-audit | not run (no requirements file) | **no known vulnerabilities** (pinned `requirements.txt`) |
| Acceptance criteria in the test document | 15 | **28**, each mapped to tests |

### Real defects found and fixed

1. **The local management API had no authentication** (round 1, AEGIS). Anyone who could reach
   the port could install or remove IMS packages, which rewrites gateway tags and scripts. Now
   every endpoint but `/healthz` needs a token, with an admin token split from a
   telemetry-only ingest token.
2. **`NaN`/`Infinity` sensor readings produced invalid JSON** (round 4, AEGIS). One faulted
   reading would have made the cloud reject the whole telemetry message. Such readings are now
   dropped, counted as bad, and can't be serialised.

### Hardening prompted by the review

* http(s)-only URL handling. Package downloads are HTTPS-only, including on redirects.
* Injection-safe generated Jython.
* Input limits and zip-bomb protection on HAMF exports, plus tag-name validation.
* The inbox refuses symlinks and non-regular files.
* Header-safe token validation.
* Secrets can come from Docker/Kubernetes secrets, and tokens can be rotated with verified
  writes.
* TLS 1.2+ with certificate and hostname verification pinned explicitly.
* An AST-enforced parameterised-SQL check.
* Full telemetry validation.
* Secret masking in logs.
* IoT endpoint validation.

### False positives, with the evidence recorded

Six findings were judged **not defects** after checking the code. In each case the control
already existed; it was made explicit and a test was added that proves it:
* TLS "not verified", twice for downloads and once for MQTT. A test shows an untrusted
  certificate is rejected.
* SQL injection.
* `rotate-tokens` exposing tokens.
* An IoT endpoint "URL scheme".

### What this review does not cover

* The **live Ignition 8.3 REST endpoints**: the gateway needs an API key first.
* The **live AWS IoT link, shadows and Basic Ingest rule**: the device certificate is
  rejected by AWS, and the rule isn't created yet.
* The **Jython publisher running inside a live gateway**: it is compile-checked under the
  gateway's Jython, but only that.

### On the review process

* **Model context.** The reviewers run on a local 14B model with a 16K-token context. Rounds 1–2
  sent the whole project in one diff, which overflowed the context, so the reviewers lost
  their instructions and filed no real verdicts. From round 3 the project was split into six
  per-area reviews sized to fit.
* **Escalations.** Two reviewer escalations were false alarms: a "missing" test document that
  was present, and a transient network error. Both were answered with no changes to network
  or access.
* **Noise.** By round 6 the model mostly restated the test document as findings, so a literally
  empty findings list may never appear. That's why the exit criterion was settled as no
  actionable findings.
* **Grid changes made for this review** (in the ARES//GRID repo):
  * `ignition-gateway-agent` added to the reviewed repos;
  * `/api/reviews` now accepts the author's test document (`c300cf0`).
  * Worth knowing: the Grid's pip-audit step only scans `requirements*.txt`.

---

* **Reviewers:** PROBE (QA and test), AEGIS (security), on the ARES//GRID local model (qwen3:14b)
* **Review vehicle:**
  * rounds 1–2: [PR #1](https://github.com/HyperSolutionsInc/ignition-gateway-agent/pull/1), the whole project in one diff;
  * from round 3: six per-area PRs, #2–#7, which together cover every file each round (see Round 2)
* **Full reviewer reports:** Obsidian `Team/Reviews/ignition-gateway-agent PR<n>/` (PR1–PR7), and as comments on each PR
* **Exit criterion:** both reviewers pass all six areas, with no actionable findings left
  (settled after round 6, see Outcome)

## Summary by round

| Round | Review | Head | PROBE (QA) | AEGIS (security) | Fix commit |
| --- | --- | --- | --- | --- | --- |
| 1 | R-0004 | `c904554` | changes_requested | changes_requested (no structured report) | `19e3be0` |
| 2 | R-0005 | `0576b4c` | no verdict filed | no verdict filed | none (no findings) |
| 3 | R-0006–R-0011 | `19e3be0` (main), 6 areas | 5 pass, 1 no verdict (#5) | 4 pass, 2 with findings (#3 changes_requested, #6 pass with Medium/Low) | `7bbaa26` |
| 4 | R-0012–R-0017 | `7bbaa26` (main), 6 areas | **6 pass** | 3 pass; changes_requested on #4, #5, #7 | `9d6cd24` |
| 5 | R-0019–R-0024 | `9d6cd24` (main), 6 areas | **6 pass** | **6 pass**; PR #7 pass still listed 2 Medium | `0aee0c2` |
| 6 | R-0025–R-0030 | `0aee0c2` (main), 6 areas | **6 pass** (#6 after escalation E-0046) | 4 pass, #2 changes_requested (restated controls), #6 recorded changes_requested (its report concludes pass) | none: no actionable findings |

---

## Round 1: R-0004 (`c904554`, 2026-11-03)

### What the reviewers reported

**PROBE: changes_requested.** It built the environment and ran the suite: **47 passed**.
It reported **no defects**. It marked A1–A15 "not testable here" only because the Grid told it
*"No test document was filed by the author"*. The test document existed (`docs/TEST-PLAN.md`,
linked in the PR), but the Grid's `/api/reviews` endpoint had no way to attach one.

**AEGIS: changes_requested by default.** It ended without calling `submit_review`, so there's
no structured security report. Its notes summarise the design. The one actionable item is to
**add authentication to the local HTTP API**, which the test document had already flagged as
security-relevant surface.

### Independent run of AEGIS's toolchain

The Grid doesn't keep tool output, so the same scans were run by hand against `c904554`:

| Tool | Result |
| --- | --- |
| Bandit 1.8.6 | 4 Medium: B310 `urlopen` accepts any scheme at `cli.py:197`, `ignition.py:101`, `shadow.py:344`; B104 bind all interfaces at `config.py:47` |
| pip-audit | **Never ran in the Grid scan**, because the Grid only audits `requirements*.txt` and dependencies were declared in `pyproject.toml`. Run by hand against the resolved runtime dependency (paho-mqtt 2.1.0): no known vulnerabilities |
| Secret scan | No matches |

### Findings and resolution

| # | Source | Severity | Finding | Resolution |
| --- | --- | --- | --- | --- |
| 1-1 | AEGIS | High (unauthenticated management API) | The local HTTP API accepted package installs and removals, which rewrite gateway tags and scripts, from anyone who could reach the port | Every endpoint except `/healthz` now needs a token. The **admin token** guards packages and status. A separate **ingest token** is the only thing written into the gateway project, and it grants `/v1/telemetry` only. Constant-time comparison. Tokens are generated once with 32 random bytes and mode 0600 if not configured. The server refuses to start without both |
| 1-2 | Bandit B310 ×3 | Medium | URL fetches accepted any scheme (`file:`, `ftp:`) | Ignition and agent URLs are validated as http(s). Package downloads are HTTPS-only (opt-out flag for labs), and a custom redirect handler refuses any redirect to another scheme. The stdlib already blocked `file:` but allowed `ftp:` and HTTPS→HTTP |
| 1-3 | Bandit B104 | Medium | Binds 0.0.0.0 | Kept: the agent must listen on the container interface for Ignition to reach it. Compose publishes on loopback only, and 1-1 now puts every endpoint behind a token. Justified in code |
| 1-4 | Review of 1-1 | Medium | The new ingest token and the agent URL are substituted into generated Jython, an injection path | Both are validated (token `[A-Za-z0-9_-]{16,128}`; URL `http(s)://host[:port]`) and inserted as escaped literals. Tests try code-injection payloads |
| 1-5 | Toolchain gap | Low | pip-audit couldn't see the dependencies | Added a pinned `requirements.txt`. The image installs from it (reproducible builds), and pip-audit scans it |
| 1-6 | PROBE | Process | No test document reached the reviewers | The test document is filed in Obsidian with the review. The Grid's `/api/reviews` now accepts `test_doc` (Grid commit `c300cf0`) |

**Also added:** the startup refresh rewrites the shared publisher and rescans the gateway when
the ingest token or URL changes, which makes token rotation work. `render` writes a placeholder
instead of the live token.

### Verification of the fixes

* Tests: **75 passed** (47 → 75). New `tests/test_security.py` covers each control, plus
  injection cases in `test_generator.py`. Acceptance criteria A16–A19 were added to the test
  document.
* Bandit: **no issues**. The 5 `# nosec` markers each have a written justification, listed in
  `docs/TEST-PLAN.md`. pip-audit on `requirements.txt`: **no known vulnerabilities**.
* Container on the Kingdel lab host:
  * the image builds;
  * `/healthz` returns 200, and `/v1/status` without a token returns 401;
  * the CLI inside the container authenticates with the generated token;
  * the token files are `600 agent`;
  * the ingest token is accepted on telemetry and rejected on `/v1/packages`.
* Commits: `19e3be0` on `main`, merged to `ares/review` as `0576b4c`.

### Notes on the review process

* AEGIS's round 1 output was a design summary rather than a security review. Its 16K context
  probably couldn't hold the full-project diff (about 120 KB, truncated by the Grid at 60 KB).
  The fixes above therefore also draw on running AEGIS's toolchain by hand.
* The Grid's pip-audit step only looks for `requirements*.txt`. Projects that declare
  dependencies only in `pyproject.toml` get no dependency audit.

---

## Round 2: R-0005 (`0576b4c`, 2026-11-03)

The round 1 fixes were sent back with the test document attached, so this was the first round
where the reviewers had it.

### What the reviewers reported

**Neither PROBE nor AEGIS filed a verdict.** Both ended without calling `submit_review`, so the
Grid recorded `changes_requested` by default. **No findings were raised.** Both final notes
summarise the round 1 fixes as present and sound:
* separate admin and ingest tokens, constant-time comparison, 0600 token storage;
* http(s)-only URL validation;
* escaped values in generated Jython;
* Bandit and pip-audit clean.

They also repeat the test document's not-testable-yet list.

### Why no verdict: review method, not code

The whole-project diff is about 120 KB, truncated by the Grid to 60 KB, which is roughly 15K
tokens. The reviewers run with `num_ctx: 16384`. When the diff arrives, Ollama drops the oldest
context to make room, and that includes the task's workflow instructions and the
`submit_review` step. The model then summarises "the provided content" instead of finishing
the review. Both rounds show exactly this pattern. Resending the same PR would fail the same way.

### Change to the review method (round 3 onward)

The project is reviewed as **six per-area PRs**. Each one's diff is only that area, at
11–22 KB (about 3–6K tokens). Its base branch holds the rest of the project, so the checkout is
complete and PROBE runs the full suite. A build script checks that every non-test, non-doc file
is in exactly one area each round. Each PR gets its own test document with only that area's
acceptance criteria and review focus.

| PR | Area | Files |
| --- | --- | --- |
| #2 | HAMF parsing and script generation | `hamf.py`, `generator.py`, `ignition_scripts/publisher.py` |
| #3 | Gateway install, REST client, inbox | `installer.py`, `ignition.py`, `inbox.py` |
| #4 | Device Shadow | `shadow.py` |
| #5 | MQTT link, telemetry, outbox | `iot.py`, `telemetry.py`, `store.py` |
| #6 | HTTP API, config, deployment | `server.py`, `config.py`, `Dockerfile`, `deploy/*`, `docs/aws-iot-policy.json`, ignore files |
| #7 | CLI and packaging | `cli.py`, entry points, `pyproject.toml`, `requirements.txt` |

PR #1 was closed with a pointer to #2–#7. No code changed in this round.

---

## Round 3: R-0006 to R-0011 (`19e3be0`, six areas, 2026-11-03)

This was the first per-area round. Every reviewer but one filed a structured verdict, which
confirms the context-size diagnosis from round 2.

| PR | Area | Review | PROBE | AEGIS |
| --- | --- | --- | --- | --- |
| #2 | HAMF parsing & generation | R-0006 | ✅ pass | ✅ pass, no findings |
| #3 | Install, REST, inbox | R-0007 | ✅ pass | ⚠️ changes_requested, 2 Medium |
| #4 | Device Shadow | R-0008 | ✅ pass | ✅ pass, no findings |
| #5 | MQTT link, telemetry, outbox | R-0009 | no verdict filed (empty notes) | ✅ pass, no findings |
| #6 | HTTP API, config, deploy | R-0010 | ✅ pass | ✅ pass, 1 Medium and 1 Low listed |
| #7 | CLI & packaging | R-0011 | ✅ pass | ✅ pass, no findings |

Every QA report that filed a verdict found **no defects and no coverage gaps**.

### Findings and resolution

AEGIS's pass on #6 still listed findings. You asked for every finding to be fixed, so they
were fixed as well.

| # | Source | Severity | Finding | Assessment | Resolution |
| --- | --- | --- | --- | --- | --- |
| R3-1 | AEGIS #3 (`inbox.py`) | Medium | Inbox files aren't validated before processing | Files are only parsed as JSON/zip, never executed. On inspection the real gaps were: no size cap on inbox files, symlinks were followed, and zip members were decompressed without limits (zip-bomb risk). A related gap: tag/folder names could contain `/` or `[`, which would change a tag path | 64 MB export cap. At most 500 zip members, 256 MB uncompressed across all members, enforced by bounded reads that ignore declared sizes. Corrupt zip → clean error. Inbox refuses symlinks and non-regular files without reading them (`O_NOFOLLOW` + `fstat`), refuses oversize files, and is mode 0700. Unsafe names (`/ [ ] . ..`, control characters, edge whitespace, more than 255 characters) refused. The real export still passes |
| R3-2 | AEGIS #3 (`ignition.py`) | Medium | The API token isn't validated | Valid: a token containing CR/LF could inject a request header | The Ignition token must be 8–1024 printable characters with no whitespace. Operator-supplied agent tokens must be 16–128 URL-safe characters |
| R3-3 | AEGIS #6 (`config.py`) | Medium | Tokens are stored unencrypted at rest | Encrypting them inside the state volume would need a key kept in the same volume, so it adds no protection. The real improvement is to support an external secret store | Any token can be read from `<NAME>_FILE` (Docker/Kubernetes secrets); a compose secrets block is documented. Generated tokens stay 0600 in a volume private to the agent's uid. The decision is recorded in the test document |
| R3-4 | AEGIS #6 (`config.py`) | Low | No token rotation | Valid | `gateway-agent rotate-tokens [--admin] [--ingest]`: atomic 0600 writes, and it refuses externally managed tokens. On restart, the new ingest token is written into the gateway publisher |

### Verification of the fixes

* Tests: **103 passed** (75 → 103). New `tests/test_hardening.py` covers each control; A20–A23
  were added to the test document.
* Bandit: no issues. pip-audit: no known vulnerabilities.
* Kingdel: the image builds; the real FWT-02 export renders all 3,901 tags under the stricter
  name rules; `rotate-tokens` works in the container.
* Commit `7bbaa26` on `main`. Round 4 re-reviews **all six areas**, not just the two with
  findings, so the final clean result covers the shipped code end to end.

---

## Round 4: R-0012 to R-0017 (`7bbaa26`, six areas, 2026-11-04)

All six areas were re-reviewed after the round 3 fixes.

| PR | Area | Review | PROBE | AEGIS |
| --- | --- | --- | --- | --- |
| #2 | HAMF parsing & generation | R-0012 | ✅ pass | ✅ pass (2 Low items, both confirmations of existing controls) |
| #3 | Install, REST, inbox | R-0013 | ✅ pass | ✅ pass, no findings (after escalation E-0045 was answered, see below) |
| #4 | Device Shadow | R-0014 | ✅ pass | ⚠️ changes_requested, 2 Medium |
| #5 | MQTT link, telemetry, outbox | R-0015 | ✅ pass | ⚠️ changes_requested, 3 Medium |
| #6 | HTTP API, config, deploy | R-0016 | ✅ pass | ✅ pass (3 Low items, all confirmations, "no action required") |
| #7 | CLI & packaging | R-0017 | ✅ pass | ⚠️ changes_requested, 1 Medium and 1 Low (the report body concludes "pass", but the recorded verdict is changes_requested) |

**PROBE passed all six areas, with no defects and no coverage gaps**, including PR #5, which had
no verdict in round 3. The round 3 fixes on #3 and #6 were accepted by AEGIS.

**Process note:** AEGIS stalled on PR #3 with escalation E-0045, saying the test document was
"not found". It was at exactly the path the escalation named. Once the escalation was answered
with the path and a summary, the review resumed and passed. This is a false negative from the
local model, not a missing artefact.

### Findings and resolution

| # | Source | Severity | Finding | Assessment | Resolution |
| --- | --- | --- | --- | --- | --- |
| R4-1, R4-2 | AEGIS #4 (`shadow.py`) | Medium | Package download doesn't verify TLS certificates | **Not a defect.** It already used `ssl.create_default_context()`, which verifies the certificate and hostname by default | Made explicit in `download_tls_context()` (`CERT_REQUIRED`, `check_hostname`, TLS 1.2 or later). A test shows a download from a server with an untrusted certificate fails with `CERTIFICATE_VERIFY_FAILED` |
| R4-3 | AEGIS #5 (`iot.py`) | Medium | MQTT connection doesn't explicitly verify the server | **Not a defect** (same default) | Made explicit in `iot.tls_context()`: `CERT_REQUIRED`, `check_hostname`, TLS 1.2 or later, and only the configured CA (Amazon root) is trusted. Tested |
| R4-4 | AEGIS #5 (`store.py`) | Medium | Possible SQL injection in the outbox | **Not a defect.** Every query was already parameterised | A syntax-tree test enforces that every `execute` takes a constant SQL string, and injection payloads round-trip as data |
| R4-5 | AEGIS #5 (`telemetry.py`) | Medium | Telemetry input isn't fully validated | **Real defect found.** A `NaN` or `Infinity` reading (for example from a faulted sensor) was accepted and serialised as invalid JSON, which would make the cloud reject the whole message | Non-finite readings are dropped and counted as bad, and the encoder uses `allow_nan=False`. Every field is now validated: IMS/version format, timestamp range, positive 64-bit `tag_id`, value type and string length, and per-post tick and reading limits |
| R4-6 | AEGIS #7 (`cli.py`) | Medium | `rotate-tokens` may expose tokens | **Not a defect.** Only token names are printed | Tested: no token value appears on stdout or stderr |
| R4-7 | AEGIS #7 (`cli.py`) | Low | Sensitive values may be logged | Nothing logged a token, but there was no backstop | A logging filter masks every configured secret in messages, arguments and tracebacks. Tested |
| — | AEGIS #2, #6 | Low | Five items | All describe existing controls (escaped literals, name validation, token format check, auth check, body-size limit) with "continue" or "no action required" | Recorded as confirmations; no change |

### Verification of the fixes

* Tests: **137 passed** (103 → 137). New `tests/test_round4.py` covers each item; A24–A27 were
  added to the test document, along with the evidence for each finding judged not a defect.
* Bandit: no issues. pip-audit: no known vulnerabilities.
* Commit `9d6cd24` on `main`. Round 5 re-reviews all six areas.

---

## Round 5: R-0019 to R-0024 (`9d6cd24`, six areas, 2026-11-04)

**First fully passing round: all twelve reviews returned `pass`.** Every QA report found no
defects and no coverage gaps. Security reports for #2, #4 and #6 listed no findings; those for
#3 and #5 listed only confirmations of existing controls (the render placeholder marker, the
TLS 1.2 minimum), both "no action required".

Because the goal is that every finding gets fixed, the round wasn't treated as clean: AEGIS's
pass on PR #7 still listed two Medium items.

| # | Source | Severity | Finding | Assessment | Resolution |
| --- | --- | --- | --- | --- | --- |
| R5-1 | AEGIS #7 (`cli.py` rotate-tokens) | Medium | Rotation doesn't handle new tokens not being stored securely | Largely a repeat of R3-3/R3-4: writes were already atomic with mode 0600 and tested. The real residual gap was that a failed write could leave a temp file behind, and success wasn't verified | `_write_secret` removes its temp file and keeps the old secret on failure, fsyncs before the swap, and reads the file back to check its content and 0600 mode before reporting success. Tested with a simulated disk failure and a filesystem that ignores the mode |
| R5-2 | AEGIS #7 (`cli.py` check-iot) | Medium | IoT endpoint URL scheme isn't validated | `IOT_ENDPOINT` is a hostname, not a URL, so open redirect (CWE-601) doesn't apply. Validating it still catches misconfiguration | `IOT_ENDPOINT` must be a bare RFC 1123 hostname (no scheme, port or path), `IOT_PORT` must be 1–65535, and the client id must be AWS-valid, all checked before any connection. `run` and `check-iot` report this cleanly |

### Verification

* Tests: **147 passed** (137 → 147); A28 was added to the test document.
* Bandit: no issues. pip-audit: no known vulnerabilities.
* Commit `0aee0c2` on `main`. Round 6 re-reviews all six areas.

---

## Round 6: R-0025 to R-0030 (`0aee0c2`, six areas, 2026-11-04)

| PR | Area | Review | PROBE | AEGIS |
| --- | --- | --- | --- | --- |
| #2 | HAMF parsing & generation | R-0025 | ✅ pass | ⚠️ changes_requested, 2 Medium (restatements, see below) |
| #3 | Install, REST, inbox | R-0026 | ✅ pass | ✅ pass (1 Low: placeholder-token confirmation) |
| #4 | Device Shadow | R-0027 | ✅ pass | ✅ pass (lists acceptance criteria A14/A15 as "Medium" with "continue with the current implementation") |
| #5 | MQTT link, telemetry, outbox | R-0028 | ✅ pass | ✅ pass (lists A24–A26 as "Medium" with "ensure…" remediations) |
| #6 | HTTP API, config, deploy | R-0029 | ✅ pass (147 tests, no defects or gaps; resumed after escalation E-0046) | recorded changes_requested; **the report body's verdict is "pass"**, and its only finding is "no critical or high-severity issues found" |
| #7 | CLI & packaging | R-0030 | ✅ pass | ✅ pass, no findings |

### Assessment: no actionable findings

| Item | What it says | Why no code change |
| --- | --- | --- |
| #2 ID 1 | Agent URL and ingest token inserted into Jython "could inject code if not validated", via "a malicious HAMF export" | They are validated (`http(s)://host[:port]`; `[A-Za-z0-9_-]{16,128}`) and inserted as escaped literals, which is the requested remediation, with injection tests (A18). Neither value comes from a HAMF export; both are agent configuration |
| #2 ID 2 | `_check_name` "does not properly validate" names | It refuses `/ [ ] . ..`, control characters, edge whitespace and more than 255 characters, with tests of attack names and real-world names (A20). The finding names no input that gets through |
| #4, #5 | Acceptance criteria A14, A15, A24, A25, A26 copied in as "Medium" findings | Each remediation is "continue" or "ensure" for something already in place and tested |
| #6 security | Verdict recorded as changes_requested | The report's own verdict is pass, with no findings. The structured verdict the model submitted contradicts its text |

**Process notes:**
* By round 6 the reviewers mostly re-describe the test document's controls as findings. This is a
  limit of the local model, and it means a literally empty findings list may never appear.
* PR #6's QA review was blocked by escalation E-0046 ("network error" on checkout, asking to
  adjust network settings). GitHub was reachable and the PR head was already fetched, so the
  answer was to retry unchanged; **no network or install changes were approved**.
