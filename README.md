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
- Mood presets that adjust tempo targets during a set. To define your own, copy
  `presets.toml.example` to `presets.toml` next to your `config.toml` (or in the working
  directory when you have no config file).
- Voice liners that play spoken clips over the music on a schedule.
- Lyrics from sidecar files or tags, with scrolling and highlighting when timestamps are available.
- Offline use after installing dependencies and downloading the model. The first indexing run
  downloads model weights from Hugging Face unless you provide a local checkpoint.
- Keyboard controls and automated accessibility checks. See [Accessibility testing](docs/accessibility-testing.md)
  for the limits of those checks and the required screen-reader release sampling.

## Quick start

Install Git, uv, and Node.js with npm first. The project requires Python 3.14; `uv sync` can
install that interpreter. CI uses Node.js 24.6.0.

```bash
git clone https://github.com/blindndangerous/AutoDJ
cd AutoDJ

# Install exactly the dependencies recorded in uv.lock.
uv sync --frozen --all-extras
npm ci
mkdir -p music index models
```

This is the supported install. `--frozen` gives you the exact dependency versions CI tested.

On Windows, the standard locked environment uses CPU-only PyTorch. For an experimental AMD ROCm
environment, see [Windows AMD GPU setup](docs/windows-amd.md); use its launcher for GPU indexing
and serving.

AutoDJ works without a configuration file. Its defaults use `music/`, `index/`, and `models/`
under the current directory and listen on `127.0.0.1:8080`. To change those defaults, copy the
example to a private configuration file. On Windows PowerShell:

```powershell
Copy-Item config.toml.example config.toml
```

On macOS or Linux:

```bash
cp config.toml.example config.toml
```

If you created `config.toml`, set `[library] music_dir` for your music folder. Set
`[library] beets_db` to your beets database, or clear it if you do not use beets. Run doctor before
indexing and serving:

```bash
# Check configuration, paths, dependencies, and security settings without writing the index.
uv run autodj doctor

# Point AutoDJ at your music folder once and let it learn the library.
# For a quick embed-only smoke test, skip the post-passes.
uv run autodj index --limit 50 --no-enrich --no-analyse
uv run autodj index               # full library, can take hours

# Start the web UI.
uv run autodj serve

# Open http://localhost:8080 in your browser.
```

Future runs of `autodj index` embed new files and refresh the post-processing cache. See
[Operations](docs/operations.md) for diagnosis, `autodj backup`, `autodj restore`, container
ownership, and upgrades.

## Use it from other devices

To open AutoDJ from a phone, tablet or another computer on your home network, start it with
`--lan`:

```bash
uv run autodj serve --lan
```

Startup prints the addresses to open, such as `http://nas:8080` and `http://192.168.1.20:8080`,
and an 8-digit pairing code. Open one of the addresses on the other device and enter the code once;
that browser stays paired. Run `uv run autodj devices pairing-code` for a fresh code later. In the
web page, Settings, Browser access lists the paired devices with a Revoke button each, and **Sign
out this browser** ends this browser's pairing. To make
it permanent, set `[server] lan = true` in `config.toml` or `AUTODJ_LAN=1`.

`--lan` listens on all interfaces, allows only this machine's own names and addresses in the
browser's address bar, and keeps pairing on. It creates the pairing secret in
`index/.access-token` the first time. On Linux and macOS only you can read that file. On Windows
it gets the index folder's permissions, and if the index folder is on a network share, anyone who
can read the share can read the token. For HTTPS, add `--ssl-certfile` and `--ssl-keyfile`. See [Operations](docs/operations.md) for containers, custom DNS names and other
advanced overrides.

## Play on Sonos or any network player

