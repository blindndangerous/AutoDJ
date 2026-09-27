# AutoDJ operations

Commands labeled Bash require a Linux host or WSL2. Native Windows operators should use the
PowerShell equivalents below. Run Linux container ownership and smoke commands inside WSL2 with
the repository on a WSL filesystem so UID 10001 and POSIX modes have their documented meaning.

## Configuration precedence

AutoDJ resolves defaults, `config.toml`, sibling `config.local.toml`, environment variables, then
explicit CLI flags. Omitting `--config` is valid. Explicitly naming a missing file is an error. Put
access tokens only in ignored local configuration or `AUTODJ_ACCESS_TOKEN`.

## Diagnose before serving

Run `uv run autodj doctor`. Use `uv run autodj doctor --json` for automation. A required failed
check returns exit 1. Doctor does not write the index and redacts both server and Hugging Face
tokens.

## Local network access

Start the native server for other devices on your network with one switch:

```bash
uv run autodj serve --lan
```

`--lan` (or `[server] lan = true`, or `AUTODJ_LAN=1`) binds `0.0.0.0` unless `--host` or
`[server] host` names a specific non-loopback address. It allows the Host names and origins of
this machine that it can detect: the hostname, `<hostname>.local`, the fully qualified name, every
non-loopback address, and `localhost`, `127.0.0.1` and `::1`. With `--ssl-certfile` and
`--ssl-keyfile` it allows the matching `https://` origins too. It uses a configured
`access_token` or `AUTODJ_ACCESS_TOKEN` when there is one; otherwise it loads or creates
`<index_dir>/.access-token`. On Linux and macOS only you can read that file. On Windows it gets the index folder's permissions, and if the index folder is on a network share, anyone who can read the share can read the token. Deleting `.access-token` makes the next start create a new
token, which ends every paired session; each browser must pair again. Startup prints the addresses
to open and the current pairing code with how long it stays valid; it never prints the token.
`uv run autodj devices pairing-code` also works with the saved token and says so on stderr.

`uv run autodj doctor` shows the detected hosts and whether the token will be created. Doctor sees
LAN mode only from `[server] lan` or `AUTODJ_LAN`, not from a `--lan` given only to
`autodj serve`. Name lookups are given two seconds; on a machine with broken DNS, detection keeps
the hostname, the `.local` name and the route address.

```bash
uv run autodj serve --lan --ssl-certfile radio.pem --ssl-keyfile radio-key.pem
```

`--lan --insecure-lan` keeps the detected allowlists but turns pairing off; use it only on a
trusted network. `--insecure-lan` without `--lan` still needs explicit allowlists.

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
end-to-end TLS: run `autodj serve` directly with both `--ssl-certfile` and `--ssl-keyfile` and a
certificate trusted by every browser. AutoDJ does not support TLS termination in front of its
server: its `Secure` cookie flag and origin checks follow its own TLS setting.

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

The certificate and key are local files on the AutoDJ server. This supports private LAN access,
not public Internet hosting. Do not publish the loopback service directly to the internet.

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
- **With `--server-audio` as well**, the machine's own speakers keep the server volume, because
  the page's volume and Mute only change the page's own listening. Set that volume before
  starting stream mode, or run `--server-audio` without `--stream` to change it from the page.
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

## Backup classifications

Re-derivable data includes `vectors.index`, `tracks.db`, `index-manifest.json`, and `dj_meta.db`.
Unique data includes profiles, liners, configured dayparts, optional history, and `web_state.json`.
A full archive contains available data from both classifications and labels every item in
`manifest.json`.

## Stopped-service backup

```bash
# Linux/WSL2 Bash
docker compose --profile lan --profile stream down
uv run autodj backup backups/autodj-$(date +%F).zip
```

Stopped mode refuses `tracks.db-wal`, `tracks.db-shm`, `dj_meta.db-wal`, `dj_meta.db-shm`, and
SQLite rollback journals. Do not copy a live SQLite main file by itself. Backup rechecks sidecars
after copying. These checks can detect activity but cannot prove the process is stopped. Stopping
the service is the operator's responsibility. Backup refuses an existing destination unless
`--force` is explicitly supplied.

## SQLite online backup

```bash
# Linux/WSL2 Bash
uv run autodj backup --online backups/autodj-live-$(date +%F).zip
```

SQLite online backup includes committed DJ metadata WAL state consistently while serving and
archives one manifest-selected index generation. It retries a bounded generation race and refuses
continuous index churn instead of mixing generations.

## Restore and validate

```bash
# Linux/WSL2 Bash
docker compose --profile lan --profile stream down
uv run autodj restore --force backups/autodj-2026-08-02.zip
uv run autodj doctor
docker compose up
```

Restore refuses unknown archive schema versions and existing destinations without `--force`. It
rejects encrypted, non-regular, unsafe, or symlink-derived content; preflights declared sizes and
target-filesystem free space; checks every member size and digest; and stages every payload before
replacing any target. An install failure rolls prior targets back. Cleanup warnings after a
successful install name retained recovery files and do not mean rollback occurred. Do not serve
until doctor exits 0. Keep an untouched archive until playback and profile and liner inventory are
confirmed.

For a native Windows process, press Ctrl+C in the terminal running `uv run autodj serve`, then wait
for the process to exit. If a service manager runs AutoDJ, stop that service and wait for it to
report that the process has stopped. The following commands create a stopped backup:

```powershell
$stamp = Get-Date -Format yyyy-MM-dd
uv run autodj backup "backups\autodj-$stamp.zip"
```

To restore on native Windows, stop AutoDJ the same way, then run:

```powershell
uv run autodj restore --force "backups\autodj-2026-09-12.zip"
uv run autodj doctor
```

Replace the archive path with the backup you intend to restore. Use the same configuration for
backup, restore, and doctor that the previous `serve` process used. After doctor succeeds, restart
with the previous `uv run autodj serve` command and its options, or restart the service manager.

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

4. Run `uv sync --frozen --all-extras`, `npm ci`, and `npm run build` from the fetched release and
   its committed locks. If you use the experimental Windows AMD environment, update it by following
   [Experimental Windows AMD GPU setup](windows-amd.md).
5. Run `uv run autodj doctor`.
6. Run Python, frontend, and container gates from `CONTRIBUTING.md`.
7. Start loopback-only and verify `/api/version` before enabling LAN access.
