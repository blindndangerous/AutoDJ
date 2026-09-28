# Threat model: AutoDJ

Last reviewed: 2026-09-07. Re-review every major release.

## Scope and trust boundaries

AutoDJ is a single-user local music player with these exposed surfaces:

- CLI commands read local audio and configuration, then write index, profile, liner, history, or
  backup data as the invoking user.
- The web UI uses FastAPI and WebSocket. The default host process binds to `127.0.0.1:8080`.
  The default Compose service listens on container-internal `0.0.0.0` but publishes only on host
  `127.0.0.1`.
- LAN mode requires either an access token or explicit `--insecure-lan`, plus exact Host and Origin
  allowlists. `AUTODJ_ACCESS_TOKEN` is supported for secret injection. `serve --lan` fills in
  both allowlists and the token automatically; see "Automatic LAN mode" below.
- Backup and restore trust configured source and destination roots after applying path, type,
  identity, size, digest, and free-space checks.

Cloud sync, multi-user roles, billing, and public Internet hosting are out of scope. Use TLS for any
network where observers could read HTTP traffic, and terminate it in AutoDJ itself with
`--ssl-certfile` and `--ssl-keyfile`. A TLS-terminating reverse proxy is not supported: the
`Secure` cookie flag and the advertised-origin check follow AutoDJ's own TLS setting, so a proxy in
front of plain HTTP leaves session cookies without `Secure`. For remote access, reach the LAN
through a private overlay network such as a VPN rather than publishing the port.

## CLI risks

- Crafted audio metadata is handled through Mutagen with guarded errors.
- Checkpoints preserve index progress across interruption. `--limit` bounds operator-requested
  indexing work.
- Error and diagnostic output must not expose access tokens or Hugging Face tokens. `autodj doctor`
  serializes secret fields as `<redacted>` and does not write index state.
- Background jobs accept a fixed subcommand allowlist, and each subcommand accepts only the flags
  the web UI sends: currently `index --limit <positive integer>`, and no flags for `enrich`,
  `prune`, `stats`, or `list-indexes`. Anything else, such as `--force` or a second `--config`
  or `--name`, is refused. They run with `shell=False` and a UTF-8 child pipe.

## Web request policy

Default loopback mode is anonymous. For a non-loopback bind, startup validation requires a token or
explicit insecure-LAN acknowledgement. Wildcard binds also require nonempty exact allowed-host and
allowed-origin lists.

When `server.access_token` or `AUTODJ_ACCESS_TOKEN` is set:

- The token must contain at least 32 UTF-8 bytes.
- The browser never receives or sends the token. Operators share a short pairing code instead.
- The server derives that code as `HMAC-SHA256(token, "pair:<window>")` reduced to eight decimal
  digits, where the window advances every 300 seconds. Current and previous windows are both
  accepted, so one code stays usable for at most ten minutes and is compared in constant time.
- A browser posts the code and a device name to `/api/pair`. On success the server records a new
  device identity and returns a session bound to that device.
- The session is `<expiry>.<device>.<nonce>` signed with HMAC-SHA256 under the same token and
  carried in an `autodj_session` cookie. Default lifetime is 90 days, configurable between 60
  seconds and one year.
- The cookie is HttpOnly and SameSite Strict. It becomes Secure when AutoDJ serves with TLS.
- Every request revalidates the signature, the expiry, and whether the device is still active, so
  `autodj devices revoke` and `autodj devices reset` take effect immediately, including on already
  connected WebSockets.
- The pairing body is limited to 4096 bytes before downstream parsing.
- A fixed-window limiter permits five attempts per client and 100 total attempts per 60 seconds,
  with bounded state for 1024 clients.
- Wrong, well-formed codes are counted per client address within each 300-second code window. A
  client that sends ten is locked out until the window ends: `/api/pair` answers 429 with
  `Retry-After` for right and wrong codes alike, and the server logs a warning naming the address.
  Other clients keep pairing with the same code.
- Fifty wrong codes in one window across all clients pause pairing for everyone as a last resort:
  every code issued so far stops working, `/api/pair` answers 429 with `Retry-After` and a detail
  saying to try again with a new code, and the server logs a warning. `autodj devices pairing-code`
  prints how long its code stays valid and when the next one starts, which is the one to use after
  a pause.
- These limits cap one address at about ten guesses per window, roughly a 2 percent chance per
  year of nonstop guessing, and many addresses together at about fifty per window, roughly 10
  percent per year, instead of about 65 percent under the request limiter alone. An attacker with
  many LAN addresses can keep pairing paused, but already paired devices keep working.
- Rotating the token invalidates every outstanding pairing code and every existing session at once.
- The HTTP API and WebSocket both enforce session, Host, and Origin policy.

