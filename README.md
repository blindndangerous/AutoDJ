# AutoDJ

[![CI](https://github.com/blindndangerous/AutoDJ/actions/workflows/ci.yml/badge.svg)](https://github.com/blindndangerous/AutoDJ/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/blindndangerous/AutoDJ/graph/badge.svg)](https://codecov.io/gh/blindndangerous/AutoDJ)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An auto-DJ that picks the next song based on what is playing now.  Point it at the folder where your music lives and it will play forever, picking each next track because it actually sounds like what you just heard.  Everything runs on your own computer.  Nothing is sent to any cloud service.

## What you get

- **A non-stop set, picked for you.**  Each next track is the one that sounds closest to whatever is playing.  The result feels like a long mix where every change of song still makes sense.
- **Smooth crossfades, not hard cuts.**  Two tracks overlap for a few seconds at every change, with optional EQ ducking so the basslines do not fight each other.
- **A web page to control it.**  Open `http://localhost:8080` in any browser.  See what is playing, the album art, the lyrics, the queue.  Change the volume.  Skip a song.  Search and add tracks.
- **Mood presets.**  Pick from built-in profiles (Wakeup, Chill, Workout, Party, etc.) or write your own.  Each preset shapes the BPM curve so a Wakeup set starts slow and ramps up while a Party set stays fast.
- **Voice liners.**  Drop spoken clips ("You're listening to AutoDJ FM") into a folder and AutoDJ will play one over the music every few tracks, just like a real radio station.
- **A queue.**  Search the library, click "Now" to interrupt, click "Next" to add to the line.  Reorder with Up / Down.  Remove with one click.
- **Lyrics that scroll.**  If a song has an LRC file or lyrics in its tags, the web page shows them and highlights the current line.
- **Works offline.**  Once installed there is no network requirement.  Use it on a NAS, on a laptop in airplane mode, on a Raspberry Pi.
- **Accessibility.**  Controls support keyboard use.  Screen-reader claims are limited to the browser and flows recorded for each release; see [Accessibility testing](docs/accessibility-testing.md) for the policy and its limits.

## Quick start (the short version)

```bash
git clone https://github.com/blindndangerous/AutoDJ
cd AutoDJ

# Install exactly the dependencies recorded in uv.lock.
uv sync --frozen --all-extras
npm ci
mkdir -p music index models
```

This is the supported install. `--frozen` gives you the exact dependency versions CI tested.

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

## Containers (no Python install needed)

If you have Docker Compose installed:

```bash
git clone https://github.com/blindndangerous/AutoDJ
cd AutoDJ
mkdir -p music index models
sudo chown 10001:10001 music index models
chmod 0755 music index models
AUTODJ_MUSIC_DIR=./music AUTODJ_INDEX_DIR=./index AUTODJ_MODEL_DIR=./models \
  docker compose up --build
```

Open `http://localhost:8080`. Container runs as UID/GID 10001. Default Compose publication is host
loopback only. See [Operations](docs/operations.md) for WSL2, bind mounts, and authenticated LAN
startup.

The container does not run the indexing step (it goes faster on a machine with a GPU, which a container does not always have).  Run `uv run autodj index` on the host first, then start the container.

## How to use the web UI

After `autodj serve`, point a browser at `http://localhost:8080`.  Four tabs:

- **Now Playing.**  What is playing, the next track, album art, lyrics, the cue strip on the progress bar.
- **Queue & Search.**  Find any track in your library and play it now or queue it up.  Reorder the queue.
- **Settings.**  Pick a preset, change the crossfade length, switch transition effects, set a BPM range, toggle voice liners, choose an audio output device.
- **Library tools.**  Run index / enrich / prune / stats jobs without leaving the page.

### Keyboard shortcuts (Now Playing tab)

| Key | What it does |
|---|---|
| Space | Play / pause |
| N | Skip to the next track |
| S | Shuffle (jump to a random track) |
| M | Mute / unmute |
| Up / Down | Volume up / down (5%) |
| ? | Open the shortcut list |

The shortcuts are scoped to the Now Playing tab on purpose.  When you switch to Settings or the Library tab, arrow keys go back to navigating dropdowns and the shortcut keys do not interfere with typing in the search box.

### Music players already configured: just press play

The default `serve` mode is browser-driven: the server picks tracks; the browser plays them.  This means the volume in the browser is independent of any CLI volume, and switching audio output devices in the browser only affects the browser.

If you want server-side audio output instead (for example to send sound to a Bluetooth speaker through ALSA on Linux), pass `--server-audio`.

## Voice liners

Drop short spoken clips into a folder.  AutoDJ will fade the music down for a couple of seconds and play one of them now and then.

1. Open the **Settings** tab.
2. Tick **Enable voice liners**.
3. The Trigger / Mix / Library boxes appear.
4. Click the **Choose liner file** button to upload an MP3 / WAV / OGG / M4A / FLAC / AAC.
5. Set how often you want them to play.

You can pick three trigger styles, in any combination:

- **Every N tracks** -- after every 5 (or whatever) songs.
- **Every N minutes** -- on a wall-clock timer.
- **Random window** -- pick a random delay between two values.

Rotation modes: random, sequential, weighted (server stores weights but the browser falls back to uniform random for now).

## How well does this work?

It works well when your library has the genre clustering you expect.  Pop tracks pick more pop, jazz picks more jazz, an acoustic intro picks acoustic, a heavy drop picks something else heavy.

It does not work well when:

- The library is tiny (under ~50 tracks) -- there is not enough variety for the picker to behave like a DJ.  AutoDJ warns when the no-repeat window is bigger than the library.
- All your files are tagged "Unknown Artist" -- the picker still works on sound alone, but the web UI looks bare.
- Your tracks are very compressed (96 kbps MP3) -- the audio analysis still works but is less accurate.

## Configuration

AutoDJ starts with validated defaults. If `config.toml` exists in the working directory, AutoDJ
loads it, then loads sibling `config.local.toml`. Environment variables override files, and
explicit CLI flags override all other sources. Omitting `--config` is valid. Passing
`--config /path/to/config.toml` makes that file explicit, so a missing path is an error. Shipped
`config.toml.example` lists supported environment variables and settings.

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

For non-loopback bindings, including LAN access, authentication can be disabled only with an explicit trusted-LAN acknowledgement.  This still enforces the configured Host and Origin allowlists:

```bash
uv run autodj serve --host 0.0.0.0 --insecure-lan \
  --allowed-host radio.local --allowed-origin http://radio.local:8080
```

`--insecure-lan` disables authentication; use it only on a trusted private network.  Multi-user accounts, roles, cloud identity, and public Internet hosting are not supported.

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

The index is portable.  Build it on a fast machine (one with a GPU is best), then copy `index/` to another machine that mounts the music library at any path.  AutoDJ stores music files and DJ metadata relative to a configurable root, so the same index works on Windows, Linux, and macOS as long as `music_dir` points at the right place on each machine.  Legacy DJ-meta rows with absolute paths are migrated to relative keys on the next `autodj index` or `autodj analyse` run.

Per-machine file overrides go in `config.local.toml` next to loaded base configuration. Environment
variables and CLI flags still take precedence.

## Troubleshooting

**The first index run is taking forever.**  This is the slow pass.  AutoDJ has to listen to every file and remember what it sounds like.  On a CPU it can take many hours for a 10000-track library.  On a machine with an NVIDIA GPU it is much faster.  Run with `--limit 50` first to confirm it works, then leave the full run going overnight.

**Browser says "loading module ... was blocked".** You probably ran `npm run build` once and then
deleted `node_modules`. Either delete `src/autodj/static_dist` (server falls back to unbundled
source) or run `npm ci && npm run build`.

**No sound from the web UI.**  Click the **Play** button once -- browsers require a user gesture before they will play audio.  After the first click, AutoDJ unlocks its audio context and plays normally for the rest of the session.

**Voice liner upload button is missing.**  The whole "Library" panel hides until you tick the **Enable voice liners** checkbox.  Tick it first, then the upload form appears.

**Cue point list is empty.**  AutoDJ analyses each track in the background after it starts playing.  Wait a few seconds; the cue strip on the progress bar should fill in.  Pass `autodj -v serve` to see "Background analysis done: ... -> 5 cues" log lines as they finish.

**Lyrics card never appears.**  AutoDJ checks three places, in order: an LRC file next to the audio file (timestamped, scrolls), the `lyrics` field in the beets database, the embedded ID3 / Vorbis / MP4 lyric tag.  If none of those is present, the lyrics card stays hidden.

**Hotkeys do nothing on the Settings tab.**  This is on purpose.  Hotkeys only fire when the Now Playing tab is visible so they do not fight with the dropdowns and sliders on Settings.  The `?` shortcut still works from any tab.

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
tests/
    unit/               # pytest unit tests
    integration/        # pytest integration tests with FastAPI TestClient
    jsmodules/          # vitest unit tests for the JS modules
    playwright/         # cross-browser audits against a running server
```

## Development

If you plan to change the code:

```bash
# Python tests + linting + type checking + dead-code + dep audit.
uv sync --frozen --all-extras
uv run python scripts/ci_pytest.py
uv run ruff check src tests scripts
uv run mypy src/autodj
uv run pyright src/autodj
uv run vulture              # dead-code scan
uv run deptry src/autodj    # dep-declaration audit

# Web UI build (optional -- the server falls back to unbundled source
# when the build output is missing).
npm ci
npm run build           # writes src/autodj/static_dist/

# JS lint + module unit tests.
npm run lint
npm test

# Chromium audit against a running server.
AUTODJ_BROWSERS=chromium npm run audit:ci
```

Pre-commit runs these hooks: `trailing-whitespace`, `end-of-file-fixer`, `mixed-line-ending`,
`check-added-large-files`, `check-merge-conflict`, `check-yaml`, `check-toml`, `check-json`,
`detect-private-key`, `check-case-conflict`, `check-symlinks`, `gitleaks`, `actionlint`, `ruff`,
`ruff-format`, `bandit`, `mypy`, `vulture`, `deptry`, `interrogate`, `xenon`, `pip-audit`,
`pip-licenses`, `osv-scanner`, `trivy-fs`, `pytest`, `eslint`, and `commitlint`. Install them with
`uv run pre-commit install` and `uv run pre-commit install --hook-type commit-msg`.

Gates outside pre-commit include lock checks, the coverage-exclusion policy, Pyright, Vitest, the
Vite build, the frontend dead-code scan, npm audit, Playwright audits, container smoke, and release
verification. Run their commands directly or through CI as described in
[Contributing](CONTRIBUTING.md).

## Release artifacts

Every tag publishes what `uv build` produces: a wheel, an sdist, a CycloneDX SBOM, and a cosign
signature bundle beside each file. They exist so a build can be verified and archived, and so
AutoDJ can be installed without a checkout — the wheel already carries the minified web UI, so it
needs no Node toolchain.

AutoDJ is not on PyPI. To install a tagged wheel:

```bash
uv pip install "autodj[all] @ https://github.com/blindndangerous/AutoDJ/releases/download/v0.16.1/autodj-0.16.1-py3-none-any.whl"
```

Drop `[all]` for the lighter install that only runs `enrich`, `prune`, `stats`, and `playlist`.

Installing a wheel resolves dependencies fresh from PyPI instead of from `uv.lock`, so you give up
the exact versions CI tested. The clone plus `uv sync --frozen --all-extras` above stays the
supported path; reach for the wheel only when you want AutoDJ without a source tree.

## Credits and licensing

- AutoDJ's own code is MIT licensed.
- **The default model weights are not, and this restricts what you may do with them.** [MuQ-large-msd-iter](https://huggingface.co/OpenMuQ/MuQ-large-msd-iter) ships its code under MIT but its *weights* under [CC-BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/), which requires attribution and forbids commercial use. AutoDJ never redistributes those weights: no checkpoint is committed here or baked into the container image, and `autodj index` downloads them from Hugging Face onto your own machine. If you want to use AutoDJ commercially, point `[model] name` at a checkpoint you are licensed for, or get permission from the publisher.
- Installing the `play` or `all` extras pulls copyleft dependencies from PyPI: `mutagen` (GPL-2.0-or-later) and `pynput` (LGPL-3.0). `librosa` pulls `soxr` (LGPL-2.1-or-later). AutoDJ neither vendors nor redistributes them. The core install is permissive-only.
- Audio analysis uses [librosa](https://librosa.org/) (ISC).
- Vector search uses [FAISS](https://github.com/facebookresearch/faiss) (MIT).
- The web UI uses [FastAPI](https://fastapi.tiangolo.com/) and a hand-written ES module front end (no React, no Vue, no framework).
- Cue-point importers read [Mixxx](https://mixxx.org/), [Rekordbox](https://rekordbox.com/), and [Traktor](https://www.native-instruments.com/en/products/traktor/) library files.

If AutoDJ is useful to you, a star on GitHub is appreciated.  Issues and pull requests welcome.

## Contributors

AutoDJ was built collaboratively by humans and AI assistants.  Each contributor is named with the part of the work they led.

### Human contributors

- **[blindndangerous](https://github.com/blindndangerous)**: project vision, library design, requirements, UX direction (web UI flow, mode semantics, gapless feel), every accessibility decision, all real-world testing on a 10k-track library, every release call.
- **[jage9](https://github.com/jage9)**: additional contributions and feedback.

### AI assistants

- **Claude (Anthropic)**: paired-programming partner across the whole codebase.  Worked on the MuQ + librosa indexing pipeline, the FAISS similarity engine, crossfade audio math with EQ-ducking, the transition effects (CLI + AudioWorklet), the FastAPI + WebSocket web layer, the section-nav SPA, the gapless prefetch + silence detector, the harmonic Camelot rule set, and the test suite.  Every line was reviewed and guided by a human before it shipped.

If you contribute, add yourself here in the same shape as the rows above.
