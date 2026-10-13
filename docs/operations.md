# AutoDJ operations

Commands labeled Bash require a Linux host or WSL2. Native Windows operators should use the
PowerShell equivalents below. Run Linux container commands inside WSL2 with the repository on a
WSL filesystem so user IDs and POSIX modes have their documented meaning.

## Configuration precedence

AutoDJ resolves defaults, `config.toml`, sibling `config.local.toml`, environment variables, then
explicit CLI flags. `config.local.toml` is read only when a `config.toml` was loaded next to it;
on its own it is ignored. Omitting `--config` is valid. Explicitly naming a missing file is an error. Put
access tokens only in ignored local configuration or `AUTODJ_ACCESS_TOKEN`. An unknown section or
key in either file, such as a setting a newer AutoDJ removed, produces a warning and is ignored.
Invalid values for supported settings still need fixing.

## Diagnose before serving

If `autodj serve` exits with an error in an interactive terminal, it offers to run
`autodj doctor` with the same Python environment, configuration and named index.
Doctor still asks before repairs. After resolving the checks, run your serve command again.
In scripts, serve prints the suggestion without prompting. To run Doctor yourself, use the
same `--config` and `--name` options as serve.

Run `uv run autodj doctor`. Use `uv run autodj doctor --json` for automation. A required failed
check returns exit 1. In an interactive terminal, doctor offers to remove unsupported config
keys and create missing index/model directories. Doctor collects unsupported keys across all known
config sections and shows the proposed removals together for one confirmation. It removes them
in one edit per affected file, saving one uniquely named backup beside each original file,
preserving unrelated settings and formatting, then retrying validation.
Review the key names before accepting: a misspelled setting may need correcting instead of removing.
Complex TOML edits that cannot be made safely are left for manual correction.

Doctor also warns about obsolete repeat settings in `web_state.json` in the selected index.
Startup ignores those keys and loads the other settings. Doctor offers optional cleanup,
with a backup, while preserving current
repeat limits, EQ and other saved values. An empty old `related_artists_file` setting can be
removed this way. A nonempty path needs manual review so you can preserve its related-artist
data. Malformed files and unsupported saved-state versions are left unchanged.

Use `uv run autodj doctor --fix` to apply supported repairs automatically without prompting.
Without flags, doctor asks before each repair in an interactive terminal. Use `--no-fix`
for diagnostics without repair prompts. `--json` and noninteractive runs without `--fix`
remain read-only, except that the index check takes the index lock, as `serve` does.
`--json` cannot be combined with `--fix`. Doctor rechecks after repairs; remaining required
failures still return exit 1. Doctor also offers to run its recommended AutoDJ recovery commands:
indexing (including `index --force` for an outdated or inconsistent index), downloading missing
models through indexing, DJ metadata analysis, and `serve --lan` for network streaming.
These commands use the same Python environment, configuration, and named index. Doctor explains
the work before asking; indexing and analysis can take a long time. `--fix` approves these commands
too. Dependency installation remains manual.

Each recovery runs at most once, and doctor rechecks afterwards so it can skip commands that are
no longer needed. Failed commands are reported and cause exit 1. For an old or corrupt DJ metadata
cache, doctor first moves the cache and its SQLite sidecar files into a backup directory; stop
serving and indexing before accepting this repair. `serve --lan` runs until stopped and does not
change the saved configuration. Doctor redacts both server and Hugging Face tokens in its
configuration report.

