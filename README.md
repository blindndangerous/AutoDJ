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
- **Built for screen readers.**  AutoDJ was written by a blind developer.  Every control is keyboard-accessible and announced clearly to NVDA / JAWS / VoiceOver.

## Quick start (the short version)

```bash
git clone https://github.com/blindndangerous/AutoDJ
cd AutoDJ

# Install Python dependencies (uv handles the heavy lifting).
uv sync --extra all

# Point AutoDJ at your music folder once and let it learn the library.
# This pass is the slow one -- it listens to every track and writes
# what it learned to disk, enriches beets metadata, and backfills DJ
# intro/outro/cue analysis.  For a quick embed-only smoke test, skip
# the post-passes.
uv run autodj index --limit 50 --no-enrich --no-analyse
uv run autodj index               # full library, can take hours

# Start the web UI.
uv run autodj serve

# Open http://localhost:8080 in your browser.
```

That is the whole flow.  Index once, serve forever.  Future runs of `autodj index` only embed new files and refresh the post-processing cache.

## Containers (no Python install needed)

If you have Podman or Docker installed:

```bash
git clone https://github.com/blindndangerous/AutoDJ
cd AutoDJ
podman compose up        # or: docker compose up
```

Open `http://localhost:8080`.  Music goes in `./music`; the index lives in `./index`.

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

A config file is optional.  Without one, AutoDJ uses sensible defaults: index lives in `./index`, music is read from `./music`, the web UI listens on `localhost:8080`.

Drop a `config.toml` in the repo root (or pass `--config /path/to/config.toml` on every command) to change anything.  The shipped `config.toml.example` is annotated.

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

The index is portable.  Build it on a fast machine (one with a GPU is best), then copy `index/` to another machine that mounts the music library at any path.  AutoDJ stores the music files relative to a configurable root, so the same index works on Windows, Linux, and macOS as long as `music_dir` points at the right place on each machine.

Per-machine overrides go in `config.local.toml` next to `config.toml`.  AutoDJ reads it last, so anything in there wins.

## Troubleshooting

**The first index run is taking forever.**  This is the slow pass.  AutoDJ has to listen to every file and remember what it sounds like.  On a CPU it can take many hours for a 10000-track library.  On a machine with an NVIDIA GPU it is much faster.  Run with `--limit 50` first to confirm it works, then leave the full run going overnight.

**Browser says "loading module ... was blocked".**  You probably ran `npm run build` once and then deleted `node_modules`.  Either delete `src/autodj/static_dist` (the server falls back to the unbundled source) or re-run `npm install && npm run build`.

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
        app.js              # 1300-line bootstrap
        modules/            # 16 ES modules (lyrics, queue, hotkeys, ...)
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
uv sync --extra all --extra dev
uv run pytest
uv run ruff check
uv run mypy src
uv run vulture src/autodj   # dead-code scan
uv run deptry src/autodj    # dep-declaration audit

# Web UI build (optional -- the server falls back to unbundled source
# when the build output is missing).
npm install
npm run build           # writes src/autodj/static_dist/

# JS lint + module unit tests.
npm run lint
npm test

# Cross-browser audit against a running server.
AUTODJ_URL=http://192.168.50.40:8080 npm run audit:regression
```

The Python side uses `uv`, `pytest`, `ruff`, `mypy`, `bandit`, `vulture` (dead-code), and `deptry` (dependency declarations).  The web side uses `vite` for bundling, `vitest` for unit tests, `eslint` for JS lint, and `@playwright/test` for cross-browser audits.  All of these run as `pre-commit` hooks; install once with `uv run pre-commit install`.

## Credits and licensing

- AutoDJ is MIT licensed.
- The default music model is [MuQ-large-msd-iter](https://huggingface.co/OpenMuQ/MuQ-large-msd-iter) by Tencent.
- Audio analysis uses [librosa](https://librosa.org/).
- Vector search uses [FAISS](https://github.com/facebookresearch/faiss).
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
