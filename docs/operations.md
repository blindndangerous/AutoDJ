# AutoDJ operations

Commands labeled Bash require a Linux host or WSL2. Native Windows operators should use the
PowerShell equivalents below. Run Linux container ownership and smoke commands inside WSL2 with
the repository on a WSL filesystem so UID 10001 and POSIX modes have their documented meaning.

## Configuration precedence

AutoDJ resolves defaults, `config.toml`, sibling `config.local.toml`, environment variables, then
explicit CLI flags. `config.local.toml` is read only when a `config.toml` was loaded next to it;
on its own it is ignored. Omitting `--config` is valid. Explicitly naming a missing file is an error. Put
access tokens only in ignored local configuration or `AUTODJ_ACCESS_TOKEN`. An unknown section or
key in either file, such as a setting a newer AutoDJ removed, is an error that names it.

## Diagnose before serving

Run `uv run autodj doctor`. Use `uv run autodj doctor --json` for automation. A required failed
check returns exit 1. Doctor redacts both server and Hugging Face tokens and writes nothing,
except that its index check takes the index lock, as `serve` does.

The `index` check loads the index with the same code `serve` uses. It passes with the generation
number and track count, warns when there is no index yet or the index is empty, and otherwise
fails with the name and message of the error `serve` would stop with, for example
`UnsupportedIndexError` for an index made by an older AutoDJ. The `dj-meta-db` check runs SQLite's
integrity check on `dj_meta.db` without changing it. The `beets-db` check warns when
`[library] beets_db` names a file that does not exist: indexing then scans the music folder
instead, and `autodj enrich` fails. The `dependencies` check warns when FFmpeg is not on the
PATH: indexing then skips `.m4a`, `.mp4` and `.aac` files, and stream mode cannot start.

## Indexing and DJ analysis

`autodj index` embeds new tracks, then by default enriches them from beets and runs the DJ
analysis that `autodj analyse` also runs: intro and outro, beat grid and cue points for every
track that has none yet. Both commands print one progress line every 25 tracks, and the file
checks one every 5000 files, so the log in the web page's library tools stays readable.

With `[playback] import_external_cues` on (the default), intro and outro markers set in Mixxx,
Rekordbox or Traktor take the place of the detected intro and outro, so the crossfade follows
them. A track analysed before AutoDJ read those markers gets them the first time it plays while
`autodj serve` runs; there is no need to analyse it again.

Each run publishes the tracks embedded so far as a new index generation every 100 tracks and at
the end, so an interrupted run resumes where it stopped. `autodj analyse` saves its results every
25 tracks.

To give network drives a rest during a long run, set a pause before each track, in milliseconds,
in `config.toml`:

```toml
[index]
throttle_ms = 500
```

It applies to the tracks `autodj index` embeds and the tracks `autodj index` and
`autodj analyse` analyse. The default, 0, means no pause.

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

In the web page, Settings, Browser access lists every paired device with when it was paired and
when it was last seen, and marks this browser. Each row has Rename and Revoke. Names are 1 to 64
printable characters. **Show pairing code** shows a code with a Copy button and how long it stays
valid; the code changes by itself every five minutes and cannot be edited. Each code pairs one
device. While the code is shown the page checks the list every five seconds; when a device pairs
it says the device's name and hides the used code. Any paired device can show a code, so any
paired device can pair another one.

From the command line, `autodj devices list` prints each device's id, `autodj devices rename
DEVICE_ID NAME` renames one, `autodj devices revoke DEVICE_ID` revokes one, `autodj devices reset`
revokes them all, and `autodj devices pairing-code` prints the current code.

## Container ownership and exposure

Create bind sources before startup:

```bash
# Linux/WSL2 Bash
mkdir -p music index models
sudo chown 10001:10001 music index models
chmod 0755 music index models
AUTODJ_MUSIC_DIR=./music AUTODJ_INDEX_DIR=./index AUTODJ_MODEL_DIR=./models \
  docker compose up --build
