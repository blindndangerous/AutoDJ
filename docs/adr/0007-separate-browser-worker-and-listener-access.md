# 7. Separate browser control, scoped workers, and listener access

Date: 2026-10-16
Status: Proposed; retrospective description of existing implementation.
Decision authority: Project maintainer; explicit acceptance pending.

## Context and scope

Browser control, remote analysis, announcer/live input, and public listening
have different authority. [ADR 4](0004-server-mix-bus-and-stream-mode.md) already
records removal of secret stream URLs; [ADR 5](0005-a-station-owns-its-playout-chain.md)
distinguishes shared and station routes. This record covers those trust
boundaries, not a new multi-user role system or unrestricted Internet hosting.

This record reconstructs the current boundary from the linked implementation,
tests, and operational docs. Original decision dates and deliberations are not
established. The options below are tradeoffs for review, not historical claims.

## Proposed decision and constraints

Keep browser pairing sessions, scoped machine credentials, and listener
surfaces separate. Apply the request policy before protected handlers consume
untrusted bodies or touch files.

- Anonymous loopback and authenticated LAN operation remain distinct modes.
  Non-loopback security requirements, Host/Origin validation, body limits,
  rate limits, TLS/proxy posture, and session rules are defined in the threat model.
- Paired browser control is server-wide. Analysis-worker credentials authorize
  only worker operations; announcer/live credentials are station-scoped where
  defined. A worker token must not become a browser-control credential.
- Public stream, status, player, request, and metadata routes expose only their
  configured listener capabilities; public listening does not grant control.
  Not every listener route is unconditionally enabled or anonymous.
- Route classification and station resolution must preserve the same security
  and request limits at default and named-station addresses.
- Audio decoding and uploaded names must stay inside supported local file/pipe
  boundaries; declarative PAL import must not execute uploaded text.

## Alternatives for review

- Separate credentials per capability: narrower authority and clearer
  revocation, with more policy paths to test.
- One shared token for control, workers, and listeners: simpler credential
  handling, but listeners or automation could gain station-changing authority.
- Full user/role infrastructure: could support independent operators, but adds
  identity and authorization requirements absent from the current product.

## Consequences and revisit conditions

New endpoints must declare their trust category and station/shared ownership.
Security fixes must consider aliases, WebSockets, uploads, and early rejection.
Revisit for independently authorized station operators or a changed deployment
boundary; do not infer either from the presence of multiple stations.

## Implementation and operational references

