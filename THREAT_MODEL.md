# Threat model: AutoDJ

Last reviewed: 2026-09-07. Re-review every major release.

## Scope and trust boundaries

AutoDJ is a single-user local music player with these exposed surfaces:

- CLI commands read local audio and configuration, then write index, profile, liner or backup
  data as the invoking user. `library.db` keeps the complete play history, with performance
  counts; `autodj export-plays` reads it and writes a CSV or text report to standard output or
  to the `--out` file.
- The web UI uses FastAPI and WebSocket. The default host process binds to `127.0.0.1:8080`.
  The Compose service runs `serve --lan` on the host network, so it listens on every host
  interface on port 8080 and every browser must pair.
- LAN mode requires either an access token or explicit `--insecure-lan`, plus exact Host and Origin
  allowlists. `AUTODJ_ACCESS_TOKEN` is supported for secret injection. `serve --lan` fills in
  both allowlists and the token automatically; see "Automatic LAN mode" below.
- Backup and restore trust the configured index, liners and profiles locations. Restore
  checks member names and the AutoDJ version before it replaces anything.

Cloud sync, multi-user roles, billing, and public Internet hosting are out of scope. Use TLS for any
network where observers could read HTTP traffic, and terminate it in AutoDJ itself with
`[server] ssl_certfile` and `ssl_keyfile` (or `--ssl-certfile` and `--ssl-keyfile`). A proxy that
forwards to AutoDJ over plain HTTP is not supported: the `Secure` cookie flag and the
advertised-origin check follow AutoDJ's own TLS setting, so such a proxy leaves session cookies
without `Secure`.

