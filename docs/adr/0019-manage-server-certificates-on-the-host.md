# 19. Manage server certificates on the host

Date: 2026-10-31
Status: Proposed
Decision authority: Project maintainer; implementation requested in [#697](https://github.com/blindndangerous/AutoDJ/issues/697), architectural acceptance pending.

## Context

Voice-track recording from another device requires trusted HTTPS. The operator
needs to create or import a certificate, select it, and install its public copy
on recording devices. Certificates and browser pairings have separate lifecycles.

## Decision

Keep a bounded inventory of up to 20 certificate/key pairs in
`<index_dir>/.tls`, shared by all stations under [ADR 5](0005-a-station-owns-its-playout-chain.md).
Each pair has an immutable random identifier. An atomically written inventory
selects one active pair. Private-file helpers protect the inventory, certificates
and keys with owner-only permissions on POSIX and Windows. These files stay
outside application backups, like the existing access-token and pairing files.

The saved selection overrides TOML certificate paths when configuration loads.
Explicit serve CLI certificate flags still override that selection for that run.
An externally configured pair remains externally owned and available for renewal;
the web page never removes its files. The operator can import a copy to manage it.

Creation, import, activation and deletion require a direct native-mode localhost
request: loopback socket peer, local URL, and no forwarding headers. Existing
browser authentication and Host/Origin policy still apply. Remote browsers can
read public metadata. Public certificate download requires direct localhost or
native HTTPS. A proxy's claimed identity cannot grant host certificate authority.

Use `cryptography` for platform-independent certificate creation and inspection.
Generate RSA-2048, SHA-256, one-year self-signed server certificates with explicit
DNS/IP subject alternative names. Imported certificates retain their real dates.
Check the key, validity, server usage and management hostname before activation.
Store private keys only on the host; the download endpoint serializes only the
public leaf certificate even if an external PEM contains other material.

Existing HTTPS listeners switch their live SSL context and renewal watcher to
the selected pair. Open connections retain their original context handshake.
Initial HTTP-to-HTTPS setup requires a restart so listener protocol, cookies and
origin policy change together. The wizard explains that before selection and
completes only in a secure HTTPS browser. Selection survives restart. Failed
activation restores the previous selection; the active pair cannot be deleted.

When the existing restart service reports support, setup offers **Restart now**
through the shared [restart flow](0016-update-and-restart-from-the-web.md).
The browser probes HTTPS on the same host and listening port, with normal TLS
verification and no credentials, before navigating to resume the Trust step.
The usual confirmation, restart authorization and rate limits still apply.

## Alternatives and consequences

Replacing a single PEM pair hides prior certificates and complicates rollback.
Multiple simultaneous SNI certificates add hostname routing that this LAN setup
does not need. Keeping several saved pairs with one active pair supports planned
renewal and rollback without changing listener routing.

Shelling out to OpenSSL would depend on an additional executable and platform
quoting. The cryptography dependency supplies the same creation and parsing API
on supported platforms. Client trust installation remains an OS/browser operation.
The page provides a guide; it cannot remove trust from another device.

Revisit host-only management if authenticated remote administration or multiple
server processes sharing this inventory become supported requirements. As with
other private state, atomic replacement does not establish power-loss durability
on every filesystem. An administrator changing files outside AutoDJ owns that
coordination.

## Current contracts

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0019-01` | Selection survives configuration loading and active files cannot be deleted. | automated | `tests/integration/test_certificates_api.py::test_local_setup_persists_selection_and_download_contains_only_public_certificate` |
| `ADR-0019-02` | Certificate download contains only public certificate material. | automated | `tests/integration/test_certificates_api.py::test_local_setup_persists_selection_and_download_contains_only_public_certificate` |
| `ADR-0019-03` | Remote and forwarded requests cannot change certificates or bootstrap trust over HTTP. | automated | `tests/integration/test_certificates_api.py::test_remote_or_forwarded_requests_cannot_manage_or_bootstrap_trust` |
| `ADR-0019-04` | Trust installation and microphone recording work on the chosen client OS/browser. | manual | Follow the HTTPS setup in docs/guides/operations/network.md on a second device; record OS, browser, trust installation, HTTPS status, microphone permission and recording result. |
| `ADR-0019-05` | HTTPS restart uses the existing confirmation and waits for its validated destination before navigation. | automated | `tests/jsmodules/updates.test.js::can restart into the validated HTTPS setup URL after its health check answers`; `tests/jsmodules/updates.test.js::does not request a restart for an invalid HTTPS destination`; `tests/jsmodules/updates.test.js::does not navigate when the HTTPS restart is refused` |
| `ADR-0019-06` | Configuration preserves saved HTTPS selection without certificate parsing dependencies; missing required packages refuse serving while diagnosis remains available. | automated | `tests/unit/test_runtime_packages.py::test_config_keeps_saved_https_without_cryptography`; `tests/unit/test_runtime_packages.py::test_serve_refuses_before_configuration_or_server_start` |
| `ADR-0019-07` | Unusable configured or selected HTTPS refuses startup before persistent writes, locks or listeners; certificate dates only warn so HTTPS renewal stays reachable. Explicit TLS overrides retain precedence. | automated | `tests/unit/test_serve_refusal.py::test_unusable_https_refuses_before_writes_and_doctor_agrees`; `tests/unit/test_serve_refusal.py::test_explicit_valid_tls_overrides_an_unusable_saved_selection`; `tests/unit/test_serve_refusal.py::test_certificate_dates_warn_without_disabling_https` |
| `ADR-0019-08` | DNS-01 creates a separate Cloudflare TXT record and removes only its owned record; CNAME delegation is refused. | automated | `tests/unit/test_acme_dns.py::test_owned_txt_cleanup_and_cname_refusal` |
| `ADR-0019-09` | Current directory terms and uncertain hostname reachability require explicit consent before registration or issuance. | automated | `tests/unit/test_acme_service.py::test_terms_and_host_confirmation_before_registration` |
| `ADR-0019-10` | Staging results never enter the certificate inventory; temporary certificate keys and unreferenced credentials are removed. | automated | `tests/unit/test_acme_worker.py::test_staging_never_enters_inventory_and_secrets_are_removed` |
| `ADR-0019-11` | A renewal adds an inventory pair without overriding a manual selection made during issuance. | automated | `tests/unit/test_acme_worker.py::test_renewal_respects_manual_selection` |
| `ADR-0019-12` | ACME mutations retain direct native loopback and existing browser Origin checks. | automated | `tests/integration/test_acme_api.py::test_acme_local_policy_rejects_remote_forwarded_and_bad_origin` |
| `ADR-0019-13` | Renewal uses ARI when available, otherwise two thirds of actual validity with jitter that only advances the time. | automated | `tests/unit/test_acme_service.py::test_renewal_schedule_uses_actual_validity_and_ari` |
| `ADR-0019-14` | Rolling daily budgets survive reconstruction; retries back off at 15 minutes, 1 hour, 6 hours and 24 hours and honor later Retry-After. | automated | `tests/unit/test_acme_service.py::test_persistent_budget_and_retry_schedule` |
| `ADR-0019-15` | One supervised child admits one operation, has a 15-minute deadline, and receives no secrets in its arguments. Waiting for child termination does not hold the management lock. | automated | `tests/unit/test_acme_worker.py::test_child_has_one_slot_deadline_and_no_secret_arguments`; `tests/unit/test_acme_review.py::test_slow_child_reaping_does_not_hold_management_lock` |
| `ADR-0019-16` | Doctor reports saved failures without issuance, DNS writes or secret disclosure. | automated | `tests/unit/test_acme_worker.py::test_doctor_acme_is_read_only_and_redacted` |
| `ADR-0019-17` | An order receipt survives a later authorization-fetch failure; retry resumes that order. | automated | `tests/unit/test_acme_client.py::test_library_resume_uses_saved_order_after_auth_fetch_failure` |
| `ADR-0019-18` | Let's Encrypt remains an option in the existing two-step modal with staging first, private native inputs, associated field guidance and one status announcement per action. | automated | `tests/jsmodules/secure-connections-acme.test.js::offers ACME third in the existing wizard with staging first and private native inputs`; `tests/jsmodules/secure-connections-acme.test.js::associates zone format and token permissions with their inputs`; `tests/jsmodules/secure-connections-acme.test.js::announces successful preflight exactly once while retaining heading focus` |
| `ADR-0019-19` | A real test CA validates DNS-01 issuance and owned cleanup, including an interrupted cleanup retry. | automated | `tests/integration/test_acme_pebble.py::test_pebble_dns01_issuance_and_owned_cleanup` |
| `ADR-0019-20` | A successful certificate delete remains successful when one lineage needs cleanup; startup and saved retries sweep unreferenced lineages independently. | automated | `tests/unit/test_acme_review.py::test_deleted_lineage_sweep_is_independent_and_retryable`; `tests/unit/test_acme_review.py::test_startup_sweeps_lineage_after_interrupted_certificate_delete` |
| `ADR-0019-21` | Releasing pending DNS cleanup requires confirmation, logs owned record IDs, and leaves manual-removal instructions. | automated | `tests/unit/test_acme_review.py::test_cleanup_release_is_confirmed_logged_and_unblocks_removal` |
| `ADR-0019-22` | Successful renewal retains the active pair, its predecessor and any pair still served pending restart, pruning only older pairs in that lineage. | automated | `tests/unit/test_acme_review.py::test_successful_renewal_keeps_only_active_and_previous_in_its_lineage`; `tests/integration/test_acme_api.py::test_renewal_pruning_keeps_the_pair_still_served_until_restart` |
| `ADR-0019-23` | Failure to establish child process ownership terminates and reaps the spawned child before another tick. | automated | `tests/unit/test_acme_review.py::test_job_setup_failure_terminates_and_reaps_child` |
| `ADR-0019-24` | Each authorization retains its own cleanup receipt; one failed cleanup does not prevent cleanup of other owned records. | automated | `tests/unit/test_acme_client.py::test_two_authorizations_keep_every_receipt_and_cleanup_independently` |
| `ADR-0019-25` | Publication failure reports the observed saved selection, including failure to persist an activation rollback. | automated | `tests/unit/test_acme_review.py::test_activation_failure_message_matches_saved_selection` |
| `ADR-0019-26` | New ACME controls work with the supported browser/screen-reader combinations and preserve one announcement per action. | manual | Run tests/playwright/acme_audit.mjs for automated keyboard, axe, theme and reflow checks. Follow docs/accessibility-testing.md with a supported screen reader for acquisition, consent, errors, cleanup release, activation and hostname handoff. Record actual speech, platform and browser evidence separately. |
| `ADR-0019-27` | An invalid task checkpoint remains visible as needing attention without blocking other tasks or removing account keys whose references are unknown. | automated | `tests/unit/test_acme_review.py::test_bad_task_does_not_stop_other_renewals_and_reports_attention`; `tests/unit/test_acme_review.py::test_invalid_child_failure_counter_releases_slot_without_overwriting_state` |
| `ADR-0019-28` | Retrying publication of an issued pair restores its renewal schedule instead of immediately ordering another certificate. | automated | `tests/unit/test_acme_review.py::test_publication_retry_restores_future_renewal_schedule` |
| `ADR-0019-29` | Renewal details use the same direct native-mode loopback policy as saved ACME operations. Other callers receive certificate metadata without those details. | automated | `tests/integration/test_acme_api.py::test_inventory_renewal_details_require_direct_native_localhost` |

## Amendment 2026-10-31: diagnose missing required packages before serving

The owner requires an incomplete runtime to refuse startup. `cryptography`
remains a core dependency. Reading the saved certificate selection does not
import certificate parsing code, so maintenance commands can load configuration
without losing that selection. Serving and Doctor share a read-only installed
distribution check against AutoDJ's core requirements. The owner's review decision
limits refusals to core packages: extras are warn-only. Doctor also checks the
saved runtime profile's extras when identifiable and offers setup synchronization;
missing extras appear as a warning in the web UI. No HTTP fallback is introduced.

Doctor offers package synchronization through setup, preserving the installation
boundary in [ADR 12](0012-separate-runtime-setup-from-model-execution.md).
Adopted environments require an explicit ownership explanation; unattended
`--fix` takes ownership only to repair missing required packages. Ambiguous
environments receive a manual command; unattended `--fix` exits non-zero when it
cannot perform the repair. Wheel installations use installed project metadata and
are explicitly manual-only, rather than guessing a setup checkout. Doctor runs
independent checks even when a package is missing and labels checks blocked by
imports as skipped, with the reason in text and JSON. JSON diagnostics never run repairs.
See [package troubleshooting](../guides/backups-and-updates.md#autodj-says-a-python-package-is-missing).
This amendment records the owner's missing-package policy without changing this
record's Proposed status or certificate selection precedence.

### Clarification 2026-10-31: serve refusal policy

The owner limits startup refusal to unsafe operation or risk to data: invalid
configuration, unsafe network exposure, unusable configured HTTPS, incompatible
library data, or missing required packages. Doctor and startup share the exposure,
TLS and library checks. Startup validates before layout/settings writes, LAN token
creation, logging, serve locks and station construction, including pending-restore
refusals. The successful preflight and SSL context pass through to server startup;
the token is saved only after the serve claim succeeds. A configured HTTPS pair
must be readable, parse and match; encrypted keys cannot prompt during startup.
The owner's review decision makes expired or not-yet-valid certificates warn-only
in Doctor and Secure connections, keeping HTTPS renewal reachable. It never falls
back to HTTP. Importing or selecting a new certificate retains the existing validation.

An empty library, missing model, missing FFmpeg, an available update, an optional
extra or an ordinary folder warning is not a generic startup veto. Doctor's whole
report is not used as a serve gate. Existing web diagnostics remain available for
these conditions. See the library compatibility clarification in
[ADR 6](0006-use-a-catalog-with-explicit-migrations.md#clarification-2026-10-31-check-library-compatibility-before-startup-writes).

## Implementation and verification

[Inventory](../../src/autodj/certificates.py),
[routes](../../src/autodj/certificates_api.py),
[TLS reload](../../src/autodj/tls.py),
[store tests](../../tests/unit/test_certificates.py), and
[route tests](../../tests/integration/test_certificates_api.py).
Operational instructions live in the [network guide](../guides/operations/network.md).
Run focused store, route, TLS and shared route-classification tests, frontend
interaction tests and a browser audit. Record actual platform and screen-reader
evidence separately from automated checks.

## Amendment 2026-10-31: Let's Encrypt issuance and renewal

The operator approved the ACME decisions for the extension to
[PR #700](https://github.com/blindndangerous/AutoDJ/pull/700), stacked on
[PR #698](https://github.com/blindndangerous/AutoDJ/pull/698). This amendment
extends certificate acquisition and renewal; the original inventory, Active/Saved/
Expired model, activation, deletion, access checks and two-step modal remain
authoritative. The original record's Proposed status is unchanged.

Use the installed `acme` library with DNS-01 and one built-in provider:
Cloudflare's fixed HTTPS API origin, an explicit zone ID and an operator-supplied
zone-scoped DNS-edit API token. Zone inspection requires zone read permission.
The adapter verifies the selected active zone and rejects out-of-zone names.
Cloudflare zone access does not prove that a supplied token has no wider scope;
the operator must create the token with only the selected zone's permissions.
No HTTP-01, TLS-ALPN-01, wildcard, IP identifier, arbitrary provider endpoint,
RFC 2136 or acme-dns is supported. Other providers retain external renewal/import.
One ordinary DNS name is supported per built-in certificate in this release.

Create a separate TXT record rather than replacing an RRset. Persist a random
ownership marker before creation and the returned record ID before notifying the
CA. The marker permits receipt recovery after a lost creation response. Cleanup
compares the saved ID and record contents and never deletes unrelated records.
A CNAME at the challenge name is an explained unsupported case. Every
authorization retains a separate persisted receipt; cleanup attempts each
independently, including receipt recovery after a lost response. Cleanup failure
remains visible and keeps the required credential until cleanup can finish.
An operator can explicitly confirm **Stop retrying DNS cleanup**. That records
the owned TXT IDs in the host log and displays the challenge name and manual
removal instructions. It releases the pending work without deleting DNS records.

The host resolver preflight returns its observed addresses. Addresses that cannot
all be shown to belong to this host require explicit operator confirmation;
NAT and bridged containers are warnings, not hard blocks. Private-only names,
localhost, IP literals, wildcards and names outside the selected zone are blocked.
Directory terms are displayed before issuance. The backend requires explicit
acceptance and persists the URL and time; changed terms stop automatic issuance.
The frontend must warn that production certificate names enter public Certificate
Transparency logs. It must offer the separate, explicit
“Let's Encrypt staging test — not trusted” action first. Staging never publishes
a certificate into the inventory or serving context.

One server-owned supervisor launches one Python child for blocking work, separately
from requests and playout under [ADR 8](0008-bound-background-work-outside-playout.md).
There is no unbounded operation queue. The child has a 15-minute deadline;
requests have 5-second connect and 15-second read limits and a 1 MiB response
limit. Shutdown terminates the child with a 5-second grace before killing it.
The Windows child uses the existing Job Object ownership helper. Child output
is discarded and library logging is disabled. Credentials travel through private
files, never command-line arguments. Saved operation metadata is distinct from
certificate state and is not another certificate inventory.
Child termination waits outside the management lock, while retaining the worker
slot until reaping finishes. Status and unrelated certificate management remain
available during termination.

Private ACME files live under `<index_dir>/.tls/acme`, using the existing
owner-only atomic file helpers and checked private reads. Staging and production
have separate account keys. Account keys and DNS tokens are omitted from all
public status and Doctor views, and removed when no setup, lineage or pending
cleanup references them. Startup and per-task retries recover credential cleanup
after an interrupted certificate deletion. A failure cleaning one task does not
block other tasks or turn a successful certificate deletion into an API error.
Each checkpoint is validated independently. An unreadable or invalid task is
skipped for execution and reported as needing attention in local status and
Doctor. Its file and possible account-key references remain until storage is
repaired; valid tasks continue. Renewal details in inventory responses use the
same direct native-mode loopback policy as saved ACME operations.
Existing backup exclusion of `.tls` applies. Durable
budget receipts contain no credentials and survive account or lineage deletion.

Persist order URLs before fetching authorizations and resume recoverable orders
after interruption. Limit orders to three per environment and canonical name
per rolling day, independent of account keys, and account registrations to three
per environment per rolling day. These are local protection limits, not a claim
about CA quotas or ARI replacement-order exemptions. Retry transient failures at
15 minutes, 1 hour, 6 hours, then 24 hours, honoring a later Retry-After. Invalid
authorization, terms and other permanent failures require operator correction.

Persist the renewal time returned by ARI; fall back to two thirds of the actual
certificate validity with up to five percent of its lifetime subtracted as jitter.
Only a lineage selected in the existing inventory renews automatically; saved,
never-activated lineages do not. Renewal adds a new pair of type “Let's Encrypt”
and conditionally activates it through the existing inventory lock and activation
path. A successful renewal retains the active pair and the pair it replaced,
pruning older pairs only from that lineage. A pair still served pending restart
is also retained. Retrying publication of an already issued pair restores its
certificate renewal time when its saved retry time is zero. A manual selection
made during issuance wins; the renewed pair stays Saved with an explicit
paused-renewal note.
Issuance failures retain the working pair and appear in row metadata and read-only
Doctor diagnostics. Activation uses the original rollback behavior; when storage
also prevents rollback, the failure reports the observed selection rather than
claiming it was unchanged.

Public certificates cannot cover localhost. The existing activation route therefore
accepts an optional explicit destination `hostname` query parameter for Let's
Encrypt rows only. It keeps the original body-independent request handling.
That hostname must already be in the server's allowed hosts and covered by the
certificate. The request still requires direct native-mode loopback and existing
browser authorization/Origin policy. No allowlist is widened. Existing callers
without the optional hostname retain the original management-host validation.
The existing two-step modal explains the hostname handoff and first-HTTPS restart
before activation. A cross-host restart destination must match the active Let's
Encrypt certificate and an advertised HTTPS origin in fresh local-only inventory
metadata; the port stays unchanged. The browser probe omits credentials and uses
normal TLS verification. No certificate is installed as a trusted root for this
acquisition method.

Implementation entrypoints: [DNS adapter](../../src/autodj/acme_dns.py),
[ACME client](../../src/autodj/acme_client.py),
[private service](../../src/autodj/acme_service.py), and
[worker ownership](../../src/autodj/acme_worker.py).
The [network guide](../guides/operations/network.md) and
[domain HTTPS guide](../guides/operations/domain-https.md) describe acquisition,
renewal and recovery. The [threat model](../../THREAT_MODEL.md) records the access,
credential and provider boundaries. Automated mappings refer to executed tests;
they do not prove public DNS propagation, Cloudflare token scope, real-provider
issuance, platform process teardown, operating-system trust or screen-reader speech.

The opt-in Pebble integration uses checksum-verified release binaries and a
temporary loopback CA/DNS environment. The Linux CI job requires it to execute;
missing binaries fail that job. Ordinary local suites without the binary paths
skip it explicitly. The test uses the real ACME protocol and DNS validation with
a test-only Cloudflare request bridge; it does not contact Cloudflare or a public CA.