- [Threat model](../../THREAT_MODEL.md) is the detailed security policy and
  [operations](../operations.md#local-network-access) documents supported deployment.
- [Middleware](../../src/autodj/security.py), [pairing](../../src/autodj/pairing.py),
  [machine tokens](../../src/autodj/machine_tokens.py), and [station routes](../../src/autodj/station_routes.py).
- [Worker API](../../src/autodj/worker_api.py), [announcer breaks](../../src/autodj/breaks_api.py),
  [live routes](../../src/autodj/live_routes.py), and [decoder](../../src/autodj/stereo.py).
- [Public pages and requests](../operations.md#public-pages) and
  [announcer integration](../operations.md#announcer-breaks-dj-breaks-news-and-weather).

This documentation pass adds no runtime dependency, configuration switch, or
migration. Any future change to this boundary needs its own affected-path and
compatibility assessment.

## Current contracts

IDs are stable and are never recycled. Automated means an existing regression
test is mapped; structural validation does not execute it, prove platform
coverage, or accept this ADR. Manual and gap rows remain explicit obligations
or evidence limits. Narrative above retains rationale and scope.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0007-01` | Revoking a paired device removes its control authority without a server restart. | automated | `tests/integration/test_pairing_server.py::test_revoked_device_loses_api_access_without_server_restart` |
| `ADR-0007-02` | Worker credentials stay worker-scoped; announcer tokens stay station-scoped. | automated | `tests/unit/test_worker_api.py::test_a_machine_token_opens_nothing_but_the_worker_routes`; `tests/unit/test_machine_tokens.py::test_an_announcer_token_is_bound_to_one_station` |
| `ADR-0007-03` | Pairing rejects untrusted origins and oversized bodies before creating a session. | automated | `tests/integration/test_pairing_server.py::test_pairing_rejects_untrusted_browser_origin`; `tests/integration/test_pairing_server.py::test_pairing_rejects_oversized_body_before_json_parsing` |
| `ADR-0007-04` | Default/named station route aliases preserve security and body limits. | automated | `tests/integration/test_station_routes.py::test_station_paths_security_matrix`; `tests/integration/test_station_routes.py::test_station_paths_keep_their_body_limits` |
| `ADR-0007-05` | A new deployment topology must respect the supported TLS/proxy and public-listener boundary. | manual | Review the threat model and deployment configuration: proxy secret configured, client identity and secret headers stripped at the gateway, backend not published, browser HTTPS and protected inter-host transport. Endpoint tests do not certify third-party proxy behavior. |
| `ADR-0007-06` | Trusted-proxy control registers an identified browser without code pairing; its cookie is not native device authority. | automated | `tests/integration/test_trusted_proxy_server.py::test_sso_registers_browser_without_pairing` |
| `ADR-0007-07` | Only the asserted owner can manage their SSO browser records. | automated | `tests/integration/test_trusted_proxy_server.py::test_sso_browser_management_is_scoped_to_owner` |
| `ADR-0007-08` | Missing proxy identity rejects control without making configured public metadata private. | automated | `tests/integration/test_trusted_proxy_server.py::test_sso_rejects_missing_identity_and_preserves_public_metadata` |
| `ADR-0007-09` | SSO control WebSockets require asserted identity without native pairing. | automated | `tests/integration/test_trusted_proxy_server.py::test_sso_websocket_needs_identity_but_no_pairing` |
| `ADR-0007-10` | Proxy identity requires exactly one matching shared-secret header over HTTP and WebSocket; startup refuses an absent secret. | automated | `tests/unit/test_trusted_proxy_security.py::test_proxy_secret_is_required_for_http_identity`; `tests/integration/test_trusted_proxy_server.py::test_sso_websocket_rejects_identity_without_single_proxy_secret`; `tests/integration/test_trusted_proxy_server.py::test_sso_app_refuses_startup_without_proxy_secret` |
| `ADR-0007-11` | SSO control and microphone sockets expire after the shorter of 900 seconds and the configured session TTL. | automated | `tests/integration/test_trusted_proxy_server.py::test_sso_control_socket_expires_and_requires_a_new_proxy_handshake`; `tests/unit/test_trusted_proxy_security.py::test_sso_microphone_rejects_frames_after_socket_age_limit` |
| `ADR-0007-12` | Direct and file proxy-secret sources are mutually exclusive; a shared-readable secret file cannot authorize startup. | automated | `tests/unit/test_trusted_proxy_security.py::test_proxy_secret_sources_are_mutually_exclusive`; `tests/unit/test_trusted_proxy_security.py::test_proxy_secret_file_rejects_other_readers` |
| `ADR-0007-13` | Non-loopback trusted-proxy startup without AutoDJ TLS warns once about exposing the secret. | automated | `tests/integration/test_trusted_proxy_server.py::test_proxy_plaintext_network_bind_warns_once` |
| `ADR-0007-14` | A worker pairing code is made only by a paired browser, works once for ten minutes, follows the browser pairing's per-client and server-wide wrong-code limits, and is traded only by a program (no Origin) over HTTPS or loopback for an analysis-worker token. A worker token opens neither the pairing-code nor the worker-settings routes. | automated | `tests/unit/test_worker_api.py::test_a_pairing_code_trades_once_for_an_analysis_worker_token`; `tests/unit/test_worker_api.py::test_a_pairing_code_expires_and_a_new_one_replaces_it`; `tests/unit/test_worker_api.py::test_wrong_pairing_codes_lock_out_a_client`; `tests/unit/test_worker_api.py::test_many_wrong_pairing_codes_burn_the_code`; `tests/unit/test_worker_api.py::test_the_pairing_route_takes_only_programs_over_https`; `tests/unit/test_worker_api.py::test_a_worker_token_cannot_manage_workers_or_make_pairing_codes` |
| `ADR-0007-15` | A paired worker keeps its token only in a per-user file readable by its owner alone, one entry per server, refuses a file others can read, and never combines `--pair` with a given token. | automated | `tests/unit/test_worker.py::test_pair_with_server_trades_a_code_for_a_token`; `tests/unit/test_worker.py::test_saved_worker_tokens_are_private_and_kept_per_server`; `tests/unit/test_worker.py::test_worker_pair_saves_the_token_and_later_runs_use_it` |

## Verification

Run the following checks for affected changes and record results and platform
limits in the relevant PR. Test links do not establish execution or change this
record's Proposed status.

- [ ] Verify browser session/origin/body-limit policy with
  [security tests](../../tests/integration/test_server_security.py) and
  [pairing tests](../../tests/integration/test_pairing_server.py).
- [ ] Verify worker-only authority and station-bound tokens with
  [worker API tests](../../tests/unit/test_worker_api.py) and
  [token tests](../../tests/unit/test_machine_tokens.py).
- [ ] Verify security parity for route aliases with
  [station route tests](../../tests/integration/test_station_routes.py).
- [ ] Verify listener and live-source boundaries with
  [request-page tests](../../tests/integration/test_request_page_server.py) and
  [live-source tests](../../tests/integration/test_live_source.py).

## Evidence gaps

The threat model contains deployment assumptions and known limitations;
these are not erased by passing endpoint tests. Acceptance of this reconstructed
boundary is pending. This review does not certify third-party proxies or perform
a penetration test.

## Amendment 2026-10-18: Provider-neutral browser SSO

Status remains Proposed; this amendment records the operator-approved implementation
scope without accepting the retrospective record as a whole. Requiring a separate
pairing code after gateway authorization duplicates the access ceremony. Reusing
code-paired authority for automatically registered SSO browsers would also leak
permissions when switching back to native mode.

Explicitly select `server.auth_mode = "native"` (default) or `"trusted_proxy"`
per instance. Native mode preserves ordinary loopback access and existing
code-entry pairing and revocation. Trusted-proxy mode requires the proxy shared
secret described in the 2026-10-19 amendment below, plus the stable
`X-AutoDJ-User-ID` header for control, accepts optional `X-AutoDJ-User-Name` as
label only, and never falls back to native pairing. The proxy owns authentication
and application authorization; AutoDJ adds no provider SDK, groups or application
roles. Fixed neutral headers make provider-specific mapping an operator concern.
A direct OIDC client was rejected for this scope because it adds provider/session
machinery already supplied by the gateway.

Automatically register SSO browsers in a separate table in the existing registry
database. Its cookie is identification only, always bound to the asserted user.
The owner may list, rename or forget their records; account switching creates a
separate record. Forgetting does not revoke access. Native paired-device records
remain intact and SSO records never confer native authority after a mode switch.
Keep the public-listener and scoped-machine-credential boundaries unchanged.

The deployment proxy must strip client identity and proxy-secret headers before
inserting verified values and its secret. Direct requests without that secret are
unauthenticated even when they assert an identity. Preserve exact Host/Origin policy
and body limits. Optional `server.sso_logout_url` provides generic “Sign out through
SSO”; its shared-session effects belong to the provider. Open WebSockets may
survive sign-out or revocation until their bounded connection age expires.
Per-action user auditing,
immediate socket revocation and passkey classification are deferred. Provider
migration requires deliberate stable-ID mapping.

Affected paths are server configuration, request middleware, the browser registry,
access endpoints and browser-access UI. Operations and the threat model define the
proxy contract and verification. Tests must exercise native compatibility, missing
identity, separate browser ownership, account switching, unit-level credential
separation when switching modes, protected
WebSockets and anonymous listener routes. Third-party proxy and interactive login
verification remain deployment evidence, not claims established by unit tests.

## Amendment 2026-10-19: Require proxy proof and bound SSO authority

Status remains Proposed. PR #489 security remediation corrects the earlier
network-only trust assumption. Require `server.trusted_proxy_secret`, supplied
through private configuration, `AUTODJ_TRUSTED_PROXY_SECRET`, or an owner-only file
selected by `server.trusted_proxy_secret_file` or `AUTODJ_TRUSTED_PROXY_SECRET_FILE`.
Use the existing secret-file reader and private-file writer described in
[ADR 10](0010-preserve-state-through-atomic-writes-and-recovery.md). Keep the secret
outside ordinary station backups. A direct secret and a secret file are mutually
exclusive, including when supplied through different configuration layers.

Refuse startup without at least 32 printable ASCII secret bytes without spaces,
on loopback as well as LAN binds. Require exactly one `X-AutoDJ-Proxy-Auth` and
compare it in constant time before reading proxy identity. Identity headers alone
grant no access. Strip client copies of all three headers at the gateway and
send the secret only to AutoDJ after authorization. Keep the backend unpublished,
browser HTTPS enabled and inter-host connections protected with TLS.

Bound control and microphone sockets to the shorter of `session_ttl_seconds` and
900 seconds on a monotonic clock, then close with 4401. Reconnection returns through
the gateway for fresh authorization. This does not promise immediate revocation
or override provider session policy. Bound browser inventory to 20 records per
user, evicting the least recently seen record atomically before a new insertion.
Forgetting a record remains housekeeping and never revokes authentication.

### Security re-review clarification 2026-10-19

Reject ambiguous direct/file secret sources instead of choosing one silently.
Resolve file contents into the private request-policy snapshot without replacing
the operator's source settings, so repeated startup validation remains safe.
Check permissions on the same opened file before reading: reject POSIX group/other
permission bits and Windows access grants to accounts other than the account
running AutoDJ. Other secret readers retain their existing policy unless they
request this private-file check.

Warn once at application startup for a non-loopback trusted-proxy bind without
AutoDJ TLS. Keep same-host container operation available; the warning cannot
establish whether the proxy hop stays on that host. Use TLS between hosts.
SSO network failures keep the existing WebSocket retry path. Only explicit
authentication rejection or a gateway sign-in redirect triggers reauthentication.
Native microphone sessions retain one validity check per frame; SSO also checks
after receiving to reject frames that arrive after its connection-age limit.

## Amendment 2026-10-23: Worker pairing codes

Status remains Proposed. Issue #554 adds a way to give a worker its own token without copying it.
A paired browser asks `POST /api/machine-tokens/pairing-code` for an 8-digit code, optionally
naming the token. The code is random, kept only in the server's memory, works once for ten minutes
(the longest a browser pairing code lives), and a new code replaces it. A restart drops it.

`autodj worker --pair` trades the code at `POST /api/worker-pair` for a new analysis-worker token
named by the operator or by the worker's host name. That route needs no session and no token, but
the middleware treats it like a machine route: it refuses a request with an `Origin` header and a
plain-HTTP request from another machine. Wrong codes follow the browser pairing limits: ten from one
client address lock that client out until the code ends, and fifty in all burn the code. Nothing is
counted while no code is open, so the counts stay bounded. Code creation and the exchange are
audited like other unsafe requests, and neither logs the code or the token.

The worker saves the token in a per-user file readable only by its owner (mode 0600, or an access
list for the current user alone on Windows), one entry per server address, and refuses to read a
file others can read. An explicit `--token` or `AUTODJ_WORKER_TOKEN` wins over the file; `--pair`
refuses to run with either. Contracts ADR-0007-14 and ADR-0007-15 cover this amendment.
