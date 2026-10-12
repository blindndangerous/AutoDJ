# AutoDJ

[![CI](https://github.com/blindndangerous/AutoDJ/actions/workflows/ci.yml/badge.svg)](https://github.com/blindndangerous/AutoDJ/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

AutoDJ plays music from your own library and chooses the next track using audio similarity, tempo,
key, and your playback settings. Analysis and playback run on your computer. Your music is not
uploaded to a cloud service.

## What you get

- Automatic track selection with configurable repeat avoidance and discovery.
- Crossfades with optional EQ ducking and transition effects.
- A browser interface for playback, album art, lyrics, search, and queue management.
- Mood presets that adjust tempo targets during a set. To define your own, add
  `[presets.NAME]` tables to `config.toml`; `config.toml.example` shows how.
- Voice liners that play spoken clips over the music on a schedule.
- Several named clockwheels: repeating orders of music folders, liners, tracks and preset
  changes, edited from the keyboard, imported from SAM Broadcaster `.PAL` files and switched by
  the scheduler.
- A cart wall for instant jingles, station IDs and sound effects, with numbered keys and
  duck, over or replace playback modes.
- Spoken time checks from recorded minute clips or an external TTS command, scheduled or after
  the song that crosses the hour.
- A scheduler that switches profiles, presets or the clockwheel, plays liners, starts or stops the
  stream and turns discovery on or off at set times.
- Broadcast recording to dated MP3 files, with hourly or show splits, scheduled controls and retention.
- Lyrics from sidecar files or tags, with scrolling and highlighting when timestamps are available.
- Offline use after installing dependencies and downloading the model. The first indexing run
  downloads model weights from Hugging Face unless you provide a local checkpoint.
- Keyboard controls and automated accessibility checks. See [Accessibility testing](docs/accessibility-testing.md)
  for the limits of those checks and the required screen-reader release sampling.

## Quick start

Install [Git](https://git-scm.com/downloads) and
[uv](https://docs.astral.sh/uv/getting-started/installation/) first. The project requires Python
3.14; the launcher can obtain that interpreter. Running AutoDJ does not need Node.js; the server
serves the web UI files as they are in the checkout. Install [FFmpeg](https://ffmpeg.org/download.html)
and put it on the PATH if your library has `.mp3`, `.m4a`, `.mp4` or `.aac` files: AutoDJ indexes,
analyses and plays those with FFmpeg, and without it `autodj index` skips them with a warning.
Stream mode also needs it.

```bash
git clone https://github.com/blindndangerous/AutoDJ
cd AutoDJ
```

On Windows PowerShell:

```powershell
.\autodj.cmd setup
.\autodj.cmd serve
```

On macOS or Linux:

```bash
./autodj setup
./autodj serve
```

Setup detects supported hardware, installs the appropriate CPU, NVIDIA, or AMD runtime, verifies
it with a real calculation, and remembers it. It asks for your music folder and port and preserves
existing configuration. Unsupported hardware gets an explicit CPU recommendation. Starting a command
interactively before setup also opens this flow. There is no separate AMD launcher to remember.

Open the address printed by `serve` (normally `http://127.0.0.1:8080`). Use the same launcher for
diagnostics and indexing; substitute `./autodj.cmd` in Windows PowerShell:

```bash
./autodj doctor
./autodj index --limit 50 --no-enrich --no-analyse   # optional small trial
./autodj index                                    # full library
```

Future indexing runs skip unchanged tracks. See [Setup](docs/setup.md) for supported runtime
selection, unattended installation, and development; [AMD GPU support](docs/windows-amd.md) for
AMD compatibility; and [Operations](docs/operations.md) for diagnosis, backups, and upgrades.

## Use it from other devices

To open AutoDJ from a phone, tablet or another computer on your home network, start it with
`--lan`:

```bash
./autodj serve --lan
```

Startup prints the addresses to open, such as `http://nas:8080` and `http://192.168.1.20:8080`.
While no browser is paired yet, it also prints an 8-digit pairing code with how many seconds it
stays valid. Open one of the addresses on the other device and enter the code once; that browser
stays paired. Each code pairs one device and works only after someone asks for it: run
`uv run autodj devices pairing-code` for the next device. In the web page, Settings, Profiles
and access lists the paired devices with Rename and Revoke buttons, has **Show pairing code** for
adding another device, and **Sign out this browser** ends this browser's pairing. To make
it permanent, set `[server] lan = true` in `config.toml` or `config.local.toml`, or
`AUTODJ_LAN=1`.

If the other device cannot connect, check the firewall on the AutoDJ machine. Windows can show a
Windows Security alert the first time Python listens on the network; allow access on private
networks. On Linux, open the port in your firewall, for example `sudo ufw allow 8080/tcp`.

`--lan` listens on all interfaces, allows only this machine's own names and addresses in the
browser's address bar, and keeps pairing on. It creates the pairing secret in
`index/.access-token` the first time. On Linux and macOS only you can read that file. On Windows
it gets the index folder's permissions, and if the index folder is on a network share, anyone who
can read the share can read the token.

Over plain HTTP from another device, the gate stutter, bitcrusher, freeze and glitch transition
effects play as a plain crossfade in that browser, because browsers give those effects only to
HTTPS pages and to `localhost`; Settings, Transitions says so under the transition effect. For HTTPS, set
`[server] ssl_certfile` and `ssl_keyfile`, or add `--ssl-certfile` and `--ssl-keyfile`; every
device must trust the certificate. [Operations](docs/operations.md#https-on-your-home-network) shows how to make one for
your home network, and covers containers, custom DNS names and other advanced overrides.

## Play on Sonos or any network player

`autodj serve --lan --stream` also serves the live mix as an MP3 radio station, with AutoDJ's
own crossfades, EQ and voice liners already mixed in, that Sonos, VLC and other network players
can open directly. Stream mode needs `ffmpeg` on the PATH; AutoDJ refuses to start it without
ffmpeg. Find the address, and a downloadable `.m3u` playlist, under Settings, Stream
on the web page. See [Operations](docs/operations.md#radio-stream-sonos-vlc-and-other-players)
for adding the station to Sonos and VLC, what the playback controls do while streaming, and
troubleshooting.

The stream uses `/stream.mp3` (or `/stream.m3u` for a playlist). Anyone who can reach the
server can listen without a key by default. Operators can set an optional listener password,
limit connections per address, and disconnect or ban listeners from Settings, Stream. Browser
controls still require pairing in LAN mode. Older
keyed addresses such as `/stream/<secret>.mp3` no longer work, so set any player saved with one
to `/stream.mp3`.

The public `/api/nowplaying` JSON feed lets pages show the station's current song, upcoming song,
recent plays and listener count, and the public `/status` page shows the same to listeners as plain text. The upcoming song is hidden unless you turn on `[nowplaying] show_next`. Track-change
webhooks can send the same metadata to other services or Discord; configure their URLs in
`config.local.toml`. See [Operations](docs/operations.md#now-playing-feed-and-webhooks).

DMCA mode (`[dmca] enabled = true`) keeps the station inside the US statutory webcasting
licence's performance complement: album, artist and box set limits in any three hours, in every
pick mode, with a warning to confirm before you queue a song that breaks one. Public pages never
show the next song in this mode. See [DMCA mode](docs/operations.md#dmca-mode). This is general
information, not legal advice.

### Relay to Icecast or Shoutcast

Stream mode can also push the station to hosted Icecast 2.4, Shoutcast 1 or Shoutcast 2 servers,
so a host's listener capacity and directory listings carry it too. Describe each server in
`config.toml` and keep its password in `config.local.toml`:

```toml
# config.toml
[stream.relays.main]
type = "icecast"
host = "radio.example.com"
port = 8000
mount = "/live"

# config.local.toml
[stream.relays.main]
password = "the source password from your host"
```

Each relay sends the song titles, reconnects on its own, and keeps the station playing even
when nobody listens to AutoDJ directly. Settings, Stream shows each relay's state, and
`autodj doctor` checks that each server answers. See
[Relaying to Icecast and Shoutcast](docs/operations.md#relaying-to-icecast-and-shoutcast) for
every option.

## Commands

Every command takes `--help`, for example `uv run autodj serve --help`. The global options
`--config FILE` and `-v` go before the command name.

- `autodj index` builds or updates the index from your music folder, then runs the enrich and
  analyse passes unless you skip them.
- `autodj analyse` fills in intro, outro, beat grid, and cue points for indexed tracks.
- `autodj analyse-library` runs advanced analysis for tracks added through Library folders.
  The web interface starts this job in the background when requested.
- `autodj migrate-index` previews a schema-3 to schema-4 conversion; add `--apply` to save a
  recovery copy and convert without repeating analysis. Interactive `serve` offers this too.
- `autodj enrich` refreshes key and mode from your beets database.
- `autodj prune` removes index entries whose audio files no longer exist.
- `autodj stats` prints an overview of the indexed library.
- `autodj list-indexes` lists the named indexes under `[index] index_dir`.
- `autodj serve` starts the browser interface; `autodj serve --lan` opens it to your local
  network; `autodj serve --stream` also serves the live mix as an MP3 radio station.
- `autodj list-devices` lists the audio output devices available to `serve --server-audio`.
- `autodj doctor` checks configuration, saved browser settings, paths, dependencies, the model,
  and security settings. Use `--name` to check a specific index. If `serve` fails in a terminal,
  it offers to run Doctor; repairs still require confirmation.
- `autodj backup` and `autodj restore` archive and restore index and user data.
- `autodj devices list`, `rename`, `revoke`, `reset`, and `pairing-code` manage paired browsers.

### Starting without flags

With no configuration or environment overrides, `autodj serve`:

- Serves the web interface at `http://127.0.0.1:8080`, accessible on this computer only.
- Uses browser playback. The server chooses tracks, but the browser plays and mixes them.
- Leaves the radio stream and server speaker output off.
- Uses the named index `default` and does not open a browser automatically; add `--open` for that.
- Offers a backed-up schema-3 conversion in an interactive terminal if the index needs it.
  It only converts after you accept; non-interactive startup prints the manual command and exits.

For station playout with the server mixing the songs, liners and output processing, use
`autodj serve --stream`. Add `--server-audio` to hear that mix on the server's speakers too,
and `--lan` to allow other devices to connect. `--stream` is an existing option; the new
`migrate-index` command only upgrades stored index data and does not select a playback mode.

These are the built-in defaults, not a reset of your installation. `config.toml`,
`config.local.toml` and environment variables can change the address, port, index and stream
setting. Saved web settings also restore playback preferences. See [Configuration](#configuration)
for precedence and `autodj serve --help` for the available flags.

## Containers

In Docker, AutoDJ runs in LAN mode: it listens on all of the machine's network interfaces, and
each browser pairs once before it can control playback. A bare-metal `autodj serve` stays
local-only unless you pass `--lan`.

Put the folder paths in a `.env` file beside `compose.yaml`:

```dotenv
AUTODJ_MUSIC_DIR=/path/to/music
AUTODJ_INDEX_DIR=./index
AUTODJ_MODEL_DIR=./models
# The owner of those folders (`id -u` and `id -g`), so no chown is needed.
AUTODJ_UID=1000
AUTODJ_GID=1000
```

Then build, index, and start:

```bash
git clone https://github.com/blindndangerous/AutoDJ
cd AutoDJ
mkdir -p index models
docker compose run --rm --build autodj index
docker compose up -d --build
```

Startup prints the addresses to open and an 8-digit pairing code; `docker compose logs autodj`
shows them again. The container uses the host network, so those are the machine's own names and
addresses, and it restarts after a crash or a reboot.

Settings such as HTTPS certificates, extra host names, and `[stream] enabled` go in `config.toml`
and `config.local.toml` beside `compose.yaml`, as on bare metal. The container reads them from that
folder, so give relative paths such as `ssl_certfile = "certs/fullchain.pem"`. To use a beets
library, set `AUTODJ_BEETS_PATH` to the folder that holds `library.db`, and set
`beets_db = "/beets/library.db"` in `config.local.toml`. See [Operations](docs/operations.md) for
more.

Put your audio files in `music/` before running the indexing command. This setup needs no host
Python installation. The image ships the CPU-only PyTorch build, so the container indexes on CPU,
and it does not index automatically when the server starts. For a large library, you can instead build the index on a GPU-equipped host
and copy it to the mounted index directory before starting Compose.

## How to use the web UI

After `autodj serve`, point a browser at `http://localhost:8080`.  Six tabs:

- **Now Playing.**  What is playing, the next track, album art, lyrics, the cue strip on the progress bar.
- **Queue & Search.**  Find any track in your library and choose Play now, Play next (straight after the current track) or Add to queue (at the end).  Move queued tracks up, down or to the top, remove them, or clear the whole queue; each move says the new position, such as "Moved Alpha to position 2 of 5".  After Search come **Browse library**, which lists the whole library or one music or voice liner folder 100 tracks a page, and **Browse voice liners**, the liner files with Delete and Track info.
- **Track info.**  View a recording's details and edit its cue points. Open it from Now Playing, History, or the Queue, Search results, Browse library and Browse voice liners lists in Queue & Search. Use the Return to History, Return to Queue & Search or other view-specific button to return to the source item.
- **History.**  What has played, newest first, one page at a time.  Times get a date when the page holds tracks from before today.  While page 1 is shown, each new song is added to the top on its own.  Refresh history reloads the page you are on.  Export play log saves every play between two dates, for royalty reports: a CSV with one row per play (times, seconds played, artist, title, album, ISRC, label, kind, file and performances, meaning listeners who heard it) or a SoundExchange Report of Use.  `autodj export-plays` does the same from the command line; see [Play log export](docs/operations.md#play-log-export).
- **Settings.**  Split into subtabs, so each screen holds one group.  Move between them with the arrow keys, Home and End, as with the section tabs; the page remembers the last one you used.
  - **Transitions**: the transition mode, crossfade and fade-in length, EQ ducking and its cutoff, the transition effect and its level, and how Skip ends a track.
  - **DJ mixing**: beatmatching with its stretch limit and glide back, drop mixing, key shift, phrase and outro alignment, keeping vocals apart, and the filter sweep with its floor.
  - **Song picking**: preset, pick mode, how widely similarity picks spread, harmonic mixing, repeat windows, BPM range and discovery.
  - **Sound**: output processing, upload and select Stereo Tool presets, Stereo Tool input trim and AGC drive boost, a peak-safe quiet intro lift for ReplayGain playback, the ReplayGain target and peak limit, the browser's audio output device, early loading of the next song and the crossfade on silence.
  - **Display and keys**: lyrics, key notation, announcements and keyboard shortcuts.
  - **Voice liners**.
  - **Schedule**: timed events, the next few runs, and recent results.
  - **Stream**: only in stream mode.  It holds the station name, the address, quality and continuous play.
  - **Profiles and access**: **Profiles** saves the current settings under a name to apply later.  **Browser access** (with `--lan`) signs this browser out, renames or revokes paired devices, and shows the pairing code for adding another.

  A setting that has no effect right now is hidden.  For example, the effect level is hidden while no transition effect is chosen, and the DJ-mix options are hidden with saved segue points.  Changes are saved on the server and survive a restart.  Paths, server and security options, and the model stay in `config.toml`.
- **Library tools.**  Add and remove music and voice liner folders and rescan them, keep clockwheels, and run index, enrich, analyse, prune and stats jobs without leaving the page.  Browsing the library and the voice liners is in Queue & Search.  One job runs at a time; the index stats and the full job log refresh when it finishes.

### Keyboard shortcuts

- **Space** or **k** — play / pause.
- **n** — skip to the next track.
- **S** — shuffle (jump to a random track).
- **M** — mute / unmute.
- **Up** / **Down** — volume up / down (5%).
- **Comma** / **Period** — seek back / forward one bar (one measure at the current BPM).
- **Shift+T** — speak the current artist and title.
- **Shift+N** — speak the next track and its BPM.
- **Shift+R** — speak the time remaining.
- **Shift+B** — speak the current BPM.
- **Shift+K** — speak the musical key.
- **Shift+L** — speak the current lyric line once. During an instrumental break it says
  "Instrumental" and the next line; a track without lyrics says so.
- **Shift+E** — speak the elapsed and total time, as the seek slider says it.
- **Shift+V** — speak the volume, and whether it is muted.
- **Shift+Q** — speak how many tracks are queued, and the first one.
- **Shift+J** — speak the library job status: what is running, for how long, and how far it has
  got as a percentage when the job shows one, or how the last job ended.
- **?** — open the shortcut list.

Every shortcut works from any tab, except Space and the Up and Down arrows, which work on Now
Playing only because on the other tabs they scroll the page. Letter and punctuation shortcuts also
work when a button or slider has focus. Text fields and dropdowns keep their keys; Space activates
a focused button and arrow keys operate the focused slider or tab. Open dialogs keep playback
shortcuts inactive. With NVDA in browse mode, press NVDA+F2 and then a shortcut to pass that one
key through to the page.

A new track is announced while the Now Playing tab is showing. To hear it on every tab, turn on
**Announce track changes on every tab** under Settings, Display and keys; the choice is saved in this
browser only.

To turn the shortcuts off, clear **Enable keyboard shortcuts** under Settings, Display and keys. The
setting is on by default and is saved in this browser only, not on the server. With it off, no
single-key shortcut fires; buttons, sliders, tabs and dialogs keep their normal keys, and the
Keyboard shortcuts button still opens the list. If the browser blocks site storage, shortcuts stay
on and the choice lasts only until the page is reloaded.

### Browser and server audio

The default `serve` mode is browser-driven: the server picks tracks; the browser plays them.  Switching audio output devices in the browser only affects the browser.

To play on the machine's own speakers instead (for example to send sound to a Bluetooth speaker through ALSA on Linux), run `autodj serve --server-audio` and control it from the web page. It needs the `play` extra. Pick the output device with `[playback] audio_device` in `config.toml`; `autodj list-devices` shows the choices.

## Voice liners

Drop short spoken clips into a folder.  AutoDJ will fade the music down for a couple of seconds and play one of them now and then.

1. Open the **Settings** tab, then its **Voice liners** subtab.
2. Tick **Enable voice liners**.
3. The Trigger / Mix / Library boxes appear.
4. Pick an MP3 / WAV / OGG / M4A / FLAC / AAC file with **Choose liner file**, then press
   **Upload**.  To
   swap in a new recording under a name that is already there, tick **Replace existing file**
   first; without it the upload is refused so nothing is overwritten by accident.
5. Set how often you want them to play.

The liner files are listed in the **Queue & Search** tab, under **Browse voice liners**, each with
**Delete** and **Track info**.  Add or remove liner folders in **Library tools**.

You can pick three trigger styles, in any combination:

- **Every N tracks** -- after every 5 (or whatever) songs.
- **Every N minutes** -- on a wall-clock timer.
- **Random window** -- pick a random delay between two values.

Leave a trigger blank or set it to 0 to turn it off.  The random window needs both values above 0,
with the minimum no larger than the maximum.

Rotation modes: random and sequential.  The old weighted mode is gone: a config file or saved web
state that still says `weighted` is rejected, so pick one of the two.

## How well does this work?

It works well when your library has the genre clustering you expect.  Pop tracks pick more pop, jazz picks more jazz, an acoustic intro picks acoustic, a heavy drop picks something else heavy.  For what the picker actually computes, from the 1040-number track vector to the final pick, see [How AutoDJ picks the next track](docs/track-selection.md).

It does not work well when:

- The library is tiny (under ~50 tracks) -- there is not enough variety for the picker to behave like a DJ.  When the no-repeat window is bigger than the library, AutoDJ shrinks it to fit and says so in the server log; the web page does not show this.
- All your files are tagged "Unknown Artist" -- the picker still works on sound alone, but the web UI looks bare.
- Your tracks are very compressed (96 kbps MP3) -- the audio analysis still works but is less accurate.

## Configuration

AutoDJ starts with validated defaults. If `config.toml` exists in the working directory, AutoDJ
loads it, then loads sibling `config.local.toml`. Environment variables override files, and
command-line options such as `--host` or `--stream` override both. `autodj serve` also restores
the settings last saved from the web page (`web_state.json`), which override the files and
environment variables. The playback and DJ-mix settings have no `serve` options; change them in
the web page or in `config.toml`. Omitting `--config` is valid. Passing
`--config /path/to/config.toml` makes that file explicit, so a missing path is an error. Shipped
`config.toml.example` lists supported environment variables and settings. Every key and section
in `config.toml` and `config.local.toml` must be one AutoDJ knows: a removed or misspelled setting
stops AutoDJ with an error naming the section and key, so delete it rather than leave it in place.

### Secure server operation

For anonymous access on the same computer, keep the default loopback binding:

```bash
uv run autodj serve
```

For a LAN server, run `uv run autodj serve --lan` or start the Docker container (see
[Containers](#containers)). AutoDJ generates its server secret, saves it in the index folder, and
prints an 8-digit pairing code during startup while no browser is paired. Enter that code in the first browser; each code pairs one device, and
`autodj devices pairing-code` gives one for the next. Paired browsers receive distinct, persistent device sessions and do not
need to sign in again unless revoked or expired.

For a native LAN server, use `--lan` or `[server] lan = true` as described under
[Use it from other devices](#use-it-from-other-devices); it creates and keeps the server secret
for you. Set the secret yourself only when you configure the allowlists by hand without LAN mode,
for example in gitignored `config.local.toml`:

```toml
[server]
host = "0.0.0.0"
access_token = "generate-at-least-32-random-bytes"  # Internal pairing/session secret.
allowed_hosts = ["radio.local"]
allowed_origins = ["https://radio.local:8080"]
ssl_certfile = "/srv/autodj/tls/radio.pem"
ssl_keyfile = "/srv/autodj/tls/radio-key.pem"
```

The server secret must contain at least 32 UTF-8 bytes. Do not enter it in a browser or copy the
placeholder above. To generate one, run:

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The certificate and matching private key make the server use HTTPS; `--ssl-certfile` and
`--ssl-keyfile` on `autodj serve` do the same.

`config.toml` and its local variants are gitignored. Never pass the server secret as a CLI
argument because shell history and process listings can expose it. AutoDJ derives short-lived
pairing codes from that secret and exchanges a valid code for a device-bound HttpOnly cookie.
Use `autodj devices list`, `rename`, `revoke`, `reset`, and `pairing-code` to manage browsers. TLS protects
pairing codes and session cookies on the wire.

For non-loopback bindings, including LAN access, authentication can be disabled only with an explicit trusted-LAN acknowledgement.  This still enforces the Host and Origin allowlists that `--lan` detects:

```bash
uv run autodj serve --lan --insecure-lan
```

`--insecure-lan` disables authentication; use it only on a trusted private network.  Multi-user accounts, roles, cloud identity, and public Internet hosting are not supported.

The server does not expose FastAPI's generated API documentation pages (`/docs`, `/redoc`, and
`/openapi.json`).

Index generation manifests are the only publication signal used by live reload. A partially
written generation is not activated. Incomplete model directories are ignored instead of being
treated as usable caches.

Common things to set:

```toml
[library]
music_dir = "/mnt/nas/music"
beets_db  = "/home/me/.config/beets/library.db"   # optional

[playback]
crossfade_seconds       = 5
crossfade_eq_duck       = true
discovery_every         = 8        # pick a sonically distant track once per 8 songs
show_lyrics             = true
beat_sync_fx            = true
key_sync_fx             = true
transition_mode         = "full_intro_outro"
```

Set repeat limits in **Settings > Picking > Repeats**. Artist, album, track and title
have separate Tracks and Minutes boxes. Zero turns off a limit; when both are set,
both must be satisfied before a repeat is allowed. Save applies the rules to future
picks and remembers them across restarts. Old repeat keys in `config.toml` or
`web_state.json` produce warnings and are ignored. Re-enter any limits that still use
the old keys in **Settings > Picking > Repeats**. Current repeat rules are preserved;
Doctor can optionally remove obsolete keys with a backup.

**Use related artists** applies the artist limit to the links in your editable TOML file.
The page shows its fixed location, `index/<name>/related-artists.toml`. AutoDJ copies
a sample there on first use and leaves your edits alone on upgrades. After editing the
file, save the repeat settings to reload it. See [repeat rules](docs/track-selection.md#repeat-avoidance-and-discovery).

### Multi-machine / NAS setups

AutoDJ stores track paths relative to `music_dir`, so an index works on Windows, Linux and macOS
as long as `music_dir` points at the library on each machine. Only tracks under `music_dir` are
indexed.

The simplest setup gives each machine its own index. After you add tracks to the library, run
`uv run autodj index` on each machine; it only embeds the new and changed tracks.

You can also build an index on a fast machine (one with a GPU is best) and copy it to another, but
do not copy the whole `index/` folder. It also holds that machine's web settings, liners,
profiles, pairing secret and paired browsers, and copying them would overwrite the other machine's
own. [Operations](docs/operations.md#more-than-one-machine) lists which files are safe to copy.

Per-machine file overrides go in `config.local.toml` next to loaded base configuration. Environment
variables and CLI flags still take precedence.

## Troubleshooting

**The first index run is taking forever.**  This is the slow pass.  AutoDJ has to listen to every file and remember what it sounds like.  On a CPU it can take many hours for a 10000-track library.  A compatible GPU can speed up the embedding step.  In one small, repeated benchmark on a Ryzen AI 7 PRO 350 with Radeon 860M graphics, the GPU was about 1.9x faster than CPU after warmup; that smoke-test result does not predict full-library time.  See [Windows AMD GPU setup](docs/windows-amd.md) for the tested configuration.  Run with `--limit 50` first to confirm it works, then leave the full run going overnight.

**The first index run on Windows warns about symlinks.**  The Hugging Face download library
prints a warning that your machine does not support symlinks in the model folder.  The download
still works; the cache only takes more disk space.  Turning on Windows Developer Mode removes the
warning.

**No sound from the web UI.**  Click the **Play** button once -- browsers require a user gesture before they will play audio.  After the first click, AutoDJ unlocks its audio context and plays normally for the rest of the session.

**Voice liner upload button is missing.**  The whole "Library" panel hides until you tick the **Enable voice liners** checkbox.  Tick it first, then the upload form appears.

**Cue point list is empty.**  AutoDJ analyses each track in the background after it starts playing.  Wait a few seconds; the cue strip on the progress bar should fill in.  The server log shows a "Background analysis done: ... -> 5 cues" line as each one finishes.

**Lyrics card never appears.**  AutoDJ checks three places, in order: an LRC file next to the audio file (timestamped, scrolls), the `lyrics` field in the beets database, the embedded ID3 / Vorbis / MP4 lyric tag.  If none of those is present, the lyrics card stays hidden.

**Space or the arrow keys do nothing on the Settings tab.** Space and the Up and Down arrows only
act on Now Playing, where they do not scroll the page; use k to play or pause from other tabs.
The other shortcuts work on every tab, except while typing or using a dropdown. With NVDA, use
focus mode (NVDA+Space) so the app receives the keys, or press NVDA+F2 before a single shortcut.

## Project layout

```
src/autodj/
    cli.py              # the autodj command
    server.py           # FastAPI web server + WebSocket
    player.py           # crossfade + audio output
    indexer.py          # builds the FAISS index
    similarity.py       # picks the next track
    static/             # web UI source files
        app.js              # bootstrap
        modules/            # ES modules (lyrics, queue, hotkeys, ...)
        index.html
        app.css
```

The test suites are listed under "Where things live" in
[Contributing](CONTRIBUTING.md).

## Development

If you plan to change the code, [Contributing](CONTRIBUTING.md) has the clone, install,
pre-commit and gate commands, plus the PR checklist. It is the one place those commands are
kept current.

Pre-commit runs these hooks: `trailing-whitespace`, `end-of-file-fixer`, `mixed-line-ending`,
`check-added-large-files`, `check-merge-conflict`, `check-yaml`, `check-toml`, `check-json`,
`detect-private-key`, `check-case-conflict`, `check-symlinks`, `gitleaks`, `actionlint`, `ruff`,
`ruff-format`, `bandit`, `mypy`, `vulture`, `deptry`, `pip-audit`,
`pip-licenses`, `osv-scanner`, `trivy-fs`, `pytest`, `eslint`, and `commitlint`. Install them with
`uv run pre-commit install`. The quick ones run on every commit. `bandit`, `vulture`, `deptry`,
the dependency audits, `trivy-fs` and `pytest` run once per `git push`,
and CI runs everything on every push.

Gates outside pre-commit include lock checks, the coverage-exclusion policy, Vitest, the
frontend dead-code scan, npm audit, Playwright audits, container smoke, and release verification.
Run their commands directly or through CI as described in [Contributing](CONTRIBUTING.md).

## Release artifacts

Every tag publishes what `uv build` produces: a wheel, an sdist, a CycloneDX SBOM, and a cosign
signature bundle beside each file. They exist so a build can be verified and archived, and so
AutoDJ can be installed without a checkout. The wheel carries the web UI files, so it needs no
Node toolchain.

AutoDJ is not on PyPI. To install a tagged wheel, pick a version from the
[releases page](https://github.com/blindndangerous/AutoDJ/releases) and replace both `X.Y.Z`
placeholders with it:

```bash
uv pip install "autodj[all] @ https://github.com/blindndangerous/AutoDJ/releases/download/vX.Y.Z/autodj-X.Y.Z-py3-none-any.whl"
```

Keep `[all]` for the full application. The base dependency set also includes MuQ, librosa,
mutagen, and the web server, so omitting extras does not produce a lightweight installation
without model or analysis dependencies. The extras add server-side audio output (`play`, for
`serve --server-audio`) and explicit torch floors (`index`).

Installing a wheel resolves dependencies fresh from PyPI instead of from `uv.lock`, so you give up
the exact versions CI tested. The clone plus `uv sync --frozen --all-extras` above stays the
supported path; reach for the wheel only when you want AutoDJ without a source tree.

## Uninstall

AutoDJ keeps everything in directories you chose; it installs no system service. To remove a
source checkout, stop AutoDJ, run `autodj backup` first if you want to keep profiles or liners,
then delete what you no longer want:

- `.venv/` holds the Python dependencies, and `node_modules/` the Node development tools if you
  installed them.
- `index/` (or your `[index] index_dir`) holds the index, DJ metadata, profiles, liners, and saved
  web state.
- `models/` (or your `[index] model_dir`) holds the downloaded model weights. Hugging Face can
  also keep files in its own cache, `~/.cache/huggingface` or the directory named by `HF_HOME`.
- `config.toml`, `config.local.toml`, and `.env` hold your settings and the LAN
  server secret.
- `music/` holds whatever audio you copied there. Keep it if it is your only copy.

Then delete the checkout itself. For Compose, remove the containers and the named volumes
`autodj-index` and `autodj-models` (Compose prefixes them with the project name, usually the
directory name), then the image:

```bash
docker compose down --volumes
docker image rm autodj:local
```

Bind-mounted directories you passed through `AUTODJ_MUSIC_DIR`, `AUTODJ_INDEX_DIR`, or
`AUTODJ_MODEL_DIR` are not removed by Compose; delete them by hand. A wheel install is removed
with `uv pip uninstall autodj`.

## Credits and licensing

- AutoDJ's own code is MIT licensed.
- [MuQ-large-msd-iter](https://huggingface.co/OpenMuQ/MuQ-large-msd-iter) uses MIT-licensed code and
  [CC-BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) model weights. The weights require
  attribution; the license forbids commercial use. AutoDJ never redistributes those weights.
  It downloads them during indexing; they are not committed here or included in the container image.
  Commercial use needs permission from the publisher or a separately licensed compatible model.
  Changing `[model] name` does not add
  support for another architecture. See [Model selection](docs/model-selection.md).
- Dependencies have their own licenses. The base installation, and therefore the container
  image, includes `mutagen` under GPL-2.0-or-later and `librosa` with its LGPL-licensed `soxr`
  dependency. Omitting extras does not make every dependency permissively licensed. CI logs a dependency license inventory, and
  container images include their installed dependencies.
- Audio analysis uses [librosa](https://librosa.org/) (ISC).
- Vector search uses [FAISS](https://github.com/facebookresearch/faiss) (MIT).
- The web UI uses [FastAPI](https://fastapi.tiangolo.com/) and a hand-written ES module front end (no React, no Vue, no framework).
- Cue-point importers read [Mixxx](https://mixxx.org/), [Rekordbox](https://rekordbox.com/), and [Traktor](https://www.native-instruments.com/en/products/traktor/) library files, and the [Serato](https://serato.com/) cue tags inside audio files (format from the [serato-tags](https://github.com/Holzhaus/serato-tags) notes).

If AutoDJ is useful to you, a star on GitHub is appreciated.  Issues and pull requests welcome.

## Contributors

AutoDJ was built collaboratively by humans and AI assistants.  Each contributor is named with the part of the work they led.

### Human contributors

- **[blindndangerous](https://github.com/blindndangerous)**: project vision, library design, requirements, UX direction (web UI flow, mode semantics, gapless feel), every accessibility decision, all real-world testing on a 10k-track library, every release call.
- **[jage9](https://github.com/jage9)**:
  - Station playout: music and liner folder libraries, track info with editable cues, segue-point
    liner timing, least-recently-played and native clockwheel rotation with PAL import, persistent play
    history, Smooth and Stereo Tool processing, and the 0.20.0 release preparation.
  - Guided `autodj doctor` repairs, the automatic GPU setup scripts and the launcher.
  - Windows AMD GPU setup guide and launcher, and MuQ compatibility with patched dependencies.
  - Web UI keyboard work: seek by one measure with `,` and `.`, status-speak hotkeys
    (Shift+T, N, R, B and K), `?` for the shortcut dialog, song-first page titles, and shortcuts
    that keep working while a button or slider has focus.
  - The session history tab, the fade-in seconds setting, and a fix for a track playing twice.
  - A WCAG 2.2 AA audit of the web UI, and settings that hide when the setting they depend on is off.
  - Dependency and container security updates, CI fixes, testing and feedback.

### AI assistants

- **Claude (Anthropic)**: paired-programming partner across the whole codebase.  Worked on the MuQ + librosa indexing pipeline, the FAISS similarity engine, crossfade audio math with EQ-ducking, the transition effects (CLI + AudioWorklet), the FastAPI + WebSocket web layer, the section-nav SPA, the gapless prefetch + silence detector, the harmonic Camelot rule set, and the test suite.
- **Codex (OpenAI)**: pull request review, automated verification, model research, and documentation corrections.

If you contribute, add your name and the work you contributed.
