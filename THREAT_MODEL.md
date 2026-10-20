# Threat model: AutoDJ

Architecture discovery starts at the [ADR index](docs/adr/README.md).
[Proposed access-boundary ADR 7](docs/adr/0007-separate-browser-worker-and-listener-access.md)
summarizes existing trust separation for review and links its code/tests;
this threat model retains detailed security policy and limitations.

Last reviewed: 2026-10-19 (authenticated proxy boundary and bounded SSO sockets). Re-review every major release.

## Scope and trust boundaries

The [speech studio](docs/operations.md#speech-studio) adds browser-control routes
for shared provider connections and station-local production. They use the same
loopback/LAN authentication and Origin rules as other controls; machine, worker,
announcer and listener credentials do not authorize production. JSON writes are
capped at 64 KiB before parsing. Credentials live in an owner-only private file,
are write-only to the browser and are excluded from backups and errors.
Only configured endpoints can receive text. Addresses are checked and pinned for
each connection; metadata/link-local destinations, redirects and remote result URLs
are refused. Trusted-local HTTP is an explicit setting and does not encrypt traffic.
Each operation runs in a bounded owned child with cancellation and a wall deadline;
audio size, duration, temporary storage and admission are bounded. Generation and
retries require explicit idempotent requests and can incur provider charges.
Provider identity and health are separate from protocol compatibility; mocked
checks do not certify third-party services or the intelligibility of their output.

AutoDJ is a single-user local music player with these exposed surfaces:

- CLI commands read local audio and configuration, then write index, profile, liner or backup
  data as the invoking user. `library.db` keeps shared track metadata, cues, and analysis vectors. Each
  station's `station.db` keeps its play history and performance counts;
  `autodj export-plays` reads the selected station's history and writes a CSV
  or text report to standard output or to the `--out` file.
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
- Shows (`/api/shows`, #106) are browser routes like every other change: the signed-in session
  and an allowed `Origin`, never a machine token. A show's list holds file paths, but a song can
  only be added when it is a song in the library, and on the air each path is looked up in the
  library again, so a path edited into `shows.json` never reaches the player. Generating a list
  is the rotation tester's dry run on a copy of the player (its own clock, history and random
  generator, nothing written), one at a time for the whole server (409 for another), on its own
  thread below normal priority, for at most 20 seconds, cancelled at shutdown. A show holds at
  most 500 songs and a station at most 200 shows; request bodies are capped before they are read
  (8 KiB, 1 MiB for a list of songs), for PUT as for POST. On the air a show only queues its songs, a
  couple at a time, from the event loop's worker threads, holding `queue_lock` only to compare
  and insert; in DMCA mode a song that would break a limit is skipped, never played.
  `shows.json` is written atomically; an invalid show is skipped and kept, a file that is not a
  file of shows is set aside, and one that cannot be read just now is never written over.
- `POST /api/stations` requires a paired browser session and an allowed Origin. Machine and
  announcer tokens do not cover it. Its body is capped at 4 KiB, and the eight-station limit
  bounds how many stations it can add. The browser supplies a slug, never a path; all state
  paths come from `StationPaths`. Duplication copies only named settings and asset files and
  refuses links and junctions. It removes relay targets, listener credentials, and webhooks
  from the new definition, which is backed up as `station.json` without secrets. It never copies
  `relays.json` or `secrets.json`, so the source's own Stereo Tool key stays with the source.
- Machine tokens let a program on another computer use one narrow API without a browser. The
  only scope so far is "analysis worker" (`autodj worker`); see "Machine tokens and analysis
  workers" below.
- Broadcast recordings contain listener-facing encoded audio only. The recording folder is
  chosen in `config.toml` or `config.local.toml` and is never accepted as an API parameter or
  sent to the browser.
  Recording status and controls are paired routes; the public stream cannot control recording.
  Backups include the recording settings and running state, not the recorded files.
- Paired Stream settings can manage relay destinations and source passwords. Relay definitions
  live in a station-local `relays.json`; the file is atomically written readable by its owner
  only: mode 0600 on POSIX, and on Windows a protected access list that grants only the user
  AutoDJ runs as (no inherited entries). It is excluded from backups, and a restore skips one
  found in an archive, leaving the station's own file in place. Relay APIs and status files expose connection details and safe errors,
  never passwords. Environment passwords override saved passwords and are read-only in the UI.
  Relay source connections do not support TLS, so passwords sent to those servers are not encrypted
  by AutoDJ; use a trusted network or a server that protects its source connection separately.

Cloud sync, independent application roles, billing and public Internet hosting
outside the documented authenticating gateways are out of scope. Use TLS for any
network where observers could read HTTP traffic. In native mode, terminate TLS in AutoDJ itself
with `[server] ssl_certfile` and `ssl_keyfile` (or the matching CLI flags). Native session cookie
flags and advertised-origin checks follow AutoDJ's own TLS setting; a plain-HTTP backend does not
inherit secure native-session behavior merely because a proxy terminates TLS. Explicit
trusted-proxy mode has the separate identity boundary described below.
A proxy that forwards to AutoDJ over plain HTTP is not supported in native mode.
The plain-HTTP exception applies only to trusted-proxy mode with a required shared
secret on loopback or a private connection on the same host. Use TLS between hosts.

One supported native-mode remote setup is a Cloudflare Tunnel that connects to AutoDJ over HTTPS and checks
its certificate against the domain (`originServerName`), with Cloudflare Access (an email
one-time PIN policy) in front and `cloudflared` set to refuse requests without a valid Access
token; see [docs/operations.md](docs/operations.md#https-with-your-own-domain). Access decides
who reaches AutoDJ at all; AutoDJ's pairing and Host and Origin checks still apply behind it.
Every tunnelled request arrives from the `cloudflared` machine's address. Without a matching
`[stream] trusted_proxies` entry (which applies in every mode), the per-address pairing lockout counts all remote browsers as one
client, and someone who passed Access could use up that budget and delay other remote pairings
for one code window. Never expose AutoDJ to
the internet without an authenticating gateway, including through port forwarding.

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
  `analyse`, `prune` or `stats`. Anything else, such as `--force` or a second `--config`, is
  refused. They run with `shell=False` and a UTF-8 child pipe.

## Web request policy

No response can be framed by another page (clickjacking): `SecurityMiddleware` adds
`frame-ancestors 'none'` to every response's Content-Security-Policy (a response without a policy
gets one with only that directive) and `X-Frame-Options: DENY`, including its own rejections and
404s. Only a response whose own policy names other frame ancestors keeps it unchanged: today that
is the embeddable player alone (see "Radio stream listening").

Default native loopback mode is anonymous. For a native non-loopback bind, startup validation requires a token or
explicit insecure-LAN acknowledgement. Wildcard binds also require nonempty exact allowed-host and
allowed-origin lists.

In native mode, when `server.access_token` or `AUTODJ_ACCESS_TOKEN` is set:

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
related-artists file the browser can enable is `related-artists.toml` in the library folder.
AutoDJ checks its size before reading and refuses files larger than 1 MiB. Browser errors do
not include artist names or unexpected top-level keys from that file.
Treat a paired browser as trusted.

The cart wall routes under `/api/carts` require a paired browser like every other `/api` route.
A slot names a voice liner by catalog ID or liner-folder file name, or an uploaded clip by file
name, never a path; the names pass the same checks as `/api/liners/file`. Uploaded clips go to
the station's `carts` folder, with the voice liner size cap and extension list,
are never overwritten, and are deleted again if they do not decode. `carts.json` is validated
when the server starts, and a file that fails is set aside. Backups include the wall and the
uploads. A paired browser can play carts into the server audio or stream output.

Time check settings, preview, test, pack and clip routes under `/api/time-checks` also require a
paired browser; a machine token is refused there like on every browser route. The browser
receives clip and voice pack coverage, pack names (folder names), the names of the folders and
audio files directly in the station's time checks folder, the file name (only) of a configured
intro or outro, and the station time zone, but no paths or TTS command. It can change enabled,
hour format, rule, which packs are ticked and their order, and choose the recorded clips folder
and the intro and outro by name from what is in the time checks folder; each name must be one
plain name (no separator, drive, colon, `..`, leading dot or device name) and exist there, and
the saved file is checked the same way, so no browser input becomes a path. It names a pack to
test or preview; an unknown name is refused.

It can add a voice pack as a zip (`POST /api/time-checks/packs`, body capped at 256 MB plus
64 KiB before any route reads it) and remove one (`DELETE /api/time-checks/packs/{name}`) only
in the station's own time checks folder; packs listed in `voice_packs` and shared packs are
refused. The default station without a `clips_folder` keeps its packs in the library's shared
folder, so a paired browser on that station can remove or replace a pack every station uses; the
confirmation says so while there are other stations. The zip is streamed to a hidden file in that
folder, and the zip work runs one upload at a time per station. Before `zipfile` opens it, the
end-of-directory record is found exactly where `zipfile` finds it (at the very end with no
comment, else the last signature, which must be whole and end the file, or the upload is
refused) and must list at most 3000 entries in a directory of at most 2 MB, with no zip64, so a
small zip listing millions of entries cannot use up memory. An entry name invalid in its flagged
encoding is refused as not a zip. Then every entry is checked before anything is unpacked: absolute paths, drive letters, `..`, links and encrypted entries are
refused, and so are subfolders and anything but `.wav`, `.mp3`, `.flac` and `.ogg` plain file
names, compared after Unicode NFC and case folding (operating system leftovers such as
`__MACOSX` are skipped). At most 2000 files and
512 MB unpacked are allowed, counted from the headers and again while writing, so a header that
understates a size cannot unpack more. Each file must then pass the break header check
(libsndfile reads it as the type its extension says, at most 192 kHz, two channels and two
minutes) before the hidden staging folder is renamed into place; a refused zip leaves nothing.
Replacing renames the old folder aside first and puts it back if the rename fails (when that
fails too, the old folder's hidden path is logged as an error and kept); replace and remove
refuse links and junctions. Hidden upload, staging and removed leftovers older than an hour are
deleted when the station starts and are never backed up; a set-aside pack is only logged. One clip (`POST /api/time-checks/clips`, capped like a liner
upload) goes to the same folder under the same name and audio checks, written to a hidden file
and hard-linked into place unless the request asks to replace. A paired browser can therefore
fill that folder up to these caps per request, like the liner folder. The text-to-speech command
and its arguments come only from local configuration. TTS runs with an argument array and a 20-second timeout,
without a shell, on a worker thread. The spoken text is built on the server from the station clock
("It's 3:05"); no browser input reaches the command line or the SAPI script, which speaks its
stdin as plain text. Preview and test can start the TTS program, once for each new minute's text,
because results are cached; one synthesis runs at a time.

`/api/stereo-tool-presets` lists, uploads (`/upload`) and deletes (`DELETE /{name}`) Stereo Tool
presets, under the same session, Host and Origin policy as every other `/api` route. Uploads go
only to the `shared/stereo-tool-presets` folder in the index folder. A name must be one plain
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
short. On Linux and macOS the file has mode 0600, so only the owner can read it. On Windows it
gets a protected access list granting only the user AutoDJ runs as, in place of the index
folder's inherited permissions. An unreadable file stops startup with an error instead of silently replacing
the token. A configured `access_token` or `AUTODJ_ACCESS_TOKEN` always wins. Anyone who can read
the index directory can derive pairing codes, the same as for a token in `config.local.toml`;
deleting the file rotates the token and ends every paired session on the next start. Startup
prints the addresses, and a pairing code only while no browser is paired; it never prints the
token. The code goes to the console only: the server log file (`<index>/logs/autodj.log`, kept
seven days with default file permissions) gets the start-up block without it.
`--lan --insecure-lan` keeps the
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
which announcer breaks and voice tracks share. A voice track is always a DJ break. It can be
booked with server audio or stream output and saved-point transitions, independently of the
external announcer switch. Turning that switch off drops only waiting announcer uploads. Uploads and
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
change after the next song is picked early only when external breaks, a due ad break, or a manual
voice tracker request needs it; the tracker requests planning on demand.

Every FFmpeg and `ffprobe` command that decodes a file or an upload now passes
`-protocol_whitelist file,pipe`: library decoding (`load_with_ffmpeg`), loudness measurement and
processed decoding, the intro lift, the duration probe, liner and cart decoding from uploads, and
the ALAC transcoder for browser playback. A library file or a liner that probes as a playlist can
therefore reach no network address. The encoders that read raw samples AutoDJ writes to their
pipe (the stream encoder and the mastering chain, `-f f32le -i pipe:0`) probe nothing and need no
whitelist. Only break and matched intro uploads also force the demuxer, because liners, carts and library files may
legitimately be any type FFmpeg reads.

### Matched intros

Matched intros (#127) are voice clips an operator attaches to a song, or to the song's artist, in
Track info. Their routes, `/api/intros` and `/api/stations/<slug>/intros`, are browser routes:
they need the signed-in session when `access_token` is set and an allowed `Origin` for an upload
or a delete, like every other change, and never take a machine token. An upload has the liner
upload's limits: the middleware caps the body at `[server] liner_upload_max_mib` plus 64 KiB
before any route reads it, the declared length is checked again, the body must arrive within 60
seconds plus one second per 256 KiB, the type must be a liner audio type (415 for any other), and
the file streams to a hidden part file under a name the server makes up (`intro-<id><ext>`) in
the station's `intros` folder, never a name from the request. It is then decoded the way an
announcer break is, with the header checked first, the demuxer forced, at most 60 seconds, on the
one decode thread; a file that does not decode or holds no speech is refused and deleted. The
song is named by its library ID, which must be a song in the library; an artist intro takes the
artist from that song's record, so no free text becomes a match rule. Each station keeps at most
2000 intros. Listen serves only a file named by an intro the station keeps, never a path, and
refuses anything but a regular file. An intro in the file that is not valid is skipped (it never
plays), logged and said in Track info, and written back unchanged with every save; a file that is
not a file of intros at all is set aside; one that cannot be read just now is never written over.

On the air, an intro is chosen and decoded by the render-ahead worker one song ahead and mixed into
the song change like a break; the mix bus only reports it heard by adding to a queue that never
waits, and the station's tick writes when it aired. A matched intro takes the place of the
automatic liner at that change, and a DJ break booked for the change wins over it. Its chance is
rolled on its own random source, so the rotation tester and the hour preview keep predicting the
songs the station picks. A library scan removes the intros of songs it found gone only when it
read every folder, so a share that is away loses none.

### Ad breaks

Ad spots and breaks (#51) are managed in the Ads tab. Their routes, `/api/ads` and
`/api/stations/<slug>/ads`, are browser routes: the signed-in session when `access_token` is set
and an allowed `Origin` for every change, never a machine token. A spot upload is capped by the
middleware at the largest `[ads] max_upload_mb` of any station plus 64 KiB before any route reads
it; the route checks the declared length against its own station's cap, the body must arrive
within 60 seconds plus one second per 256 KiB, the type must be a liner audio type (415 for any
other), and the file streams to a hidden part file under a name the server makes up
(`spot-<id><ext>`) in the station's `ads` folder, never a name from the request. It is then decoded
like an announcer break, header first, demuxer forced, at most `[ads] max_spot_seconds`, on the one
decode gate, which breaks, voice tracks, matched intros and spots share across the server; a
file that does not decode or holds no sound is refused and deleted. A station keeps at most
`[ads] max_spots` spots and 50 breaks. A spot is deleted only while no break is being booked, and
never while booked. A JSON body nested too deeply to parse is refused with 422. JSON changes (spots, breaks, rules) are read
by the route itself at most 16 KiB, and the break route's POST is also capped by the middleware.
Text fields are trimmed, control characters removed and lengths capped; the report and its CSV are
built from the station's own log, and CSV cells starting with a formula character are written as
text. Listen serves only the file a stored spot names, never a path, and only a regular file.

On the air, a due break's spots are booked on the open song change from the station's
once-a-second upkeep on a worker thread, never the event loop or the audio thread, and decoded and
mixed by the render-ahead worker as standalone breaks; their files are the station's own, so a
finished break never deletes them, and they take no room in the announcer breaks' upload caps.
`ads.json` is replaced atomically; an entry in it that is not valid is skipped, logged, said in the
tab and written back unchanged; a file that is not a file of ads is set aside. The play log is
append-only, flushed per write; a damaged line is skipped and counted, never fatal, and a write
that fails is kept in memory and written at the next upkeep.

## Radio stream listening

`GET /api/nowplaying` and `HEAD /api/nowplaying` are public and exempt from pairing, and so is the
status page `GET /status` (and HEAD), which renders the same snapshot as HTML.
They expose only the station name, artist/title/album/duration/timing of the current
and optional next song, a capped recent history, the local stream listener count and
the stream path, and while a live DJ is on the air their display name and start time
(#48). They contain no file paths, internal IDs, credentials or pairing
state. The response is cached on track and listener changes, and only this route has
the wildcard CORS header. The next song is left out unless the operator turns on `show_next`.
The feed adds today's peak listener count and the server uptime; both reveal roughly when
AutoDJ restarted and how popular the station is, which the operator accepts by running a public
station. The status page HTML-escapes every value (station names and song tags are
operator- or file-controlled text), sends `Content-Security-Policy: default-src 'none';
script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'`,
`X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`, and loads only
`/modules/status-page.js` and `/modules/status-page.css`, which were already public with the
rest of `/modules/`. Like every response, it cannot be framed by another site.

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

The player page and embeddable player (`GET|HEAD /player` and `/embed`, and
`/stations/<slug>/player` and `/embed` for a configured slug) are public in the same way as the
status page, but each answers 404 until the station's `[player_page] enabled` or `embed` turns it
on, so a station exposes nothing new by default. They render the same public snapshot as the
status page, HTML-escaped, plus the station's public stream path; never a file path, credential,
token, internal host or pairing state. A station's listener password is never put in either page:
the browser asks the listener, as any radio player would. The pages have no forms and no controls
beyond listening; their script fetches only the feed named in `data-feed` and plays only the
`.mp3` path named in `data-stream`, each refused unless it is a path that resolves to the page's
own origin, and the policy (`default-src 'none'; script-src 'self'; style-src 'self';
connect-src 'self'; media-src 'self'; base-uri 'none'; form-action 'none'`) refuses anything
else. Nothing loads or plays until the listener presses Play, and Stop closes the connection, so
an open page does not hold a listener slot; listener limits, bans and Basic credentials apply to
the stream exactly as for any other player. The full page adds `frame-ancestors 'none'` and
`X-Frame-Options: DENY`. The embeddable player is made to be framed: its `frame-ancestors` is
`'self'` plus the origins in `embed_origins` (checked as canonical `http(s)://host[:port]`
origins at startup, so a value cannot inject other directives), or `*` when the operator lists
`"*"`. Clickjacking it can at most start or stop playback in the visitor's own browser, which
changes nothing on the server. Its `?theme=` parameter is accepted only as `light` or `dark`,
and anything else is ignored. The volume is kept in the visitor's own browser storage.

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
  a session like any other path, and then returns 404. `/stream.aac` and `/stream.opus` (and
  their `/stations/` forms) are public stream paths too, for the other formats below. Optional
  HTTP Basic credentials protect only these stream paths and `/stream.m3u` (and the
  `/stations/` stream paths below); the playlist never includes them.
- Each station's stream is also public at `/stations/<slug>.mp3` and `/stations/<slug>.m3u`,
  but only for a configured slug (`[stations.<slug>]`). The default station's is
  `/stations/default.mp3`, public with the same credentials as `/stream.mp3` even on a server
  with one station. Any other `/stations/...` path needs a session and then returns 404. A
  stopped station answers 503, which shows that the slug exists; this is accepted. Each
  station's listener credentials, bans and listener limits are its own: a ban on one station
  does not cover another, and the listener slots of all stations add up. Starting and stopping
  a station (`POST /api/stations/<slug>/start` and `/stop`) and the station list
  (`/api/stations`) need pairing, which is still server-wide: a paired browser controls every
  station. Slugs are checked against a strict pattern before they name a folder. The web page's
  station switcher is only a choice of addresses in that browser: it grants nothing, and each
  request still passes the same session check. With several stations, each station's socket
  push to its paired pages also carries the station list, the same fields as `/api/stations`.

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
  status. Five failed Basic-auth attempts per address (one /64 for IPv6) per minute trigger a
  temporary throttle. The throttle remembers at most 4096 addresses per minute, so a flood of
  failures from many addresses costs bounded memory and time on the event loop. When it is full
  it forgets the address below five failures that failed least recently; a locked address is
  never forgotten before its minute ends, and if every place holds a locked address, a new
  address is refused as locked too. The live DJ source login uses the same throttle.
  Paired browsers can see listener addresses and User-Agent names. `listener_bans.json` contains
  banned IP addresses and is included in backups.

Other stream formats (#50):

- A station can also serve its mix as AAC (`/stream.aac`, `/stations/<slug>.aac`) and Opus
  (`/stream.opus`, `/stations/<slug>.opus`). Each is off until the operator turns it on in
  `config.toml` or a paired Settings page. These paths are public for every station whether the
  format is on or off; a format that is off answers 404 without a session, which tells anyone
  that it is off. That is accepted: a format that is on is as public as the MP3 stream.
- They pass exactly the checks of `/stream.mp3`: the Host allowlist, the station's listener
  password and its throttle, bans and `max_listeners_per_address`. `max_listeners` counts the
  listeners of all of a station's formats together, so turning a format on adds no listener
  slots and no way around the limit.
- Each format that is on is at most one more ffmpeg process per station, started with a fixed
  argument list (`-c:a aac -f adts`, or `-c:a libopus -ar 48000 -f ogg`) and a bitrate from a
  fixed list; nothing a listener or the browser sends reaches its command line. Turning a
  format on probes it once with a fixed tenth of a second of silence. The encoder starts for
  the format's first listener and stops a minute after its last; a request that is then turned
  away (listener limit, ban) can keep it running for that minute, so repeated requests can keep
  an enabled format's encoder up, which costs a few percent of one core and nothing more. The
  encoder gets audio through its own
  bounded queue (2 seconds); one that stalls loses its oldest audio with a logged warning and
  never holds up the mix bus, the MP3 stream or the other format, and a listener that falls
  behind is dropped as on the MP3 stream. Measured cost is a few percent of one CPU core per
  encoder (see Operations, More formats).
- An Opus stream never carries ICY metadata; an AAC stream carries the same ICY titles as MP3,
  to players that ask.

Listener statistics (#50):

- Each station counts its listening as daily totals: the most listeners at once and when, the
  number of sessions, their total and longest length, how many fell in each of six length
  bands, and the same peak, sessions and time per format. They are kept in the station's
  `listener_stats.json` and shown and exported (CSV) only to a paired browser, through
  `GET /api/stream/stats` and `GET /api/stream/stats/export` (and the station-named forms).
  They are not part of the public feed.
- No listener's address, User-Agent, connection time or any per-session record is written to
  disk or sent to the page in the statistics. While a listener is connected, AutoDJ holds only
  its format and the start time in memory, to measure the session when it ends. The listener
  list that paired browsers already see (addresses and player names of connected listeners) is
  unchanged and is not stored either.
- Retention is bounded: `[stream] stats_days` (90 by default, 0 to 3650) deletes older days at
  once and then each tick; 0 deletes every day kept and records nothing new. So the file holds
  at most one small row per day. A file that cannot be read is moved aside with a `.bad`
  suffix and counting starts again, as for `listener_bans.json`. The file is included in
  backups; it reveals only how popular the station was and when.
- The public feed's `listener_count` and today's peak now count the listeners of every format.

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
  30 searches a minute, and the same words within 30 seconds are answered from memory. The library's titles become readable to anyone who can reach the page;
  the operator accepts that by turning requests on, and the blocklists keep chosen folders and
  artists out.
- **Name a song.** Songs are named by an opaque id: a keyed BLAKE2b hash of the file path, under
  a key made when the server starts. The id reveals nothing about the path and cannot be computed
  from one. The page never contains a path. Ids are worked out on demand for the songs a search
  shows, and only those (the latest 10,000 per station) can be requested, so no map of the whole
  library is built. An id that matches nothing, a song no search showed, or a blocked song gets the
  same "Song not found" answer, so ids cannot be used to probe for hidden files.
- **Send a request.** The form is `application/x-www-form-urlencoded` and holds only `token`,
  `track` and an optional `name`, each given once; anything else gets 400. The body is capped at
  2048 bytes, and a longer body gets 413 before it is parsed. The name is trimmed, at most 40
  printable characters, and HTML-escaped wherever it is shown, to the operator too.
- **Cross-site posts.** These are refused twice. Like every unsafe request, the POST must carry
  exactly one allowed `Origin`. The form also carries a signed token: an HMAC-SHA256 of the
  station's name, the song's id and the time it was issued, under a per-process key. The token is
  good for two hours, only for that station and only for that song, so a token from one form
  cannot send a request for another song. There is no session and no cookie, and the page sends
  `Cache-Control: no-store`.
- **Abuse limits, with no CAPTCHA.** Each address may make 10 form posts per ten minutes, and
  `per_listener` accepted requests per `per_listener_minutes`. Every limit counts an IPv6 address
  with its whole network of `[requests] ipv6_prefix` bits, a /56 by default, so a host cannot
  rotate through the addresses (or the /64s) of its delegated network to get past them. It counts
  an IPv4-mapped IPv6 address as its IPv4 address. With `ipv6_prefix = 64`, a home delegated a
  /56 has 256 allowances; with a /56, several homes sharing one /56 share one allowance. The station has a daily cap,
  `daily_limit`, and a cap on requests waiting, `max_pending`. Over a limit the answer is 429 with
  `Retry-After`. The per-address tables keep at most 4096 addresses and forget idle ones. At most
  four searches or request checks run at once across every station. The rest wait on the event
  loop (an asyncio semaphore), holding no worker thread, and one that cannot start within two
  seconds gets 503 "busy". So a flood cannot park the worker threads that carts, breaks, approvals
  and the player's helpers need.
- **What cannot be bypassed.** Each accepted request still has to keep the repeat rules and, in
  DMCA mode, the DMCA limits at the place it would play. Optional bridge songs receive the same
  projected checks, plus blocklist, daypart and clockwheel matching. A bridge cannot target an
  arbitrary path supplied by a listener: candidates come only from the station's indexed music.
  It is never played at once or at a set
  time, and with `approval = "manual"` (the default) nothing reaches the queue until the operator
  approves it.

Addresses behind a reverse proxy count as one unless `[stream] trusted_proxies` names the proxy,
as for the listener limits, so one visitor behind a shared address can use up that address's
allowance for everyone behind it. Many addresses (a botnet) can still fill the daily cap. That
denies requests for the day but nothing else, since the operator decides in manual mode.
Listener addresses are held in memory only, never written to `listener_requests.json` or logs.
The file keeps titles, names, times and the decision. A listener's name is removed 30 days after
its request was decided, whatever the number of decisions kept. Pending requests expire after
three hours and cannot be approved after that. The file is tidied when it is opened and every
hour, whether or not anyone uses the page or the card.

Placing a request never holds the queue's lock during the projection. The places are worked out
from a copy of the queue, with no lock held. Then, under `queue_lock` alone, the queue is compared
with that copy and the song is inserted, or the places are worked out again, at most three times,
when it changed. The mix bus, which takes `queue_lock`, therefore never waits on the projection,
the key cache or the library. The projection takes `history_lock` and `queue_lock` one after the
other, never one inside the other.
Bridge search is bounded to at most six 24-neighbour vector queries, 36 projected candidate
checks and three projected request checks per placement attempt. All run outside `queue_lock`; a
changed queue makes the plan retry, up to the same few attempts as any request, rather than
insert stale bridge tracks. When no smooth legal position exists, the operator sees why and
the request either waits or uses the best legal position according to station settings.
The operator's request list never runs the bridge search, so polling it costs one projection per
waiting request. The four transition settings change only through the authenticated operator
route `POST /api/requests/settings`, which validates them as config.toml does.

`[stream] trusted_proxies` now applies in every mode, not only in stream mode. Without it, an
existing `FORWARDED_ALLOW_IPS` is honoured. Every trusted peer that is not loopback is named in a
startup warning: it must append or overwrite `X-Forwarded-For`, and it must terminate TLS before
machine tokens are accepted through it. The page's policy is `default-src 'none';
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

### Live DJ source mounts and talkover (#48)

**New listener surface.** `PUT` and `SOURCE` on `/live` (the default station) and
`/stations/<slug>/live` are Icecast source mounts on the existing server port: no new port is
opened. They are the only unsafe-method routes that skip the browser session and Origin checks,
because source clients have neither; the security middleware exempts exactly those two path
shapes and those two methods, and only when the path was sent exactly so: a percent-encoded
spelling such as `/%6cive` gets no exemption (and the route refuses it), so the exemption and
the source framing below always judge the same bytes. A `Host` header, when sent, must still be allowed; a request with
none (an HTTP/1.0 source client) is admitted to the route's own checks, since a browser always
sends `Host` and so a rebinding page cannot reach the mount without one. Responses on these
paths, refusals by the middleware included, always carry `Connection: close` and bypass
response compression.

**Framing and smuggling.** Source clients send a body with no `Content-Length` and no
`Transfer-Encoding`, ending when they hang up. `autodj.live_protocol.IcecastSourceH11Protocol`
(uvicorn's h11 protocol with one change) recognises such a request only as the first request on
a connection, only for those two methods and path shapes and only when the head names no
length; it then adds `Transfer-Encoding: chunked` to the head and wraps every later byte from
the client as chunk data. No byte after the head can therefore be parsed as a second request,
and the connection carries the one request until it closes. Any request that frames its own
body, malformed heads, folded headers and heads over 16 KiB are left to h11 unchanged, which
frames or refuses them as before. On TLS the protocol sees decrypted bytes the same way. Every
connection must complete its first request head within 10 seconds or is closed (slow-loris on
the head), and the server takes at most 256 connections beyond every station's listener limit
at once (uvicorn's `limit_concurrency`; more get 503).

**Credentials.** Each station has its own DJ accounts in `live_djs.json`: usernames, display
names and scrypt hashes (N=2^14, r=8, p=1, 16-byte random salts), never passwords; written by
an atomic rename, mode 0600 on POSIX (on Windows the folder's ACL governs, as for
`relays.json`). Passwords are 12 to 256 characters. A damaged file refuses every login. One
`Authorization: Basic` header is required; usernames and hashes are compared in constant
time, and an unknown username costs the same scrypt run as a wrong password, so timing does
not reveal which usernames exist. A DJ credential opens only that station's source mount: it
is not a session, not a machine token, and not valid on another station. Passwords and
`Authorization` values never reach a log, an `/api` response or the WebSocket state; the
accounts route returns names and a `password_set` flag only. Removing an account disconnects
that DJ at once. **Backups keep the accounts and talkover settings but strip every salt and
hash**, so an archive holds no DJ secret and each password must be set again after a restore;
a `live_djs.json` that cannot be read is left out of the archive rather than copied whole.

**Guessing and floods.** Five failed logins in a minute from one address (one /64 for IPv6)
lock it out for the rest of the minute (429); only one login per address is checked at a time;
at most two scrypt checks run at once across the server, and at most eight logins may be checked
or waiting at once (more get 503), so a flood costs bounded CPU, memory (16 MiB per check) and
waiting tasks. The address is the client uvicorn reports, so behind a reverse proxy it is the
one a trusted proxy names (`[stream] trusted_proxies`), the same trust as listener requests,
and an untrusted peer's forwarding headers are ignored. Failed logins are logged with the address, never the username or
password.

**Transport.** A Basic password can be read by anyone on the path, so by default a source is
accepted only over HTTPS (directly, or through a trusted proxy that says so) or plain HTTP
straight from loopback with no proxy headers, the rule machine tokens follow. BUTT and Mixxx
DJs on a LAN often have no HTTPS; `[server] live_plain_http_lan = true` (off by default) also
accepts plain HTTP from the private IPv4 ranges (10/8, 172.16/12, 192.168/16), link-local
(169.254/16, fe80::/10) and IPv6 unique local addresses (fc00::/7), never from any other address
or with proxy headers. With it on, a DJ's password crosses the LAN in the clear, where anyone
on that network can capture it and then put audio on the air; an operator turns it on only for
a network they trust, with passwords used nowhere else. Carrier-grade NAT and VPN overlay
ranges (100.64/10) are not treated as LAN.

**Decoding.** Only MP3, AAC (ADTS) and Ogg are accepted, by content type. Each source gets its
own FFmpeg process with fixed arguments, no shell, the demuxer named by the content type (no
probing for other formats), `-protocol_whitelist pipe` (no file or network access from inside
the stream), a 32 KiB probe, and stderr discarded so crafted metadata cannot reach the logs.
The route never waits on FFmpeg: encoded chunks go into a 64-chunk queue drained by a writer
thread, and a full queue (a stuck decoder) ends the source; a reader thread drains decoded PCM
into a buffer capped at 2 seconds that drops its oldest audio. A source is disconnected after
10 seconds with no bytes (slow-loris), 30 minutes of standby sending only silence, 15 seconds
of silence on the air, an input rate over 512 kbit/s over five seconds plus a 64 KiB burst,
2 GiB in one connection, or a decoder exit. One source per station: the claim is reserved
under the station's lock before FFmpeg starts, so two simultaneous logins cannot both get it.

**The audio thread.** The mix bus only reads the buffers the routes fill: it never takes the
live desk's lock, never waits on the network or a decoder, never writes a log line (an underrun
is counted on the source and logged by its route), and inserts silence on an underrun. The
buffers' own lock is held only to append a reference or copy one block.
Starting a decoder, recording the session and readying automation all happen on other
threads. Liners, time checks and carts are not played over a live DJ.

**Talkover.** `WS /api/live/mic` and `/api/stations/<slug>/live/mic` need an allowed Host, an
allowed Origin and, with pairing on, a valid session, checked again on every message, as the
page's own socket does. **With pairing off** (a loopback-only server, or `insecure_lan`), the
microphone follows the same policy as every other control on the page: anyone who can open the
page can talk on the air, as they can already skip, queue or stop. The Live DJ settings and the
Talk help say so. One talker per station. Messages must be binary 16-bit mono PCM, at
most 32 KiB, at a declared rate from 8 to 192 kHz; a talker may send at most twice real time
over a second plus one message (else close 1009), the buffer keeps half a second, and the
socket closes after 2 seconds without a message. A paired operator can already control the
station's playout; talkover adds speaking on the air, which is the point. The microphone is
captured only while the Talk control is held (or, after a quick tap on the button, until the
next press or two minutes, whichever comes first), and released after.

**What a DJ can do.** A DJ with valid credentials can put any audio on the station's air and
their display name in the public feed and stream title, which is what the account is for;
give accounts only to trusted DJs. Their sets are logged in History as kind `live`.

## Relay credentials (outbound)

Relays (`[stream.relays.<name>]`) push the stream to external Icecast or Shoutcast servers.
Besides the Stereo Tool licence key (see "Stereo Tool library and licence key"), this is the
only place AutoDJ holds a credential for another system:

- Each relay's source password comes from `config.toml`, `config.local.toml` (recommended, so the
  shared file can be committed or copied without it) or `AUTODJ_STREAM_RELAY_<NAME>_PASSWORD`.
  An environment variable only sets the password of a relay a TOML file already names; it
  cannot add a relay. Two relay names that map to one variable (`a-b` and `a_b`, `Main` and
  `main`) are refused when that variable is set: startup stops, Settings refuses the name, and
  saved relays are not loaded, so one server's source password is never sent to another. While
  it is unset, each relay keeps the password from its own table.
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

## Stereo Tool library and licence key

AutoDJ may not redistribute Thimeo's Stereo Tool, and the container image has none. Each install
downloads Thimeo's Stereo Tool plugin library (`libStereoTool`) itself, and may give it a licence
key. Both are set from Settings, Sound, under the same session, Host and Origin policy as every
other settings route: when pairing is on, only a paired browser reaches them. There are no user
roles (see "Scope and trust boundaries"), so every paired browser may do this. Machine tokens
(analysis workers, the announcer) work only under their own route prefixes and never reach these
routes, nor do live DJ source credentials. Tests check that a request without a session, or with
only a bearer token, is refused.

**Download.** `POST /api/stereo-tool/download` takes one field, `accept_terms`, which must be
`true`; the page sends it only when the operator ticks that they accept Thimeo's terms. The body is
capped at 8 KiB. The request carries no address and no file: the server always fetches
`https://download.thimeo.com/Stereo_Tool_Generic_plugin.zip`. It follows at most five redirects,
each checked before it is requested: HTTPS only, port 443, no user information, no whitespace,
control characters or non-ASCII anywhere in the address, no `%` or backslash in its host part, and
a host that is `thimeo.com` or ends in `.thimeo.com`. Certificates are verified with the system's
trust store. A declared or streamed size over 768 MB is refused, and so is a download still running
after 30 minutes. The zip is written under a hidden `.download-<random>.zip` name in
`<index_dir>/tools/stereo-tool/`. Every member name is checked first, with backslashes read as
folder separators (Thimeo's zip uses them): a name that is absolute, has a drive, a NUL or a `..`
part refuses the whole zip. Only the one member for this
platform is read (`libStereoTool_<version>/lib/Linux/IntelAMD/64/libStereoTool_intel64.so`,
`.../Linux/ARM/64/libStereoTool_noX11_arm64.so` or `.../Windows/64/libStereoTool_64.dll`); it must
be the only match, not a link, and at most 256 MB as it is read, and the zip's CRC check must pass.
It is written to another hidden name, and its header must be a 64-bit little-endian ELF shared
object for the right machine (x86-64 or AArch64) on Linux, or a PE DLL for x64 on Windows. Only
then is it renamed over the library. On Windows a loaded copy is renamed aside first and put back
if the new file cannot take its place, and a new file a virus scanner holds open is retried a few
times; an interrupted update's set-aside copy is restored, never deleted, when it is the only one
left. The zip and part files are always deleted, so a failed download never replaces a working
library. The version comes from the zip's folder name and is written beside the library. Nothing
downloaded is executed or loaded during the download. One download runs at a time; a second
answers 409. Each attempt is audited as `stereo-tool-download` with its outcome and status,
besides the request's own audit line, and the log names the version and source URL only.

The download trusts thimeo.com and its TLS certificate: whoever controls Thimeo's download host
controls the code AutoDJ's helper loads. Thimeo publishes no checksum or signature to check
against. The page never uploads a file: an upload route would let anyone who can change settings
run any code on the server; the fixed download cannot. The library lives on the writable index
volume (`/index` in the container), so anyone who can write the index folder can replace the code
the helper loads. That is the same trust AutoDJ already gives the index folder and the
configuration. No other route writes there: preset uploads go to
`stereo-tool-presets`, restore writes only its listed folders, and backups leave `tools/` out.

**Helper process.** The server never loads Thimeo's library itself. Each station that uses Stereo
Tool starts `python -m autodj.stereotool_host` (AutoDJ's own code) with the library path, the
preset path and a private folder as Stereo Tool's home, all fixed by AutoDJ, with no shell. It
loads the library with `ctypes`, binds Stereo Tool's web and UECP servers to `127.0.0.1` before
creating the instance (`stereoTool_global_setBindIp`), creates it with no GUI and no `.ini` read
(`stereoTool_Create3`), and loads the preset with `ID_SAVE_ALLSETTINGS` (20386 for version 11
and later, 10386 before, chosen from the library's reported version), which leaves out Stereo
Tool's configuration settings. Path arguments are passed as the bytes the library expects (the
ANSI code page on Windows); a path that cannot be spelled that way is refused with a message,
never passed on mangled. Errors shown on the page name the library file, never its folder. Audio goes in on stdin and out on stdout; anything the library
prints is moved to stderr, where only AutoDJ's own report lines (version, latency, licence
verdict, an error) are read, each length-capped, and the rest is discarded. A crash or hang in
Thimeo's code ends or stalls only the helper: the master bypasses at its fixed delay, kills the
helper and starts a new one, and on Windows the helper is in AutoDJ's job object like every
mastering process. The helper runs as AutoDJ's user with AutoDJ's rights, so it is isolation from
crashes, not a sandbox.

**Licence key.** The key is write-only. `PUT /api/stereo-tool/license` (8 KiB body) saves one for
every station or, with `"scope": "station"`, for the station the URL names; `DELETE` clears it.
Answers say only where a key comes from ("install", "station", "environment", "station-config"),
that none is set, that a saved or configured key cannot be used (a problem naming the setting),
and what the running Stereo Tool said of it (licensed or not, and the features it lists), never
the key. Saving the key already saved restarts nothing; a key for one station restarts only that
station's Stereo Tool. A refused key is never repeated: the validation message is fixed text, and
the 422 handler drops the rejected input. Keys are 4 to 2048 printable ASCII characters with no
spaces. They are stored with `write_private_file`, the same as relay passwords: atomically, mode
0600 on POSIX and a protected access list for the current user only on Windows: the key for
every station in `<index_dir>/.stereo-tool-license`, and a station's own key under `stereo_tool`
in `<station folder>/secrets.json`, with its other secrets. A change to `secrets.json` is a
read, change and atomic write under one lock, so it never drops the listener login or webhooks,
and a damaged `secrets.json` is reported and never written over. A station's own key in the old
`<station folder>/.stereo-tool-license` is moved into `secrets.json` on first read, then that
file is deleted. Backups name their files explicitly and include none of these, and restore
never writes them. The environment
(`AUTODJ_STEREOTOOL_LICENSE_KEY`) overrides the key saved for every station and is read-only in
the page. A problem with a key is logged once, never the key or the file's contents.

The key reaches the library over the helper's stdin, in a small header before the audio, and the
buffers that held it are zeroed once it is written (in the server) and once `stereoTool_Create3`
returns (in the helper). It is never on a command line, so the process list does not show it to
other users, and never in the helper's environment: the helper gets AutoDJ's environment without
any `AUTODJ_*` variable or Hugging Face token. Who can still read it: the user AutoDJ runs as
(from the key file, or from either process's memory, where Python and the library may keep copies
while Stereo Tool runs), root and administrators, and anyone who can read the environment of the
server process when the key is given in `AUTODJ_STEREOTOOL_LICENSE_KEY` (on Linux, the same user
and root). In Docker the same holds on the host: the host's root can read the container's process
memory and files. AutoDJ never logs a mastering command line or the key: a stuck process is logged
without its exception text, and the helper's report lines carry no key.

## Backup and restore boundary

A backup is a plain ZIP file: the published index generation, `dj_meta.db`, `web_state.json`,
the liners, profiles and uploaded Stereo Tool preset folders, and a `manifest.json` naming the
AutoDJ version and the files. It is safe to make while AutoDJ serves: the index is copied under
its publication lock and `dj_meta.db` through SQLite's backup API. The archive is written to a
temporary file and renamed into place, and it may not be written inside the liners, profiles or
uploaded preset folder.

A station's live DJ accounts (`live_djs.json`) are archived with every salt and password
hash removed, so a backup holds no DJ credential; DJ passwords are set again after a restore.

With several stations, the archive also holds each other station's state files and folders
under `stations/<slug>/`, including its `listener_bans.json`, `listener_stats.json` and
`recording.json`, and the
manifest lists the stations. A folder shared with another station is stored once. The backup
may not be written inside any station's folders either. Every station's state files are copied
under the index's publication lock together with the index generation; each SQLite database is
copied through SQLite's backup API, which gives a consistent copy of that database a moment
later, and the generation is checked against its digests after the lock is released. Backups skip
AutoDJ's own temporary and set-aside files and check the free space first. A link that leaves
its folder (a file link pointing elsewhere, a state file linked elsewhere, or any linked
subfolder, symbolic link or Windows junction) makes the backup fail with the link's name, so a
link cannot pull another file on the machine into an archive; a file link within the same folder
is read as that file. Hard-linked files are also refused because their other names may be outside
the backup folder. Restore refuses links stored in an archive and destinations that a link takes
outside their folder, and it removes a link or junction it set aside as a link, never walking into
its target.

Restore requires a backup from the same major and minor version. It refuses member names that
are absolute, contain `..`, backslashes or drive letters, or fall outside the known index files,
liners, profiles and uploaded Stereo Tool presets, and it refuses an archive whose files differ
from its manifest. A `stations/<slug>/` member is accepted only for a slug that matches the
station-name pattern (so it can never name a folder outside `stations/`), is not `default`, and
is configured with a `[stations.<slug>]` table; a backup holding any other station is refused
before anything is unpacked, as is one whose folders would land in the same place or hold one
another or a state folder. Names Windows reserves for devices (`CON`, `PRN`, `AUX`, `NUL`,
`CONIN$`, `CONOUT$`, `CLOCK$`, `COM1` to `COM9`, `LPT1` to `LPT9`, in any case and with any
extension), names holding `*`, `?`, `"`, `<`, `>` or `|`, and names ending in a dot or a space are
refused. Restored files keep the permission bits they were saved with, so a private file comes
back private on Linux and macOS. An archive that declares more than 256 GiB, or more than the free space
beside its destinations, is refused before anything is unpacked; a member never unpacks to more
than its declared size.
Every file is unpacked beside its destination and the index is checked before anything is
replaced. Restored liners, profiles and uploaded preset folders replace the current ones whole.
The index files
are checked against the SHA-256 digests in their manifest; other files have no digests. Every
file and folder that is replaced is first set aside, and a replacement that fails puts all of
them back. Every step is written to a journal first, so a crash part way can be rolled back by
`autodj doctor`, which asks first and moves rather than deletes; `autodj serve` refuses to start
while a restore is half done. SQLite journal and WAL files beside a replaced database are set aside
with it, so a stale journal is never replayed into a restored database. `autodj serve` holds a lock
file in the index folder it serves, and restore refuses while it is held; an `autodj index` or
`autodj analyse` run holds no such lock, so the operator stops those first. Filesystem locations remain
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

## Trusted-proxy browser identity (2026-10-18)

`server.auth_mode` explicitly selects `native` (default) or `trusted_proxy` per
instance. Native loopback access, code-entry pairing and immediate device
revocation retain their existing semantics. Trusted-proxy mode accepts the
provider-neutral `X-AutoDJ-User-ID` assertion for control requests; the optional
`X-AutoDJ-User-Name` is a label, never authority. Missing or invalid identity is
rejected without falling back to a native pairing cookie or code. Host/Origin,
request-size, public-route and machine-credential boundaries still apply.

The proxy proves its identity with a separate random shared secret, configured as
`server.trusted_proxy_secret`, `AUTODJ_TRUSTED_PROXY_SECRET`, or an owner-only file
selected by `server.trusted_proxy_secret_file` or `AUTODJ_TRUSTED_PROXY_SECRET_FILE`.
The secret needs at least 32 printable ASCII bytes without spaces. A direct secret
and a secret file are mutually exclusive, including across TOML and environment
settings. AutoDJ checks permissions on the opened file before reading its contents:
POSIX group/other permission bits are an error, and Windows access lists must
grant access only to the account running AutoDJ. AutoDJ refuses trusted-proxy startup without a
valid secret, even on loopback or with `insecure_lan`. Secrets remain outside shared
configuration and station backups and are never logged. Restrict access to both
services' configuration and environment.

A non-loopback trusted-proxy bind without AutoDJ TLS logs one startup warning.
It remains available for same-host container networking, but this is not proof
that the proxy connection stays local. Use TLS between hosts; anyone who captures
the static secret can impersonate admitted users until the secret is rotated.

The proxy must strip client-supplied identity and `X-AutoDJ-Proxy-Auth` headers.
It sets verified identity values and exactly one secret header only after
application authorization succeeds, including for control and microphone WebSocket
handshakes. AutoDJ compares the secret with `hmac.compare_digest`. Missing, wrong
or duplicate secret headers leave control requests unauthenticated. Direct-port
identity spoofing, including by local processes without the secret, grants no
authority. Keep the backend unpublished and use TLS between hosts to protect the
secret in transit. The proxy must provide browser HTTPS and must not let
public-listener exceptions include write methods, browser-management routes or
control WebSocket upgrades.

SSO browser records are separate from native paired-device authorizations. Their
cookie identifies a browser only; it cannot authorize any control request. Records
are owned by the stable asserted user ID. Only that owner may list, rename or
forget them; a different account gets a different record. Forgetting does not
revoke SSO access, and forgotten records can be recreated after authorization.
Each user keeps at most 20 browser records. Inserting a new record evicts the
least recently seen record in the same SQLite write transaction. Inventory is
housekeeping and does not grant or revoke authentication.
Changing providers requires deliberate stable-ID migration. Display names, email
addresses, browser labels and passkey metadata are not stable identity keys.

AutoDJ delegates access revocation and optional logout to the gateway/provider.
An operator-configured logout URL may end sessions in other applications. Already
open SSO control and microphone WebSockets expire after the shorter of 900 seconds
and `session_ttl_seconds`, measured with a monotonic clock. They close with 4401;
a new connection passes through provider authorization again. Revocation is bounded
by this connection age and the provider's own authorization/session behavior.
No immediate provider-to-socket revocation or per-action identity audit is promised.
Browser cookies do not reveal which passkey authenticated a session,
and no device-bound/synced classification is inferred. See the
[deployment procedure](docs/guides/operations/network.md#single-sign-on-through-a-trusted-proxy)
and [ADR 7 amendment](docs/adr/0007-separate-browser-worker-and-listener-access.md#amendment-2026-10-18-provider-neutral-browser-sso).
