# Threat model: AutoDJ

Last reviewed: 2026-08-02. Re-review every major release.

## Scope and trust boundaries

AutoDJ is a single-user local music player with these exposed surfaces:

- CLI commands read local audio and configuration, then write index, profile, liner, history, or
  backup data as the invoking user.
- The web UI uses FastAPI and WebSocket. The default host process binds to `127.0.0.1:8080`.
  The default Compose service listens on container-internal `0.0.0.0` but publishes only on host
  `127.0.0.1`.
- LAN mode requires either an access token or explicit `--insecure-lan`, plus exact Host and Origin
  allowlists. `AUTODJ_ACCESS_TOKEN` is supported for secret injection.
- Backup and restore trust configured source and destination roots after applying path, type,
  identity, size, digest, and free-space checks.

Cloud sync, multi-user roles, billing, and direct public Internet hosting are out of scope. Use TLS
for any network where observers could read HTTP traffic. For remote access, place AutoDJ behind a
trusted TLS reverse proxy, mTLS, or a private overlay network.

## CLI risks

- Crafted audio metadata is handled through Mutagen with guarded errors.
- Checkpoints preserve index progress across interruption. `--limit` bounds operator-requested
  indexing work.
- Error and diagnostic output must not expose access tokens or Hugging Face tokens. `autodj doctor`
  serializes secret fields as `<redacted>` and does not write index state.
- Background jobs accept fixed subcommands and validated arguments. They run with `shell=False`.

## Web request policy

Default loopback mode is anonymous. For a non-loopback bind, startup validation requires a token or
explicit insecure-LAN acknowledgement. Wildcard binds also require nonempty exact allowed-host and
allowed-origin lists.

When `server.access_token` or `AUTODJ_ACCESS_TOKEN` is set:

- The token must contain at least 32 UTF-8 bytes.
- The login handler compares the token in constant time and exchanges it for a signed
  `autodj_session` cookie.
- The cookie is HttpOnly and SameSite Strict. It becomes Secure when AutoDJ serves with TLS.
- The login body is limited to 4096 bytes before downstream parsing.
- A fixed-window limiter permits five attempts per client and 100 total attempts per 60 seconds,
  with bounded state for 1024 clients.
- The HTTP API and WebSocket both enforce session, Host, and Origin policy.

Public assets, `/healthz`, `/api/version`, `/api/auth/status`, and `/api/login` remain available
without a session cookie. Unsafe HTTP methods require one allowed Origin. Audio and liner file
endpoints use indexed or validated plain-file allowlists rather than arbitrary filesystem paths.

## Request and audit records

HTTP responses receive `X-Request-ID`. WebSocket connections also receive an internal request ID.
Audit records use fixed JSON fields for request ID, action, outcome, method, route template, and
status. They do not include tokens, request bodies, query strings, client-supplied filenames, or
music paths.

Rejected requests and rate-limit transitions are audited. Successful or rejected unsafe HTTP
actions are audited after response status is known. WebSocket connection, control, error, and
disconnect events are audited. These records support single-user incident review but do not provide
per-user attribution.

## Backup and restore boundary

Backups classify published index and SQLite data as derived. Profiles, liners, dayparts, optional
history, and `web_state.json` are unique data. Each archived payload has an exact destination, size,
classification, and SHA-256 digest in schema 1 `manifest.json`.

Stopped backup rejects SQLite WAL, shared-memory, and rollback-journal sidecars and rechecks state
during copying. This detects activity but does not prove no writer exists, so the operator must stop
service. Online backup uses SQLite backup API for live DJ metadata and retries bounded index
generation changes.

Restore rejects unknown schema and incompatible release lines, unsafe paths, normalized path
collisions, symlink or reparse traversal, encrypted or non-regular ZIP members, invalid mappings,
undeclared files, size mismatches, and digest mismatches. It checks central-directory metadata and
target free space before extraction, stages every payload, and rolls installed targets back after a
failure. Filesystem roots remain trusted through operating-system ACLs. AutoDJ does not defend
against an attacker who already controls those roots and can race filesystem operations.

## Container and dependency controls

The container image runs as UID/GID 10001 with all capabilities dropped and `no-new-privileges`.
Base images and the copied `uv` binary use immutable digests. CI builds and smoke-tests the image.
It verifies bind ownership and host loopback publication, generates a CycloneDX SBOM, and blocks on
Trivy HIGH or CRITICAL findings with fixes available.

`uv.lock` and `package-lock.json` are committed. CI uses frozen installs, runs `pip-audit` and
`npm audit`, scans source tree with Trivy and OSV-Scanner, checks secrets with Gitleaks, and produces
dependency and container SBOM artifacts. `osv-scanner.toml` records the rationale for its one
ignored advisory. `scripts/check_pip_audit_suppressions.py` rejects the matching pip-audit
suppression after its 2026-11-02 expiry unless reviewed.

## Reporting a vulnerability

Use the private process in [SECURITY.md](SECURITY.md).