`autodj serve --lan --stream` also serves the live mix as an MP3 radio station, with AutoDJ's
own crossfades, EQ and voice liners already mixed in, that Sonos, VLC and other network players
can open directly. Find the address, and a downloadable `.m3u` playlist, under Settings, Stream
on the web page. See [Operations](docs/operations.md#radio-stream-sonos-vlc-and-other-players)
for adding the station to Sonos and VLC, what the playback controls do while streaming, and
troubleshooting.

## Commands

Every command takes `--help`, for example `uv run autodj serve --help`. The global options
`--config FILE` and `-v` go before the command name.

- `autodj index` builds or updates the index from your music folder, then runs the enrich and
  analyse passes unless you skip them.
- `autodj analyse` fills in intro, outro, beat grid, and cue points for indexed tracks.
- `autodj enrich` refreshes key and mode from your beets database.
- `autodj prune` removes index entries whose audio files no longer exist.
- `autodj stats` prints an overview of the indexed library.
- `autodj list-indexes` lists the named indexes under `[index] index_dir`.
- `autodj serve` starts the browser interface; `autodj serve --lan` opens it to your local
  network; `autodj serve --stream` also serves the live mix as an MP3 radio station.
- `autodj playlist` writes an offline M3U playlist from the similarity picker.
- `autodj list-devices` lists the audio output devices available to `serve --server-audio`.
- `autodj doctor` checks configuration, paths, dependencies, the model, and security settings.
- `autodj backup` and `autodj restore` archive and restore index and user data.
- `autodj setup-lan` writes a `.env` for the authenticated Compose LAN service.
- `autodj devices list`, `revoke`, `reset`, and `pairing-code` manage paired browsers.

## Containers

If you have Docker Compose installed:

```bash
git clone https://github.com/blindndangerous/AutoDJ
cd AutoDJ
mkdir -p music index models
sudo chown 10001:10001 music index models
chmod 0755 music index models
# Copy your audio files into music/ before indexing.
AUTODJ_MUSIC_DIR=./music AUTODJ_INDEX_DIR=./index AUTODJ_MODEL_DIR=./models \
  docker compose run --rm --build autodj index
AUTODJ_MUSIC_DIR=./music AUTODJ_INDEX_DIR=./index AUTODJ_MODEL_DIR=./models \
  docker compose up --build
```

Open `http://localhost:8080`. Container runs as UID/GID 10001. Default Compose publication is host
loopback only. See [Operations](docs/operations.md) for WSL2, bind mounts, and authenticated LAN
startup.

Put your audio files in `music/` before running the indexing command. This setup needs no host
Python installation. The image ships the CPU-only PyTorch build, so the container indexes on CPU,
and it does not index automatically when the server starts. For a large library, you can instead build the index on a GPU-equipped host
and copy it to the mounted index directory before starting Compose.

## How to use the web UI

After `autodj serve`, point a browser at `http://localhost:8080`.  Five tabs:

- **Now Playing.**  What is playing, the next track, album art, lyrics, the cue strip on the progress bar.
- **Queue & Search.**  Find any track in your library and choose Play now, Play next (straight after the current track) or Add to queue (at the end).  Move queued tracks up, down or to the top, remove them, or clear the whole queue; each move says the new position, such as "Moved Alpha to position 2 of 5".
- **History.**  What has played, newest first, one page at a time.  Times get a date when the page holds tracks from before today.  Refresh history reloads the page you are on.
- **Settings.**  Pick a preset, change the crossfade length, switch transition effects and their level, set a BPM range, set how soon a song or an artist may repeat, toggle voice liners, choose an audio output device.  **Profiles** saves the current settings under a name to apply later.  **Browser access** (with `--lan`) signs this browser out or revokes another paired device.
- **Library tools.**  Run index, enrich, analyse, prune and stats jobs without leaving the page.  One job runs at a time; the index stats and the full job log refresh when it finishes.

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
**Announce track changes on every tab** under Settings, Announcements; the choice is saved in this
browser only.

To turn the shortcuts off, clear **Enable keyboard shortcuts** under Settings, Keyboard. The
setting is on by default and is saved in this browser only, not on the server. With it off, no
single-key shortcut fires; buttons, sliders, tabs and dialogs keep their normal keys, and the
Keyboard shortcuts button still opens the list. If the browser blocks site storage, shortcuts stay
on and the choice lasts only until the page is reloaded.

### Browser and server audio

The default `serve` mode is browser-driven: the server picks tracks; the browser plays them.  Switching audio output devices in the browser only affects the browser.

To play on the machine's own speakers instead (for example to send sound to a Bluetooth speaker through ALSA on Linux), run `autodj serve --server-audio` and control it from the web page. It needs the `play` extra. Pick the output device with `[playback] audio_device` in `config.toml`; `autodj list-devices` shows the choices.

## Voice liners

Drop short spoken clips into a folder.  AutoDJ will fade the music down for a couple of seconds and play one of them now and then.

1. Open the **Settings** tab.
2. Tick **Enable voice liners**.
3. The Trigger / Mix / Library boxes appear.
4. Click the **Choose liner file** button to upload an MP3 / WAV / OGG / M4A / FLAC / AAC.  To
   swap in a new recording under a name that is already there, tick **Replace existing file**
   first; without it the upload is refused so nothing is overwritten by accident.
5. Set how often you want them to play.

You can pick three trigger styles, in any combination:

- **Every N tracks** -- after every 5 (or whatever) songs.
- **Every N minutes** -- on a wall-clock timer.
- **Random window** -- pick a random delay between two values.

Leave a trigger blank or set it to 0 to turn it off.  The random window needs both values above 0,
with the minimum no larger than the maximum.

Rotation modes: random and sequential.  The old weighted mode is gone: a config file or saved web
state that still says `weighted` is rejected, so pick one of the two.

## How well does this work?

It works well when your library has the genre clustering you expect.  Pop tracks pick more pop, jazz picks more jazz, an acoustic intro picks acoustic, a heavy drop picks something else heavy.  For what the picker actually computes, from the 1040-number track vector to the final softmax draw, see [How AutoDJ picks the next track](docs/track-selection.md).

It does not work well when:

- The library is tiny (under ~50 tracks) -- there is not enough variety for the picker to behave like a DJ.  When the no-repeat window is bigger than the library, AutoDJ shrinks it to fit and says so in the server log; the web page does not show this.
- All your files are tagged "Unknown Artist" -- the picker still works on sound alone, but the web UI looks bare.
- Your tracks are very compressed (96 kbps MP3) -- the audio analysis still works but is less accurate.

## Configuration

AutoDJ starts with validated defaults. If `config.toml` exists in the working directory, AutoDJ
loads it, then loads sibling `config.local.toml`. Environment variables override files, and
explicit CLI flags override all other sources. Omitting `--config` is valid. Passing
`--config /path/to/config.toml` makes that file explicit, so a missing path is an error. Shipped
`config.toml.example` lists supported environment variables and settings. Every key and section
in `config.toml` and `config.local.toml` must be one AutoDJ knows: a removed or misspelled setting
stops AutoDJ with an error naming the section and key, so delete it rather than leave it in place.

### Secure server operation

For anonymous access on the same computer, keep the default loopback binding:

```bash
uv run autodj serve
```

For a fresh-clone Compose LAN server, run setup once and start the LAN profile:

```bash
uv run autodj setup-lan --host-name radio.local
docker compose --profile lan up autodj-lan
```

Replace `radio.local` with the hostname or IP browsers use. AutoDJ writes a gitignored `.env`,
generates its server secret, and prints an 8-digit pairing code during startup. Enter that code
once in each browser. Paired browsers receive distinct, persistent device sessions and do not
need to sign in again unless revoked or expired.

For native serving, equivalent settings can be stored in gitignored `config.local.toml`:

```toml
[server]
host = "0.0.0.0"
access_token = "generate-at-least-32-random-bytes"  # Internal pairing/session secret.
allowed_hosts = ["radio.local"]
allowed_origins = ["https://radio.local:8080"]
```

The server secret must contain at least 32 UTF-8 bytes. Do not enter it in a browser or copy the
placeholder above. Start the native server with a certificate and matching private key:

```bash
uv run autodj serve --ssl-certfile radio.pem --ssl-keyfile radio-key.pem
```

`config.toml` and its local variants are gitignored. Never pass the server secret as a CLI
argument because shell history and process listings can expose it. AutoDJ derives short-lived
pairing codes from that secret and exchanges a valid code for a device-bound HttpOnly cookie.
Use `autodj devices list`, `revoke`, `reset`, and `pairing-code` to manage browsers. TLS protects
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
no_repeat_window        = 500
show_lyrics             = true
beat_sync_fx            = true
key_sync_fx             = true
transition_mode         = "full_intro_outro"
```

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

**Browser says "loading module ... was blocked".** You probably ran `npm run build` once and then
deleted `node_modules`. Either delete `src/autodj/static_dist` (server falls back to unbundled
source) or run `npm ci && npm run build`.

**The server will not start: "Built static bundle is stale".** The web bundle in
`src/autodj/static_dist` was built from different web sources than the ones in your checkout,
usually because you pulled new code without rebuilding. Run `npm ci && npm run build`.
`autodj doctor` reports the same problem as a failed `frontend-bundle` check.

**No sound from the web UI.**  Click the **Play** button once -- browsers require a user gesture before they will play audio.  After the first click, AutoDJ unlocks its audio context and plays normally for the rest of the session.

**Voice liner upload button is missing.**  The whole "Library" panel hides until you tick the **Enable voice liners** checkbox.  Tick it first, then the upload form appears.

**Cue point list is empty.**  AutoDJ analyses each track in the background after it starts playing.  Wait a few seconds; the cue strip on the progress bar should fill in.  Pass `autodj -v serve` to see "Background analysis done: ... -> 5 cues" log lines as they finish.

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
    static_dist/        # built output (gitignored; produced by `npm run build`)
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
Vite build, the frontend dead-code scan, npm audit, Playwright audits, container smoke, and release
verification. Run their commands directly or through CI as described in
[Contributing](CONTRIBUTING.md).

## Release artifacts

Every tag publishes what `uv build` produces: a wheel, an sdist, a CycloneDX SBOM, and a cosign
signature bundle beside each file. They exist so a build can be verified and archived, and so
AutoDJ can be installed without a checkout — the wheel already carries the minified web UI, so it
needs no Node toolchain.

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
source checkout, stop AutoDJ, run `autodj backup` first if you want to keep profiles, liners, or
history, then delete what you no longer want:

- `.venv/` and `node_modules/` hold the Python and Node dependencies.
- `index/` (or your `[index] index_dir`) holds the index, DJ metadata, profiles, liners, and saved
  web state.
- `models/` (or your `[index] model_dir`) holds the downloaded model weights. Hugging Face can
  also keep files in its own cache, `~/.cache/huggingface` or the directory named by `HF_HOME`.
- `config.toml`, `config.local.toml`, `presets.toml`, and `.env` hold your settings and the LAN
  server secret.
- `music/` holds whatever audio you copied there. Keep it if it is your only copy.

Then delete the checkout itself. For Compose, remove the containers and the named volumes
`autodj-index` and `autodj-models` (Compose prefixes them with the project name, usually the
directory name), then the image:

```bash
docker compose --profile lan --profile stream down --volumes
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
- **[jage9](https://github.com/jage9)**: Windows AMD setup, MuQ compatibility, keyboard shortcut fixes,
  and additional contributions and feedback.

### AI assistants

- **Claude (Anthropic)**: paired-programming partner across the whole codebase.  Worked on the MuQ + librosa indexing pipeline, the FAISS similarity engine, crossfade audio math with EQ-ducking, the transition effects (CLI + AudioWorklet), the FastAPI + WebSocket web layer, the section-nav SPA, the gapless prefetch + silence detector, the harmonic Camelot rule set, and the test suite.
- **Codex (OpenAI)**: pull request review, automated verification, model research, and documentation corrections.

If you contribute, add your name and the work you contributed.