The `index` check reads the current catalog when one exists. Before its first import, it checks
the legacy index that `serve` or `index` would import. It reports catalog counts, warns when the
library is empty, and reports an error that would prevent startup or import. The `dj-meta-db`
check runs SQLite's integrity check on `dj_meta.db` without changing it. The `beets-db` check
warns when `[library] beets_db` names a file that does not exist: folder scanning still imports
basic song records, but optional beets enrichment cannot run. The `dependencies` check warns when
FFmpeg is not on the PATH: advanced analysis cannot process `.mp3`, `.m4a`, `.mp4` and `.aac`
files, and stream mode cannot start. The `categories` check warns about rotation categories with no
songs, playlist lines that match no library track (naming the first few), and a query or playlist
that cannot be read; its detail counts each category's songs and the songs placed by their release
year (see [Rotation categories](#rotation-categories)).

## Indexing and DJ analysis

`autodj index` scans the registered folders and saves basic song records in `library.db` before
loading the model. It then embeds eligible songs that do not yet have vectors. By default it also
refreshes metadata from beets and runs the DJ-meta backfill (`autodj analyse`) for tracks under
the configured music root whose folders have advanced analysis enabled. This backfill adds intro,
outro, beat-grid and cue data; `--no-analyse` skips it, while advanced embedding still runs.
In a terminal, analysis commands show completed-track progress. Redirected output and web job
logs keep plain progress messages.

In the web page, add, remove and rescan music and voice liner folders in the **Library tools**
tab. Its folder table shows each folder's type, path and scan options. **Queue & Search** uses one
paginated list: choose Music or Voice liners, choose all folders or one folder, and optionally
search by artist or title. Each result has Play now, Play next, Add to queue and Track info.
Manage liner files, including Delete and Play dates, under **Settings > Voice liners** (see
[Liner and cart play dates](#liner-and-cart-play-dates)). Track info's Return button puts focus
back on the result that opened it.

With `[playback] import_external_cues` on (the default), intro and outro markers set in Mixxx,
Rekordbox or Traktor take the place of the detected intro and outro, so the crossfade follows
them. A track analysed before AutoDJ read those markers gets them the first time it plays while
`autodj serve` runs; there is no need to analyse it again.

Indexing, analysis and server playback decode files the same way: FFmpeg decodes `.mp3`, `.m4a`,
`.mp4` and `.aac` files, and soundfile decodes the rest, with FFmpeg as the fallback when soundfile
cannot read a file. A file that indexes therefore also plays, at the same level.

FFmpeg can repair some damaged files while it decodes them. When it does, the terminal shows
"Decoded FILE with warnings" with FFmpeg's message, above the status line. A file that cannot be
decoded or analysed shows "Could not index FILE, skipped" or "Could not analyse FILE, skipped" with
the reason. At the end of the run, one line counts both kinds and names a log in the index
directory that lists each file and its messages: `decode-indexing.log` for embedding and
`decode-analysing.log` for DJ analysis and `decode-library.log` for `autodj analyse-library`. Each run replaces its log, and a run with nothing to
report removes it. FFmpeg's routine note that an MP3's duration is estimated from its bitrate is
left out, because the decode itself is complete.

`autodj index` uses the same registered folders as Library tools. `--limit` caps the advanced
embedding pass, not the basic import. Folders with advanced analysis disabled stay playable
without embedding. Each completed vector is saved in `library.db`, so an interrupted run resumes
with remaining songs. `--force` replaces vectors for eligible songs without deleting cues,
history or settings; a failed embedding leaves the previous vector in place. `--workers` controls
audio prefetch. `--no-enrich` skips the optional beets refresh.

The old published-index format, including `index-manifest.json`, is deprecated. If there is no
catalog, `index` or `serve` imports a valid old index once and keeps the originals for recovery.
A stale manifest is ignored when the catalog already exists. For an unreadable legacy index,
`index --force` can build a catalog from the configured folders without importing that index.
`migrate-index` remains available for older formats. Legacy `analyse`, `prune`, and `stats`
commands still use the old files; `enrich` uses the catalog when present.

To give network drives a rest during a long run, set a pause before each track, in milliseconds,
in `config.toml`:

```toml
[index]
throttle_ms = 500
```

It applies to the tracks `autodj index` embeds and the tracks `autodj index` and
`autodj analyse` analyse. The default, 0, means no pause.

### Drop mixing and key shift

Two `[djmix]` options in `config.toml` (also under DJ mixing in the web Settings) change how the
server mix joins songs. Both are off by default and both apply only when the server does the
mixing, in stream mode or with server audio.

```toml
[djmix]
beatmatch = true
drop_mix = true
key_shift = true
```

`drop_mix` brings the next song in so that its drop lands on the downbeat that starts the ending
song's next section, counted in whole phrases (`phrase_bars`) from that song's drop or breakdown.
The crossfade ends on that downbeat: the new song's build-up plays under the old one, and the
drop comes in at full level as the old song finishes fading out. With the `auto` transition
effect a noise riser builds up to the drop; an effect you chose by name is kept, and any riser,
siren or horn layer ends on the drop. It needs `beatmatch` on and both songs' tempos and beat
grids trusted, so the beats are locked together. Drops come from cues you set in Rekordbox,
Traktor, Serato or Mixxx (a cue of type drop, or a hot cue named "Drop"); the drop AutoDJ detects
itself is used only when it is unmistakable, the second after it at least 18 dB louder than the
second before it and the level holding for three seconds after. Breakdowns count as a starting
point only when you marked them. A pair mixes the usual way when the new song would have to start
more than 90 seconds in, when the ending song would stop more than 32 seconds earlier than usual,
or when there is less than 4 seconds of build-up. The overlap is the usual crossfade length, at
most 30 seconds. A drop mix is not moved or shortened by `vocal_guard`: the build-up before a
drop rarely has singing, and moving the crossfade would take the drop off the downbeat. Look for
"Drop mix:" lines in the log to see which pairs used it.

`key_shift` plays the next song one or two semitones higher or lower during the crossfade when
its key clashes with the ending song's but the shifted key would mix (by the Camelot rule, or by
your `harmonic_mode` when that is on, so an energy boost pick is not undone). The tempo does not
change. After the crossfade the song slides back to its own key over `beatmatch_glide_bars` bars,
at least 4 seconds, so from then on it plays in its true key. The `auto` transition effect treats
a shifted pair as matching keys. Songs with an unknown key, or a clash that needs a bigger shift,
mix unchanged. "Key shift:" lines in the log name the songs it moved.

Both run the phase vocoder over the overlap and the glide only, never the whole song. A key shift
also resamples that stretch first and locks the phases of each partial together, which roughly
doubles the work of a beatmatched glide: on a desktop PC about 0.75 seconds of processor time per
transition with a 6-second crossfade and a 6-second glide, against about 0.4 seconds for
beatmatch alone. It is done ahead of time while the previous song plays.

## Local network access

Start the native server for other devices on your network with one switch:

```bash
uv run autodj serve --lan
```

`--lan` (or `[server] lan = true`, or `AUTODJ_LAN=1`) binds `0.0.0.0` unless `--host` or
`[server] host` names a specific non-loopback address. It allows the Host names and origins of
this machine that it can detect: the hostname, `<hostname>.local`, the fully qualified name, every
non-loopback address, and `localhost`, `127.0.0.1` and `::1`. With TLS files set
(`[server] ssl_certfile` and `ssl_keyfile`, or `--ssl-certfile` and `--ssl-keyfile`) it allows
the matching `https://` origins too. It uses a configured
`access_token` or `AUTODJ_ACCESS_TOKEN` when there is one; otherwise it loads or creates
`<index_dir>/.access-token`. On Linux and macOS only you can read that file. On Windows it gets the index folder's permissions, and if the index folder is on a network share, anyone who can read the share can read the token. Deleting `.access-token` makes the next start create a new
token, which ends every paired session; each browser must pair again. Startup prints the addresses
to open and, while no browser is paired, a pairing code with how long it stays valid; it never
prints the token. After that no code is printed or usable until someone asks for one:
`uv run autodj devices pairing-code` prints a code that pairs one browser, and it also works with
the saved token and says so on stderr.

Giving a network address with `--host` (or `[server] host`) does the same at that address, so
`uv run autodj serve --host 192.168.1.20 --port 9000` serves on 192.168.1.20 port 9000 with
pairing. Without `--host`, `autodj serve` listens only on this machine (`127.0.0.1:8080`). A
network host keeps its old manual behaviour when you also give allowlists, an access token, or
`--insecure-lan` (see [Advanced overrides](#advanced-overrides)).

`uv run autodj doctor` shows the detected hosts and whether the token will be created. Doctor sees
LAN mode only from `[server] lan` or `AUTODJ_LAN`, not from a `--lan` given only to
`autodj serve`. Name lookups are given two seconds; on a machine with broken DNS, detection keeps
the hostname, the `.local` name and the route address.

```bash
uv run autodj serve --lan --ssl-certfile radio.pem --ssl-keyfile radio-key.pem
```

`--lan --insecure-lan` keeps the detected allowlists but turns pairing off; use it only on a
trusted network. `--insecure-lan` without `--lan` still needs explicit allowlists.

### HTTPS on your home network

A browser trusts a certificate only when the certificate names the address in the address bar
and the device trusts whoever signed it. Without a domain of your own, make a self-signed
certificate and install it as trusted on each device that opens AutoDJ. With a domain,
[HTTPS with your own domain](#https-with-your-own-domain) gets a certificate that browsers
already trust.

List every name and address you open AutoDJ by in `subjectAltName`; startup of
`autodj serve --lan` prints them, and `localhost` is the address it gives for this machine. This
example is for a machine called `nas` at `192.168.1.20`:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -subj "/CN=nas" \
  -addext "subjectAltName=DNS:nas,DNS:nas.local,DNS:localhost,IP:192.168.1.20" \
  -keyout autodj-key.pem -out autodj.pem
```

On Windows, Git for Windows includes OpenSSL as `C:\Program Files\Git\usr\bin\openssl.exe`; in
PowerShell, put the command on one line without the trailing backslashes. Keep
`autodj-key.pem` where only the account that runs AutoDJ can read it, and point `[server]` at
both files, for example in `config.local.toml`:

```toml
[server]
lan = true
ssl_certfile = "/srv/autodj/tls/autodj.pem"
ssl_keyfile = "/srv/autodj/tls/autodj-key.pem"
```

Startup then prints `https://` addresses, and LAN mode allows their `https://` origins. Copy
`autodj.pem`, never the key, to each device and install it as a trusted certificate; each
operating system, and Firefox, has its own place for that. Until a device trusts it, its browser
shows a certificate warning. The certificate above expires after 365 days; before then, make a
new one, replace the two files, and install the new certificate on each device. AutoDJ picks up
the replaced files within five minutes.

### Paired devices

In the web page, Settings, Profiles and access lists every paired device with when it was paired and
when it was last seen, and marks this browser. Each row has Rename and Revoke. Names are 1 to 64
printable characters. **Show pairing code** shows a code with a Copy button and how long it stays
valid; the code changes by itself every five minutes and cannot be edited. Each code pairs one
device. While the code is shown the page checks the list every five seconds; when a device pairs
it says the device's name and hides the used code. Any paired device can show a code, so any
paired device can pair another one.

From the command line, `autodj devices list` prints each device's id, `autodj devices rename
DEVICE_ID NAME` renames one, `autodj devices revoke DEVICE_ID` revokes one, `autodj devices reset`
revokes them all, and `autodj devices pairing-code` prints the current code.

## Docker

In Docker, AutoDJ always runs `serve --lan` on the host network: it listens on all of the
machine's interfaces (`0.0.0.0`), detects the machine's own names and addresses, and requires every
browser to pair. A bare-metal `autodj serve` stays local-only unless you pass `--lan`.

Settings come from three places beside `compose.yaml`:

- `.env` holds the host folders and the user the container runs as. Set `AUTODJ_UID` and
  `AUTODJ_GID` to the owner of the folders (`id -u` and `id -g`), so the container writes files
  you own and nothing needs a `chown`. Without them it runs as UID and GID 10001.
- `config.toml` and `config.local.toml` hold everything else, as on bare metal. The container
  mounts this folder read-only and starts in it, so relative paths in them, such as
  `ssl_certfile = "certs/fullchain.pem"`, are read from here.
- `AUTODJ_BEETS_PATH` in `.env` mounts the folder holding beets' `library.db` read-only at `/beets`;
  set `beets_db = "/beets/library.db"` in `config.local.toml` to use it.

```dotenv
AUTODJ_MUSIC_DIR=/path/to/music
AUTODJ_INDEX_DIR=./index
AUTODJ_MODEL_DIR=./models
AUTODJ_UID=1000
AUTODJ_GID=1000
#AUTODJ_BEETS_PATH=/home/you/.config/beets
```

Without `.env`, music comes from `./music` and the index and model live in the named volumes
`autodj-index` and `autodj-models`.

```bash
docker compose up -d --build
docker compose logs autodj
```

The log shows the addresses to open and, while no browser is paired, an 8-digit pairing code. The
server secret is created on first start and saved in the index folder. Enter the code once in each
browser; later visits reuse that browser's paired-device cookie. The container restarts after a
crash or a reboot unless you stop it with `docker compose down`.

Manage paired browsers from the running container:

```bash
docker compose exec autodj autodj devices list
docker compose exec autodj autodj devices pairing-code
docker compose exec autodj autodj devices revoke DEVICE_ID
```

Over plain HTTP, network observers can see the pairing code and session cookie. Use it only on a
trusted private network. Otherwise set a certificate trusted by every browser
(`[server] ssl_certfile` and `ssl_keyfile`). A proxy that ends TLS and talks plain HTTP to AutoDJ
is not supported: the `Secure` cookie flag and the origin checks follow AutoDJ's own TLS setting.
For remote access, see [HTTPS with your own domain](#https-with-your-own-domain). The image's
health check tries HTTP and then HTTPS, so it works either way.

### Advanced overrides

`--lan` covers names this machine knows about. For a DNS name it cannot detect, such as a name
in your router's DNS or a CNAME, add it with `--allowed-host` and `--allowed-origin` (or
`[server] allowed_hosts` and `allowed_origins`); with `--lan` these merge with the detected
lists, and without it they replace them. These flags and `--access-token` are hidden from
`autodj serve --help`. In Docker, put the names in `config.toml`.

To start a private LAN server with TLS for a custom name without `--lan`, set
`AUTODJ_ACCESS_TOKEN` (or `[server] access_token` in `config.local.toml`) and pass the host and
origin as flags:

```bash
uv run autodj serve --host 0.0.0.0 \
  --allowed-host radio.local \
  --allowed-origin https://radio.local:8080 \
  --ssl-certfile radio.pem \
  --ssl-keyfile radio-key.pem
```

The certificate and key are local files on the AutoDJ server. Never publish the AutoDJ port to
the internet. The only supported way to reach AutoDJ from outside your network is the Cloudflare
Tunnel with Cloudflare Access described next.

## HTTPS with your own domain

This setup uses `autodj.example.net` for the domain and `192.168.1.20` for the machine that runs
AutoDJ; use your own. It needs a domain whose DNS is hosted on Cloudflare. The result:

- AutoDJ serves HTTPS itself on the LAN with a free Let's Encrypt certificate for the domain.
- At home, the domain resolves to the LAN address, so traffic stays on your network.
- Away from home, a Cloudflare Tunnel carries requests to AutoDJ over HTTPS, and Cloudflare
  Access asks for a one-time PIN sent to your email before any request reaches AutoDJ.
- Nothing on your network is opened to the internet: the certificate is issued through DNS,
  and the tunnel is an outbound connection.

### 1. Get the certificate with certbot

Certbot's `dns-cloudflare` plugin proves you own the domain by adding a DNS record (the DNS-01
challenge), so no port has to be reachable from the internet. On a Linux machine that stays on
(it can be the AutoDJ machine), install certbot and the plugin, for example
`sudo apt install certbot python3-certbot-dns-cloudflare`.

In the Cloudflare dashboard, create an API token with the "Edit zone DNS" template, limited to
your domain's zone. Save it where only root can read it:

```bash
sudo install -m 600 /dev/null /etc/letsencrypt/cloudflare.ini
echo "dns_cloudflare_api_token = YOUR_API_TOKEN" | sudo tee /etc/letsencrypt/cloudflare.ini
```

Request the certificate:

```bash
sudo certbot certonly --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  -d autodj.example.net
```

Certbot installs a timer that renews the certificate once it has 30 days left, about every 60
days.

### 2. Copy the certificate to the AutoDJ machine on every renewal

If certbot runs on another machine, a deploy hook copies each renewed certificate across. Save
this as `/etc/letsencrypt/renewal-hooks/deploy/autodj.sh` and make it executable with
`sudo chmod 755`. The `autodj` account and the target folder are examples; use the account that
runs AutoDJ and a folder only it can read. The root account on the certbot machine needs an SSH
key that account accepts.

```sh
#!/bin/sh
set -eu
# certbot sets RENEWED_LINEAGE to the renewed certificate's folder.
scp "$RENEWED_LINEAGE/fullchain.pem" "$RENEWED_LINEAGE/privkey.pem" \
  autodj@192.168.1.20:/srv/autodj/tls/
```

Run it once by hand with `RENEWED_LINEAGE=/etc/letsencrypt/live/autodj.example.net` set, to copy
the first certificate. If certbot runs on the AutoDJ machine itself, the hook can `cp` the two
files instead, or `[server]` can point straight at `/etc/letsencrypt/live/autodj.example.net/`
when the AutoDJ account can read it.

AutoDJ checks the two files every five minutes and loads a renewed certificate without a
restart. New connections get it; open ones keep the old certificate until they reconnect. If
AutoDJ finds the files half copied, or a certificate that does not match the key, it logs a
warning, keeps serving the previous certificate, and tries again when the files change.

### 3. Configure AutoDJ

In `config.local.toml` on the AutoDJ machine:

```toml
[server]
lan = true
allowed_hosts = ["autodj.example.net"]
allowed_origins = ["https://autodj.example.net"]
ssl_certfile = "/srv/autodj/tls/fullchain.pem"
ssl_keyfile = "/srv/autodj/tls/privkey.pem"
```

This does the same as `autodj serve --lan --allowed-host autodj.example.net --allowed-origin
https://autodj.example.net` with `--ssl-certfile` and `--ssl-keyfile`; the command-line TLS
options, when given, replace both configured files. `lan = true` also allows
`https://autodj.example.net:8080`, the address used at home, and keeps pairing on.
`https://autodj.example.net` without a port is the address used through the tunnel. AutoDJ
refuses to start when only one of the two files is set or either file is missing.

### 4. Keep home traffic on the LAN

In your router's or local DNS server's settings (for example a Pi-hole local DNS record), add an
`A` record so `autodj.example.net` resolves to `192.168.1.20` inside your network. At home, open
`https://autodj.example.net:8080`: the browser trusts the Let's Encrypt certificate and nothing
leaves the LAN. A device that uses its own DNS, such as a browser with DNS over HTTPS turned on,
skips this record and goes through the tunnel instead.

### 5. Reach it from outside with a Cloudflare Tunnel and Cloudflare Access

Set up Access first, so the hostname is never reachable without it:

1. In Cloudflare Zero Trust, under Access, add a self-hosted application for
   `autodj.example.net`.
2. Give it an Allow policy with an "Emails" rule listing your own address, and only the
   addresses of people you trust with the player. Turn on the "One-time PIN" login method.
3. Note the application's Audience (AUD) tag and your team name.

Then install `cloudflared` on a machine on the LAN and create the tunnel with
`cloudflared tunnel login`, `cloudflared tunnel create autodj` and
`cloudflared tunnel route dns autodj autodj.example.net`. Its `config.yml`:

```yaml
tunnel: TUNNEL_ID
credentials-file: /etc/cloudflared/TUNNEL_ID.json
ingress:
  - hostname: autodj.example.net
    service: https://192.168.1.20:8080
    originRequest:
      # Check AutoDJ's certificate against the domain, not the IP address.
      originServerName: autodj.example.net
      httpHostHeader: autodj.example.net
      # Refuse any request that did not pass Cloudflare Access.
      access:
        required: true
        teamName: YOUR_TEAM_NAME
        audTag:
          - YOUR_AUD_TAG
  - service: http_status:404
```

The tunnel connects to AutoDJ over HTTPS and checks its certificate, so the connection is
encrypted the whole way. Do not use `noTLSVerify` or an `http://` service. A tunnel managed from
the Cloudflare dashboard takes the same settings in the public hostname's TLS, HTTP and Access
options.

Cloudflare Access is required. Never put AutoDJ on the internet without it, whether through this
tunnel, port forwarding or any other proxy: AutoDJ's pairing is built for a home network, not for
strangers on the internet. After the Access login, pair each remote browser as usual. Every
request through the tunnel reaches AutoDJ from the `cloudflared` machine's address, so the
per-address limit on wrong pairing codes counts all remote browsers together unless
`[stream] trusted_proxies` includes that direct peer. This setting affects client addresses on all
routes in every mode, including pairing, the listener request limits and audit logs. Set it only
when the proxy appends the address it saw to `X-Forwarded-For`, or overwrites the header with it.
A proxy that passes a client's own `X-Forwarded-For` through unchanged lets any client choose its
address.

The radio stream link (`/stream.mp3`) works at home. Through the tunnel it is behind
Access too, so players that cannot log in, such as Sonos, cannot use it from outside.
On the local network, anyone who can reach AutoDJ can listen without pairing.

## DMCA mode

DMCA mode keeps the station's programming inside the performance complement
of the US statutory webcasting licence (section 114, reported to
SoundExchange). This is general information, not legal advice: you remain
responsible for your licence and your reports.

Turn it on in `config.toml` and restart AutoDJ:

```toml
[dmca]
enabled = true
```

### The limits

In any three hours of actual plays, counting the song about to play:

- at most 3 songs from one album, and at most 2 of them in a row;
- at most 4 songs by one artist, and at most 3 of them in a row;
- at most 4 songs from one box set or compilation, and at most 3 of them in a row.

"Actual plays" are the song starts AutoDJ records, including plays from before
a restart, which it reads back from the play log. A song skipped after a few
seconds still counts.

Artists are split the way the repeat rules split them, so every artist a credit
names counts, featured artists included: "Mark Ronson feat. Bruno Mars", or a
title with "(feat. Bruno Mars)", counts as a play by both. Album names are
compared the way the repeat rules compare them (case, accents and punctuation
ignored), so two artists' albums with the same name count as one album. That is
stricter than the licence, never looser.

A box set or compilation is recognised from its album names: albums whose
names end in a disc or volume number, such as "Woodstock (Disc 2)", "Hits CD1",
"Anthology Vol. 3" or "Box (Disc 1 of 4)", count as one set. AutoDJ's index has
no album-artist or compilation tag, and a one-disc compilation needs none: it is
one album, and the album limit is the stricter one. A box set whose discs all
carry the same album name counts as one album, again the stricter limit.

### Where the limits apply

- Every pick mode: similar songs, discovery, random, least recently played and
  the clockwheel, including clock slots that do not enforce the repeat rules,
  and the first song of each set.
- When nothing the pick mode would choose keeps every limit, AutoDJ looks
  further rather than break one. It plays a song that keeps every limit from
  anywhere in the library, preferring one the repeat rules allow, then one not
  played recently, then any other (not the song playing or about to). The log
  says which song would have broken which limit and what plays instead.
- Only when no song in the whole library keeps every limit (a very small
  library) does AutoDJ break one: it plays a song that breaks the fewest limits,
  and logs a warning naming each one. The music never stops.
- Songs you queue from Search or Library: Play now, Play next and Add to queue
  check the limits at the time the song is expected to start, worked out from
  the lengths of the song playing and the songs queued before it. If the song
  would break a limit, nothing is queued; a dialog names the limit in words and
  offers **Queue anyway** (or **Play anyway**) and **Cancel**. Choosing Queue
  anyway queues it and logs a warning; Cancel leaves the queue as it was. The
  result is spoken either way. The expected start is an estimate: crossfades and
  skips move it, so near a three-hour boundary, check again before relying on it.
  A queued song plays even if it breaks a limit when it starts, and the log
  says so.

### Public pages

The public now-playing feed (`/api/nowplaying`), the `/status` page and the
track-change webhooks never include the next song in DMCA mode, whatever
`[nowplaying] show_next` says. In Settings, Stream, the "Show the next song
publicly" box stays in the tab order but is unavailable, with the reason read
as its description, and a line under the feed links says DMCA mode is on. The
song playing is shown while it plays: the feed gives its artist, title and
album, and the stream's ICY title its artist and title.

`autodj doctor` has a `dmca` check. With DMCA mode on, it warns about any
setting that conflicts with it and says how to fix it: today that is
`[nowplaying] show_next = true`.

## Now-playing feed and webhooks

`GET /api/nowplaying` is public and needs no paired browser. It accepts GET and HEAD, sends
`Cache-Control: no-cache` and `Access-Control-Allow-Origin: *`, and reports the last cached
track or listener change. The response schema is version 2:

```json
{
  "schema": 2,
  "station_name": "AutoDJ",
  "current_track": {"artist": "Artist", "title": "Song", "album": "Album", "duration": 180.0, "started_at": "2026-10-11T12:00:00+00:00", "elapsed": 12.0},
  "next_track": {"artist": "Artist 2", "title": "Next song", "album": "Album 2", "duration": 200.0},
  "history": [{"artist": "Artist 3", "title": "Earlier song", "duration": 190.0, "played_at": "2026-10-11T11:56:00+00:00"}],
  "listener_count": 2,
  "peak_listeners_today": 5,
  "uptime_seconds": 7260,
  "stream_path": "/stream.mp3"
}
```

Version 2 added `peak_listeners_today` and `uptime_seconds`. Clients that read version 1 keep
working, since no field was removed or renamed.

- `listener_count` is the number of players connected to this server's stream now (0 outside
  stream mode). Listeners of a relay to another server are not counted.
- `peak_listeners_today` is the most listeners connected at once since midnight in the station
  time zone (`[server] station_timezone`, the one the scheduler uses; the computer's time zone
  when it is not set). It is kept in memory only, so it starts again from the current count when
  AutoDJ restarts.
- `uptime_seconds` counts whole seconds since this station started: for the default station,
  since this AutoDJ server started; for another station, since it was last started.

Each station has its own feed, with its own name, songs, listeners and stream path:
`/api/nowplaying` for the default station, `/api/stations/<slug>/nowplaying` for any station
(the default one is `default`). See [Several stations](#several-stations).

`current_track` and `next_track` are `null` when unavailable. History is newest first.
The feed never contains local file paths, internal IDs, credentials or pairing state.
`next_track` is `null` unless `[nowplaying] show_next = true` or **Show the next song
publicly** is ticked in Settings, Stream. It is off by default because some webcasting
licences, such as the US statutory licence, forbid announcing specific songs in advance, and
it is always `null` in [DMCA mode](#dmca-mode).
`history_limit` defaults to 5 and accepts 0 through 20.

Put webhook URLs in `config.local.toml` only. Each `[[webhooks]]` table accepts an HTTP or
HTTPS `url`, `format = "generic"` or `"discord"`, and optional
`events = ["track_change"]`. Up to ten destinations are accepted:

```toml
[[webhooks]]
url = "https://example.com/secret-endpoint"
format = "generic"

[[webhooks]]
url = "https://discord.com/api/webhooks/your-secret"
format = "discord"
```

The generic POST body is the feed object above plus `"event": "track_change"`.
The Discord preset sends one embed with the station, artist, title and album.
Delivery runs outside playback, on one background thread per destination, with a
five-second request timeout. A track change is tried three times, waiting about 1 and then
4 seconds, or the destination's `Retry-After` up to 30 seconds; a newer track change replaces
one still waiting. A test send is tried once. The paired `GET /api/webhooks` route shows only the destination host, format
and last result; `POST /api/webhooks/{index}/test` sends the current snapshot.
Webhook URLs are never returned to the browser. Back up `config.local.toml`
separately if you need to restore secrets.

### Status page

`GET /status` is a public page for listeners, in the spirit of an Icecast status page: the
station name, the song playing, the next song when **Show the next song publicly** is on, the
number of listeners now, today's peak and how long the station has been on air (**Uptime**, which
says "Not on air" while the station is stopped). Every station
has one at `/stations/<slug>/status`; `/status` is the default station's. It needs
no pairing and does not load the paired app. The server writes every value into the page as
plain text, so it works without JavaScript; with JavaScript, `/modules/status-page.js` polls
the station's own feed every 15 seconds (skipping polls while the tab is hidden) and updates the
text. Only the now-playing line is a live region: screen readers hear it, politely, when the
song changes, and nothing else on a poll. A **Pause updates** toggle stops the polling until
pressed again. In stream mode the page also links to the stream and
its playlist file. It sends a Content-Security-Policy that allows only its own scripts,
stylesheet and feed requests. It uses the system's light or dark colours, or the Light or Dark
theme chosen under Settings, Display and keys, Theme in a browser that opened the web page at the
same address (`/modules/theme-init.js` reads that choice before the page is drawn).

### Player page and embeddable player

A station can also have a public page where listeners press Play to hear it in the browser, and
a compact version of it to put on another website. Both are off until you turn them on, for each
station on its own:

```toml
[player_page]
enabled = true                 # the player page, /player
embed = true                   # the embeddable player, /embed
embed_origins = ["https://nightowlradio.example"]   # sites that may show /embed in a frame
```

For another station, write the same keys under `[stations.<slug>.player_page]`; its pages are at
`/stations/<slug>/player` and `/stations/<slug>/embed` (the default station's also answer at
`/stations/default/player` and `/stations/default/embed`). A page that is off answers 404. The
settings are read when AutoDJ starts. `GET /api/stations` lists each station's `player_path` and
`embed_path`, or `null` while that page is off.

**The player page** (`/player`) has the station's name, a **Play** button, a **Volume** slider,
the song playing, the next song when **Show the next song publicly** is on (never in DMCA mode),
the songs played most recently (as many as `[nowplaying] history_limit`), and links to the stream,
its playlist file and the status page. Nothing plays until Play is pressed; the button then reads
**Stop**, and a line under it says "Connecting to the stream.", "Playing.", "Stopped." or why the
stream could not be played. Stop closes the connection, so a stopped page does not hold a listener
slot, and Play joins the live stream again. The volume is remembered in that browser. The song
line is the only part a screen reader hears on its own, once per song, as on the status page;
**Pause updates** stops the page following the station. Without JavaScript the browser's own audio
controls play the stream instead. If the stream stalls while playing, the line says
"Buffering." once and "Playing." when the sound is back; if nothing has started 15 seconds after
Play, it stops and says the stream could not be played. Where the browser does not let a page set
the volume (Safari on iPhone and iPad), the slider is replaced by a note that the device's own
volume applies. The page uses the system's light or dark colours, or the Theme chosen in the web
UI in that browser, and cannot be shown inside another site's frame. Like the rest of AutoDJ, both
pages are in English (`lang="en"`); there is no language setting.

**The embeddable player** (`/embed`) has the station's name, the song playing, Play, Volume,
Pause updates and, when the player page is on, a link that opens it in a new tab. Put it on a web
page with an `<iframe>`, giving the frame a title so screen readers can name it:

```html
<iframe src="https://radio.example.net/embed" title="Night Owl Radio player"
        width="100%" height="240" style="border: 0"></iframe>
```

A frame 240 px high fits it on a wide page; give it about 330 px where the frame is as narrow as a
phone, since the controls then wrap onto more lines. Add `?theme=light` or `?theme=dark` to the
address to match the site; without it the player
follows the visitor's system colours. A browser shows the embed only inside pages from the sites
in `embed_origins` (and this server's own pages): list each site as `https://host`, with a port
if it is not the usual one, or `"*"` to let any site show it. Leaving the list empty keeps it to
this server's own pages. The list goes into the page's `Content-Security-Policy:
frame-ancestors`, so browsers enforce it; it does not stop anyone from listening to the stream
itself, which is public anyway.

Both pages use the station's public feed and stream and nothing else: no pairing, no controls
beyond listening, and no file paths, credentials or other private details. A station with a
listener password still asks for it: AutoDJ never puts the password in the page, so the browser
asks the listener, and a browser that will not ask inside another site's frame cannot play the
embed. A station that does not stream (the default station outside stream mode) shows its song
and says it cannot be played there. While a station is stopped, both pages answer 503, say it is
off air, and notice when it is back.

### Settings

In stream mode, **Settings, Stream** has a **Now-playing feed** group:

- **Now-playing link** shows this page's address for the feed, with **Copy now-playing link**.
  Opened as `localhost`, the page notes that the link only works on that computer.
- **Status page link** shows the address of `/status`, with **Copy status page link** and an
  **Open the status page (new tab)** link.
- **Show the next song publicly** turns `show_next` on or off and is saved like the other
  stream settings. Outside stream mode, set `[nowplaying] show_next` in `config.toml`. In
  [DMCA mode](#dmca-mode) it is unavailable, and says why.
- **Track-change webhooks** lists each destination by number, host, format and last delivery
  result (Not sent, Delivered or Delivery failed). Its **Send test** button sends the current
  snapshot once and says whether it was delivered. With no webhooks configured, the group says so
  and points to `config.local.toml`; webhooks are added or changed there and take effect on
  restart.

## Listener requests

A station can take song requests from its listeners on a public page, `/requests` (and
`/stations/<slug>/requests` for every station). It is off until you turn it on, for each station
on its own; while it is off the page answers 404. The settings are read when AutoDJ starts:

```toml
[requests]
enabled = true
approval = "manual"          # or "auto": a request that keeps the rules is queued at once
per_listener = 3             # requests one address may make ...
per_listener_minutes = 60    # ... in this many minutes
daily_limit = 50             # requests the station takes per day, from everyone
max_pending = 20             # requests that may wait for you at once (manual approval)
window_minutes = 30          # how far ahead in the queue a request may be placed
blocked_artists = ["Night Owl Jingles"]
blocked_folders = ["Christmas"]   # full paths, or folders inside [library] music_dir
```

For another station, write the same keys under `[stations.<slug>.requests]`.

**What a listener does.** The page has a search box for title, artist or album. The songs it
finds are links: at most 20, and never from a blocked folder or by a blocked artist. Choosing one
opens a confirmation page with an optional "Your name" field (up to 40 characters, which you might
say on air) and **Send request**. The answer is a page whose title and first heading say what
happened, so a screen reader hears it as the page loads. It is one of:

- **Request received**: waiting for you.
- **Request accepted**: queued.
- **Not right now**: it would break a repeat rule or a DMCA limit. The page does not say which
  play caused it.
- **Already on the way**.
- **Too many requests**: that address's limit, with how many it may make.
- **No more requests today**.
- **This form has expired**: the form is good for two hours, for the song it was opened for.
- **The station is busy**: too many searches or requests at once, across all stations. Try again
  in a few seconds.

The page needs no JavaScript and works without pairing. A listener is only ever told roughly when
a song may play ("within the next half hour", "within the next hour or so"), never a set time.

**What the station checks**, in this order:

1. The form's token.
2. The address's limits. Each address may run 30 searches a minute and send 10 forms in ten
   minutes, whatever becomes of them. Only accepted requests count toward `per_listener` per
   `per_listener_minutes`; a request turned away, for example by the repeat rules, does not. An
   IPv6 address counts together with its whole /64, since one home is usually given a /64 and could
   otherwise step through its addresses. An IPv4 address written as IPv6 counts as itself.
3. The daily cap.
4. That the song is in the library, is not blocked, and is not playing, coming up or waiting
   already.
5. How many requests are waiting.
6. Where it could play.

For the last check, AutoDJ looks at each place in the queue that would start within
`window_minutes`. The first place, after the song playing and any "play next" song, always counts.
For each place, AutoDJ projects when the song would start. It then checks the repeat rules (track,
artist, album, title, and related tracks and artists) as of then, and in
[DMCA mode](#dmca-mode) the DMCA limits too. Of the places that break nothing, it picks the one
where the song joins its neighbours most smoothly. It weighs sound (the embeddings), tempo (half
and double time count as a match), Camelot key and the step in energy, and the sooner place wins a
tie. Working out the place and adding the song there happen as one step, so the queue cannot
change in between. A request is a song in the queue like any other. It never takes the place of a station ID, a
liner, a break or a time check, which play between songs.

**What you do.** While requests are on, Queue & Search has a **Listener requests** card. It says
how many requests are waiting and how many the station took today. Each waiting request shows who
sent it (if they gave a name), when, and either where it would fit and why ("Fits after Song by
Artist: same key, 2 BPM apart.") or what stops it. Each request has three buttons:

- **Approve** queues it at that place, checked again at that moment. It is left out while no place
  keeps the rules.
- **Play next** queues it first, after the song playing and any "play next" song, whatever fits.
  DMCA limits still hold, counting that "play next" song.
- **Turn down**.

Focus moves to the next request's Approve button (its first button when Approve is not offered),
or to the card's title when none is left, and the result is said once just after that. Recent
decisions are listed below. The card refreshes every 15 seconds while it is open, without
speaking: rows are updated in place, and a row that has focus stays until focus leaves it. A
request still waiting after three hours expires, and can no longer be approved. The same actions are available as paired routes: `GET /api/requests`,
and `POST /api/requests/<id>/approve`, `/play-next` and `/reject` (each also under
`/api/stations/<slug>/` for a station by name). The web UI's card shows the default station's
requests.

Requests are kept in `listener_requests.json` in the station's state folder: waiting requests and
the last 50 decisions, with titles, names and times. A listener's name is removed 30 days after
the decision, however many decisions there are. The file never holds an address, and it is not
part of backups. At most four searches or request checks run at once, across all stations, and
the same search within 30 seconds is answered from memory, so a flood of requests cannot take the
server's time from the music. Listener addresses are kept in memory only, for the limits, and are forgotten when
AutoDJ restarts. Requests do not yet get bridge songs to lead into them, and are not yet matched to
clockwheel categories.

Letting listeners choose when songs play can make a station an "interactive" service, which the
US statutory webcasting licence does not cover. Requests therefore never play at once or at a
promised time, each listener is limited, and in DMCA mode requests obey the album and artist
limits. General information, not legal advice.

## Radio stream (Sonos, VLC and other players)

Stream mode serves the live AutoDJ mix as an MP3 radio station that any network audio player can
open, with AutoDJ's own crossfades, EQ, transition effects and voice liners already mixed in.
Speakers hear exactly what the web page would play in server-audio mode; there is only one mix.

Turn it on at startup with `autodj serve --stream`, combined with `--lan` so other devices on
the network can reach it:

```bash
uv run autodj serve --lan --stream
```

`[stream] enabled = true` in `config.toml`, or `AUTODJ_STREAM_ENABLED=1`, does the same without
the flag. In Docker, set it in `config.toml` and restart with `docker compose up -d`.

`uv run autodj doctor` warns, without failing, if stream mode is on but the server only listens
on loopback, since no other device could reach it.

### Finding the stream address

Open the web page and go to Settings, Stream. The "Stream address" field holds the full URL;
"Copy address" copies it. A "Download playlist file (.m3u)" link gives the same address as a
one-line playlist, for players that prefer to open a file rather than type a URL. The listener
count is shown below the address. The address is built from the address the page was opened
with, so a page opened as `localhost` or `127.0.0.1` gives a link only that computer can use (the
page says so); open AutoDJ by the network address in its start-up message before copying a link
for a speaker.

The listening address is `/stream.mp3`, with a playlist at `/stream.m3u`. Anyone who can
reach the server can listen without pairing. Playback controls and settings still require
pairing when LAN authentication is enabled. Older addresses containing a stream key, such as
`/stream/<secret>.mp3`, no longer work: set any speaker or player saved with one to
`/stream.mp3`. AutoDJ no longer uses the `index/.stream-secret` file those versions made, and you
can delete it.

### Listener access controls

Listening stays public by default. To require HTTP Basic authentication, put these lines in
`config.local.toml` and restart AutoDJ:

```toml
[stream]
listener_user = "radio"
listener_password = "a long private password"
```

Give a player `http://user:pass@host/stream.mp3` (or use `https://` when AutoDJ serves HTTPS).
The password protects `/stream.mp3` and `/stream.m3u`; the playlist itself never contains
credentials, so a player opening it must already know them. Some players, possibly Sonos, cannot
send HTTP Basic credentials. The page's **Listen here** button may also fail when a password is
set. HTTP sends the password across the network in plain text; use HTTPS if the network is not
trusted. Settings, Stream shows whether a password is set, but edits to this private file require
a restart.

`max_listeners_per_address = 0` leaves the per-address limit off by default. A positive value
limits one IP address to that many simultaneous connections, in addition to `max_listeners`.
Change the value in Settings, Stream. If clients reach AutoDJ through a proxy or tunnel, list the **direct proxy
peer's** IP address or CIDR in `[stream] trusted_proxies`, for example
`trusted_proxies = ["192.168.1.10"]`. Uvicorn then reads `X-Forwarded-For` from right to left
and uses the first untrusted address. It does not use the RFC 7239 `Forwarded` header. Without
this setting, AutoDJ uses the socket peer and ignores forwarded headers. Set trusted proxies
before enabling a per-address limit or bans behind a proxy or Cloudflare Tunnel; otherwise every
listener shares the proxy's address. A trusted proxy's `X-Forwarded-Proto: https` also keeps
playlist URLs on HTTPS. The trusted proxy setting changes client addresses on all routes in every
mode, not only in stream mode, including pairing, the listener request limits and audit logs.
Only trust a proxy that appends the address it saw to `X-Forwarded-For`, or overwrites the header
with it; never one that passes a client's own `X-Forwarded-For` through, which would let any
client choose its address. Restart after changing trusted proxies.

Settings, Stream lists each direct listener's address, player name and connection time. Use
**Disconnect** to end one connection, or **Ban** to block an address or CIDR for one hour, one day,
seven days or until removal. The form accepts an optional note. A ban disconnects matching
listeners immediately; blocked stream and playlist requests return 403. The **Bans** list removes
blocks. Bans survive restarts in the active index's `listener_bans.json` and are included in
AutoDJ backups. Relayed listeners remain under the relay host's own controls.

### Adding the station to Sonos

In the Sonos app: Browse, then TuneIn, then My Radio Stations, then Add New Radio Station. Paste
the stream address and give the station a name. Sonos app versions vary, and some do not offer
Add New Radio Station directly. If yours does not:

- Add the station through the TuneIn app or the TuneIn website instead, then find it from Sonos
  under My Radio Stations, or
- Open the downloaded `.m3u` file with a player or file manager that can hand it to Sonos.

### Adding the station to VLC

Media, then Open Network Stream, then paste the stream address (or point VLC at the downloaded
`.m3u` file) and press Play.

### Controls while streaming

- **Skip** takes effect immediately on the server with a short fade. A speaker hears it a few
  seconds later, once its own playback buffer catches up. **Skip style** (Settings, Transitions, or
  `[playback] skip_style`) can make it a DJ-style exit instead: Echo out, Backspin or Loop roll.
  Each lasts one to two beats, at most 2 seconds, and ends on a beat when the song's tempo is
  trusted; then the next song starts. Skip style applies to stream mode and server audio only;
  browser playback keeps its own skip.
- **Voice liners** fire on the same triggers as in the browser. With **Talk up to the vocal**
  (Settings, Voice liners, Mix, or `[playback] liners_talk_up`), a liner due as a song starts is timed to end
  just before the singing comes in: at the first line of the song's synced lyrics, or else at its
  detected intro end. A liner too long for the intro starts during the crossfade when that fits,
  and otherwise plays as the song starts. Liners played by the browser are not timed this way.
- **Keep vocals apart** (Settings, DJ mixing, or `[djmix] vocal_guard`, on by default) shortens
  or moves a crossfade when both songs have synced lyrics, so the outgoing singer finishes before
  the incoming one starts. The fade never gets longer, or shorter than 1 second; when nothing
  avoids the clash it is left as it was. Songs without synced lyrics crossfade as before.
- **Pause** holds the stream for everyone; it plays silence rather than disconnecting, so
  speakers stay connected and resume from the same spot. It also keeps the set alive past the
  idle timeout below.
- **Seek** is not available while streaming. The seek slider is hidden on the page, and the comma
  and full-stop keyboard shortcuts announce "Seeking is not available while streaming."
- **EQ** is applied once, on the server, so every listener hears the same shaped sound.
- **Sound processing** is chosen in Settings, Sound. Stereo Tool loads the selected `.sts` preset
  when its processor starts; changing the preset restarts it. The bundled Included preset is
  read-only. Uploads (up to 2 MB) live in `index/stereo-tool-presets` beside the active index
  folder and survive restarts. Deleting the active upload in Settings switches to Included
  first; a preset file removed by hand is replaced by Included when Stereo Tool next starts. Stereo Tool's
  settings use preset files; AutoDJ applies the drive boost to a temporary copy, leaving your saved preset unchanged.
  An uploaded preset without a single `Drive` line under `[Pre Compressor]` plays unchanged,
  without the boost; Settings, Sound shows "Preset ... has no pre-compressor Drive setting;
  AGC drive not applied" under the preset, and `/api/status` reports it as
  `playout.preset_warning`.
  `[playback] stereotool_input_trim_db` adjusts the input from -12 to +6 dB before the fixed
  output limiter. `[playback] stereotool_agc_drive_db` (0 to 12, default 7) raises the selected
  preset's `[Pre Compressor] Drive` after the tracks are blended; 0 keeps its base value and does
  not disable AGC. Smooth uses its continuous mix compressor and does not level each song's quiet
  passages. Music and liners stay as original files: AutoDJ caches loudness measurements in the
  catalog and applies the measured gain when it decodes a track. Liners also pass through voice
  compression, and server-mixed playback holds decoded track audio in memory while it is mixed.
  Earlier versions wrote processed copies to a `prepared` folder inside the active index
  folder. It is no longer read or written and is safe to delete: its 32-bit float WAVs take
  about 21 MB per minute of audio, often tens of gigabytes for a large library. After
  upgrading, a library scan measures loudness in the background; a song played before it is
  measured is measured as it loads, so that first start takes longer.
  `[playback] intro_lift_max_db` (0 to 6, default 4) caps an extra lift for a quiet opening;
  it fades back to normal by eight seconds. Zero disables it. Analysis caches the source's
  peak-safe opening lift in `library.db`; already cataloged songs get it on their next library scan.
  ReplayGain browser, server audio and stream playback use the cached lift, with decoded server
  audio checked against its peak again. Smooth and Stereo Tool use their continuous processors instead of this intro-only lift.
  Off leaves the source unchanged.
- **Page volume and mute** affect only the page's own "Listen here" playback in the browser.
  Each speaker keeps its own volume, set on the speaker or in its own app.
- **With `--server-audio` as well**, the page's volume and Mute also set the volume and mute of
  the machine's own speakers, and the page's "Listen here" playback follows them. Stream
  listeners and speakers still hear the stream at full level.
- Quality (bitrate) is chosen under Settings, Stream: 128, 192, 256 or 320 kbps, default 320.
- **Station name**, also under Settings, Stream, is the name Sonos, VLC and other players show
  for the station (`[stream] station_name`, default AutoDJ). A player shows a new name the next
  time it connects.
  Changing it restarts the encoder; every listener, including speakers, reconnects on its own a
  moment later.

### Idle sets

The stream starts idle: nothing plays until a listener connects. The first listener to connect
starts a new set from the beginning of a track. While at least one listener stays connected the
set keeps playing; a listener that disconnects and reconnects within 30 seconds
(`[stream] idle_grace_seconds`) finds the same set still going. After 30 seconds with nobody
connected, the set stops; the next listener to connect starts a fresh set from the start of a
track, never mid-song.

The listening address stays the same across restarts. There is no key to copy or renew.

### Relaying to Icecast and Shoutcast

AutoDJ can push the same station to one or more hosted Icecast or Shoutcast servers, so a
station can use a host's listener capacity and directory listings as well as direct
connections. Relays run in stream mode only. Supported servers:

- Icecast 2.4 or later, which accepts a source over HTTP PUT.
- Shoutcast 1 (DNAS 1.9).
- Shoutcast 2 (DNAS 2), through its Shoutcast 1 source login with a stream ID.

Describe each server as a named table in `config.toml`:

```toml
[stream.relays.main]
type = "icecast"
host = "radio.example.com"
port = 8000
mount = "/live"

[stream.relays.backup]
type = "shoutcast2"
host = "sc.example.net"
port = 8000
stream_id = 1
bitrate = 128
```

Put each password in `config.local.toml` under the same table name, where it is merged with
the table above:

```toml
[stream.relays.main]
password = "the source password from your host"
```

Or set it in the environment as `AUTODJ_STREAM_RELAY_<NAME>_PASSWORD`, with the name
upper-cased and `-` written as `_`, for example `AUTODJ_STREAM_RELAY_MAIN_PASSWORD`. A relay
without a password stops AutoDJ at startup with an error naming the table.

The keys:

- `type`: `icecast`, `shoutcast1` or `shoutcast2`. Required.
- `host`: the server's host name or IP address, without `http://`, port or path. Required.
- `password`: the source password. Required.
- `port`: the server's listener port, default 8000. Shoutcast sources log in on the port above
  it (8001 for 8000), as Shoutcast expects, so give the port listeners use.
- `mount`: Icecast only, and required there: the mount point, such as `/live`.
- `user`: Icecast only: the source user name, default `source`.
- `stream_id`: Shoutcast 2 only: the stream ID, default 1.
- `bitrate`: 128, 192, 256 or 320. Leave it out to send exactly the stream's own audio, which
  needs no extra encoder and follows the Quality setting. A relay with its own bitrate gets a
  second FFmpeg encoder, shared by every relay with that bitrate.
- `public`: `true` asks the server to list the station in its public directories. Default
  `false`.
- `enabled`: `false` keeps the table but does not connect. Default `true`.

What a relay does:

- **It keeps the station playing.** While any relay is enabled, the station plays as if
  "Keep playing without listeners" were on, even with nobody connected to AutoDJ itself,
  because AutoDJ cannot see the relay's listeners. Pause still pauses everyone.
- **It sends the song title** to the server each time a new song starts, through Icecast's
  `/admin/metadata` or Shoutcast's `/admin.cgi`, timed to the audio like the stream's own
  titles. A failed title update is shown and logged, and the next song tries again.
- **It reconnects on its own.** When the server cannot be reached, refuses the login or drops
  the connection, the relay tries again after 1 second, then 2, 4 and so on, up to once a
  minute. A connection that lasted 30 seconds starts the count again. The audio sent after a
  reconnect is live; nothing is saved up while disconnected. A server too slow to take the
  audio (more than 5 seconds behind) is disconnected and tried again. Changing Quality makes
  the relays that send the stream's own audio log in again at the new bitrate. If the stream's
  encoder restarts at the same bitrate, after a crash say, the relay logs in again after the
  same growing wait as after a failure.
- **It never slows down the stream.** Each relay sends from its own queue and threads, so a
  stalled server cannot hold up the mix or the other listeners.

Settings, Stream, lists each relay with its state, read as one sentence each: "connecting",
"live", "not connected, trying again" with the last error, or "off in the configuration". The
list is plain text and does not interrupt the screen reader when it changes. The log has one
warning when a relay starts failing and a line when it is live again. `autodj doctor` checks
that each server answers on its source port, without sending the password, and shows the state
the running AutoDJ last reported (it writes `relay-status.json` in the index folder whenever a
relay changes state).

The password crosses the network to the server as the protocol requires: in an HTTP Basic
login for Icecast, and as plain text for Shoutcast, including in title updates. AutoDJ does
not use TLS for relays, so use a host you trust on networks you trust. See the threat model.

### Recording the broadcast

Recording needs `serve --stream`. It saves the encoded MP3 that listeners receive, including
liners and output processing. Starting a recording keeps the station playing without listeners.
Set `[recording] folder` in `config.toml`, or in `config.local.toml` for a path on this
machine only. The browser and the API can neither change nor see the folder.
The default is `recordings`. Files use the station time zone and names such as
`AutoDJ-2026-10-11_13-00-00.mp3`. The station name is reduced to safe filename characters.

In Settings, Stream, Broadcast recording, the **Record broadcast** toggle button starts and
stops recording (screen readers say whether it is pressed), and **Split recording now** starts
a new file. Under **Start a new file**, **Every clock hour** (`split = "hour"`) splits at
each clock hour, and **For each show** (`split = "show"`) splits when the operator presses
Split, when a scheduled Start or Split recording event runs, or when a profile is applied.
The scheduler also has Stop recording. A new encoder, such as after a bitrate change, starts
a new file.
The running choice survives a server restart; a new dated file begins when audio resumes.

`[recording] retention_days = 0` keeps files forever. A positive value deletes only dated
MP3 files in the configured folder whose modification time is older than that many days.
Cleanup runs in the recorder worker about once an hour. Set `free_space_mb` to the minimum
free space that must remain on the recording volume. The defaults are 0 days and 100 MB.
The `config.toml` values apply until you save recording settings in the web page. From then on
the saved split, retention and free space, and whether recording was running, live in the
index's `recording.json` and win over `config.toml`. Backups include `recording.json` but not
the recorded MP3 files; archive the recordings folder yourself. A damaged `recording.json` is
renamed with a `.bad` suffix and recording starts stopped with the `config.toml` settings.

If disk space drops below the floor, a write fails, or the recorder queue fills, recording
pauses and reports the error in Settings and the server log. The stream keeps playing.
The writer tries a new file when space and audio return; audio missed during the failure is
not backfilled. A failed file may be incomplete.

For command-line automation, use the paired HTTP API against the running server:
`GET /api/recording` reports status, while `POST /api/recording/start`,
`POST /api/recording/stop` and `POST /api/recording/split` control it. The requests require
the same paired session cookie as the web UI when LAN authentication is enabled. There is
no separate `autodj record` CLI command because the existing CLI has no authenticated
client for a running server.

### Troubleshooting

- **"Stream mode needs ffmpeg on the PATH"** — install ffmpeg and make sure it is on the PATH
  for the account running AutoDJ, then start again. `autodj doctor` reports this too, and the
  container image already includes ffmpeg. AutoDJ never falls back to browser-only mode
  silently; it refuses to start until ffmpeg is available or `--stream` is dropped.
- **The stream address gives a 403 or the player cannot connect** — the host name in the address
  is not in the allowed list. Start with `--lan` so this machine's own names and addresses are
  allowed automatically, or add the exact name the player uses with `--allowed-host` (and its
  origin with `--allowed-origin`) if it is a name `--lan` cannot detect, such as a router DNS
  entry.
- **A player gets a 503 "Stream listener limit reached"** — `max_listeners` (default 8) is full.
  Raise `[stream] max_listeners` (or `AUTODJ_STREAM_MAX_LISTENERS`) if you regularly have more
  simultaneous listeners.
- **A relay says "the server rejected the user name or password"** — check the password in
  `config.local.toml` and, for Icecast, the `user` and `mount`. Many hosts give a separate
  source password; the admin password is not always accepted.
- **A relay says "the mount point may already be in use"** — another source, perhaps an
  earlier AutoDJ that has not let go yet, is connected to that Icecast mount. The relay keeps
  trying and takes over once the mount is free.
- **A relay to Shoutcast cannot connect** — Shoutcast takes sources on the port above its
  listener port. Give the listener port in `port`, and make sure the one above it is open.
- **A player gets a 503 "stream encoder failed"** — ffmpeg crashed repeatedly (five times within
  a minute) and the encoder is cooling down for 60 seconds before it tries again. Check that
  ffmpeg is present and working (`ffmpeg -version`), then try the address again after a minute.

## Several stations

One AutoDJ server can play more than one station from the same music library. The root sections
of `config.toml` (`[playback]`, `[stream]` and the rest) are the default station, exactly as
before: a server with one station needs no change. Each other station is a `[stations.<slug>]`
table. Each station has its own player, mix bus, mastering process, encoder, relays, recorder,
schedule, carts, time checks, clockwheels, liners, profiles and saved settings; the library, the
similarity index, pairing and the web server are shared.

The station switcher in the web page is still to come. Until then, a station other than the
default one is controlled through its API addresses, described below.

### Configuration

The slug names the station in addresses, folders and log lines. It uses lower-case letters,
digits and inner hyphens, at most 32 characters, such as `nightowl`. `default` is reserved for the
root sections. A server can have at most 8 stations besides the default one.

This example adds a station called Night Owl Radio. In `config.toml`:

```toml
[stations.nightowl]
autostart = true

[stations.nightowl.stream]
station_name = "Night Owl Radio"
bitrate = 192
continuous = true
max_listeners = 20

[stations.nightowl.playback]
loudness_processing = "smooth"

[stations.nightowl.recording]
folder = "recordings/nightowl"
```

In `config.local.toml`, beside the default station's secrets:

```toml
[stations.nightowl.stream]
listener_user = "owl"
listener_password = "choose another strong password"

[stations.nightowl.stream.relays.main]
password = "that station's source password"

[[stations.nightowl.webhooks]]
url = "https://example.com/your-secret-endpoint"
```

The rules:

- **A station starts from the defaults, not from the root sections.** Nothing is copied from the
  default station. Copying would quietly give the new station the default station's relays,
  webhooks, listener password and recording folder. Set everything the station needs in its own
  tables, including machine settings such as `playback.stereotool_path`.
- **A station's own sections** are `playback`, `djmix`, `transitions`, `replaygain`, `stream`,
  `recording`, `nowplaying`, `player_page`, `requests`, `time_checks`, `dmca` and `presets.<name>`, plus
  `webhooks` in `config.local.toml`. They take the same keys as the root sections.
- **Shared sections:** `library`, `index`, `model`, `huggingface` and `server` belong to the whole
  server and are read only from the root. So are `stream.trusted_proxies` (from the root
  `[stream]`) and `server.station_timezone`.
- **Another station always streams.** It mixes on the server and never opens the sound card, so
  its `stream.enabled` is ignored with a warning, and so is its `stream.trusted_proxies`.
- **`autostart`** (default `true`) starts the station with the server. With `false` it is
  configured but stays stopped until it is started.
- **Secrets stay out of `config.toml`.** Listener credentials, relay passwords and webhooks go in
  `config.local.toml` under the same table names; the two files are merged key by key.
  `config.toml` refuses a station's `webhooks`, as it does the root ones, and a station named
  in `config.local.toml` but not in `config.toml` is refused, so a misspelled name cannot start
  a station of its own. A relay password can
  also come from `AUTODJ_STATION_<SLUG>_RELAY_<NAME>_PASSWORD`, upper-cased, with `-` written as
  `_`: for the example above, `AUTODJ_STATION_NIGHTOWL_RELAY_MAIN_PASSWORD`. Only relays named in
  the TOML are looked up. The other `AUTODJ_STREAM_*` variables apply to the default station only.
- **Each station needs its own recording folder.** A recorder deletes old recordings from its
  folder, so two stations sharing one could delete each other's. Leaving `recording.folder`
  unset in a station uses the default `recordings`, which the default station already uses, so
  set it.
- **Each relay target belongs to one station.** Two stations pushing to the same host, port and
  mount (or Shoutcast stream) are refused.
- **A station without a `stream.station_name` is called by its slug**, for example `nightowl`, in
  stream metadata and on its pages, so it never takes the default station's "AutoDJ". Two
  stations given the same name are warned about at startup: listeners could not tell them apart.
- **Unknown keys are warned about and ignored**, as in the root sections; the warning names the
  station's table. `autodj doctor` offers to remove an unknown key from a station section.
- **Errors name the station**, for example
  `[stations.nightowl] playback.crossfade_seconds must be between 0 and 20, got 99.0`.

A station keeps its state in `<index_dir>/<name>/stations/<slug>/`, with the same file names as
the default station: `web_state.json`, `schedule.json`, `carts.json` and `carts/`,
`time_checks.json`, `listener_bans.json`, `recording.json`, `relay-status.json`, `clockwheels/`,
`liners/` (unless `playback.liners_folder` is set) and `profiles/`. Its plays are kept in the
shared `library.db`. The default station's files stay where they always were.

### Running

`autodj serve` starts the default station as before, then every other station with
`autostart = true`. Each other station mixes on the server and streams at
`/stations/<slug>.mp3`, with a one-line playlist at `/stations/<slug>.m3u`; the startup log lists
the addresses. These are public like `/stream.mp3`, and the station's own `listener_user` and
`listener_password` protect them. The default station keeps `/stream.mp3`, `/stream.m3u` and
`/status`, and also answers at `/stations/default.mp3`. A station's stream needs ffmpeg, even when
the default station is browser-driven.

Each station also has its own public status page and now-playing feed, for listeners and for
other sites:

- `/stations/<slug>/status`, the status page, with the station's name, song, listeners and links
  to its stream and playlist. It polls the station's own feed, and only a feed on this server.
- `/api/stations/<slug>/nowplaying`, the now-playing feed, in the same form as `/api/nowplaying`
  (see [Now-playing feed and webhooks](#now-playing-feed-and-webhooks)).
- `/stations/<slug>/requests`, the listener request page, when the station's
  `[stations.<slug>.requests]` turns it on (see [Listener requests](#listener-requests)).
- `/stations/<slug>/player` and `/stations/<slug>/embed`, the player page and embeddable player,
  when the station's `[stations.<slug>.player_page]` turns them on (see
  [Player page and embeddable player](#player-page-and-embeddable-player)).
- Both are public without pairing, like the stream, but only for a configured station: any
  other name needs a paired browser and then answers 404. The station's listener password
  protects only its stream, not these.
- The default station keeps `/status` and `/api/nowplaying`, and also answers as `default`
  (`/stations/default/status`, `/api/stations/default/nowplaying`).
- While a station is stopped, its stream, its feed and its status page all answer 503, "off air".
  The page still shows the station's name and says it is off air right now; an open page notices
  when the station is back on air and shows the song again. The feed answers
  `{"detail": "That station is off air"}`.
- Each station sends its own track-change webhooks, set in `config.local.toml` under
  `[[stations.<slug>.webhooks]]`, with only that station's feed.

Every control route of a station answers under `/api/stations/<slug>/`, for example
`/api/stations/nightowl/status`, `/api/stations/nightowl/skip` or
`/api/stations/nightowl/queue/add`; the default station keeps its `/api/...` addresses. These
need a paired browser, like the default station's.

- `GET /api/stations` lists every configured station: its slug, name, state (`stopped`,
  `stopping`, `stuck stopping`, `idle`, `playing` or `paused`), listener count and current
  song, and its public addresses: `status_path`, `nowplaying_path`, `stream_path`, `m3u_path`,
  `player_path` and `embed_path`. The stream and playlist are `null` for a default station that
  does not stream, and the player page and embed are `null` while they are off. For example:

  ```json
  {"slug": "nightowl", "name": "Night Owl Radio", "state": "playing", "listeners": 3,
   "title": "Artist — Song", "status_path": "/stations/nightowl/status",
   "nowplaying_path": "/api/stations/nightowl/nowplaying",
   "stream_path": "/stations/nightowl.mp3", "m3u_path": "/stations/nightowl.m3u",
   "player_path": "/stations/nightowl/player", "embed_path": null}
  ```
- `POST /api/stations/<slug>/start` builds and starts a stopped station. Starting a running
  station changes nothing.
- `POST /api/stations/<slug>/stop` stops a station completely: its listeners are disconnected,
  its encoder, relays, recorder and mastering process are closed and its player ends, so it uses
  no memory or processor time. Its routes then answer 409 and its stream answers 503, "off air".
  This starts as soon as the stop is asked for: while the player finishes its work, which can
  take several seconds, the station is listed as `stopping`, its routes already answer 409, a
  start request is refused at once with 409, and a second stop request returns at once. Its
  open pages are told at once. Other stations can be started and stopped meanwhile. If the
  stopping station used Stereo Tool, it keeps it until it has stopped, and another station asking
  for it is told to try again in a few seconds. The default station cannot be stopped.
- The stop reply is the station's entry, as in `GET /api/stations`, plus `warnings`: a list of
  what went wrong, in plain words, such as "Closing its recorder failed; the server log has the
  details.". It is empty when everything stopped cleanly. The station is stopped either way.
- If a station's player has not finished 30 seconds after a stop, the station is listed as
  `stuck stopping`, and the stop's `warnings` say so. Its player and mastering process keep
  running, because closing the mastering process under a running player is unsafe, so it keeps
  Stereo Tool, and it cannot be started again. The log says every minute that it is still stuck;
  only a restart of AutoDJ frees a player that never ends. When the player ends, the mastering
  process is closed, and closed again every few seconds until it has really ended; only then is
  the station `stopped` and Stereo Tool free. The same holds for a station that failed to start.
- On a normal shutdown, and when Python exits, AutoDJ kills any mastering process still running,
  after making sure none can start again. On Windows each mastering process (Stereo Tool and its
  FFmpeg limiter, or the FFmpeg master) is also tied to AutoDJ's own process with a job object,
  so it ends even when AutoDJ is killed outright or crashes. On Linux and macOS, a hard kill of
  AutoDJ (such as `kill -9`) can leave one running; stop it by hand.

A station that cannot start, with the server or when asked, says why: in the log at startup, and
in the reply to a start request. Whatever it had already started (its player, mastering process,
encoders and relays) is stopped again. The default station and the other stations keep running.

Each station starts with its saved settings (its `web_state.json`), its active clockwheel and its
own play history. The first time a station runs, its liner folder is added to Library folders as
a liner source. A station plays only the liners in its own folder; the default station plays every
other registered liner, as before. When one station's liner folder sits inside another's (for
example `liners/nightowl` inside the default station's `liners`), the inner folder's liners belong
to the inner station only. This holds for automatic liners, clockwheel liner steps, the Liners
list and the clips a cart can use.

### Stereo Tool: one station at a time

One Stereo Tool licence covers one running copy, so AutoDJ lets only one station use Stereo Tool
at a time. A second station set to `loudness_processing = "stereotool"` does not start while
another station uses it, and choosing Stereo Tool in a station's settings, or applying a profile
that does, is refused with a message naming the station that has it. A station that has just
left Stereo Tool still counts until its current song ends. Use `"smooth"` for the other
stations; with a second licence, a later version can lift this limit.

Stereo Tool presets are shared by every station, and each station chooses its own. A preset that
any station uses, running or stopped, cannot be deleted; the message names the station.

### Logs

A log line from a station other than the default one starts with its slug, such as
`[nightowl] Stream station tick failed`, and that station's threads carry the slug in their
names (`autodj-player-nightowl`, `autodj-stream-writer-nightowl`). The default station's lines
and thread names are unchanged.

### Cost of each running station

Each running station is a complete playout chain in the same process:

- **Threads, about 10 or more:** the player, the render-ahead worker (decoding, time-stretching
  and effects, in heavy bursts at each song change), the mix bus, the mastering process's reader
  and writer, the encoder's reader and writer, the recorder, the liner worker, one per webhook
  and two per relay.
- **Subprocesses, 2 to 4:** the ffmpeg MP3 encoder (plus one per relay bitrate that differs) and
  either the ffmpeg master filter (`smooth`) or Stereo Tool with an ffmpeg limiter.
- **Memory:** about 21 MB per minute of decoded audio, so roughly 150 to 250 MB per station for
  the playing song, the next render and the overlap. The similarity index is shared, so another
  station adds no index memory.
- **An idle station still runs** its mix bus, mastering and encoder on silence. Stopping a
  station is the real saving.
- **One process, one interpreter lock.** A heavy render on one station could make another
  station's mix bus late. The soak test below measures it.

### Soak test

`tests/integration/test_multi_station.py` has a soak test with two stations, the default one and
Night Owl Radio. Both mix buses run while two threads do song-sized time-stretches back to back,
standing in for each station's render-ahead work, and Night Owl is stopped and started again
every cycle. The test counts the 20 ms blocks each bus sends out. It fails if a stop leaves any
of the station's threads, encoders or mastering process behind, and, in a long run, if either bus
falls short (an underrun). ffmpeg and Stereo Tool are replaced by stand-ins, so it measures AutoDJ's own
work.

Every test run, including CI, runs it for 4 seconds with one restart. To run it for longer, on the
machine that will host the stations, set `AUTODJ_SOAK_SECONDS`. This runs it for four hours,
restarts Night Owl every 10 minutes, and samples memory and processor use every 30 seconds:

```bash
AUTODJ_SOAK_SECONDS=14400 AUTODJ_SOAK_REPORT=soak.jsonl \
  uv run pytest tests/integration/test_multi_station.py -k soak -s -p no:cacheprovider
```

The settings, all optional:

- `AUTODJ_SOAK_SECONDS`: how long to run. A long run uses four-minute songs for the render
  stand-ins and fails if a bus sends fewer than 99% of the blocks it should. The short default
  run uses 20-second songs and only reports the block counts: a few seconds on a shared CI
  machine is too short to judge underruns, so there it checks the restart and leaks only.
- `AUTODJ_SOAK_CYCLE_SECONDS`: how often Night Owl is stopped and started (default 600 in a long
  run).
- `AUTODJ_SOAK_SAMPLE_SECONDS`: how often memory and processor use are sampled (default 30 in a
  long run).
- `AUTODJ_SOAK_REPORT`: a file the samples are added to, one JSON object per line. Each has the
  seconds since the start, `rss_mb` (resident memory, on Linux only), `cpu_percent` (processor
  time as a percentage of one core since the last sample), `threads` and `restarts`. The last
  line is the summary.

At the end the test prints a summary line: the blocks each bus sent against the number expected,
the longest gap between blocks in milliseconds, the number of restarts and renders, memory at the
start and the end, and Night Owl's thread count before and after. Memory that keeps climbing from
one restart to the next, or a thread count that grows, points to a leak; attach the report file to
an issue.

## Windows PowerShell setup

For native no-container operation, create paths and run diagnostics as follows:

```powershell
New-Item -ItemType Directory -Force music, index, models, backups | Out-Null
uv run autodj doctor
$stamp = Get-Date -Format yyyy-MM-dd
uv run autodj backup "backups\autodj-$stamp.zip"
```

For the separate experimental AMD GPU environment, follow
[Experimental Windows AMD GPU setup](windows-amd.md). Its launcher and dependencies are separate
from the standard locked `.venv`.

For Docker on Windows, run Compose inside WSL2 (see [Docker](#docker)). Docker Desktop
bind-mount ownership depends on its WSL2/Linux filesystem mapping, and its host networking must
be turned on in Docker Desktop's settings. Run `bash scripts/container_smoke.sh` inside WSL2 for
the authoritative check. Do not use world-writable Windows mounts.

## Editing tags

Track info has a **Tags** section for the song or liner it shows. It has a field each for Title,
Artist, Album, Genre, Year, BPM, Key and ISRC, filled in with what AutoDJ has for the file.
Change any of them and choose **Save tags**. AutoDJ writes only the fields you changed, so a tag
you did not touch stays as it is in the file. Leave a field empty to remove that tag. Title cannot
be empty.

The fields take:

- Year: four digits, such as 1999.
- BPM: a number from 1 to 400, such as 128 or 127.5. M4A files keep a whole number.
- Key: a letter key such as Am, F#m, Db or C major, or a Camelot key such as 8A. AutoDJ writes it
  as a letter key, and Track info shows it in the key notation set in Settings.

A key or BPM you set here is kept when AutoDJ analyses the song later; the analysis still
measures energy and the rest. Clear the field to let the analysis set it again.
- ISRC: 12 characters, such as AB1CD2600001. Hyphens and spaces are ignored.

A field AutoDJ cannot take is marked invalid with the reason under it, and focus moves to the
first one. When the save works, the page says so once and focus stays on Save tags.

AutoDJ writes MP3, FLAC, M4A, Ogg Vorbis, Opus, WAV and AIFF tags. For any other kind of file,
or one it cannot read, the Tags section says it cannot edit them. The write is safe to
interrupt: AutoDJ copies the file to a hidden `.<name>.<random>.autodj-tags.tmp` beside it,
tags the copy, flushes it to disk, reads every changed field back, and only then puts it in
place of the original in one step. On Linux and macOS it then flushes the folder too; Windows
has no folder flush, so there the step is as durable as the file system makes it. If anything
before that step fails, the original file is left exactly as it was and the page says why. A
copy left behind by a crash or power cut is removed the next time that file's tags are saved,
once it is an hour old (a younger one may be another AutoDJ's save in progress); it is safe to
delete by hand.

Because the file is replaced by a new one:

- Its modification time becomes the time of the save.
- It keeps its permissions. On Linux and macOS it keeps its owner and group when AutoDJ is
  allowed to set them (usually only when AutoDJ runs as that user or as root).
- Access control lists on Windows are not carried over; the new file takes its folder's.
- A hard link to the file keeps the old tags, because it still points at the old file. A
  symbolic link stays a link, and the file it points to is the one changed, but only when that
  file is inside one of the library folders; a link pointing elsewhere is refused.
- An MP3 tag keeps its ID3 version: a version 2.3 tag stays 2.3.

Saving tags does not analyse the song again. The library and search show the new details at
once, and the song keeps its advanced analysis, cue points and loudness measurement. A later
scan sees the same file and leaves it alone.

A song that is playing or up next on any station cannot be saved; save it once it has played.
That includes the song the server mix is preparing next. AutoDJ checks again at the moment it
replaces the file, while the stations wait for it, so a song that comes up in between is still
refused.
If another program has the file open, AutoDJ waits a few seconds and then says the file is busy
without changing it.

When `[library] beets_db` names a beets database, the Tags section also has **Also update the
beets library**, ticked by default. With it ticked, AutoDJ copies the changed fields into the
song's beets entry (ISRC only when your beets database has an `isrc` field) and sets its
modification time, so `beet update` has nothing new to read. The file is always written first.
If beets has no entry for the file, or its database cannot be written (for example while beets
itself holds it), the file and AutoDJ's library keep the new tags, beets keeps the old ones,
and the page says so; run `beet update` to read the new tags into beets. When beets has no field
for any tag you changed (an ISRC, in most beets libraries), beets is left exactly as it was, so
`beet update` still reads the new file. Beets is never changed when the file could not be saved.

If the file is saved but AutoDJ's own library cannot be updated, the page says the tags were
saved in the file and what went wrong. A rescan then reads the new tags, and the song is
analysed again.

In Docker, the music folder is mounted read-only by default, so saving tags fails with a message
that AutoDJ is not allowed to change the file. Remove `:ro` from the music volume in
`compose.yaml` to allow it, and from the beets volume to let AutoDJ update beets.

## Moved files

When a file moves between registered folders in the same catalog, a scan can recognize it by its
size and a fingerprint of 64 KiB from the middle of the file. The existing vector, metadata,
analysis and cues stay with that track. A track is re-pathed only when exactly one missing file and
exactly one new file match. A copy, two identical files, or a file whose bytes changed (retagged)
is handled as a changed or new song. Register the destination folder before rescanning. This does
not make a copied catalog portable to paths on another machine.

Legacy imports need index schema 4. When started in an interactive terminal, `autodj serve` detects a
schema-3 index and offers to save a recovery copy and convert it. Entering Yes converts the index
and continues startup; No leaves it unchanged and stops startup. Stop any other AutoDJ serving,
indexing or analysis process before accepting. Services and other non-interactive launches exit
with instructions instead of waiting for input.

To preview and convert manually, use the same configuration and named index as your server:

```bash
uv run autodj migrate-index
uv run autodj migrate-index --apply
# With another configuration or named index:
uv run autodj --config station.toml migrate-index --name gold --apply
```

The preview writes nothing. Applying copies the original index generation, cue database, station
library database and saved web settings into a recovery folder under
`index/.migration-backups/default.schema3-backup-...`. The location is printed. SQLite databases
are copied with their committed changes, including changes still in the write-ahead log.
The copy is verified before
live files change and is retained after conversion. This is a migration recovery folder, not an
`autodj restore` ZIP archive; it preserves the original files for recovery after an interrupted
conversion. It does not copy music, external liner folders or configuration files.

Vectors are copied unchanged, so the expensive analysis does not run again. The converted index
is checked before publication. Missing music files are listed and keep their existing analysis,
but cannot be recognised as moved until they have a fingerprint. A conversion failure stops
startup; it does not silently rebuild the index. Schema 2 still requires a rebuild.

For files that moved, `--map moves.json` accepts old to new paths relative to `music_dir`, such as
`{"Old/a.flac": "New/a.flac"}`. Paths must use forward slashes, and destinations must be unique
and not already indexed. If the move has not happened yet, the old file is used for fingerprinting.
The original script remains available for callers that already use it:

```bash
uv run python scripts/convert_index_v3_to_v4.py --index-dir index --music-dir /music --map moves.json
uv run python scripts/convert_index_v3_to_v4.py --index-dir index --music-dir /music --map moves.json --apply
```

The first command is a dry run. Both entry points use the same converter and recovery copy.

## More than one machine

The current `library.db` stores absolute paths. Each machine should use its own catalog and
register folders using paths visible on that machine. Changing `music_dir` or copying a catalog
does not rewrite those paths. Older published indexes store paths relative to `music_dir` and
remain portable as a one-time import when the destination has no catalog.

### One catalog per machine

Each machine should keep its own catalog and register its local music folders in Library tools.
After adding tracks, run `autodj index` on that machine to import them and embed eligible songs.
The catalog stores absolute file paths, so pointing `music_dir` at the same files from another
mount point does not relocate an existing catalog. Create a catalog using the paths visible on
that machine; do not copy a populated `library.db` expecting it to adapt to a different mount.

### Importing a legacy index on another machine

Older published indexes store paths relative to `music_dir` and can still be imported when the
destination has no catalog. Set `music_dir` to that machine's library root, then place the legacy
index files in its named index directory before running `serve` or `index`. The first import keeps
the source files for recovery; after a catalog exists, the legacy manifest is ignored. The
`migrate-index` command remains available for older index schemas.

For this one-time legacy import, stop AutoDJ and any `index` or `analyse` run on both machines.
Copy only the files needed by the old format from `index/<name>/`:

- The generation files named in `index-manifest.json`: `tracks.g<number>.db` and
  `vectors.g<number>.index`. The number is the manifest's `generation` written with 20 digits.
- `dj_meta.db`, the intro, outro, beat grid and cue cache.
- `index-manifest.json` last, after the files it names are in place. AutoDJ loads only what the
  manifest names and checks both files against the SHA-256 in it, so a missing or half-copied file
  is refused with an error instead of being served.

Do not copy machine-specific data:

- `index/<name>/web_state.json`, the settings chosen in the web page.
- `index/<name>/liners/`, the voice liners uploaded on that machine.
- `index/profiles/`, the saved profiles.
- `index/.access-token`, the LAN pairing secret. Copying it would let every browser paired with
  one machine use the other.
- `index/.paired-devices.sqlite3`, the paired browsers and their sessions.
- `index/.stream-secret`, if it is there. Older versions made it for keyed stream addresses;
  AutoDJ no longer uses it, and you can delete it.
- Any `-wal`, `-shm`, `.lock` or `.tmp` file. The index lock file is made when it is needed.

Do not copy `library.db` as part of this legacy import. Run `uv run autodj doctor` on the
destination, then let the first `serve` or `index` import the relative-path legacy index into its
local catalog. This procedure does not relocate an existing catalog.

## Using a GPU machine

Advanced analysis on a NAS processor is slow. A computer with a graphics card can do it for the
server instead, over the network, with `autodj worker`. The worker asks the server for songs that
need analysis, downloads each one from the server, analyses it with the same model and code as
`autodj index`, and sends the result back. It never reads the server's music folder, so the two
machines need no shared drive and no matching paths, and the server keeps its own play history and
settings. Results appear in the server's library within a couple of seconds, without a restart.

While a worker is connected, the server pauses its own analysis so the two never analyse the same
song. Two minutes after the last worker leaves, the server analyses whatever is left on its own
processor again, so new music is always analysed sooner or later. This includes `autodj index`
run by hand on the server: its advanced analysis stops taking songs while a worker is connected
and leaves them to the worker (`autodj index --force` does not stop).

### 1. Give the server HTTPS

A worker token is sent with every request, so the server accepts it only over HTTPS (or from the
server machine itself). Set up TLS as in [HTTPS on your home network](#https-on-your-home-network).
With a self-signed certificate, copy the certificate file (not the key) to the GPU machine and pass
it with `--ca-cert`.

### 2. Create a token

On any paired browser, open Settings, Analysis workers. Under Machine tokens, type a name for the
computer, such as Alienware, leave Token use at Analysis worker, and choose Create token. The
token appears once, in a read-only field with a Copy token
button; copy it now, because it is never shown again. Choose Done to hide it. If it is lost,
choose Revoke on its row and create another. A worker token can only claim songs, download those
songs and post their analysis.

Use one token per machine. Settings counts a worker's songs and rate per token, so two machines
sharing a token show up as one worker, and revoking it stops both.

On a server without a browser at hand, for example inside a container, the same can be done from
the command line (`docker exec -it autodj autodj tokens ...` in a container):

```bash
uv run autodj tokens create Alienware --scope analysis-worker   # prints the token once
uv run autodj tokens list                                       # ids, names, scopes, last use
uv run autodj tokens revoke <id>
```

`tokens create` prints only the token on standard output, and its warning that the token is never
shown again on standard error.

### 3. Install AutoDJ on the GPU machine

Install AutoDJ as usual with the `index` extra, then check that PyTorch sees the graphics card:

```bash
uv run python -c "import torch; print(torch.cuda.is_available())"
```

`True` means the worker runs on the GPU (NVIDIA CUDA, or AMD ROCm as in
[Windows AMD GPU setup](windows-amd.md)); `False` means it would run on the processor. The GPU
machine needs no music folder and no library. The model downloads on first use.

The worker's results count only if it ran exactly the server's model weights. `[model] revision`
is a branch name (`main`), and two machines that downloaded `main` at different times can hold
different weights, so the server compares the snapshot commit of each machine's downloaded copy
(or, for a `[model] manual_path` folder, a digest of its weight files). The server needs its own
copy of the model for this, which `autodj index` downloads. If the copies differ, the worker stops
before it loads anything and says which commit the server has; set
`[model] revision = "<that commit>"` in the GPU machine's `config.toml` and start the worker again,
and it downloads exactly the server's weights.

### 4. Run the worker

Put the token in `AUTODJ_WORKER_TOKEN`, so it stays out of the shell history, and start the
worker with the server's address:

```bash
export AUTODJ_WORKER_TOKEN='adj1.…'
uv run autodj worker --server https://nas.local:8080
```

```powershell
$env:AUTODJ_WORKER_TOKEN = 'adj1.…'
uv run autodj worker --server https://nas.local:8080
```

- `--once` stops when nothing is left to analyse. Without it the worker keeps running and picks up
  new songs within half a minute.
- `--idle-exit MINUTES` also stops when nothing is left, but only once the queue has stayed empty
  for that many minutes, asking again every half minute. Use it when the server may still be
  scanning new music as the worker starts. New songs during the wait start the count again.
  `--once` is the same as `--idle-exit 0`; given both, `--idle-exit` sets the wait.
- `--batch N` claims N songs at a time (default 4, at most 16).
- `--device cuda` insists on the GPU and stops if PyTorch sees none; `--device cpu` forces the
  processor. The default uses the GPU whenever PyTorch sees one, and the worker says which it uses
  when it starts.
- `--ca-cert FILE` trusts the server's self-signed certificate.
- `--token TOKEN` gives the token on the command line instead of the environment variable.

The worker logs one line per song. Network trouble is retried with a growing pause, up to five
minutes, so a worker left running rides out a server restart. Ctrl+C finishes the song in hand,
gives the rest of its batch back and exits; a second Ctrl+C quits at once. A worker that stops
without giving its songs back (a crash or a pulled cable) loses them after ten minutes, and the
server hands them out again. A song the worker cannot decode is held back for six hours, and a song
whose file changed on the server since the last scan for one hour; a Rescan that reads the song's
new file clears either hold at once.

Settings, Analysis workers shows each worker (connected or not, its graphics card, when it was
last seen, songs analysed and failed, and songs per hour), how many songs are waiting, and whether
the server's own analysis is paused. Revoking a token stops its worker at its next request.

### Sharing the GPU with another program

On the Alienware the 8 GB graphics card is shared with radiodj's text-to-speech container
(`qwen3-tts`, rootless podman, about 6 GB), which leaves too little room for the model. The
worker can stop it first and start it again when it is done:

```bash
uv run autodj worker --server https://nas.local:8080 --idle-exit 10 \
  --before-command "podman stop qwen3-tts" --after-command "podman start qwen3-tts"
```

```powershell
uv run autodj worker --server https://nas.local:8080 --idle-exit 10 `
  --before-command "podman stop qwen3-tts" --after-command "podman start qwen3-tts"
```

The worker logs "Queue empty; waiting up to 10 minutes for new songs." when it runs out of work,
and "Queue empty for 10 minutes; stopping." when it gives up.

- `--before-command` runs once, after the worker has checked the token and the model with the
  server and before it loads the model.
- `--after-command` runs once, when the worker stops for any reason: the queue stayed empty for the
  `--idle-exit` wait (or was empty with `--once`), Ctrl+C, a revoked token or another error. The
  container stays stopped while the worker waits for new songs; neither command runs between
  empty polls. The worker moves the model off the graphics card and
  frees its memory first.
- Each runs without a shell: the text is split into words as a shell would, but pipes, `&&` and
  variables mean nothing, so put several steps in a script and name the script. Each may run for
  two minutes. The worker logs the command, its output and its exit code, and goes on even if it
  fails.

Without `--once` or `--idle-exit` the worker keeps the text-to-speech container stopped for as long
as it runs.

### The new-music routine

1. Import the new music with beets on the NAS, as usual (`beet import /path/to/new`).
2. In AutoDJ, open Library tools and choose Rescan (or restart the server). The new songs can be
   played at once, before advanced analysis.
3. The worker catches up: a worker left running analyses them within a minute or so; otherwise run
   the `--idle-exit 10` command above on the Alienware. You can start it at the same time as the
   Rescan: it analyses songs as the scan finds them, and exits once nothing new has turned up for
   ten minutes, starting the text-to-speech container again.

## Announcer breaks (DJ breaks, news and weather)

An announcer program on the station's own machine can send a station spoken breaks: a DJ talking
about the songs, news, weather or music news. It asks AutoDJ which song change is still open,
writes a break for that pair of songs, and uploads it. AutoDJ mixes the break into that song
change, reports when it airs, and deletes the file afterwards.

AutoDJ prepares each song change one song ahead: while one song plays, the next one is already
rendered together with its handoff. So a break cannot join the change that is coming up; it joins
the one after, the *open slot*. As soon as the next song is rendered, AutoDJ also picks the song
that follows it, so the open slot names both songs: the one the break follows (*ending*) and the
one it leads into (*upcoming*). A break booked for the slot pins that pair: the upcoming song
stays, whatever happens to the queue, because the break names it. A song queued after the booking
plays after the upcoming song. News and weather therefore air about one song later than a system
that splices them in at once.

### Turning breaks on

Breaks need saved-point playback (Transition mode: Use saved segue points, or Clockwheel) and are
off by default. Turn them on for each station that takes them:

```toml
[breaks]
enabled = true
dj_placement = "auto"
```

For another station, the same keys go under `[stations.<slug>.breaks]`. With breaks off, AutoDJ
picks songs exactly as before and the API says so. The other `[breaks]` keys, with their
defaults, are in `config.toml.example`:

- `dj_placement`: where a DJ break goes when its request says `auto`. `"auto"` talks up the
  upcoming song when its intro (Start to Intro) is at least `min_intro_seconds` long (2 seconds),
  and otherwise plays the break between the songs. `"talk_up"` talks up any intro; with no intro
  point the break still plays between the songs. `"standalone"` always plays it between the songs.
- `duck_db`: how far the music drops under a break's voice, -30 to 0 dB. Unset, it is
  `[playback] liners_duck_db`.
- `max_upload_mb` (40), `max_seconds` (300), `max_queued` (6 breaks waiting per station),
  `max_per_slot` (2), `quota_mb` (400 MB of waiting files): the limits on uploads.
- `default_expires_seconds` (1800) and `max_expires_seconds` (7200): how long a break may wait.
- `requests_per_minute` (120): requests one token may make.

### Placement

- **Talk-up**: the break's speech ends just as the upcoming song's vocal starts, with the song's
  intro playing under the voice. The voice starts at the ending song's Segue, or earlier over its
  Outro when the request allows it (`allow_outro`).
- **Standalone**: the ending song fades, the break plays, and the upcoming song starts when the
  speech ends.

News, weather and music news are always standalone. When one song change has several breaks,
the standalone ones play first, and only the last DJ break can talk up the song. Liners the clock
or the queue put in the same change play before the breaks. AutoDJ finds where a break's speech
starts and ends, so trailing silence in the file does not delay the music; it keeps a short tail
under the music. With Smooth or Stereo Tool processing, a break gets the same voice processing
as a voice liner.

### The token

An announcer token can read one station's coming songs and send that station breaks, and nothing
else. It works for one station only. In Settings, Analysis workers, Machine tokens, type a name,
choose Token use: Announcer, choose the Station, and choose Create
token. Copy the token when it appears; it is never shown again. From the command line:

```bash
uv run autodj tokens create announcer --scope announcer --station default
uv run autodj tokens create nightowl-announcer --scope announcer --station nightowl
```

Keep the token out of logs and shell history, for example in an environment variable read from a
file only the announcer's account can read. AutoDJ stores only a digest of it and never logs it.
Revoking the token in Settings or with `autodj tokens revoke <id>` stops it at its next request.

The token travels in the `Authorization: Bearer` header. AutoDJ accepts it over plain HTTP only
from the same machine (`127.0.0.1` or `::1`) and only when no proxy carried the request: a plain
HTTP request with a `Forwarded`, `X-Forwarded-*` or `X-Real-IP` header is refused (403) even from
loopback, because a reverse proxy on the same machine may have brought it in from the network in
the clear. From anywhere else, or through a reverse proxy, it needs HTTPS. A reverse proxy must
terminate TLS itself and send `X-Forwarded-Proto: https`, and AutoDJ must trust that proxy: the
addresses in `[stream] trusted_proxies` when that list is set, otherwise a proxy on the same
machine (`127.0.0.1` or `::1`), so a TLS proxy on the station's own host needs no setting. A
request with no proxy headers is treated as before. A request with an `Origin` header, as every
browser sends, is refused: these routes are for programs only.

### API reference

The routes answer under `/api/announcer/` for the default station and under
`/api/stations/<slug>/announcer/` for any station by name; a token for another station gets 403.
Every reply is JSON and is not cached. In the examples `$TOKEN` holds the token and the server
listens on port 8080.

Songs are described as objects with `id` (the library's id for the song, stable across
restarts), `title`, `artist`, `album`, `year` and `duration_s`.

#### GET context

What is playing, what is next, and the open slot.

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/api/announcer/context
```

```json
{
  "station": "default",
  "accepting": true,
  "reason": null,
  "now": {"id": "4be1…", "title": "First Song", "artist": "First Artist", "album": "First Album",
          "year": 1999, "duration_s": 212.4, "elapsed_s": 31.0, "remaining_s": 181.4},
  "next": {"id": "9a0c…", "title": "Second Song", "artist": "Second Artist", "…": "…"},
  "slot": {
    "slot_id": "5f2d0c9e8b7a6f5e4d3c2b1a09f8e7d6",
    "ending": {"id": "77aa…", "title": "Third Song", "…": "…"},
    "upcoming": {"id": "c3d4…", "title": "Fourth Song", "…": "…"},
    "closes_in_s": 181.4,
    "airs_in_s": 402.0,
    "upcoming_intro_s": 9.5,
    "pinned": false,
    "breaks_booked": 0
  },
  "history": [{"id": "…", "title": "…", "artist": "…", "album": "…", "played_at": 1760000000.0}],
  "requests": [{"id": "…", "title": "…", "…": "…"}],
  "limits": {"kinds": ["dj", "news", "weather", "music_news"],
             "extensions": [".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"],
             "max_bytes": 41943040, "max_seconds": 300.0, "max_queued": 6, "max_per_slot": 2,
             "default_expires_s": 1800, "max_expires_s": 7200, "dj_placement": "auto"}
}
```

- `slot` is `null` and `accepting` is `false` while no change is open: breaks are off, the station
  does not use saved-point playback, nothing is playing, or the next song is still being rendered.
  `reason` says which in words; ask again a few seconds later.
- `closes_in_s` is how long until the slot's change is rendered and the slot closes; plan to
  upload well before then. `airs_in_s` is roughly when the song change itself happens.
- `upcoming_intro_s` is the upcoming song's intro in seconds, or `null` without an intro point:
  what a talk-up has to fit.
- `history` is the latest plays, newest first; `requests` is the queue.

#### POST breaks

Upload one break for the open slot as `multipart/form-data`:

- `file` (required): the audio, one of the `limits.extensions` types, at most `limits.max_bytes`
  and `limits.max_seconds`.
- `kind` (required): `dj`, `news`, `weather` or `music_news`.
- `slot_id`, `ending_id`, `upcoming_id` (required): the slot and the ids of its two songs, as the
  context gave them.
- `placement`: `auto` (the default, following `dj_placement`), `talk_up` or `standalone`. Only a
  DJ break may ask for `talk_up`.
- `allow_outro`: `true` lets the voice start over the ending song's outro. Default `false`.
- `expires_s`: how long the break may wait, 30 seconds to `limits.max_expires_s`. A break whose
  song change would air later than that is dropped. Default `limits.default_expires_s`.
- `label`: a short description for logs and status, such as "Weather, 3 pm".

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  -F kind=dj -F slot_id=5f2d0c9e8b7a6f5e4d3c2b1a09f8e7d6 \
  -F ending_id=77aa… -F upcoming_id=c3d4… \
  -F label="Link into Fourth Song" -F file=@break.wav \
  http://127.0.0.1:8080/api/announcer/breaks
```

`201` books the break:

```json
{"break_id": "0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a", "kind": "dj", "label": "Link into Fourth Song",
 "state": "queued", "reason": null, "slot_id": "5f2d…", "ending_id": "77aa…",
 "upcoming_id": "c3d4…", "placement": "auto", "mixed_as": null, "allow_outro": false,
 "speech_s": 11.8, "created_at": 1760000000.0, "expires_at": 1760001800.0, "eta": null,
 "aired_at": null, "updated_at": 1760000000.0, "airs_in_s": 401.2}
```

Refusals:

- `409` with `"reason": "slot_closed"` (the slot was rendered or replaced) or `"pair_changed"`
  (the ids do not match the slot's songs), and `"slot"` holding the slot that is open now, or
  `null`. Write the break again for that slot. `409` with `"slot_full"` when the slot already
  holds `max_per_slot` breaks, and a plain `409` while breaks are off.
- `429` when the station already holds `max_queued` waiting breaks, or the token sent more than
  `requests_per_minute` requests; wait for `Retry-After` seconds.
- `413` for a file over `max_upload_mb` (refused from the declared `Content-Length` before the
  body is read), `415` for a file type that is not allowed, `422` for a file AutoDJ cannot decode,
  one whose contents do not match its extension, one over 192 kHz or with more than two channels,
  one longer than `max_seconds`, one with less than half a second of sound, a missing file, an
  unknown field or a field out of range.
- `503` with `Retry-After` while other breaks are being decoded; `507` when the waiting files
  would pass `quota_mb`; `408` when the body takes longer than 60 seconds plus one second per
  256 KiB of its declared length to arrive.
- `401` for a missing, wrong or revoked token, `403` for a token for another station, a request
  from another machine over plain HTTP, or a request with an `Origin` header.

A refused upload leaves no file behind.

#### GET breaks and GET breaks/{break_id}

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8080/api/announcer/breaks/0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a
```

`GET breaks` lists the announcer's breaks still waiting, then the finished ones, newest first, as
`{"breaks": [...]}`. Voice tracks booked in the web UI are not listed, and an announcer token
cannot look them up or cancel them (404). A break's `state` is one of:

- `queued`: booked, waiting for its song change to be rendered.
- `rendered`: mixed into the song change; it will air. `mixed_as` says `talk_up` or `standalone`
  and `eta` when it should air.
- `on_air`: its voice is playing.
- `aired`: its speech has finished; `aired_at` is when it started.
- `dropped`: it will not air; `reason` says why, for example "cancelled by the announcer", "the
  song before it changed", "the station jumped to another song", "skipped before it aired" or
  "its file could not be decoded".
- `expired`: it was not on the air in time; `reason` says so.

AutoDJ keeps the last 200 finished breaks. Breaks do not survive a restart: after one, ask for the
context again.

#### DELETE breaks/{break_id}

Drops a break that is still `queued`; the reply is the dropped break. A break already rendered
into the stream cannot be taken back: the reply is `409` with its state in `reason`.

```bash
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8080/api/announcer/breaks/0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a
```

### What happens to the files

Uploads go to the station's `announcements` folder (`<index_dir>/<name>/announcements`, or
`stations/<slug>/announcements` beneath it for another station) under a name AutoDJ makes up. That
folder is never part of liner rotation. A break's file is deleted once it has aired, been dropped
or expired, and any file left by a previous run is deleted when the station starts (an upload
still arriving is left alone). Room for an upload (its place in `max_queued` and `max_per_slot`
and its bytes in `quota_mb`) is held from before its body is read until it is booked or refused,
so uploads sent together cannot pass the limits together. Decoding an upload happens away from the
playout, one upload at a time, so a burst of uploads never holds up the audio or the next render.

A break is decoded with limits a library song does not need: the file's header is read first, and
a file over 192 kHz, with more than two channels, or longer than `max_seconds` is refused before
any sample is decoded. FFmpeg is told the format from the extension instead of guessing it, may
open only local files and pipes, and its output (stereo at 44.1 kHz) is read against a byte
budget. The break is decoded again the same way when it is mixed in, and where its speech starts
and ends is found again in the audio that plays, so Smooth or Stereo Tool processing cannot move
the voice off its marks. A break mixed into a render that is thrown away unplayed (the station
jumped, or the render was replaced) is dropped at once; one that has still not started ten
minutes after its expected air time expires.

The log records each break by the first eight characters of its id: when it is booked, mixed in,
aired, dropped or expired, and why.

## Voice tracking from the browser

The **Voice tracks** tab records a DJ break with your microphone and books it for the open song
change, the same as an announcer program's break (see [Announcer breaks](#announcer-breaks-dj-breaks-news-and-weather)).
It needs breaks turned on for the station (`[breaks] enabled = true`, with saved-point playback);
otherwise the tab says why it cannot take a voice track. No token is needed: the tab uses your
signed-in browser.

The browser lets a page use the microphone only over a secure connection: open AutoDJ over HTTPS,
or at `http://localhost:<port>` on the station's own computer. On a plain `http://` address from
another computer the tab says so instead of recording.

The tab shows the open song change: the ending song, the next song, how long the next song's
intro is, the time left to book it and roughly when it airs. That change is the one after the next
song, because the next one is already mixed. The time left counts down on the page; it is not
read out on its own.

1. Choose **Record**. The first time, the browser asks to use the microphone. Speak, then choose
   **Record** again to stop. **Alt+R** starts and stops recording from anywhere in the tab without
   moving focus, which suits a screen reader user who wants to stay on another control.
2. Choose **Listen back** to hear the take, on the Preview output device when one is chosen in
   Settings, Sound (headphones, say). **Record** again replaces it; **Discard recording**
   throws it away.
3. Choose the **Placement**: **Automatic** (follows `[breaks] dj_placement`; the description under
   it says what it will do for this change), **Talk up the next song's intro** (your voice ends as
   the vocal starts), or **Standalone, between the songs**. For a talk-up, **Start over the ending
   song's outro** lets your voice begin while the ending song plays out.
4. Choose **Book voice track**. The browser turns the take into a mono WAV at 44.1 kHz and
   uploads it; AutoDJ trims the silence before and after your voice as it does for any break.

The recording state is said once when recording starts and stops, then once a minute, and 30
seconds before the length limit or before the song change closes. Recording stops by itself at
three minutes, or sooner at the station's `max_seconds`, or when `max_upload_mb` could not hold
that much WAV (about 5 MB per minute). Three minutes is the browser's own cap: booking decodes the
whole take in the page, so the length is what keeps the page's memory down (about 80 MB at most).
Recording also stops, keeping the take and letting the microphone go, when you leave the Voice
tracks tab or the page is hidden (another browser tab, a minimised window); that is said once.

If the song change moves on before you book, the tab says which change is open now. When the open
change has the same two songs (it was planned again), **Book voice track** books the take there.
When it has other songs, the take is not booked: record again for those songs, or, if what you
said still fits, use **Listen back** and then choose **Book voice track** again. Errors, such as a
blocked or missing microphone or a Cancel that failed, are shown under the Record heading and
linked to the control they are about.

**Booked voice tracks** lists what became of each voice track (waiting, mixed in, on the air,
aired, dropped or expired, with the reason). **Cancel** drops one that is still waiting for its
song change. Voice tracks share the station's break limits and quota with announcer breaks:
`max_queued`, `max_per_slot` and `quota_mb`. Uploads and cancels count against
`requests_per_minute` for the station.

For scripts and other stations, the routes are `GET`, `POST` and `DELETE` under
`/api/voice-tracks` and `/api/stations/<slug>/voice-tracks`. They take the browser session and the
page's `Origin`, never an announcer token. `POST` takes the announcer upload's fields (`slot_id`,
`ending_id`, `upcoming_id`, `placement`, `allow_outro` and `file`), with no `kind`: a voice track is
always a DJ break.

## Play log export

AutoDJ keeps every completed play in `library.db`, with no expiry. Export the plays between two
dates, both included, for royalty reports and station logs:

```bash
uv run autodj export-plays --from 2026-09-01 --to 2026-09-30 --out september.csv
```

Without `--out` the file goes to standard output. A one-line summary goes to standard error: how
many plays were exported, how many songs lack an ISRC, label or other reporting field, and a
warning when some songs have neither an ISRC nor both an album and a label, because SoundExchange
may not be able to match those. Add `--missing` to get the songs that lack a field instead of the
plays, one row each, with the fields they lack, how often they played, and a `matchable` column
that says `no` for the songs SoundExchange may not be able to match. Add `--name` for a named index. Dates are in the server's time zone; an ISO
8601 date and time also works, for example `--from 2026-09-01T06:00`. Each station keeps its own
play log: `--station NAME` exports another station's plays, and without it you get the default
station's, which are all the plays logged before AutoDJ ran more than one station.

In the web page, open History, go to "Export play log", choose the dates and the format, and press
Export. AutoDJ says "Exporting play log" and the Export button is unavailable until the file is
ready. Then the browser saves the file and AutoDJ says how many plays it holds, with the same
warnings as the summary, or why it failed.

The CSV has one row per play, in this column order:

- `start` and `end`: local time with its UTC offset, for example `2026-09-01T14:03:00+02:00`.
- `duration_played`: whole seconds from the start of the play to the start of the next one,
  capped at the song's length. A crossfade's overlap counts toward the incoming song, and a skipped
  song shows how long it played. The song still playing counts its full length.
- `artist` and `title`, as shown when the song played, and `album` from the library.
- `isrc` and `label`, read from the file's tags when you export, so you do not need to re-index
  after tagging. ISRC comes from `ISRC` (FLAC and Ogg), `TSRC` (MP3) or the iTunes `ISRC` atom
  (M4A). It is written as 12 characters, without hyphens or spaces and in capitals. A tag that is
  not a valid ISRC (2 letters, 3 letters or digits, then 7 digits) counts as missing, so the song
  shows up in `--missing`. Label comes from `LABEL`, `ORGANIZATION` or `PUBLISHER` (FLAC and Ogg), `TPUB` or
  `TXXX:LABEL` (MP3), or the iTunes `LABEL` atom (M4A).
- `kind`: `song` or `liner`.
- `path`: the file's path inside its music or liner folder, with `/` between folders.
- `performances`: how many listeners heard the play; see "Listener counts" below. Empty for plays
  logged before AutoDJ counted listeners.

The file is UTF-8 without a byte order mark, because reporting tools and scripts would read a byte
order mark as part of the first column name. To open it in Excel, use Data, From Text/CSV, and
choose UTF-8 (65001); LibreOffice asks for the character set when it opens the file. A cell that
starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading `'`, so a spreadsheet
shows it as text instead of running it as a formula.

What the play log does not hold yet:

- Liners mixed over a transition, and clock liner slots, are not logged: the log holds the songs
  the mixer starts. A file played from a liner folder is marked `liner`.
- Nothing marks a file as an ad or a station ID yet, so `kind` is never `ad` or `id`. Keep station
  IDs and ads in a liner folder so they are not reported as songs.
- A song cut short when a stream set stops, because the last listener left, is dropped from the
  log, as it is from the History page.

### Listener counts

Each play records its performances: how many listeners heard any part of it, each counted once.
That follows SoundExchange's definition, where a performance is each time any part of a recording
is transmitted to one listener, so one play heard by 100 listeners is 100 performances.

- In stream mode (`serve --stream`), the count is the stream listeners connected when the song
  starts, plus each listener who connects while it plays. A listener who joins partway through
  counts. When one listener leaves and another joins, both count, unlike a count of the most
  listeners connected at once.
- Players drop and reopen the stream, for example on a Wi-Fi blip or when speakers regroup, so
  AutoDJ treats a listener as a client address with a User-Agent (the name the player sends). A
  new connection from a pair already counted for the song is the same listener, not another
  performance, when that pair is still connected or its last connection ended at most 10 seconds
  earlier. A pair that comes back after more than 10 seconds counts again.
- The rule can undercount: two players behind one public address that send the same User-Agent,
  such as two speakers of the same model in one home, count once while both are connected. Behind
  a reverse proxy such as a Cloudflare Tunnel, the address is the one the proxy passes on in
  `X-Forwarded-For` only when the direct proxy peer is in `[stream] trusted_proxies`. Without
  that setting, every listener has the proxy's address and only the User-Agent tells them apart.
- Addresses and User-Agents are kept in memory only, for the song that is playing and the 10
  seconds after a disconnect. The play log stores only the count.
- While a relay to Icecast or Shoutcast is enabled (see "Relaying to Icecast and Shoutcast"),
  plays get no count. AutoDJ cannot see the relay's listeners, and a count of only its own
  listeners would understate the performances. Those plays show up in `--missing`, and their
  SoundExchange lines leave ACTUAL_TOTAL_PERFORMANCES empty; take the listener figures from the
  relay server's statistics instead.
- In every other mode the count is 1: the person at the computer or in the browser.
- Plays logged before AutoDJ counted listeners have no count. They are listed by `--missing`.

### Reporting for webcasting licenses

This is general information, not legal advice. Check the current rules for your licence with the
licensing body or a lawyer.

A US webcaster that streams sound recordings under the statutory license in section 114 of the
Copyright Act reports its plays to SoundExchange in a Report of Use. AutoDJ can write one:

```bash
uv run autodj export-plays --from 2026-09-01 --to 2026-09-30 --format soundexchange --service "My Station" --out MyStation20260901-20260930.txt
```

or choose "SoundExchange Report of Use" as the format in the History tab, then enter the name of
the service and the transmission category.

- `--service` is the name of the service, and of the filing entity if that is different.
- `--category` is the transmission category code from SoundExchange's template. The default `A`
  covers webcasts that are not simulcasts of a broadcast station; `B` is a simulcast of a
  broadcast station. The command also accepts C, H, I, J, K and L.
- `--channel` fills CHANNEL_OR_PROGRAM_NAME; it is optional.

The report has the 11 columns of SoundExchange's Report of Use templates for commercial and
noncommercial webcasters, in this order: NAME_OF_SERVICE, TRANSMISSION_CATEGORY, FEATURED_ARTIST,
SOUND_RECORDING_TITLE, ISRC, ALBUM_TITLE, MARKETING_LABEL, ACTUAL_TOTAL_PERFORMANCES,
AGGREGATE_TUNING_HOURS, CHANNEL_OR_PROGRAM_NAME and PLAY_FREQUENCY.

- Only songs are reported. Liners, station IDs and ads are not sound recordings.
- There is one line per recording: plays whose artist, title, ISRC, album and label are the same
  once written for the report. `Björk` and `Bjork` are one recording, and so are ISRCs written
  with and without hyphens.
- ACTUAL_TOTAL_PERFORMANCES adds up the performances of the recording's plays (see "Listener
  counts" above). It is left empty when any of those plays has no count.
- PLAY_FREQUENCY is the number of plays. AGGREGATE_TUNING_HOURS is left empty.
- SoundExchange needs an ISRC, or else both the album title and the marketing label, to match a
  recording. A song with neither still gets its line, but the summary and the web page warn about
  it. Run `--missing` and look for `no` in the `matchable` column to find the songs to tag before
  you send the report.
- The layout follows the rules in 37 CFR 370.4 for a file without headers: plain ASCII text, `|`
  between fields, `^` around text and nothing around numbers, no header row, and empty fields
  still separated. Accents are dropped (`Björk` becomes `Bjork`), other characters outside ASCII
  become `?`, and `|` or `^` inside a name becomes a space. The file name AutoDJ suggests follows
  the same rules: the service name, then the first and last day as `YYYYMMDD-YYYYMMDD`, then `.txt`.

Sources, checked in October 2026: SoundExchange's
[reporting requirements](https://www.soundexchange.com/service-provider/reporting-requirements/)
(the "per play, per listener" definition of actual total performances) and its Commercial
Webcaster and Noncommercial Webcaster Report of Use templates linked there,
[37 CFR 370.4](https://www.law.cornell.edu/cfr/text/37/370.4), and the definition of a performance
in [37 CFR 380.7](https://www.law.cornell.edu/cfr/text/37/380.7). Most webcasters report every
calendar month, within 45 days of the month's end.

Royalties for the compositions (the songs as written) are separate from SoundExchange. In the US
they are licensed through ASCAP, BMI, SESAC and GMR, each with its own reporting. Outside the US
the rules and the collecting societies are different.

## Repeat settings

Repeat limits are edited in Settings > Picking > Repeats and stored in
`web_state.json`. Old repeat keys in `config.toml` or `web_state.json` are
ignored with a warning; re-enter any limits that still use those keys in that panel. The related
artists file is `index/<name>/related-artists.toml` and is read only when
**Use related artists** is enabled. The related tracks file is
`index/<name>/related-tracks.toml` and is read only when **Use related tracks** is enabled.
Each must be at most 1 MiB.

## Testing the rotation

The rotation tester plays a span of time, or a number of songs, through the station's own song
picker and reports what would happen, without changing anything on air. It answers questions such
as "do a 720-minute song rule and a 240-minute artist rule starve my six-song Power category?"
before the rules go live.

It uses everything the station picks with: the pick mode or the active clockwheel and its place,
the rotation categories and their days and hours, the schedule's song-picking changes (with their
date ranges), the repeat rules including related artists and related tracks, and the DMCA limits. It starts from the
station's real play history and last-played times. The run picks with a private copy of the
player, with its own clock and its own random choices, so the on-air player, its history, the
clockwheel place and the library are never touched.

From the command line:

```bash
uv run autodj simulate                       # the next 24 hours
uv run autodj simulate --days 7 --seed 42    # a week, repeatable with the same seed
uv run autodj simulate --picks 500 --json    # 500 songs, as JSON
uv run autodj simulate --hours 12 --start 2026-10-16T18:00 --station nightowl
```

`--station` names the default station or one configured under `[stations.<name>]`; the run uses
that station's settings, clockwheels, schedule and plays. `--start` is read in the station time
zone (`[server] station_timezone`) and must fall between
2000 and 2099. Without `--seed` a run draws a seed and the report gives it, so the same seed repeats
a run song for song, in every pick mode. Runs are capped at 30 days or 20,000 songs, and at ten
minutes of work: a run that reaches that limit stops with the songs picked so far, and the report
says so.

With `[breaks] enabled = true`, the station picks the song after the next one a song early so
announcer breaks can name it (see [Announcer breaks](#announcer-breaks-dj-breaks-news-and-weather)),
and a booked break keeps that song whatever the queue does. The rotation tester does not model
either: it picks each song when the one before it ends, as a station with breaks off does.

`autodj simulate` can run beside a running server. It opens `library.db` read-only: no write
lock, no migration, and the file itself is never written, though SQLite may create the
`library.db-wal` and `library.db-shm` files beside it, as any reader does. A library it cannot
read (damaged, not an AutoDJ library, or left half written) stops the command with a message. A
categories or liner dates file it cannot read is skipped, never moved aside.

In the web page: Settings > Song picking > **Test rotation**. Choose a span or a number of songs,
optionally a start and a seed, and press **Test rotation**. The run works in the background; a
progress bar and a line of text show how far it has got, **Cancel test** stops it with the results
so far, and the end is announced once. The results appear under **Rotation test results** as
headings and lists. One run at a time per server, whichever station it tests. The run's thread
works below normal priority and sleeps for 2 milliseconds after each song it picks, so the audio
and the page keep their place; stopping the server cancels a run and waits for it. If the page
cannot reach the server while a run works, it tries again less and less often, says so once, and
after six failed tries says it has stopped checking.

The report, in words or JSON, gives:

- plays per song, per category and per artist: the fewest, the most and the average, and the most
  played;
- songs never played, with a count and the first ten, overall and per category;
- how often each rule had to be relaxed, and which: same artist, related artist, same album, same
  title, related track, the track repeat window, dayparting and back-to-back repeats, plus fallbacks to a random
  song. A clockwheel step set not to enforce the rules is counted apart;
- clockwheel steps that were skipped or came up empty, with the reason, such as "all matching
  entries blocked by rules" or "outside its hours";
- DMCA conflicts: songs replaced to keep the limits, and limits broken when nothing could keep
  them, by kind;
- dead air: picks that found nothing (the station starts afresh) and minutes with nothing
  playable;
- the schedule changes it applied.

What it approximates:

- Time moves by each song's length from the catalog: from its saved start to its segue or outro
  with saved-point transitions, less the crossfade otherwise. A song with no length counts as the
  library's average.
- A clockwheel liner adds its full length. Automatic liners, time checks, carts and the queue are
  left out.
- A scheduled change applies from the first song after its time; hard events do not cut a song
  short. Profile switches are not simulated; the report names them.

How long a run takes grows with the library. On a synthetic 77,000-song library with four
categories and a seven-step clockwheel, an older four-core PC (2018) simulated a week (about 2,500
songs) in about two minutes, and in a little over three with the DMCA limits on.

## Hour preview and backtiming

Now Playing > **Hour preview** lists what the station plays next, with the time each item is
expected to start, and says how the hour lands: how early or late the song changes fall against
the top of the hour, each clockwheel restart and each scheduled event. Choose the next 30
minutes, hour, 2 hours or 3 hours under **Show**.

Each row says in its Status column whether it is fixed or predicted:

- **Fixed: on the air**: the song playing now, with the liners and breaks mixed into it.
- **Fixed: ready to play**: what the server has already rendered, with its liners and breaks
  (server audio and stream mode only).
- **Fixed: picked**: the song chosen to follow, and with `[breaks] enabled = true` the song after
  it (the open song change) and its liners.
- **Fixed: booked break**: an announcer break booked on the open song change.
- **Fixed: in the queue**: the request queue, which plays before any pick.
- **Predicted**: the rest, from a dry run of the station's own picker that starts where the
  fixed part ends, with the station's clockwheel place, play history, repeat rules, DMCA limits,
  categories and schedule. It is the same dry run the rotation tester uses (see
  [Testing the rotation](#testing-the-rotation)), so it never touches the air: the station's
  random choices, clockwheel place, history and library are only read.

Backtiming goes to each fixed point in the span:

- **Top of the hour**: the nearer song change counts. "Lands about 35 seconds early" means the
  song change nearest the hour comes 35 seconds before it; the sentence names the song that
  causes it and whether that song is fixed or predicted. Liners and breaks that run straight into
  a song start its song change.
- **Clockwheel restart**: the station restarts the clockwheel at the nearer song change, as it
  does on the air, so the preview says how early or late that is.
- **Scheduled events**: a soft event waits for the song playing to end, so the preview says how
  late it runs; a hard event runs on time and the preview says how much of the song it cuts.

The first top of the hour (or clockwheel restart) is the card's summary. **Refresh preview** says
it once; **Shift+H** says it from any tab. While the card is open and Now Playing is showing, the
table refreshes every 30 seconds and shortly after the song changes, changing cells in place,
saying nothing and leaving focus where it is; if those refreshes start failing, the failure is
said once, and not again until one succeeds. Choosing another span under **Show** says the new
summary once. If more than 24 fixed points fall in the span, the first 24 are timed and the card
says so. Times show as "2:04:30 PM" and read as "2:04 PM and
30 seconds"; lengths show as "3:45" and read as "3 minutes 45 seconds". Times are in the station
time zone (`[server] station_timezone`); clockwheel restarts are worked out in the computer's own
time zone, as the player does.

The page asks the server only while the card is open. Each preview runs on one worker thread
below normal priority, never on the web server's event loop and never on the audio threads. The
dry run stops after 80 songs, or once it has worked about 4 seconds, and says so; the time limit
is checked before each pick, so one slow pick can run a little past it. A preview is reused for
45 seconds while nothing new is fixed, longer than the page's 30-second refresh, so an open card
costs no dry run until a song changes. Several pages asking for the same station and span at once
share one preview, at most 8 different previews wait for the worker, and one more is answered
with 503 and "try again in a few seconds". At shutdown the server waits for the worker; if it has
not stopped, the server logs an error and leaves the library open rather than close it under the
preview. The same fixed part always gives the same prediction, so a refresh does not reshuffle
the predicted songs. Every station has its own:
`GET /api/hour-preview?minutes=60` for the default station and
`GET /api/stations/<name>/hour-preview` for any station, each answering with JSON that holds the
items, the fixed points with their sentences, and the summary.

What it approximates, as the rotation tester does: a song runs from its saved start to its segue,
or its length less the crossfade; liners and booked breaks add their full length; automatic
liners, time checks and carts are left out; and a scheduled change of song picking applies from
the first song after its time. It shows where the last songs of the hour land; it does not yet
choose them to fit.

## Backup

`autodj backup` writes one ZIP file holding:

- The live index generation: `index-manifest.json` and the `tracks.g<number>.db` and
  `vectors.g<number>.index` files it names.
- `dj_meta.db`, the intro, outro, beat grid and cue cache.
- `web_state.json`, the settings chosen in the web page.
- `related-artists.toml` and `related-tracks.toml` in the active index folder.
- `recording.json`, the broadcast recording settings and whether it was running. The recorded
  MP3 files are not included; archive the recordings folder yourself.
- `library.db`, the station's registered folders, track data and cues, last-played times and
  complete play history.
- Every file in the liners folder (`[playback] liners_folder`, or `index/<name>/liners`) and in
  `index/profiles`, plus uploaded presets in `index/stereo-tool-presets`.
- `time_checks.json` and every recorded clip in `[time_checks] clips_folder` (or the default
  time_checks folder under the active index). Generated TTS cache files are rebuilt when needed.
- `categories.json`, the rotation categories. Their playlist files are not included; the script
  that writes them can write them again.
- `liner_dates.json`, the voice liners' play dates.
- `manifest.json`, which records the AutoDJ version that made the archive and lists its files.

Backups do not include `config.toml` or `config.local.toml`. Copy those yourself
and keep them with the archive. Backups also leave out the pairing secret and the paired browsers, so
after a restore on a new machine you pair your browsers again.
Use the new machine's address followed by `/stream.mp3` to listen.

Backup is safe while AutoDJ is serving: it copies the index under the index's own lock and
both databases through SQLite's backup API, so it never picks up a half-written index. It refuses to
overwrite an existing archive unless you pass `--force`, and it refuses an index made by an older
AutoDJ; convert schema 3 with `autodj migrate-index --apply` first. That conversion saves its own
recovery copy. Older backup archives that do not contain `library.db` leave the current station
catalog unchanged when restored.

```bash
# Linux/WSL2 Bash
uv run autodj backup backups/autodj-$(date +%F).zip
```

```powershell
$stamp = Get-Date -Format yyyy-MM-dd
uv run autodj backup "backups\autodj-$stamp.zip"
```

## Restore and validate

Backup archives are still restricted to the AutoDJ major and minor version that made them.
For an archive made by 0.19, restore it with 0.19 first, then upgrade and convert the index if
needed. Version 0.20 can open an existing schema-4 index directly; the application version bump
does not change that index format. Make a new backup after upgrading to include the station catalog.

Stop AutoDJ, and any `autodj index` or `autodj analyse` run, before you restore. Restore cannot
tell whether AutoDJ is running, and a running AutoDJ would keep using the files it replaces.

```bash
# Linux/WSL2 Bash
docker compose down
uv run autodj restore --force backups/autodj-2026-08-02.zip
docker compose up
```

For a native Windows process, press Ctrl+C in the terminal running `uv run autodj serve`, then wait
for the process to exit. If a service manager runs AutoDJ, stop that service and wait for it to
report that the process has stopped. Then run:

```powershell
uv run autodj restore --force "backups\autodj-2026-09-12.zip"
```

Restore only accepts an archive made by the same major and minor version of AutoDJ: a 0.19.x
backup restores on any 0.19.x release and on no other. To roll back to an earlier release, check
out that release's tag as in the [upgrade checklist](#upgrade-checklist), then restore a backup made
by that release. Restore also refuses an archive whose file names are absolute or contain `..`,
files the manifest does not list, and an index made by an older AutoDJ.

Without `--force`, restore refuses when anything it would replace already exists. With it:

- The index in the archive replaces the current index, including its older generations.
- `dj_meta.db` and `web_state.json` are replaced when the archive has them.
- The liners folder and `index/profiles` are replaced whole: files that are not in the backup are
  deleted. Parts the archive does not hold are left alone.

Restore unpacks every file into a temporary folder beside its destination and checks the index
before it replaces anything, so a damaged archive changes nothing. If the replacement itself is
interrupted, run the same restore again. Restore then runs `autodj doctor` and says so if doctor
finds a required failure; do not serve until doctor passes. Use the same configuration for backup,
restore, and doctor that the previous `serve` process used. Keep the archive until playback,
profiles and liners look right.

## Upgrade checklist

These steps apply to a native installation from a source checkout.

1. Stop AutoDJ, then create and retain a backup before changing the checkout or its dependencies.
2. Run `git status --short`. If it prints any paths, stop. Commit the changes on a branch or copy
   them outside the checkout. Continue only after `git status --short` prints nothing.
3. Replace `vX.Y.Z` below with the release tag you intend to run. Fetch the tags, then check out
   that release:

   ```bash
   git fetch --tags origin
   git switch --detach vX.Y.Z
   ```

4. Run `uv sync --frozen --all-extras` from the fetched release and its committed lock. If you use the experimental Windows AMD environment, update it by following
   [Experimental Windows AMD GPU setup](windows-amd.md).
5. Read the "Removed" and "Changed" sections of every release since yours in
   [CHANGELOG.md](../CHANGELOG.md). A configuration key that a release removed now stops AutoDJ at
   startup, so delete or rename it in your configuration files first.
6. Run `uv run autodj doctor`. If its index check fails with `UnsupportedIndexError`, the index
   was made by an older AutoDJ: run `uv run autodj index --force` to rebuild it, then run doctor
   again.
7. Start loopback-only and check that the version in the page footer is the release you checked
   out before enabling LAN access.

### Going back to an older release

Restore the backup you made before the upgrade. A newer AutoDJ writes some settings that an older
one reads differently:

- An every-N-minutes schedule event that keeps to chosen days: AutoDJ 0.20.0 and older ignore its
  days and run it every day. Edit the event, or delete it, before going back.
- A schedule event with a date range, a category with days, hours or dates, a clockwheel step with
  days or hours, or a cart with play dates:
  an older AutoDJ that does not know these fields refuses the whole file. It sets
  `schedule.json`, `categories.json` (a category with dates included) or `carts.json` aside as
  damaged and starts without it, and leaves such a clockwheel off. Files saved without
  these settings load in either version. Liner play dates are in a file of their own,
  `liner_dates.json`, which an older AutoDJ does not read.
- Voice pack play dates are kept beside the time check switches in `time_checks.json`; an older
  AutoDJ ignores them and lets every ticked pack speak.

## Cart wall

The **Carts** tab, after Queue & Search, holds four pages of twelve numbered carts for station
IDs, jingles and sound effects. Each button is named for its cart, for example "Cart 3, Station
ID, 0:07", or "Cart 4, empty". Press a cart to play it; an empty cart opens in the editor.

In the editor, choose the cart, its name, its clip and what happens to the music while it plays:

- Duck lowers the music by the voice liner duck depth.
- Over leaves the music at full volume.
- Replace fades the music out. The song keeps playing silently under the cart and fades back in
  when the cart ends, so it resumes at its later position.

A clip is a voice liner from the library or the liner folder, or an audio file uploaded under
**Uploaded clips**. Uploads use the voice liner extensions and the `[server]
liner_upload_max_mib` limit, must decode as audio, and never replace a clip with the same name. A
clip still on a cart cannot be deleted. **Rename this page** names the page shown.

One cart plays at a time: a new one cuts off the last, and **Stop carts** fades it out. Each
open paired page says "Cart 3, Station ID, playing" when a cart starts and "... ended" or
"... stopped" when it finishes, from any tab.

With focus on the cart wall, keys 1 to 9, 0, minus and equals play carts 1 to 12 on the page
shown, and Page Up and Page Down change the page. The keys do nothing in text fields and
dropdowns, and the **Enable keyboard shortcuts** checkbox (Settings, Display and keys) turns
them off. With NVDA, switch to focus mode first (NVDA+Space).

Carts play through the voice liner path. In server audio and stream mode the server mixes them
in, and they need playback running: a cart is refused while the stream is idle or paused. In
browser playback the page that plays the cart plays it, as the voice liner Test button does.

The server keeps the wall in `carts.json` in the active index folder and the uploads in its
`carts/` folder. The file is versioned; one that cannot be read is moved aside as
`carts.json.bad-<id>` and an empty wall loads. Backups include the wall and the uploads. Saving
a profile records the name of the cart page shown, and applying the profile shows that page; a
profile whose page has been renamed is refused.

A cart can also have play dates; see [Liner and cart play dates](#liner-and-cart-play-dates).

## Liner and cart play dates

A voice liner or a cart can have a **Not before** date, a **Do not play after** date, or both,
such as a promotion that must stop after the event. Both days are included, and the days are
read in the station time zone (`[server] station_timezone`). Leave both empty to play the clip
on any day.

- **Voice liners:** in **Settings > Voice liners**, press **Play dates** on a liner.
  Outside its dates, automatic liners (in the browser and on the server), clockwheel liner steps
  and scheduled liner events skip it, and a scheduled event's run says why in Recent runs. The
  liner list says the dates in words, such as "Plays until 26 December 2026", and adds "Skipped
  now: expired after 26 December 2026". **Test** never picks a skipped liner. A cart that plays
  the liner still plays it; give the cart its own dates.
- **Carts:** set the dates in the cart editor. Outside them, pressing the cart, its key or a
  scheduled Play cart event plays nothing and says why, such as "Cart 3, Promo was skipped: it
  is expired after 26 December 2026", and the cart button ends with "expired" or "not playable
  yet".

A page left open past midnight follows the station's day: the liner list adds or drops "Skipped
now", and the cart buttons and the editor's cart list add or drop "expired" and "not playable
yet", in place. Only the words change, so focus stays where it is and nothing is announced. A
refused date in the cart editor is shown under the editor and linked to its field, whether the
page or the server refused it.

Liner dates are kept in `liner_dates.json` in the active index folder, by the ID Browse voice
liners uses (the library ID of a registered liner, or the file name of one in the liner folder),
and are included in backups. Deleting a liner forgets its dates. So does a library scan for a
liner deleted outside AutoDJ: a registered liner whose file the scan found gone from a folder
that is there, or a liner folder file that is no longer in the folder. A liner on a share that is
not mounted keeps its dates, and so does every liner after a scan that could not read one of its
folders, or when the liner folder cannot be listed: such a scan marks nothing gone and forgets
nothing. A file listed but not reachable for a moment is not gone either. Dates kept by a library
ID that is no longer in the library are kept too. A liner folder that a scan finds empty forgets
its liners' dates only when the next scan finds it empty as well, and names are compared as the
file system compares them (without case on Windows). When `liner_dates.json` cannot be read just
now, or its folder is away (a dropped share says "not found" as a missing file does), the dates
last read are used, a save is refused with a message, and the file is never set aside or written
over; only a file that is not valid dates is set aside. An upload under a free name waits (it
answers that the dates could not be read) until they can be read, so it never takes an old
liner's dates, and deleting a liner says when its dates could not be forgotten yet. A liner uploaded under a name that is not in use starts without
dates, even where an earlier file of that name left some; uploading over an existing liner with
Replace keeps them. Cart dates are kept with the cart in `carts.json`. A liner or cart without
dates is saved as before, so an older AutoDJ still reads both files.

With **Pick in order** (sequential), a liner outside its dates is passed over and the rotation
keeps its place: the liner after it still comes next. Talk-up liners planned ahead pass over it
too, judged by the day they air, and so do clockwheel liner steps, by the day the next song
starts. **Test** on the server refuses a liner outside its dates and says why. Time check voice packs have dates of their own; see [Voice packs](#voice-packs).

## Scheduler

Open **Settings**, then **Schedule** to add timed station events. An event runs once, every day,
on weekdays (Monday to Friday), on chosen days, or every N minutes. An every-N-minutes event can
have an active window, such as 22:00 until 02:00; a window may cross midnight, and both ends run.
Without a window the count starts again at local midnight. An every-N-minutes event can also keep
to chosen days; leave every day unchecked to run every day.

Any repeating event can have a **Date range**: an optional start date, end date, or both. A
one-time event's date is its whole range, so the form hides **Date range** for it, unless the
event was saved with a range: then the dates stay in view to keep or clear. The event runs
only on dates from the start date to the end date, both included, in the station time zone, and
the **Next events** list skips the dates outside it. An active window that crosses midnight
belongs to the day it starts: a Friday 18:00 until 02:00 window whose end date is that Friday
runs until 02:00 on Saturday, and a window that started the day before the start date does not
run. The events list says the range in words, such as "Every day at 00:00, from 27 November to 26
December 2026", and an event with no run left says "Ended, no runs left". An event whose range
has no run left cannot be saved enabled; save it disabled, or move its end date. Schedule files
from earlier versions load unchanged, and an event without a range is saved without the two
fields.

A seasonal clockwheel, such as a holiday one from 27 November to 26 December, takes two events:

1. **Holiday music**: repeat every day at 00:00, start date 27 November, end date 26 December,
   action **Change song picking** to the holiday clockwheel. Running every day means a manual
   change, or a midnight missed while AutoDJ was stopped, is put right the next day.
2. **Regular music**: once on 27 December at 00:00, action **Change song picking** back to the
   usual clockwheel.

Use **Run now** on the first event to start the season at once when you add it partway through.

Times use `[server] station_timezone`, an IANA name such as `America/Denver`, or the system time
zone when that is not set (UTC when the system has no IANA name). The Schedule panel shows the
zone in use. When daylight saving time skips a time, the event runs at the moment the clock
jumps; when a time happens twice, the event runs the first time only.

Actions:

- Switch to a saved profile or a preset.
- Switch the Stereo Tool preset, to Included or an uploaded one. It takes effect when Output
  processing is Stereo Tool, restarts the processor at once and never skips the song playing. A
  preset deleted after the event was saved makes its runs fail in words.
- Change song picking. Besides similar songs, random and least recently played, the choices are
  the active clockwheel and each saved clockwheel by name; a named one also becomes the active one.
- Play a voice liner or station ID. This needs server audio or stream mode.
- Play a cart by page and cart number, with its saved duck, over or replace mode. This needs
  server audio or stream mode.
- Play a time check. This needs server audio or stream mode.
- Start or stop the stream (stream mode). Start keeps the station playing without listeners,
  stop keeps it off the air even with listeners, and automatic hands control back to the
  listener count. The choice lasts until the next stream event or a restart.
- Turn discovery on or off. Discovery needs a rate set in Song picking before it can be on.

Timing is soft or hard. A soft event waits for the current song to end; with nothing playing it
runs at once. A hard event runs at its time and fades out the current song through the normal
skip: after the change for profile, preset, song picking and discovery events, so the next song
follows it, and before the liner for liner events. Stream and Stereo Tool preset events never
skip.

**Run now** runs an event's action at once, to test it. Each run, failure and missed event goes
to the server log and to **Recent runs**, which keeps the last 200.

Schedules live in `schedule.json` in the active index folder, next to `web_state.json`. AutoDJ
writes the file atomically and includes it in `autodj backup`. A damaged schedule file, or an
event whose action this version does not have, is copied to `schedule.json.bad-<id>` and left
out, so the server still starts. Events are not replayed after downtime: a one-time event whose
time passed while AutoDJ was stopped is disabled and recorded as missed, and an event more than
two minutes late (the computer slept) is recorded as missed instead of running. The schedule
holds at most 100 events.

## Clockwheels

A clockwheel is a repeating order of steps that song picking follows when **Pick mode** (Settings, Picking) is set
to **Clockwheel**. Keep as many as you like in the **Library tools** tab, **Clockwheels**: create,
duplicate, rename, delete, activate one, or turn the clockwheel off. Only the active one plays.
Profiles remember the active clockwheel, and a schedule event can switch to one by name.

Step types:

- **Music** or **Voice liner** matching the conditions in the step. Choose least recently played
  or random, and whether to follow the repeat rules. A liner uses its saved overlap points with
  the next music step; two liner steps cannot be next to each other.
- **A specific track**, by its path inside a registered music folder. It follows the repeat rules;
  when they hold it back, the clockwheel moves on to the next step.
- **Switch preset**: the next song is picked and mixed with that preset.

Music and liner steps have an **Add condition** button in the same step form. Choose **Match
all** when every condition must hold, or **Match any** when one is enough. Each folder is its
own condition. For example, Match any with two folders selects from either folder; Match all
with a folder and Year before 2000 selects older songs from that folder. A step can also use
only metadata conditions, without choosing a folder. Mixed groups such as `(folder A or folder
B) and year before 2000` are not supported yet.

Available conditions:

- **Category** (Music steps): choose a saved [rotation category](#rotation-categories). With
  Match all, a song must belong to every selected category and meet the other conditions. With
  Match any, belonging to any selected category or matching another condition is enough. For
  example, Category Gold and BPM above 100 narrows Gold to faster songs; Category Gold or
  Category Current hits combines those groups. A song matching several conditions is included
  once, so it gets no extra chance of being picked. Category hours still apply even when another
  condition also matches the song. Categories are music groups, so this condition is not offered
  for voice liners.
- **Folder**: a registered music or liner folder, or a subfolder such as `Rock/Live`. Include
  subfolders applies to that condition. The registered folder must include subfolders before a
  condition can use one of them.
- **Genre**: choose a genre held in the library. Matching ignores case. Multiple labels stored
  with semicolons, or as a list, are matched separately; slashes and commas remain part of a label.
- **Year**, **BPM** and **Duration**: equal to, less than, at most, greater than, at least, or between
  two values. Between includes both ends. Year is the release year in AutoDJ's library. Duration
  is the full file length; enter seconds or `minutes:seconds`.
- **Artist**, **Title** and **Album**: exact text, contains text, or a wildcard pattern. Matching
  ignores case. In a pattern, `*` means any text and `?` means one character. Other punctuation is
  literal; these are not regular expressions.
- **Beets field**: a field from the configured Beets database, including custom fields. Choose a
  text or numeric comparison. `original_year` uses the first-release year, falling back to
  `year` when it is missing. Ordinary conditions work without Beets.

A missing value does not match a condition. Unknown year, BPM or duration values are not treated
as zero. The player reads Beets without changing it and caches the requested fields until the
database changes. An unavailable database or field produces an error when saving or activating
a step that needs it. If it becomes unavailable during playback, that step is skipped with a
status message. Other valid steps can still play.

Repeat rules apply after the conditions have selected their pool. Relaxing repeats never removes
those conditions. When explicit conditions have no matches, including category conditions, the
step is skipped and does not use unfiltered music. The existing dayparting-only fallback remains
when matched songs are held back only by their allowed days or hours. Unedited legacy category
steps keep their old empty-pool fallback; editing and saving one converts it to an explicit
category condition. Existing folder steps also open in the Music or Voice liner form and keep
their selection and repeat-rule settings.

A deleted category skips the affected step, even with Match any, and the status says why. An
existing category with no songs contributes no matches; other Match any conditions can still
supply a song. Saving or activating a step with a missing category asks you to choose a saved one.

The editor lists numbered steps with their names as Edit buttons. With focus in the step list,
Up and Down move to the previous or next step name. Left and Right move between the actions in a
row; Home and End go to its first or last available action. Enter or Space presses that action.
Delete removes the step immediately, just like its Delete button. Tab leaves the step list.
Alt+Up and Alt+Down reorder the focused step, and focus moves with it.

**Undo step change** or **Ctrl+Z** restores the last step you added, edited, moved, duplicated or
deleted. Delete and Undo are unavailable while a step is being edited; update or cancel the edit
first. You can undo several changes in order, until you save or open another clockwheel.
Ctrl+Z in a text field keeps the browser's usual text undo. Undo changes the working copy;
press **Save clockwheel** to keep the result.

These list keys do not replace the playback or spoken-status shortcuts. Nothing is saved until you press **Save clockwheel**; opening
another clockwheel with unsaved changes asks first. **Restart at these minutes past each hour**
sends the clockwheel back to step 1 at the song change nearest each minute.

For a voice liner step, **Only play if the following track fits before the next reset** is off
by default. Turn it on to leave out the liner when the liner and the selected song would run
past the next reset. The check uses their saved cue points and overlap, through the song's
next handoff. The song still follows the usual clock and repeat rules; skipping the liner does
not count it as played. With no reset times set, this checkbox has no effect.

Each clockwheel is one JSON file in `clockwheels/` in the active index folder, named after the
clockwheel and included in `autodj backup`. Names allow letters, digits, spaces, dashes and
underscores, up to 64. The format is versioned and checked strictly; an unknown field, step type
or version stops that clockwheel from loading, says why in the editor and the server log, and
leaves the clock off while the station keeps playing:

```json
{
  "version": 1,
  "name": "Morning",
  "steps": [
    {"type": "music", "source": "<library folder ID>", "folder": "Rock",
     "recursive": false, "selection": "least_recent", "enforce_rules": true},
    {"type": "liner", "source": "<library folder ID>", "folder": "",
     "recursive": false, "selection": "random", "enforce_rules": false},
    {"type": "track", "source": "<library folder ID>", "path": "Album/Song.flac"},
    {"type": "music", "conditions": [{"field": "category", "category": "<category ID>"}],
     "match": "all", "selection": "least_recent", "enforce_rules": true},
    {"type": "settings", "preset": "winddown"}
  ],
  "resync_minutes": [0]
}
```

`source` is the ID of a registered library folder, so the file never holds a full path; removing
that folder from the library stops the clockwheel until you edit its steps. Later features (carts,
time checks, ad breaks) add their own step types through `autodj.clockwheel_store.register_step_type`.

**When this step plays.** Any step can keep to chosen days, to hours (**Allowed from** and
**Allowed until**), or both, in the step form's **When this step plays** fields, with the rule
categories use: wall-clock times in the station time zone, the start included and the end not,
and a window that crosses midnight belongs to the day it starts. Outside them the step is skipped
as an empty step is, and the clockwheel moves on to its next step; a voice liner step outside
its hours leaves the following song without its liner, and a Switch preset step outside its hours
switches nothing. The hours are checked for when the song is expected to start, as the
categories' are. The step list says them after the step, such as "music from Rock, only on
Friday and Saturday from 22:00 until 02:00", the clockwheel status and the server log say why a
step was skipped ("Step 3: outside its hours: plays only ...", or "outside its days" on a day it
does not keep), and the rotation tester counts those skips. A clockwheel whose every song step is
outside its hours does what it does when no step has a song: one with category steps plays a song
from the whole library, and one of folder, condition or track steps plays nothing until a step
opens, as before. The whole-library song that a category's own hours allow is only for songs held
back by their categories, not by a step's hours. A voice liner step and the song step after it
are both judged at the song change; a liner that started after the song (#159) would still be
judged then. In the
file a step has `"days": [4, 5], "hours_from": "22:00", "hours_until": "02:00"`; a step without
them is written as before, so an older AutoDJ still reads the clockwheel. Use this for a step or
two that differ at night; for a different clockwheel at night, a schedule event that switches
clockwheels is simpler.

**Import .PAL** turns a SAM Broadcaster `.PAL` file into a new clockwheel, once; the `.PAL` file
is not used after that. Only `Dir[...]` and `SubDir[...]` lines with `QueueBottom` are read, and
nothing in the file runs. Each folder must be a registered music or voice liner folder, or a folder
inside one that adds subfolders; the innermost registered folder is used. A folder above every
registered folder (for example `SubDir['C:\Station']` when only `C:\Station\Rock` is registered)
is refused: register the parent folder, or edit that step after the import.

Upgrading from the single clockwheel: the clock saved by earlier versions is not read. The
Clockwheels section and the server log say so. Import the `.PAL` file again under a name, then
press **Activate**.

## Rotation categories

A rotation category is a named set of songs that clockwheel steps pick from, the way professional
stations schedule music: rotation A for current hits, B and C for recurrents, D for gold, or a
themed set such as "Remembering the 2000s". There is no separate weighted pick mode; how often a
category plays is set by how many steps of the clockwheel use it. Choose **Music**, add a
**Category** condition, and select its name. You can use several categories or combine a category
with folder and metadata conditions in the same step; the category itself stays unchanged.

Keep them in the **Library tools** tab, **Categories**, next to the clockwheels that use them. The
list says how many songs each category holds, how many playlist lines match no library track,
how many songs were placed by their release year (see below), and which clockwheels use it. **Add
category** and **Edit** open one form; **Check** counts the songs without saving, and **Save**
refuses a query that cannot run or names a field no song has. **Delete** asks first and names the
clockwheels with a step for the category.

A category's songs come from one of two sources:

- **A beets query**, such as `original_year:2000..2009` or `genre:rock ^artist:"bon jovi"`. It is
  read against AutoDJ's library, with the values of the live beets database (`[library] beets_db`,
  opened read-only) laid over each song, so any beets field works, including flexible attributes
  that plugins or `beet modify` add, such as `mood` or `rotation`. Without beets the query sees the
  library's own fields: title, artist, album, genre, year, bpm and length. AutoDJ runs the common
  part of the beets query language itself, as beets is not needed at run time:
  - `field:value` matches when the field contains the value, ignoring case; on a number field such
    as `year` or `bpm` the value must match exactly.
  - `field:a..b` is a range of numbers, both ends included; either end may be left open, as in
    `bpm:..100`.
  - `^` (or `-`) in front of a term leaves out what it matches.
  - Quotes keep words together: `artist:"the beatles"`.
  - A bare word matches artist, title, album, album artist, genre or comments.
  - Every term must match. Regular expressions (`field::re`), exact matches (`field:=x`),
    alternatives with commas, sorting, date ranges, path queries and plugin queries are refused
    with a message that names the term; make a category per alternative and combine them with
    Match any in the clockwheel step.
- **An M3U or M3U8 file** in the playlists folder (`[library] playlists_folder`, by default
  `playlists/` in the active index folder). Each line that is not a comment is matched to a
  library song by its full path, or by a path from the playlist's own folder or from
  `[library] music_dir` (where beets keeps relative paths); `file://` addresses work too. Lines
  that match no song, web addresses included, are counted and the first few are named, so a
  script that writes paths for another machine is easy to spot. M3U8 files are UTF-8; plain M3U
  files that are not UTF-8 are read as Windows-1252.

**Years.** A year filter should use `original_year`, the year the song first came out. beets'
`year` is the year of that particular release, so a 1985 song on a 2005 compilation has `year`
2005. When beets has no `original_year` for a song (it stores 0), its `year` stands in, and a song
with neither is left out of year filters. The list, Check and `autodj doctor` count the songs
that used `year`, so you can fix their tags with beets.

**When the songs are worked out.** AutoDJ works out each category's songs once and keeps the
answer until the categories, the beets database, a playlist file or the library changes, which a
few file-time checks before each clockwheel pick tell it; the songs are never read again for
every pick. When a playlist is being rewritten or beets is busy importing, a category keeps the
songs it held last time and the server log says why.

**At play time.** An explicit category condition picks from its category members and follows the
step's repeat rules, relaxing them rather than going silent; in DMCA mode it keeps the DMCA
limits. If no song matches its conditions, the step is skipped, including when a Match any step's
category is missing or empty. An unedited legacy category step keeps its old whole-library
fallback when all steps fail; saving it from the editor converts it to an explicit condition.
The dayparting-only fallback also remains when matched songs are held back only by their allowed
days or hours. The clockwheel keeps its place in each case.

**Dayparting.** Under **When the songs may play**, a category can allow its songs only on chosen
days, only between two times (**Allowed from** and **Allowed until**), or both, such as Friday and
Saturday from 22:00 until 02:00. Leave every day unchecked for every day and both times empty for
all day. The times are wall-clock times in the station time zone (`[server] station_timezone`);
the start time is included and the end time is not. A window that crosses midnight belongs to the
day it starts, so Friday 22:00 until 02:00 still allows the songs at 01:00 on Saturday. On the
night the clocks go back, a window inside the repeated hour is open for both passes; on the night
they go forward, one inside the skipped hour does not open.

Outside its days and hours, a category's songs are left out of every pick mode: similar songs,
discovery, random, least recently played and every clockwheel step, and a clockwheel step for
that category is skipped with "outside its hours" in the clockwheel status and the server log. A
song in several categories plays only while every one of them allows it. Relaxing the repeat rules
or the DMCA limits never brings such a song back. When nothing else at all can play, the station
plays one anyway rather than going silent, and the server log says so. A song you queue yourself
plays at any time. The list says each category's days and hours in words, such as "Plays only on
Friday and Saturday from 22:00 until 02:00", and when it is outside them now; with all seven days
ticked and no times it says "Plays every day, at any hour".

The hours are checked for when the song is expected to start, not when it is picked: the song
after this one is picked while this one plays, so a pick at 01:58 with four minutes of this song
to go checks the hours at 02:02, and a window that ends at 02:00 no longer lets its songs start
just after it closes. The rotation tester checks them the same way. Which songs are outside their
hours is worked out once a minute, so a pick only looks them up. Without `[server]
station_timezone`, the system time zone is asked for once, when AutoDJ starts; a host with no
known zone uses UTC and logs one warning. When every song is outside its hours, the warning that
one plays anyway is logged once, and again only after the hours change.

For dayparting single songs, make a category from a beets query on a field you set, such as
`beet modify daypart=night` and the query `daypart:night`, and give it the hours. Categories that
have no days or hours are saved as before, so an older AutoDJ still reads the file.

**Seasons.** The same fields take a **Not before** and a **Do not play after** date, both
included, in the station time zone, such as 1 December until 26 December for Christmas songs.
Outside them the category's songs are left out exactly as outside its hours, and a clockwheel
step for it is skipped with "outside its dates" (or "outside its days" on a day it does not keep).
The server log names what holds the songs back, such as "every song is outside its category's
dates". Dates combine with days and hours: a Friday
22:00 until 02:00 window whose last date is that Friday still plays until 02:00 on Saturday. The
list says the season in words, such as "Plays only between 1 December 2026 and 26 December
2026", and "Outside its dates now." A category without dates is saved without the two fields.

The categories are kept in `categories.json` in the active index folder, written atomically and
included in `autodj backup`; a damaged file is set aside as `categories.json.bad-<id>` and the
station starts with no categories. The playlist files are not backed up: they come from a script
or a music folder, like the music. `autodj doctor` lists categories with no songs, missing
playlist tracks, or a query or playlist that cannot be read.

### Example: rotations A to D from playlists

If a script builds rotation playlists from beets (to import them into AzuraCast, for example),
point it at the playlists folder instead. This `update-playlists.sh` writes one M3U8 per rotation
from a beets flexible attribute set with `beet modify rotation=A`, through a temporary file so
AutoDJ never reads half a playlist:

```bash
#!/usr/bin/env bash
# update-playlists.sh: write rotation playlists A to D for AutoDJ.
set -euo pipefail
out=/srv/autodj/playlists      # [library] playlists_folder in config.toml
music=/mnt/music/              # beets' directory; stripped so the lines are relative to music_dir
mkdir -p "$out"
for rotation in A B C D; do
  {
    echo "#EXTM3U"
    beet ls -f '$path' "rotation:$rotation" | sed "s|^$music||"
  } > "$out/.$rotation.m3u8.tmp"
  mv "$out/.$rotation.m3u8.tmp" "$out/$rotation.m3u8"
done
```

Run it from cron after each beets import. Then in **Categories** add four categories, A to D,
each from **A playlist file** with its file, and make a clockwheel such as A, B, A, C, station ID
liner, A, D. Each new run of the script is used from the next song AutoDJ picks. Writing lines relative to
the music folder keeps the playlists working when AutoDJ sees the music at another path, such as
`/music` in the container; with full paths, AutoDJ must see the files at the same paths as beets.

The playlists are optional when beets is on the same machine: a category from the query
`rotation:A` holds the same songs without a script.

### Example: Remembering the 2000s

1. In **Categories**, add **Remembering the 2000s** from **A beets query** with
   `original_year:2000..2009`, press **Check**, then **Save**. The list then says, for example,
   "Remembering the 2000s: 1,284 songs; 12 use the release year because the first-release year is
   missing".
2. In **Clockwheels**, create **Throwback** with the steps: voice liner from your station ID
   folder, music from category Remembering the 2000s, music from category Remembering the 2000s,
   voice liner, music from category Remembering the 2000s, then save it.
3. In **Settings**, **Schedule**, add an event at 20:00 on Saturdays that changes song picking to
   **Throwback**, and one at 21:00 that changes it back to your usual clockwheel.

For a narrower hour, add terms: `original_year:2000..2009 genre:rock ^genre:christmas`.

## Spoken time checks

Open **Settings**, then **Time checks**, and turn on **Enable time checks** (or set
`[time_checks] enabled = true`). Choose 12- or 24-hour speech, and choose **After the song that
crosses the hour** to hear the time when the song playing at the top of the hour ends. The
Schedule panel also offers a **Time check** action for set times; it needs server audio or
stream mode. Both use `[server] station_timezone` (or the detected system zone), and the time
said is the time when the check plays. A check plays at most once an hour from the rule: when a
scheduled check already played in that hour, the rule stays quiet.

A check is one clip: the optional intro, the time, and the optional outro. The time comes from
the first source that has the minute: a ticked voice pack, then a recorded minute clip, then
text to speech. A missing or unreadable file only moves on to the next source; it never stops
the music. It plays like a
voice liner, with the liner duck depth. After the hour, in server audio or stream mode, it takes
the place of the automatic liner due at that song change and uses **Talk up to the vocal** when
the clip fits before the singing. When a talk-up liner is already playing over the crossfade
into that song, the check waits for the following song rather than cutting the liner off. In
browser playback the page plays it when the next song starts; there it does not replace a voice
liner due at the same song change, so the two can overlap. The **Play a time check now** button plays one straight away: on the server in server
audio or stream mode, and in this browser otherwise.

Record one clip per minute in the configured `clips_folder` (by default the `time_checks`
folder in the active index folder): `00_00.wav` through `23_59.wav`, using 24-hour names even
when speech uses 12 hours. MP3, FLAC and OGG are also accepted. For example, `03_05.wav` plays
at 3:05 in the morning and `15_05.wav` at 3:05 in the afternoon. The Settings panel says how
many minute clips there are. Run `uv run autodj doctor` for the number missing, the first ten
missing names, and warnings for a missing folder, intro or outro clip or TTS program. A minute
without a clip uses TTS when it is configured. Without a clip or working TTS command, the check
is skipped and the server logs one warning.

### Voice packs

A voice pack is a folder of short recordings that AutoDJ joins into one sentence, the way
RadioBOSS does, so its packs work as they are: an hour file says "it's 5", a minute file says
"fifty-six", and an exact-hour file says "it's 5 o'clock". List the folders in `config.toml`;
the folder name is the pack's name in Settings, so two packs cannot share a folder name:

```toml
[time_checks]
voice_packs = ["D:/Voices/Studio", "D:/Voices/Night"]
pack_order = "rotate"   # or "random"
```

**Settings**, **Time checks** shows each pack as a checkbox under **Voice packs**. Its
description says how many of the 1440 minutes it can say, which hour, exact-hour and minute
files are missing, the optional files it uses, and any files it does not use. Tick the packs to
use; with no saved choice every listed pack is ticked. **When several packs are ticked**
chooses whether they take turns, one per check, or one is picked at random. When the chosen
pack cannot say the minute, the other ticked packs are tried before the recorded clips.
**Voice to test** picks one pack, ticked or not, for **Play a time check now**; when the
server plays it, the result says which source spoke. The browser only ever sends a pack's name; the folders stay
in `config.toml`.

A pack can have play dates, such as a holiday voice from 1 to 26 December. Under **Voice pack
play dates**, choose the pack, enter **Not before**, **Do not play after** or both, and press
**Save play dates**. Both days are included, in the station time zone. Outside its dates a ticked
pack is passed over and the other ticked packs speak; **Voice to test** still plays it. Each
pack's description says its dates in words and "Skipped now" when it is outside them. A refused
date is shown under the form and linked to its field. The dates are kept beside the switches in
`time_checks.json` under `pack_dates`, written only when a pack has dates, and are kept for a
pack taken out of `config.toml` until it comes back. Dates that cannot be read, and keys this
version does not know, are logged and not used, and are written back as they were on the next
save. Each save reads the file again first, so what another process wrote is kept. While the file
cannot be read, saves are refused with a message rather than written over it; a file that is not
valid JSON is set aside as `time_checks.json.bad-<id>` before the next save.

AutoDJ reads only the files directly in the folder, with the extension `.wav`, `.mp3`, `.flac`
or `.ogg`, and compares letters without regard to case. It recognises these names:

- Hour file: letters, spaces, `_` or `-` that include `h` and no `m`, then a two-digit hour
  from 00 to 23. Examples: `hr_05.mp3`, `H05.wav`, `hour-17.ogg`.
- Exact-hour file: an hour file name with `o` after the digits, either straight after them or
  after one space, `_` or `-`, such as `hr_05o.mp3` or `hrs05_o.mp3`. It plays alone at
  minute 00.
- Minute file: letters, spaces, `_` or `-` that include `m` and no `h`, then a two-digit minute
  from 00 to 59. Examples: `m_56.mp3`, `min 07.wav`.
- `intro` and `outro` (such as `intro.wav`): said before and after the time, joined in the same
  way. They are part of the pack's voice; `intro_clip` and `outro_clip` still wrap the whole
  check.
- `am` and `pm`: in 12-hour mode, said after the time when the pack has both.

At minute 00 AutoDJ uses the exact-hour file; without one it uses the hour file and `m_00`.
Other minutes use the hour file and the minute file. Any other audio file is listed as not
used, and AutoDJ does not guess: a name with both or neither of `h` and `m` before the number
(`hm_05.mp3`), a number out of range (`hr_24.mp3`), a number that is not two digits
(`hr_5.mp3`), a minute with `o`, an `am` file without a `pm` file, or two files for the same
piece (`hr_05.mp3` and `H05.wav`: neither is used, so hour 05 counts as missing). Rename the
files and the next check picks them up.

Hour files are named with the 24-hour clock. In 24-hour mode a pack needs all 24. In 12-hour
mode, when the file for hour 13 to 23 is missing, the file for 1 to 11 is used, and the file
for 12 stands in for 00; so a 12-hour pack with `hr_01` to `hr_12` covers the day, and its
`am` and `pm` files tell the halves apart. A 24-hour mode check never uses a 12-hour file,
which would say the wrong hour.

Joining makes the pieces sound like one sentence:

- Leading and trailing silence is cut from each piece. Silence is anything 45 dB under the
  piece's own peak, measured in 5 ms steps, so soft consonants stay; 10 ms is kept before the
  first sound and 30 ms after the last for the word's natural decay. Only the ends are cut: a
  pause inside a recording stays.
- Each piece is brought to the pieces' average speech loudness, moving no piece more than 12 dB,
  and all of them come down together if one would peak above -0.2 dBFS.
- Neighbouring pieces overlap by 40 ms with an equal-power crossfade. The overlap is exactly the
  10 ms lead plus the 30 ms decay that trimming kept, so only those quiet margins are blended:
  the first word is at full level until its last sound and the next word reaches full level at
  its first sound, with no gap and no click. The joined voice fades in and out over 3 ms.

AutoDJ keeps about 32 MB of joined checks in memory, per pack and minute, and joins again when a
pack file is replaced, added or removed. The check then plays through the voice liner path like
any other, with the same duck and talk-up timing.

`uv run autodj doctor` shows one `voice-pack-<name>` line per pack, with the same coverage
text and the folder, and warns when the folder is missing, a file is not used, or the pack
cannot say every minute. The `time-checks` line warns about a missing TTS fallback only for
minutes that neither a ticked pack nor a recorded clip can say, and warns when a saved pack
name is not in `voice_packs`.

Backups include voice packs kept in subfolders of the `time_checks` folder (for example
`<index>/time_checks/Studio`), because every file in that folder is backed up. Packs listed
from other folders are not copied, like the music library: back them up yourself with
`config.toml`.

### Text to speech

The speech says "It's 3:05", or "It's 15:05" in 24-hour mode. On the hour it says "It's 3
o'clock" in 12-hour mode and "It's 15 hundred" in 24-hour mode, and in both modes "It's midnight"
at 00:00 and "It's noon" at 12:00.

The `tts_command` is an argument array in `config.toml`. AutoDJ passes the spoken text on stdin,
replaces `{out}` with a temporary WAV path, replaces `{text}` with the text as one argument,
allows 20 seconds, and caches the result for each command and text. It runs the program
directly, never through a shell, and on a worker thread. A command that fails, times out or
writes a WAV with no sound is skipped with a warning. Examples:

```toml
# Piper, with an installed voice model (Linux or Windows)
[time_checks]
tts_command = ["piper", "--model", "/voices/en_US-lessac-medium.onnx", "--output_file", "{out}"]
```

```toml
# espeak-ng in a Linux container
[time_checks]
tts_command = ["espeak-ng", "-w", "{out}", "{text}"]
```

```toml
# Windows SAPI, with the full path to the script in your AutoDJ folder
[time_checks]
tts_command = [
  "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
  "-File", "C:/AutoDJ/scripts/time_check_sapi.ps1", "{out}", "-Voice", "Microsoft Zira Desktop",
]
```

The SAPI script speaks the text as plain text and never runs it. Name a voice with `-Voice`:
the Windows default voice can be a screen reader's SAPI bridge, such as NVDA's, which speaks
through the screen reader and writes no sound to the file. To list the installed voices, run
`powershell -NoProfile -Command "Add-Type -AssemblyName System.Speech;
[System.Speech.Synthesis.SpeechSynthesizer]::new().GetInstalledVoices().VoiceInfo.Name"`.

Choose one command, and test it with **Play a time check now**. `intro_clip`, `outro_clip`,
`clips_folder`, `voice_packs` and `tts_command` are local file settings only; the browser
cannot change or read them. The enabled switch, hour format, rule, ticked packs and pack order
live in versioned `time_checks.json` in the index folder after you change them in Settings; a
key the file lacks takes the `config.toml` value, and a wrong version stops startup with an
error. Backups include `time_checks.json` and the recorded clips; copy `config.toml` yourself
if you use custom paths or a TTS command.