The supported remote setup is a Cloudflare Tunnel that connects to AutoDJ over HTTPS and checks
its certificate against the domain (`originServerName`), with Cloudflare Access (an email
one-time PIN policy) in front and `cloudflared` set to refuse requests without a valid Access
token; see [docs/operations.md](docs/operations.md#https-with-your-own-domain). Access decides
who reaches AutoDJ at all; AutoDJ's pairing and Host and Origin checks still apply behind it.
Every tunnelled request arrives from the `cloudflared` machine's address. In stream mode, without
a matching `trusted_proxies` entry, the per-address pairing lockout counts all remote browsers as one
client, and someone who passed Access could use up that budget and delay other remote pairings
for one code window. Never expose AutoDJ to
the internet without Access, including through port forwarding.

AutoDJ reloads a renewed certificate and key when their files change, checked every five
minutes. It copies the pair and test-loads it before putting it into the live TLS context, so a
half-copied or mismatched pair is refused and the previous certificate keeps being served.

## CLI risks

- Crafted audio metadata is handled through Mutagen with guarded errors.
- Checkpoints preserve index progress across interruption. `--limit` bounds operator-requested
  indexing work.
- Error and diagnostic output must not expose access tokens or Hugging Face tokens. `autodj doctor`
  serializes secret fields as `<redacted>` and does not write index state.
- Background jobs accept a fixed subcommand allowlist, and each subcommand accepts only the flags
  the web UI sends: currently `index --limit <positive integer>`, and no flags for `enrich`,
  `analyse`, `prune`, `stats`, or `list-indexes`. Anything else, such as `--force` or a second `--config`
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
- A right code pairs nothing on its own. It works only while a pairing request is open, and each
  request pairs one device. `autodj devices pairing-code`, `GET /api/pairing-code` and a startup
  with no active paired device open a request until the code they hand out expires. The request
  lives in the paired-devices database, which the CLI and the server share. Taking the request and
  recording the device are one SQLite transaction, so two browsers racing with one code cannot
  both pair. A right code with no open request gets the same 401 as a wrong one.
- A browser posts the code and a device name to `/api/pair`. On success the server records a new
  device identity and returns a session bound to that device.
- The session is `<expiry>.<device>.<nonce>` signed with HMAC-SHA256 under the same token and
  carried in an `autodj_session` cookie. Default lifetime is 90 days, configurable between 60
  seconds and one year.
- The cookie is HttpOnly and SameSite Strict. It becomes Secure when AutoDJ serves with TLS.
- Every request revalidates the signature, the expiry, and whether the device is still active, so
  `autodj devices revoke` and `autodj devices reset` take effect immediately, including on already
  connected WebSockets.
- A paired browser can list paired devices (`GET /api/devices`), rename any of them
  (`PATCH /api/devices/{id}`) and revoke any of them (`DELETE /api/devices/{id}`) from Settings,
  Browser access. All three need a valid session.
- A paired browser can also ask for a pairing code (`GET /api/pairing-code`), so any paired
  device can pair one more. The route needs a valid session, answers 409 when pairing is off,
  and never logs the code.
- "Sign out this browser" (`POST /api/logout`) revokes the calling device as well as deleting its
  cookie, so a copied cookie stops working at once instead of lasting out its 90 days.
- The pairing body is limited to 4096 bytes before downstream parsing.
- Wrong, well-formed codes are counted per client address within each 300-second code window. A
  client that sends ten is locked out until the window ends: `/api/pair` answers 429 with
  `Retry-After` for right and wrong codes alike, and the server logs a warning naming the address.
  Other clients keep pairing with the same code.
- Fifty wrong codes in one window across all clients pause pairing for everyone as a last resort:
  every code issued so far stops working, `/api/pair` answers 429 with `Retry-After` and a detail
  saying to try again with a new code, and the server logs a warning. `autodj devices pairing-code`
  prints how long its code stays valid and when the next one starts, which is the one to use after
  a pause.
- This lockout is the guard against guessing; there is no separate request-rate limit. It caps
  one address at about ten guesses per window, roughly a 2 percent chance per year of nonstop
  guessing, and many addresses together at about fifty per window, roughly 10 percent per year.
  An attacker with many LAN addresses can keep pairing paused, but already paired devices keep
  working. In the supported remote setup Cloudflare Access stands in front of the public address,
  so only people who passed Access can reach the pairing screen from outside.
- Rotating the token invalidates every outstanding pairing code and every existing session at once.
- The HTTP API and WebSocket both enforce session, Host, and Origin policy.

Public assets, `/healthz`, `/api/version`, `/api/auth/status`, and `/api/pair` remain available
without a session cookie. Without one, `/healthz` reports only its status and `/api/version` only
the version number; the track count, commit and build time need a session. Unsafe HTTP methods require one allowed Origin. Audio and liner file
endpoints use indexed or validated plain-file allowlists rather than arbitrary filesystem paths.
The liner fetch and delete endpoints accept only one plain filename with a liner audio extension
(`.mp3`, `.wav`, `.ogg`, `.m4a`, `.flac`, or `.aac`), so they cannot read or remove configuration,
databases, or other non-audio files even when those share the liner root. Upload names get the
same check and may be at most 200 UTF-8 bytes. An upload is written to a hidden
`.<name>.<random>.part` file in the liner folder, flushed to disk, then renamed over the final
name; it is refused with 409 when that name exists unless the request asks to replace it. The
upload body is capped by `[server] liner_upload_max_mib` before any route reads it.
These checks stop a browser request from reaching outside the liner folder. They do not defend
the folder against a local process or network-share user who can already write to it: such a
user can change the liners directly, so AutoDJ uses plain file operations there and does not
require the folder to be private (a group-writable NAS share works).
The liner *root directory* comes only from configuration. `/api/playback-settings` rejects
`liners_folder`, like any other unknown field, with 422 before applying anything.
`web_state.json` keeps only fields a settings route accepts, and on restart each saved value
must pass that route's field rule, so the file can neither store nor restore `liners_folder`.
Treat a paired browser as trusted.

Time check settings, preview, and test routes under `/api/time-checks` also require a paired
browser. The browser receives clip coverage and the station time zone, but no configured clip
paths or TTS command. It can change only enabled, hour format, and rule; paths and executable
arguments come from local configuration. TTS runs with an argument array and a 20-second timeout,
without a shell, on a worker thread. The spoken text is built on the server from the station clock
("It's 3:05"); no browser input reaches the command line or the SAPI script, which speaks its
stdin as plain text. Preview and test can start the TTS program, once for each new minute's text,
because results are cached; one synthesis runs at a time.

`GET /api/history/export` returns the play log for a date range: titles, artists, albums, ISRC
and label tags, file paths inside the music folders and performance counts. Like every other `/api`
route it needs an allowed Host, and a session whenever the server requires one. It is a GET with
no side effects, so it is not Origin-checked; a cross-site page cannot read the response. It
takes only dates, a format, a service name of at most 200 characters and a one-letter category,
and it reads only the ISRC and label tags of files already in the play log.

To count performances, the stream route keeps each stream client's address and User-Agent in
memory while it is connected, until the song it heard ends, and for 10 seconds after it
disconnects, so a quick reconnect counts once. They are never written to disk; the play log stores only the count. A client that
reconnects with a different User-Agent each time, or from many addresses, can inflate the count
of the song playing; the listener limit caps how many connections it holds at once.

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
prints the addresses, and a pairing code only while no browser is paired; it never prints the
token. `--lan --insecure-lan` keeps the
automatic allowlists without any token.

Name lookups for detection (the fully qualified name and the resolver addresses) run in a
background thread that start-up waits on for at most two seconds, so broken DNS cannot stall
startup. A fully qualified name is used only when it extends the hostname and is not in a
reverse-DNS zone (`in-addr.arpa` or `ip6.arpa`), so ISP reverse-DNS names are not allowed;
`<hostname>.home.arpa` (RFC 8375) is kept. Names whose first label is `localhost` are
skipped. Inside a container with its own network the start-up block prints only the explicitly
configured hosts, because container addresses are not reachable from the network; they are still
allowed. The Compose service uses the host network and sets `AUTODJ_HOST_NETWORK=1`, so it prints
the detected addresses as a bare-metal server does.

## Radio stream listening

`GET /api/nowplaying` and `HEAD /api/nowplaying` are public and exempt from pairing, and so is the
status page `GET /status` (and HEAD), which renders the same snapshot as HTML.
They expose only the station name, artist/title/album/duration/timing of the current
and optional next song, a capped recent history, the local stream listener count and
the stream path. They contain no file paths, internal IDs, credentials or pairing
state. The response is cached on track and listener changes, and only this route has
the wildcard CORS header. The next song is left out unless the operator turns on `show_next`.
The feed adds today's peak listener count and the server uptime; both reveal roughly when
AutoDJ restarted and how popular the station is, which the operator accepts by running a public
station. The status page HTML-escapes every value (station names and song tags are
operator- or file-controlled text), sends `Content-Security-Policy: default-src 'none';
script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'`,
`X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`, and loads only
`/modules/status-page.js` and `/modules/status-page.css`, which were already public with the
rest of `/modules/`. It has no forms or controls, so framing it elsewhere gains nothing.

Configured webhooks make outbound HTTP requests to operator-chosen URLs after track
changes. Their URLs can contain tokens and are accepted only from `config.local.toml`;
the paired status API exposes the host and last result, never the URL or response body.
The send-test route requires pairing. Operators should choose destinations they trust,
since each receives the public now-playing metadata.

Stream mode (`autodj serve --stream`, or `[stream] enabled = true`, which the Compose service
reads from `config.toml`) serves the live mix as MP3 at `/stream.mp3`, plus `/stream.m3u`, a
one-line playlist naming the same address. Like an Icecast mount with no listener
authentication, listening is public: anyone who can reach the server and knows the address can
listen. This is deliberate. A station is meant to be heard, and radio players such as Sonos,
Winamp and car head units cannot pair or sign in.

What a listener can and cannot do:

- The two stream paths carry audio only. They have no control surface: a listener cannot pair a
  browser, change settings, see the queue, or reach any other endpoint. They join the short list
  of routes exempt from the session cookie check (`_PUBLIC_FILES` and `_is_public_path` in
  `security.py`): the page's own static files and modules, `/healthz`, `/api/version`,
  `/api/auth/status` and `/api/pair`.
- Every other check in `SecurityMiddleware` still applies to them, including the Host allowlist,
  so the name typed into the player must be one this AutoDJ instance is configured to accept.
- `/stream.` followed by anything else, and any path under `/stream/`, is not public: it needs
  a session like any other path, and then returns 404. Optional HTTP Basic credentials protect
  only `/stream.mp3` and `/stream.m3u`; the playlist never includes them.

What the operator should know:

- The stream carries the operator's own library and voice liners to anyone who can reach it.
  Network reach decides exposure, not a secret. On a home LAN, everyone on the LAN can listen.
  Through a tunnel, a port forward or a public address, anyone on the internet who learns the
  address can listen. Sharing the address is the same as inviting listeners.
- An optional listener password in `config.local.toml` limits who can listen, but HTTP Basic
  carries it in plain text unless AutoDJ serves HTTPS. Some radio players cannot send it. A
  tunnel access policy is another option when the player supports it.
- `max_listeners` (default 8) bounds all connections. `max_listeners_per_address` (default 0,
  off) can limit connections from one IP address. Operators can disconnect and ban addresses or
  CIDRs in Settings, Stream; bans persist and apply immediately. These controls
  reduce listener-slot exhaustion, though distributed clients and clients behind a shared
  address can still fill the slots. Forwarded addresses count only when the direct proxy peer
  is in `trusted_proxies`. Untrusted peers cannot change their address using forwarding headers;
  trusted proxies must append or replace those headers correctly. A single IPv6 address is
  counted; one host can use multiple addresses in a /64. An operator who needs a large or
  untrusted audience should relay the stream to a dedicated Icecast or
  Shoutcast server (roadmap issue #37) rather than expose AutoDJ itself.
- Listener password checks run before ban checks, so unauthenticated requests do not reveal ban
  status. Five failed Basic-auth attempts per address per minute trigger a temporary throttle.
  Paired browsers can see listener addresses and User-Agent names. `listener_bans.json` contains
  banned IP addresses and is included in backups.

Removed keyed links:

- Earlier versions put a secret in the listening address, `/stream/<secret>.mp3` and
  `/stream/<secret>.m3u`. Those links and the `POST /api/stream/rotate` route that replaced the
  secret have been removed, so there is no stream secret left to leak or guess.
- AutoDJ no longer creates or reads `<index_dir>/.stream-secret`. An existing file is left in
  place, is not used, and can be deleted.

HTTP framing:

- In stream mode Uvicorn uses the h11 protocol implementation for every route, so HTTP/1.0
  players receive close-delimited audio instead of chunked framing they cannot read. This changes
  parsing performance, not what is accepted: the same middleware, limits and routes apply.

## Request and audit records

HTTP responses receive `X-Request-ID`. WebSocket connections also receive an internal request ID.
Audit records use fixed JSON fields for request ID, action, outcome, method, route template, and
status. They do not include tokens, request bodies, query strings, client-supplied filenames, or
music paths.

Rejected requests are audited. Successful or rejected unsafe HTTP actions are audited after
response status is known. The WebSocket only pushes state; frames a browser sends on it are
ignored. WebSocket connection, error, and disconnect events are audited. These records support
single-user incident review but do not provide per-user attribution.

## Relay credentials (outbound)

Relays (`[stream.relays.<name>]`) push the stream to external Icecast or Shoutcast servers.
This is the only place AutoDJ holds a credential for another system:

- Each relay's source password comes from `config.toml`, `config.local.toml` (recommended, so the
  shared file can be committed or copied without it) or `AUTODJ_STREAM_RELAY_<NAME>_PASSWORD`.
  An environment variable only sets the password of a relay a TOML file already names; it
  cannot add a relay.
- The password is never written to `web_state.json`, the relay status file, logs, `autodj
  doctor` output, any `/api` response or the WebSocket state. The browser sees only each relay's
  name, server type, host, port, mount or stream ID, state and last error. Error texts are built
  from fixed wording, HTTP status numbers and operating-system error descriptions, never from
  request URLs, so they cannot carry the password. The configuration object hides it from its
  printed form.
- On the wire the password goes only to the configured host, as each protocol requires: in an
  HTTP Basic `Authorization` header for Icecast (the source login and `/admin/metadata`), and as
  plain text for Shoutcast (the source login line and the `pass` query parameter of
  `/admin.cgi`, the only way Shoutcast accepts title updates). Relays do not use TLS, so anyone
  who can watch the path to the server can read the password. Use a source password that is not
  used anywhere else, and prefer hosts reached over networks you trust.
- `autodj doctor` only opens and closes a TCP connection to each relay's source port; it sends
  no password and does not log in.
- The running server writes `relay-status.json` in the index folder (names, states and errors,
  no passwords) so `autodj doctor` can show the last reported state.
- Relays are outbound connections only. They add no listening port and no route, and the
  server's replies are read only up to a fixed size during login and title updates.
- While any relay is enabled the station keeps playing with no local listeners, so the operator's
  library is being sent to the external server whenever AutoDJ runs in stream mode. Set
  `enabled = false` to stop a relay without deleting it.

## Backup and restore boundary

A backup is a plain ZIP file: the published index generation, `dj_meta.db`, `web_state.json`,
the liners and profiles folders, and a `manifest.json` naming the
AutoDJ version and the files. It is safe to make while AutoDJ serves: the index is copied under
its publication lock and `dj_meta.db` through SQLite's backup API. The archive is written to a
temporary file and renamed into place, and it may not be written inside the liners or profiles
folder.

Restore requires a backup from the same major and minor version. It refuses member names that
are absolute, contain `..`, backslashes or drive letters, or fall outside the known index files,
liners and profiles, and it refuses an archive whose files differ from its manifest.
Every file is unpacked beside its destination and the index is checked before anything is
replaced. Restored liners and profiles folders replace the current ones whole. The index files
are checked against the SHA-256 digests in their manifest; other files have no digests, and there
are no free-space checks or rollback: an interrupted restore is run again. The operator must
stop AutoDJ first, because restore cannot tell whether it runs. Filesystem locations remain
trusted through operating-system ACLs. AutoDJ does not defend against an attacker who already
controls them and can race filesystem operations.

## Container and dependency controls

The container image runs as UID/GID 10001 by default with all capabilities dropped and
`no-new-privileges`. The Compose service can run as the host folders' owner instead
(`AUTODJ_UID` and `AUTODJ_GID`), so bind mounts need no ownership changes. It mounts its own
folder read-only for `config.toml`. Base images and the copied `uv` binary use immutable digests.
CI builds and smoke-tests the image. It verifies the host network, pairing before any API access,
folder ownership, and the health check, generates a CycloneDX SBOM, and blocks on Trivy HIGH or
CRITICAL findings with fixes available.

`uv.lock` and `package-lock.json` are committed. CI uses frozen installs, runs `pip-audit` and
`npm audit`, scans source tree with Trivy and OSV-Scanner, checks secrets with Gitleaks, and produces
dependency and container SBOM artifacts. Neither `pip-audit` nor OSV-Scanner ignores any
advisory.

## Reporting a vulnerability

Use the private process in [SECURITY.md](SECURITY.md).
