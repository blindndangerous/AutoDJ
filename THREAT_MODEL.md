# Threat model: AutoDJ

Last reviewed: 2026-10-13. Re-review every major release.

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
- Backup and restore trust the configured index, liners, profiles, clockwheels and Stereo Tool
  preset locations. Restore checks member names and the AutoDJ version before it replaces anything.
- Clockwheel routes require a paired browser. Names are checked as safe file names (no path
  separators, dots or Windows device names) before any file is touched. Steps name a registered
  library folder by ID plus a path inside it with no `..`, drive letter or backslash, and a track
  step must be a track the catalog holds; the browser never sends a full path. Unknown fields and
  step types are refused. PAL import parses a limited declarative subset and never executes
  uploaded text.
- Rotation category routes (`/api/categories`) require a paired browser and refuse unknown
  fields. A playlist category names a file inside the configured playlists folder by a relative
  path with no `..`, drive letter or backslash, and the resolved file must stay inside that
  folder; the browser never sends a full path and the folder is set only in `config.toml`. The
  beets query is parsed by AutoDJ itself (`autodj.beets_query`), never by a shell or as SQL:
  field names select columns only from the beets table's own column list, values are SQL
  parameters or compared in Python, and the beets database is opened read-only. Regular
  expressions are refused, so a query costs at most one pass over the library, and playlist
  files larger than 8 MB are not read. `categories.json` is written atomically and a damaged one
  is set aside, not used.
- The hour preview (`/api/hour-preview`) requires a paired browser. It only reads the station:
  the playing render, the render cursor, the open song change and the queue; its dry run picks
  with a copy of the player that has its own random generator, so it cannot steer the station's
  real random choices. Its cost is bounded: the span is 15 to 180 minutes, every preview runs on
  one worker thread below normal priority, the same station and span share one preview, at most
  8 previews wait (one more gets 503), the dry run stops at 80 songs or after about 4 seconds
  (checked before each pick), and a preview is reused for 45 seconds while what is fixed stays
  the same. Shutdown joins the worker and leaves the library open if it has not stopped.
- Machine tokens let a program on another computer use one narrow API without a browser. The
  only scope so far is "analysis worker" (`autodj worker`); see "Machine tokens and analysis
  workers" below.
- Broadcast recordings contain listener-facing encoded audio only. The recording folder is
  chosen in `config.toml` or `config.local.toml` and is never accepted as an API parameter or
  sent to the browser.
  Recording status and controls are paired routes; the public stream cannot control recording.
  Backups include the recording settings and running state, not the recorded files.

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
`repeat_rules.related_artists_file` is also rejected by the API and saved state. The only
related-artists file the browser can enable is `related-artists.toml` in the active index folder.
AutoDJ checks its size before reading and refuses files larger than 1 MiB. Browser errors do
not include artist names or unexpected top-level keys from that file.
Treat a paired browser as trusted.

The cart wall routes under `/api/carts` require a paired browser like every other `/api` route.
A slot names a voice liner by catalog ID or liner-folder file name, or an uploaded clip by file
name, never a path; the names pass the same checks as `/api/liners/file`. Uploaded clips go to
the `carts` folder in the active index folder, with the voice liner size cap and extension list,
are never overwritten, and are deleted again if they do not decode. `carts.json` is validated
when the server starts, and a file that fails is set aside. Backups include the wall and the
uploads. A paired browser can play carts into the server audio or stream output.

Time check settings, preview, and test routes under `/api/time-checks` also require a paired
browser. The browser receives clip and voice pack coverage, pack names (folder names) and the
station time zone, but no configured paths or TTS command. It can change only enabled, hour
format, rule, which configured packs are ticked and their order, and it names a pack to test
or preview; a name that is not configured is refused, so no browser input becomes a path. Paths
and executable arguments come from local configuration. TTS runs with an argument array and a 20-second timeout,
without a shell, on a worker thread. The spoken text is built on the server from the station clock
("It's 3:05"); no browser input reaches the command line or the SAPI script, which speaks its
stdin as plain text. Preview and test can start the TTS program, once for each new minute's text,
because results are cached; one synthesis runs at a time.