Public assets, `/healthz`, `/api/version`, `/api/auth/status`, and `/api/pair` remain available
without a session cookie. Without one, `/healthz` reports only its status and `/api/version` only
the version number; the track count, commit and build time need a session. Unsafe HTTP methods require one allowed Origin. Audio and liner file
endpoints use indexed or validated plain-file allowlists rather than arbitrary filesystem paths.
The liner fetch and delete endpoints accept only one plain filename with a liner audio extension
(`.mp3`, `.wav`, `.ogg`, `.m4a`, `.flac`, or `.aac`), so they cannot read or remove configuration,
databases, or other non-audio files even when those share the liner root.
The liner *root directory* comes only from configuration. `/api/playback-settings` rejects
`liners_folder`, like any other unknown field, with 422 before applying anything, and
`liners_folder` is not part of the `PlaybackState` schema that `PERSISTED_PLAYBACK_FIELDS`
derives from, so `web_state.json` never stores it. Treat a paired browser as trusted.

### Automatic LAN mode

`autodj serve --lan` (or `[server] lan`) computes the Host allowlist from this machine's own
names: the hostname, `<hostname>.local` for a hostname without a dot, the fully qualified name,
the addresses the resolver gives the hostname, the outbound IPv4 address (found by connecting a
UDP socket, which sends nothing), and `localhost`, `127.0.0.1` and `::1`. The Origin allowlist is
`http://` (and with TLS `https://`) on the served port for each of those hosts, merged with any
configured hosts and origins. The resulting configuration passes the same startup validation as
a hand-written one.

This keeps the DNS-rebinding defence: a page on an attacker-controlled name that resolves to
this machine's address still sends its own name in the Host header, which is not in the list, so
the request gets 403. It does not restrict which devices can connect; any device that can route to
the port and uses one of the listed names or addresses reaches the pairing screen. Names the
machine does not know about, such as a CNAME in router DNS, are refused until added with
`--allowed-host`. If the resolver or the operating system reports an address that another network
also uses, that address is allowed too; this widens the Host check but does not bypass pairing.

When no token is configured, `--lan` stores a generated 43-character token in
`<index_dir>/.access-token`, written atomically and regenerated if the file is empty or too
short. On Linux and macOS the file has mode 0600, so only the owner can read it. On Windows the
mode call does nothing and the file gets the index folder's permissions; if the index folder is
on a network share, anyone who can read that share can read the token and derive pairing codes. An unreadable file stops startup with an error instead of silently replacing
the token. A configured `access_token` or `AUTODJ_ACCESS_TOKEN` always wins. Anyone who can read
the index directory can derive pairing codes, the same as for a token in `config.local.toml`;
deleting the file rotates the token and ends every paired session on the next start. Startup
prints the addresses and pairing code, never the token. `--lan --insecure-lan` keeps the
automatic allowlists without any token.

Name lookups for detection (the fully qualified name and the resolver addresses) run in a
background thread that start-up waits on for at most two seconds, so broken DNS cannot stall
startup. A fully qualified name is used only when it extends the hostname and is not in a
reverse-DNS zone (`in-addr.arpa` or `ip6.arpa`), so ISP reverse-DNS names are not allowed;
`<hostname>.home.arpa` (RFC 8375) is kept. Names whose first label is `localhost` are
skipped. Inside a container the start-up block prints only the explicitly configured hosts,
because container addresses are not reachable from the network; they are still allowed.

## Stream URL, secret and security

Stream mode (`autodj serve --stream`, `[stream] enabled = true`, or the Compose `stream`
profile) serves the live mix as MP3 at `/stream/<secret>.mp3`, plus `/stream/<secret>.m3u`, a
one-line playlist naming the same URL. The secret is a bearer credential for listening only:

- It is 32 random bytes, URL-safe base64, created on first start in stream mode and stored in
  `<index_dir>/.stream-secret`, beside the paired-devices database, never in `config.toml` and
  never equal to the access token. It gets the same file permissions as the access token: mode
  0600 on Linux and macOS, the index folder's permissions on Windows.
- Anyone who has the URL can listen; it carries no control surface and cannot pair a browser,
  change settings, or reach any other endpoint. The stream route is exempt from the session
  cookie check for that reason, but every other check in `SecurityMiddleware` still applies,
  including the Host allowlist, so the name typed into the player must be one this AutoDJ
  instance is configured to accept.
- A wrong secret returns 404, is compared in constant time, and counts toward its own rate
  limiter of the same kind that guards pairing-code guesses. It is a separate instance with its
  own independent budget: wrong stream secrets do not consume pairing attempts, or the reverse.
- "Make new link" (`POST /api/stream/rotate`, authenticated, from the Settings, Stream section)
  replaces the secret, disconnects every current listener, and makes the old URL 404
  immediately. Rotation is the revocation mechanism: use it if the link is shared somewhere it
  should not be.
- `max_listeners` (default 8) bounds concurrent connections; a listener beyond it gets 503
  without affecting anyone already connected. This limits resource use, not who can connect: any
  device that can reach the port and has the current URL may take one of those slots.
- The stream carries the operator's own library and voice liners to whoever holds the link. Treat
  the URL the same way as a paired browser: do not publish it outside the LAN it was generated
  for, and use "Make new link" if it leaks.

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