```

The default process listens on container-internal `0.0.0.0` so Docker networking can reach it.
The Compose `--insecure-lan` flag acknowledges only that internal wildcard bind. Compose publishes
the port only on host `127.0.0.1` (`127.0.0.1:8080:8080`), so the default does not expose the
service to the host LAN.

Create fresh-clone LAN settings and start the authenticated service. The `lan` profile runs
`serve --lan`; inside a container detection only finds container addresses, so the host and
origin that setup writes to `.env` stay explicit and merge with them:

```bash
uv run autodj setup-lan --host-name radio.local
docker compose --profile lan up autodj-lan
```

Substitute the DNS name or IP that clients use. Setup writes the generated server secret and
Host/Origin policy to gitignored `.env`. Startup prints an 8-digit code. Enter it once in each
browser; subsequent visits reuse that browser's paired-device cookie.

Manage paired browsers from the running container:

```bash
docker compose --profile lan exec autodj-lan autodj devices list
docker compose --profile lan exec autodj-lan autodj devices pairing-code
docker compose --profile lan exec autodj-lan autodj devices revoke DEVICE_ID
```

HTTP does not protect the pairing code or session cookie from network observers. Use this Compose LAN
profile only on a trusted private network. For browser or LAN access on an untrusted network, use
end-to-end TLS: run `autodj serve` directly with a certificate trusted by every browser
(`[server] ssl_certfile` and `ssl_keyfile`, or `--ssl-certfile` and `--ssl-keyfile`). A proxy
that ends TLS and talks plain HTTP to AutoDJ is not supported: the `Secure` cookie flag and the
origin checks follow AutoDJ's own TLS setting. For remote access, see
[HTTPS with your own domain](#https-with-your-own-domain).

### Advanced overrides

`--lan` covers names this machine knows about. For a DNS name it cannot detect, such as a name
in your router's DNS or a CNAME, add it with `--allowed-host` and `--allowed-origin` (or
`[server] allowed_hosts` and `allowed_origins`); with `--lan` these merge with the detected
lists, and without it they replace them. These flags and `--access-token` are hidden from
`autodj serve --help`. To share the Compose secret with a native server, note that the native
server needs the same server secret, but only Compose reads `.env` on its own;
`uv run autodj` does not. Either copy the secret into gitignored `config.local.toml` as
`[server] access_token`, or load `.env` into the shell that starts the server so
`AUTODJ_ACCESS_TOKEN` is set. In Bash:

```bash
set -a; . ./.env; set +a
```

In Windows PowerShell:

```powershell
Get-Content .env | ForEach-Object {
  $name, $value = $_ -split '=', 2
  Set-Item -Path "Env:$name" -Value $value
}
```

Only Compose reads `AUTODJ_LAN_HOST` and `AUTODJ_LAN_ORIGIN`, and that origin uses `http://`, so
pass the HTTPS host and origin as flags. With `AUTODJ_ACCESS_TOKEN` set in the same shell, start a
private LAN server with TLS for a custom name without `--lan`:

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
per-address limit on wrong pairing codes counts all remote browsers together.

The radio stream link (`/stream/<secret>.mp3`) works at home. Through the tunnel it is behind
Access too, so players that cannot log in, such as Sonos, cannot use it from outside. Do not
share the stream link: anyone who has it and can reach AutoDJ can listen. If it leaks, use "Make
new link" in Settings, Stream.

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
the flag. For Compose, run the `stream` profile instead of `lan`, after the same one-time
`setup-lan` step described under "Container ownership and exposure" above:

```bash
docker compose --profile stream up autodj-stream
```

`autodj-stream` reuses the same `.env` that `setup-lan` wrote (`AUTODJ_ACCESS_TOKEN`,
`AUTODJ_LAN_HOST`, `AUTODJ_LAN_ORIGIN`) and runs `serve --lan --stream`; auto-detection inside
the container only sees container addresses, so the operator's host and origin stay explicit and
merge with them, the same as the `lan` profile. Only one of the `lan` and `stream` profiles can
run at a time; both publish host port 8080.

Stopping one profile does not stop the other: `docker compose --profile lan down` only removes
the `lan` profile's container, so it leaves `autodj-stream` running if that is the one you
started. Pass both profiles to stop whichever is actually running, whether that is one or the
other:

```bash
docker compose --profile lan --profile stream down --volumes --remove-orphans
```

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
  seconds later, once its own playback buffer catches up.
- **Pause** holds the stream for everyone; it plays silence rather than disconnecting, so
  speakers stay connected and resume from the same spot. It also keeps the set alive past the
  idle timeout below.
- **Seek** is not available while streaming. The seek slider is hidden on the page, and the comma
  and full-stop keyboard shortcuts announce "Seeking is not available while streaming."
- **EQ** is applied once, on the server, so every listener hears the same shaped sound.
- **Page volume and mute** affect only the page's own "Listen here" playback in the browser.
  Each speaker keeps its own volume, set on the speaker or in its own app.
- **With `--server-audio` as well**, the page's volume and Mute also set the volume and mute of
  the machine's own speakers, and the page's "Listen here" playback follows them. Stream
  listeners and speakers still hear the stream at full level.
- Quality (bitrate) is chosen under Settings, Stream: 128, 192, 256 or 320 kbps, default 320.
  Changing it restarts the encoder; every listener, including speakers, reconnects on its own a
  moment later.

### Idle sets and "Make new link"

The stream starts idle: nothing plays until a listener connects. The first listener to connect
starts a new set from the beginning of a track. While at least one listener stays connected the
set keeps playing; a listener that disconnects and reconnects within 30 seconds
(`[stream] idle_grace_seconds`) finds the same set still going. After 30 seconds with nobody
connected, the set stops; the next listener to connect starts a fresh set from the start of a
track, never mid-song.

