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