`/api/stereo-tool-presets` lists, uploads (`/upload`) and deletes (`DELETE /{name}`) Stereo Tool
presets, under the same session, Host and Origin policy as every other `/api` route. Uploads go
only to the `stereo-tool-presets` folder beside the active index folder. A name must be one plain
filename ending in `.sts`, at most 100 characters of letters, digits, spaces, dots, hyphens and
underscores, starting with a letter or digit, so it carries no separator, drive, colon or `..`;
Windows device names such as `CON.sts` or `COM0.sts` are refused. The body is capped at 2 MB
before the route reads it, and the file must contain no NUL byte, a `[section]` line and a
`key=value` line. These checks only screen out files that are plainly not presets: Stereo Tool
itself parses the contents, so a paired browser can hand that parser any text up to 2 MB. An
upload is written to a hidden `.<random>.part` file, flushed, then hard-linked to its final
name, so it never replaces an existing preset; that name answers 409. The bundled preset lives
in the package, is addressed only as `included`, and cannot be uploaded over or deleted. Delete
takes the same name check, refuses symbolic links and refuses the preset Stereo Tool is set to
use, whatever its letter case. Selecting a preset in `/api/playback-settings` requires an
existing file; a preset removed outside AutoDJ is replaced by the included one when Stereo Tool
next starts.

`POST /api/library/tags` rewrites the tags of one recording, under the same session, Host and
Origin policy as every other `/api` route. It is the one route that changes a music file. The
path must name a recording the catalog holds under a registered, present source folder, and its
real path (after following links) must still lie inside a registered folder, so a link swapped in
after a scan cannot point the write elsewhere. The body is capped at 32 KiB before the route
reads it and may carry only the eight known tag names, each a
string that is checked (length, no control characters, year, BPM, key and ISRC shapes) before
anything is written; tag values go into mutagen frames, never a path, command or SQL text. The
file is tagged as a hidden copy in its own folder, read back, and renamed over the original, so a
failure leaves the original intact; a song playing or up next is refused. With the page's
consent and `[library] beets_db` set, the same values update that beets item with bound
parameters on fixed column names. The beets database is the only file outside the music folders
the route writes.

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

## Machine tokens, analysis workers and announcer breaks