"Make new link" (Settings, Stream) replaces the stream secret. Every current listener,
including any connected speaker, is disconnected at once, and the old address stops working.
Use it if the address was shared somewhere it should not have been.

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
- **A player gets a 503 "stream encoder failed"** — ffmpeg crashed repeatedly (five times within
  a minute) and the encoder is cooling down for 60 seconds before it tries again. Check that
  ffmpeg is present and working (`ffmpeg -version`), then try the address again after a minute.

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

Create authenticated LAN settings without placing a secret on the command line:

```powershell
uv run autodj setup-lan --host-name radio.local
docker compose --profile lan up autodj-lan
```

Docker Desktop bind-mount ownership depends on its WSL2/Linux filesystem mapping. Run
`bash scripts/container_smoke.sh` inside WSL2 for the authoritative UID and mode gate. Do not
replace the 0755 and UID 10001 contract with world-writable Windows mounts.

## More than one machine

AutoDJ stores track paths relative to `music_dir`, so an index built on one machine works on
another that mounts the same library somewhere else. Point `music_dir` at the right place on each
machine in that machine's `config.local.toml`.

### Two independent indexes

The simplest setup gives each machine its own index and never copies anything. For example, a NAS
and a desktop each keep their own `index/` folder, and after you add tracks to the library you run
this on each machine:

```bash
uv run autodj index
```

`autodj index` first drops entries for deleted files (unless more than a fifth of the library is
missing, which usually means a wrong `music_dir`), then embeds tracks it has not seen and files
changed since they were embedded, enriches from beets, and analyses new tracks. Each machine keeps
its own web settings, liners, profiles and paired browsers, and neither can overwrite the other.
Use `autodj index --force` only when you want to rebuild an index from nothing.

### Copying an index to another machine

Building an index is slow without a GPU, so you can build it on a fast machine and copy it to
another. Copy only the derived files, and never the whole `index/` folder, because that folder also
holds files that belong to the machine that made them.

Stop AutoDJ, and any `autodj index` or `autodj analyse` run, on both machines first. Then copy
these files from `index/<name>/` on the source to `index/<name>/` on the destination:

- The generation files named in `index-manifest.json`, `tracks.g<number>.db` and
  `vectors.g<number>.index`. The number is the manifest's `generation` written with 20 digits.
- `dj_meta.db`, the intro, outro, beat grid and cue cache.
- `index-manifest.json` last, after the files it names are in place. AutoDJ loads only what the
  manifest names and checks both files against the SHA-256 in it, so a missing or half-copied file
  is refused with an error instead of being served.

The destination may still hold older generation files. They are ignored, and deleted the next
time AutoDJ publishes an index generation in that folder.

Do not copy these; they belong to the machine that made them:

- `index/<name>/web_state.json`, the settings chosen in the web page.
- `index/<name>/liners/`, the voice liners uploaded on that machine.
- `index/profiles/`, the saved profiles.
- `index/.access-token`, the LAN pairing secret. Copying it would let every browser paired with
  one machine use the other.
- `index/.paired-devices.sqlite3`, the paired browsers and their sessions.
- `index/.stream-secret`, the secret in the radio stream address.
- Any `-wal`, `-shm`, `.lock` or `.tmp` file. The index lock file is made when it is needed.

Run `uv run autodj doctor` on the destination before serving. Copying replaces the destination's
index, so the next `autodj index` there starts from the copied one.

## Backup

`autodj backup` writes one ZIP file holding:

- The live index generation: `index-manifest.json` and the `tracks.g<number>.db` and
  `vectors.g<number>.index` files it names.
- `dj_meta.db`, the intro, outro, beat grid and cue cache.
- `web_state.json`, the settings chosen in the web page.
- Every file in the liners folder (`[playback] liners_folder`, or `index/<name>/liners`) and in
  `index/profiles`.
- `manifest.json`, which records the AutoDJ version that made the archive and lists its files.

Backups do not include `config.toml` or `config.local.toml`. Copy those yourself
and keep them with the archive. Backups also leave out the pairing secret, the paired browsers and
the stream secret, so after a restore on a new machine you pair your browsers again and use the new
stream address.

Backup is safe while AutoDJ is serving: it copies the index under the index's own lock and
`dj_meta.db` through SQLite's backup, so it never picks up a half-written index. It refuses to
overwrite an existing archive unless you pass `--force`, and it refuses an index made by an older
AutoDJ; rebuild that with `autodj index --force` first.

```bash
# Linux/WSL2 Bash
uv run autodj backup backups/autodj-$(date +%F).zip
```

```powershell
$stamp = Get-Date -Format yyyy-MM-dd
uv run autodj backup "backups\autodj-$stamp.zip"
```

## Restore and validate

Stop AutoDJ, and any `autodj index` or `autodj analyse` run, before you restore. Restore cannot
tell whether AutoDJ is running, and a running AutoDJ would keep using the files it replaces.

```bash
# Linux/WSL2 Bash
docker compose --profile lan --profile stream down
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