A machine token is a bearer secret for a program, not a person. Settings, Analysis workers
creates, lists and revokes them; those routes (`/api/machine-tokens`) are ordinary paired-browser
routes and need a session when pairing is on. `autodj tokens list|create|revoke` does the same
from a shell on the server (it can read and write the token database anyway). Two scopes exist:
`analysis-worker` for `autodj worker`, and `announcer` for a program that sends one station
spoken breaks (#88, see [Announcer breaks](#announcer-breaks)).

- A token reads `adj1.<id>.<secret>`: 32 hex digits naming the row and 256 random bits. It is
  shown once, in the reply to the request that created it (sent with `Cache-Control: no-store`),
  and never again. `.machine-tokens.sqlite3` in `[index] index_dir` keeps its name, scopes,
  creation and last-use times and the SHA-256 of the secret, never the secret. The secret has
  full entropy, so a plain digest is enough; a copy of the database grants nothing. Backups leave
  the file out, like the paired-devices database.
- Every check compares digests in constant time and answers a wrong secret, an unknown id, a
  revoked token and a missing scope with the same 401. Revoking takes effect on the next request
  and gives back any songs the worker had claimed.
- Each token carries scopes, and each machine route names the one scope it needs: a worker token
  opens only the routes under `/api/worker/`, an announcer token only those under
  `/api/announcer/` and `/api/stations/<slug>/announcer/`. A machine token is no session and
  opens no browser route. A browser session opens no machine route: they need
  `Authorization: Bearer`, and the middleware refuses any request to them that carries an
  `Origin` header, which every browser page sends. The middleware treats a path under
  `/api/stations/<any name>/` like the same path under `/api/`, so a machine route cannot be
  reached as a browser route by another address.
- A bearer token is refused over plain HTTP unless the client is on this machine (loopback): the
  server answers 403 "Machine tokens need HTTPS". `autodj worker` itself refuses to send its token
  to `http://` on another host, and never follows a redirect, which would hand the token to the
  new address. Use AutoDJ's own TLS (`[server] ssl_certfile`); a self-signed certificate works
  with `autodj worker --ca-cert`. The token can come from `AUTODJ_WORKER_TOKEN` instead of the
  command line, which keeps it out of shell history and process lists.
- Host allowlists still apply to worker requests.

What a worker can do with a valid token:

- Claim up to 16 songs at a time that wait for analysis. Each claim is a lease kept in
  `library.db` that runs out ten minutes after the worker last reported in, so a dead worker
  strands nothing. The claim reply describes songs by lease id, extension, size, fingerprint,
  title, artist and length; it never sends a server path.
- Read the original bytes of a song it holds a live claim on (`GET /api/worker/audio?lease=`),
  and nothing else. The lease id must match the token, and the file must still have the size and
  fingerprint it was claimed with.
- Post one result per claimed song. The server refuses it unless the model id, the model weights
  (the snapshot commit of the server's downloaded copy, or a SHA-256 digest of the weight files
  of a `manual_path` folder; never the branch name, which two machines can resolve to different
  weights), the analysis version and the vector size equal its own, the vector is exactly 1040 finite float32 values
  of unit length, the analysed fields are in range, and the record and the file on disk still have
  the claimed size and fingerprint. A result for a changed file is refused as stale. A result can
  change only a song's energy, key, mode, BPM estimate (when the song has no BPM tag), tempo
  confidence, stale-check time and vector; it cannot touch paths, tags, cues, plays or settings.
- Report a song it could not analyse (held back for six hours) and give unanalysed claims back.

Limits: each token may make 600 requests a minute (429 with `Retry-After` past that); a result
body may be 64 KiB and the other worker bodies 8 KiB (413 past that). A worker's failure reason is
cut to 300 printable characters before it is logged. A malicious worker holding a valid token can
still post wrong but well-formed vectors for the songs it claims, which would make those songs
match badly in search; revoke the token and re-analyse them with `autodj index --force`. Give
tokens only to machines you trust with that.

`autodj worker --before-command` and `--after-command` run a program the operator typed, split
like a shell would but without a shell, with a two-minute limit. They come from the worker's own
command line, never from the server.

### Announcer breaks

An announcer token is bound to one station when it is created; the station is stored with the
token and every announcer route refuses a token for another station with 403. The program is
meant to run on the station's machine and reach AutoDJ at `127.0.0.1`, so the token never crosses
the network; from any other machine it needs HTTPS as above.

Loopback is trusted only when no proxy carried the request. A reverse proxy on the same machine
(nginx, Caddy, a tunnel) connects from `127.0.0.1` too, so on plain HTTP any machine-token request
(worker or announcer) that carries `Forwarded`, `X-Forwarded-*` or `X-Real-IP` is refused with
403, even from loopback. A proxy must terminate TLS and send `X-Forwarded-Proto: https`; the
server takes that header only from a proxy it trusts: the addresses in `[stream]
trusted_proxies` when that list is set, and otherwise a proxy on this machine (`127.0.0.1` and
`::1`, Uvicorn's own default, now also when the server streams), so a TLS reverse proxy on the
same host keeps working with no configuration. Only then is the request HTTPS. A request with no
proxy headers is judged as before: loopback on plain HTTP, anything else over HTTPS. A proxy that
strips every forwarding header and speaks plain HTTP to AutoDJ defeats this check; do not run one
in front of machine routes. The token and its secret are never
logged or returned after creation; log lines name a break by the first eight characters of its
own random id and the token by its name.

What an announcer can do with a valid token:

- Read its station's playing song, next song, the open slot, the latest ten plays and the first
  ten queued songs: titles, artists, albums, years, lengths and library ids, never file paths.
- Upload a break for the open slot. The body is capped by the middleware at the largest
  `[breaks] max_upload_mb` of the running stations plus 64 KiB, before any route reads it; the
  route refuses a declared `Content-Length` over its own station's limit before reading the body,
  and stops writing at that limit (413). Before the body is read, the route also holds room for
  the upload in the station's caps (`max_queued`, and `quota_mb` by the declared length) under the
  desk's lock, and moves it to the slot the form names (`max_per_slot`) before the file is
  stored, so parallel uploads cannot pass the caps together. Whatever ends the request (a
  refusal, a failure, a timeout, the client hanging up) gives the room back before the handler
  awaits anything else. The body must arrive within 60 seconds plus one second per 256 KiB it
  declares (408 past that), so a client that trickles its body cannot hold the room. The form is
  read by Starlette's `request.form` with `max_files=1`, `max_fields=16` and `max_part_size=4096`;
  that size limit covers the text fields only, and the file's size is held by the middleware's cap
  and the station's limit as it is written. python-multipart (pinned at 0.0.32 or later) refuses
  a part with more than 8 headers or a header over about 4 KiB. The file name is the
  server's own (`brk-<random id>.<extension>`), written to the station's `announcements` folder
  through a hidden part file, so the request's file name chooses only the extension, from the
  liner audio types (415 for any other).
- Have it decoded on one dedicated thread, one upload at a time across the server. The gate
  opens when that decode is over (finished, failed, or cancelled before it started), never when
  the request gives up while it runs, so a client that hangs up cannot start a second decode
  beside it and a cancelled one cannot keep the gate shut. The
  header is read first (libsndfile, or `ffprobe` for MP4 and AAC): a file over 192 kHz, with more
  than two channels, longer than `max_seconds`, or whose contents are not the type its extension
  names is refused (422) before any sample is decoded, so a small file that claims hours of
  multichannel audio at a huge rate cannot make the server allocate gigabytes. libsndfile reads
  only files already at 44.1 kHz; anything else goes through FFmpeg with the demuxer named from
  the extension (nothing is probed, so a text body named `.mp3` is never read as an HLS or concat
  playlist), `-protocol_whitelist file,pipe`, stereo 44.1 kHz output, `-t` at the length limit,
  and its output read in chunks against a byte budget (the process is killed past it, or after two
  minutes). libsndfile is read in blocks against the same budget, counting the frames it actually
  returns, because an MP3 or Ogg header can understate the length. The samples then use the read
  buffer in place, and finding the speech works ten seconds at a time, so one break takes about
  0.35 MB per second of `max_seconds` (which may be at most 900; `max_upload_mb` at most 200). The
  same checks run again when the break is mixed in. FFmpeg's demuxers still parse
  the upload, so a malformed file reaches them; the scope only lets a program the operator trusts
  do this.
- Look up and cancel its station's announcer breaks, by their random ids. Voice tracks that
  operators booked in the web UI are not listed to it and answer 404 to it.

It cannot change the queue, settings, clockwheels, liners or anything else. A booked break pins
the song after it, so a token can keep one song in place for one song change; it cannot choose
that song, which the station's own pick made. Each station holds at most `max_queued` waiting
breaks, `max_per_slot` per song change and `quota_mb` of waiting files (429, 409 and 507 past
those), each token may make `requests_per_minute` requests (429 with `Retry-After`), and a break
waits at most `max_expires_seconds`. A break mixed into a render that is thrown away unplayed is
dropped at once, and a rendered break that has not started ten minutes after its expected air time
expires, so neither can hold the station's caps. Only a reset, a jump or a stop throws away a
render that carries breaks: such a render is never judged stale by a queue edit, so it never
becomes a fallback that a re-render replaces. Expired, dropped and aired breaks are deleted by
the station's once-a-second upkeep, off the event loop (a file still held open is tried every
half minute until it goes);
files a previous run left are deleted when the station starts, except an upload still arriving
(its name is held, and part files younger than ten minutes are left).

### Voice tracks from the browser

The voice tracker (#128) books the same breaks from the web UI. Its routes,
`/api/voice-tracks` and `/api/stations/<slug>/voice-tracks`, are browser routes: they need the
signed-in session cookie when `access_token` is set and, for an upload or a cancel, an allowed
`Origin`, like every other change. They are outside the announcer prefix, so a machine token is
never accepted there, and an announcer token never reaches them by another address. The upload
takes the announcer path described above: the middleware's body cap (the same as an announcer
upload), the declared length checked against the station's own limit before the body is read,
the room held in the station's caps, the 60-second body deadline, the server's own file name, the
one-at-a-time decode with the header checked first, and the quota, slot and per-slot limits,
which announcer breaks and voice tracks share. A voice track is always a DJ break. Uploads and
cancels count against the station's `requests_per_minute`, shared by every open page; reading
the open slot does not. A cancel finds only voice tracks, so a page cannot drop an announcer's
break, and the announcer routes list, look up and cancel only announcer breaks, so an announcer
token cannot see or drop an operator's voice track. Anyone who can sign in to the web UI can already control the station, so a voice track
gives them no new power over it.

In the browser, the microphone is asked for only when Record is pressed, and every track of the
stream is stopped when the recording stops, is discarded, or the page signs out, and recording
stops by itself when the Voice tracks tab is left or the page is hidden, so the microphone is
never left open in the background. The browser
needs a secure context for the microphone: HTTPS, or `localhost` on the same computer. The take
stays in the page until it is booked, and only the WAV made from it is sent.

On-air safety: nothing an announcer does runs on the audio thread or waits for it. The breaks are
mixed by the render-ahead worker one song ahead, as liners are; the mix bus only marks a break
on the air through a short lock; the desk writes its log lines after releasing that lock. A break
that cannot be decoded at render time is dropped and the music renders without it. The song
change after the next song is picked early only on stations with `[breaks] enabled = true`.

Every FFmpeg and `ffprobe` command that decodes a file or an upload now passes
`-protocol_whitelist file,pipe`: library decoding (`load_with_ffmpeg`), loudness measurement and
processed decoding, the intro lift, the duration probe, liner and cart decoding from uploads, and
the ALAC transcoder for browser playback. A library file or a liner that probes as a playlist can
therefore reach no network address. The encoders that read raw samples AutoDJ writes to their
pipe (the stream encoder and the mastering chain, `-f f32le -i pipe:0`) probe nothing and need no
whitelist. Only break uploads also force the demuxer, because liners, carts and library files may
legitimately be any type FFmpeg reads.

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

Every station has the same two public surfaces under its own name:
`GET|HEAD /api/stations/<slug>/nowplaying` and `GET|HEAD /stations/<slug>/status`. The default
station keeps `/api/nowplaying` and `/status`, and also answers by the name `default`. These are
public only for a configured slug; any other name, and anything else under a station's name,
needs a session first and then returns 404. Each station's feed and page carry only that
station's name, songs, listener counts and public stream path, from its own snapshot. A station's
page names its own feed in a `data-feed` attribute, and the page script polls it only when it
is a path under `/api/` that resolves to the page's own origin and has no backslash, written
out or percent-encoded; anything else falls back to `/api/nowplaying`. So the attribute cannot
send the page to another host (the `connect-src 'self'` policy also refuses that). Every
station's feed, live (200) or off air (503), carries `Access-Control-Allow-Origin: *`. While a station is stopped, its feed answers 503
`{"detail": "That station is off air"}` (still with the wildcard CORS header, so outside sites
can read it) and its page answers 503 with its name and "off air". Like the stream's 503, this
shows that the slug exists and gives the station's configured name; that is accepted, since a
station's name is public whenever it is on air. Listener Basic credentials protect only a
station's stream, never its feed or page, as for the default station.

Configured webhooks make outbound HTTP requests to operator-chosen URLs after track
changes. Each station has its own (`[[stations.<slug>.webhooks]]`), which receive only that
station's feed. Their URLs can contain tokens and are accepted only from `config.local.toml`;
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
  only `/stream.mp3` and `/stream.m3u` (and the `/stations/` stream paths below); the playlist
  never includes them.
- Each station's stream is also public at `/stations/<slug>.mp3` and `/stations/<slug>.m3u`,
  but only for a configured slug (`[stations.<slug>]`). The default station's is
  `/stations/default.mp3`, public with the same credentials as `/stream.mp3` even on a server
  with one station. Any other `/stations/...` path needs a session and then returns 404. A
  stopped station answers 503, which shows that the slug exists; this is accepted. Each
  station's listener credentials, bans and listener limits are its own: a ban on one station
  does not cover another, and the listener slots of all stations add up. Starting and stopping
  a station (`POST /api/stations/<slug>/start` and `/stop`) and the station list
  (`/api/stations`) need pairing, which is still server-wide: a paired browser controls every
  station. Slugs are checked against a strict pattern before they name a folder.

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

## Listener requests

The request page (`GET|HEAD|POST /requests`, and `/stations/<slug>/requests` for a configured
slug) is the first feature that lets anyone who can reach the server change what a station does.
It answers 404 until the station's `[requests] enabled` turns it on, and then needs no pairing.

What a visitor can do, and the limits on it:

- **Search the library.** Results are at most 20 songs, showing only title, artist and album.
  Songs in blocked folders or by blocked artists are left out. A search needs 2 to 100 characters
  and is matched as plain words in memory, never as SQL, a pattern or a path. Each address may run
  30 searches a minute. The library's titles become readable to anyone who can reach the page;
  the operator accepts that by turning requests on, and the blocklists keep chosen folders and
  artists out.
- **Name a song.** Songs are named by an opaque id: a keyed BLAKE2b hash of the file path, under
  a key made when the server starts. The id reveals nothing about the path and cannot be computed
  from one. The page never contains a path. An id that matches nothing, or matches a blocked song,
  gets the same "Song not found" answer, so ids cannot be used to probe for hidden files.
- **Send a request.** The form is `application/x-www-form-urlencoded` and holds only `token`,
  `track` and an optional `name`, each given once; anything else gets 400. The body is capped at
  2048 bytes, and a longer body gets 413 before it is parsed. The name is trimmed, at most 40
  printable characters, and HTML-escaped wherever it is shown, to the operator too.
- **Cross-site posts.** These are refused twice. Like every unsafe request, the POST must carry
  exactly one allowed `Origin`. The form also carries a signed token: an HMAC-SHA256 of the
  station's name and the time it was issued, under a per-process key. The token is good for two
  hours and only for that station. There is no session and no cookie, and the page sends
  `Cache-Control: no-store`.
- **Abuse limits, with no CAPTCHA.** Each address may make 10 form posts per ten minutes, and
  `per_listener` accepted requests per `per_listener_minutes`. The station has a daily cap,
  `daily_limit`, and a cap on requests waiting, `max_pending`. Over a limit the answer is 429 with
  `Retry-After`. The per-address tables keep at most 4096 addresses and forget idle ones.
- **What cannot be bypassed.** Each accepted request still has to keep the repeat rules and, in
  DMCA mode, the DMCA limits at the place it would play. It is never played at once or at a set
  time, and with `approval = "manual"` (the default) nothing reaches the queue until the operator
  approves it.

Addresses behind a reverse proxy count as one unless `[stream] trusted_proxies` names the proxy,
as for the listener limits, so one visitor behind a shared address can use up that address's
allowance for everyone behind it. Many addresses (a botnet) can still fill the daily cap. That
denies requests for the day but nothing else, since the operator decides in manual mode.
Listener addresses are held in memory only, never written to `listener_requests.json` or logs.
The file keeps titles, names, times and the decision. The page's policy is `default-src 'none';
script-src 'self'; style-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors
'none'`, with `X-Frame-Options: DENY`, `nosniff` and `no-referrer`.

The operator's side, `GET /api/requests` and `POST /api/requests/<id>/approve|play-next|reject`,
needs a paired browser like every other station route. It never returns a path. Ids are random
128-bit hex values and are checked against a strict pattern before lookup.

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
the liners, profiles and uploaded Stereo Tool preset folders, and a `manifest.json` naming the
AutoDJ version and the files. It is safe to make while AutoDJ serves: the index is copied under
its publication lock and `dj_meta.db` through SQLite's backup API. The archive is written to a
temporary file and renamed into place, and it may not be written inside the liners, profiles or
uploaded preset folder.

Restore requires a backup from the same major and minor version. It refuses member names that
are absolute, contain `..`, backslashes or drive letters, or fall outside the known index files,
liners, profiles and uploaded Stereo Tool presets, and it refuses an archive whose files differ
from its manifest.
Every file is unpacked beside its destination and the index is checked before anything is
replaced. Restored liners, profiles and uploaded preset folders replace the current ones whole.
The index files
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
