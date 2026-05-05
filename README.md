# AutoDJ

An AI-powered local music continuity player. Give it your music library and it plays songs that sound like each other, forever — no cloud, no subscriptions, fully offline.

Uses [MuQ-large-msd-iter](https://huggingface.co/OpenMuQ/MuQ-large-msd-iter) (a music-specific deep learning model trained with Mel-Residual Vector Quantization on the Million Song Dataset) and [FAISS](https://github.com/facebookresearch/faiss) nearest-neighbor search to select and queue the next most sonically similar song.

**Highlights:**
- Pre-trained MuQ embeddings + exact FAISS cosine search — no cloud, no scrobble service required
- BPM-shaping presets (wakeup, party, workout…) and discovery mode
- Crossfade between tracks — optional pro-DJ EQ ducking eliminates bass-clash mush
- ReplayGain loudness normalisation — every track plays at consistent volume
- Web UI with album art, scrolling LRC lyrics, and a reorderable queue
- Cross-platform index — build once on a GPU box, play on any other machine via shared NAS

---

## How it works

1. **Index** — AutoDJ scans your library, extracts a 1040-dimensional audio fingerprint per song (1024-dim MuQ embedding + 16 librosa spectral/chroma features), and stores them in a FAISS index. Energy, key, mode, and tempo confidence are also extracted and stored as metadata.
2. **Play** — It picks a seed song, finds its closest sonic neighbors, and plays them back-to-back with a configurable crossfade. Recently played songs are excluded from the candidate pool.

---

## Why MuQ?

MuQ ([Tencent AI Lab, 2025](https://arxiv.org/abs/2501.01108)) is a self-supervised music representation learning model trained with Mel-Residual Vector Quantization. It outperforms MERT and MusicFM on the MARBLE music-understanding benchmark — particularly on genre classification, singer identification, instrument classification, and music-structure analysis. Its embeddings capture musical structure densely, which translates to better music-to-music similarity for next-track selection.

MuQ requires 24 kHz mono audio and fp32 inference (fp16 may produce NaN values). AutoDJ handles resampling automatically.

---

## Prerequisites

- **Python 3.13+** — [Download here](https://www.python.org/downloads/)
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** — Fast Python package manager
  ```bash
  # Windows (PowerShell)
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```
- **~1.5 GB disk space** for the MuQ model (downloaded automatically on first run)
- A music library in **MP3, FLAC, or M4A** format

**Optional but recommended:**
- [beets](https://beets.io/) — if you use beets, AutoDJ reads your `library.db` for rich metadata (artist, title, genre, BPM, year) without re-scanning files.

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/blindndangerous/autodj
cd autodj

# 2. Install everything (typical full install — index + play + web)
uv sync --extra all

# 3. (Optional) Install dev/test dependencies
uv sync --extra all --extra dev
```

### CPU-only torch (e.g. on a NAS)

Indexing automatically uses CUDA when available, CPU otherwise.  To install a smaller CPU-only torch wheel (~200 MB instead of ~2 GB) on a host without an NVIDIA GPU:

```bash
# Linux / NAS
uv pip install --index-url https://download.pytorch.org/whl/cpu \
  torch muq huggingface_hub librosa soundfile tqdm
```

CPU indexing is fine for **small batches** (`uv run autodj index --limit 50`).  A full 10k-track library on CPU takes hours; run that on a GPU host overnight.

### Minimal installs (per role)

The deps split into optional groups so a NAS or headless host doesn't need to pull torch / librosa / audio libs just to run `enrich` / `prune` / `stats` against a shared index:

| Role | Install | Pulls in |
|------|---------|----------|
| **Maintenance only** (NAS) — `enrich`, `prune`, `stats`, `playlist` | `uv sync` | core only: faiss, numpy, click, rich (~tiny) |
| **Indexer** (GPU box) | `uv sync --extra index` | + muq, torch, librosa, soundfile, tqdm |
| **Player** (listening machine) | `uv sync --extra play` | + librosa, soundfile, sounddevice, pynput, mutagen, scipy |
| **Web UI** (also needs `play`) | `uv sync --extra play --extra web` | + fastapi, uvicorn |
| **Everything** | `uv sync --extra all` | full kit |

Trying to run `index` / `play` / `serve` after a minimal install raises a clear ImportError naming the missing dep — fix with the matching `--extra`.

---

## Configuration

Edit `config.toml` to point to your music library. Every key has an inline comment explaining its purpose.

```toml
[library]
# Path to your music folder — local drive or NAS mapped drive letter.
# When using beets, this MUST match the local mount point of the beets
# `directory` setting so relative paths resolve correctly.
music_dir = "Z:/Music"

# Path to your beets SQLite library database (optional but recommended)
beets_db = "C:/Users/you/.config/beets/library.db"

[playback]
crossfade_seconds = 3.0   # Crossfade duration between tracks (0 = instant cut)
no_repeat_window  = 50    # Don't replay any of the last N tracks
# history_file    = "~/.autodj/history.jsonl"   # Optional play history log
# discovery_every = 20                          # Inject a sonically distant track every N tracks

[model]
name = "OpenMuQ/MuQ-large-msd-iter"   # Model used for audio embeddings
```

See `config.toml.example` for all options with full inline documentation.

### Beets paths

Recent beets versions store track paths *relative* to the library root (the `directory` config option) via the `relative_path` migration. AutoDJ resolves these against `music_dir`, so all you need is for `music_dir` to point to the local mount point of your beets `directory`.

Example: beets stores `10 Years/2001 - Into the Half Moon/01 Fallaway.flac` and your NAS music library is mounted at `Z:/Music`. Set:
```toml
[library]
music_dir = "Z:/Music"
beets_db  = "C:/Users/you/.config/beets/library.db"
```

Absolute paths in the database (rare — typically tracks living outside the main library tree) are used as-is.

---

## Building the index

Before playing music, AutoDJ must index your library. This is a **one-time operation** — subsequent runs are incremental (only new files are processed).

### Step 1: Test with a small batch first

```bash
uv run autodj index --limit 50
```

This indexes 50 tracks and takes a few minutes on CPU. Confirm the output looks right before committing to the full library.

### Step 2: Index your full library

```bash
uv run autodj index
```

On a CPU-only machine with 10,000+ tracks this may take several hours. **Run it overnight**, or see below for GPU acceleration.

### Rebuilding from scratch

```bash
uv run autodj index --force
```

### Pruning deleted tracks

If you delete, move, or rename files in your library, `metadata.json` will list paths that no longer exist.  Run:

```bash
uv run autodj prune
```

to drop those entries.  An auto-prune also runs at the start of every `autodj index`, so most of the time you don't need this manually.

**Safety net:** if more than 20 % of the index would be removed in one pass (almost always a sign of a misconfigured `music_dir` rather than a real library cleanup), the prune is refused and exits with `PruneSafetyError`.  Use `autodj prune --force` to override.

### Indexing on a GPU machine

If you have another machine on your local network with an NVIDIA GPU and access to the same NAS:

1. **Install AutoDJ on the GPU machine** the same way (`uv sync`).
2. **Set a shared index location** in `config.toml` on both machines:
   ```toml
   [index]
   index_dir = "Z:/autodj-index"   # NAS path both machines can reach
   ```
3. **Run the indexer on the GPU machine** — CUDA is detected automatically:
   ```bash
   uv run autodj index
   ```
4. **Play on your listening machine** — it reads the same index from the NAS:
   ```bash
   uv run autodj play
   ```

### Sharing one index across Windows + Linux machines

Track paths in `metadata.json` are stored RELATIVE to `[library] music_dir`, so the same index file works on every machine that mounts the library — even if the absolute mount path differs (`/mnt/music` on Linux, `Z:/Music` on Windows).

Two pieces are needed:

1. **Per-machine `music_dir`.**  Keep a shared `config.toml` (no host paths) and use a gitignored **`config.local.toml`** sibling to override only the local paths.  The repo ships two ready-to-copy templates:

   ```bash
   # Linux host
   cp config.local.toml.linux.example   config.local.toml
   $EDITOR config.local.toml             # adjust music_dir / beets_db

   # Windows host
   cp config.local.toml.windows.example config.local.toml
   notepad config.local.toml
   ```

   Keys in `config.local.toml` are deep-merged on top of `config.toml` at load time.  Each host keeps its own; the file never leaves the machine.

2. **Per-machine venv** (.venv binary wheels are not portable between OSes).  Set the `UV_PROJECT_ENVIRONMENT` environment variable per machine to a path on its own local disk:

   ```powershell
   # Windows — make permanent for the user
   [Environment]::SetEnvironmentVariable("UV_PROJECT_ENVIRONMENT", "C:\Users\you\.venvs\autodj-win", "User")
   ```
   ```bash
   # Linux — append to ~/.bashrc
   export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/autodj-linux"
   ```

   Then run `uv sync` once on each machine — the venv lands on local disk, the project tree on the NAS stays clean.

**Path remap (legacy bridge).**  An index built before the relative-paths migration may still contain absolute paths from the original host.  Add `path_remap` to convert them on the fly:

```toml
[library]
path_remap = [
  ["/mnt/music/", "Z:/Music/"],
]
```

Once you re-run `autodj prune` (or `autodj index`) the absolute paths are rewritten as relative and `path_remap` is no longer needed.

---

## Playing music

```bash
# Start from a random seed song
uv run autodj play

# Start from a specific song or artist (fuzzy search)
uv run autodj play --seed "Portishead"

# Use a BPM-shaping preset
uv run autodj play --preset wakeup

# Hard BPM filter — only pick tracks in the 90–130 BPM range
uv run autodj play --bpm-range 90-130

# Inject a sonically distant track every 15 tracks (press D to toggle on/off)
uv run autodj play --discovery-every 15

# Save a live M3U playlist of everything played this session
uv run autodj play --export-m3u ~/session.m3u

# Log every played track to a JSON Lines file
uv run autodj play --history-file ~/.autodj/history.jsonl

# Override crossfade duration for this session
uv run autodj play --crossfade 5

# Dry run — print track picks without playing audio (good for testing)
uv run autodj play --dry-run

# Smart shuffle — pick the most sonically DISTANT next track
uv run autodj play --smart-shuffle

# Pure shuffle — random next track, ignores similarity entirely.
# Toggle off mid-set and similarity resumes from whatever's playing —
# use it to wander into a song you like, then "lock in" by switching
# shuffle off so the auto-DJ keeps that track as its new seed.
uv run autodj play --pure-shuffle

# Hide lyrics in the CLI panel and web UI (default: on)
uv run autodj play --no-show-lyrics
```

### Keyboard controls during playback

| Key       | Action                                      |
|-----------|---------------------------------------------|
| `Space`   | Pause / Resume                              |
| `N`       | Skip to next track                          |
| `D`       | Toggle discovery mode on/off                |
| `Q`       | Quit                                        |
| `←` / `→` | Seek −10 s / +10 s                          |
| `↑` / `↓` | Volume up / down (5% per step)              |
| `M`       | Mute / Unmute                               |

`D` only works when `--discovery-every` is set (or `discovery_every` is in `config.toml`).

### Status bar

While playing, a persistent status panel is pinned to the bottom of the terminal:

```
  Now playing : Portishead — Glory Box                    [playing]  1:23 / 4:50
  Up next     : Massive Attack — Teardrop    ◈ Discovery  Vol: ████████░░  80%
  Controls: Space=Pause/Resume  N=Skip  D=Discovery  Q=Quit  ←/→=Seek±10s  ↑/↓=Vol  M=Mute
```

The `◈ Discovery` indicator appears in cyan when discovery is enabled, dimmed when disabled.

---

## Presets

Presets shape the BPM arc of a session — they bias the similarity engine toward tracks at a target tempo, which changes over time.

### Built-in presets

| Preset     | Description                                  |
|------------|----------------------------------------------|
| `wakeup`   | Linear ramp 70 → 130 BPM over 30 tracks     |
| `winddown` | Linear ramp 130 → 70 BPM over 30 tracks     |
| `sleep`    | Linear ramp 85 → 55 BPM over 40 tracks      |
| `morning`  | Gentle rise 60 → 95 BPM over 30 tracks      |
| `slide`    | Sine arch 80 → 135 → 80 BPM over 40 tracks  |
| `party`    | Constant 128 BPM                             |
| `workout`  | Constant 145 BPM                             |
| `chill`    | Constant 75 BPM                              |
| `focus`    | Constant 85 BPM                              |
| `driving`  | Constant 112 BPM                             |

```bash
uv run autodj play --preset wakeup
uv run autodj serve --preset party --discovery-every 10
```

### User-defined presets

Add your own presets to `config.toml`:

```toml
[presets.focus_deep]
bpm_target = 85
bpm_weight = 0.15        # how strongly BPM shapes picks (0–1, default 0.25)

[presets.festival]
bpm_start = 90
bpm_end   = 145          # linear ramp
horizon_tracks = 60      # reach bpm_end by track 60, then hold
bpm_weight = 0.35
discovery_every = 10     # preset ships with discovery rate built in

[presets.arch]
bpm_start = 80
bpm_end   = 140
curve = "slide"          # sine arch: low → peak → low
```

User presets shadow built-ins on name collision.

---

## Web UI

AutoDJ includes a browser-based control panel powered by FastAPI and WebSockets.

```bash
# Start the web UI (defaults: http://127.0.0.1:8080)
uv run autodj serve

# Choose a seed track, custom port, open browser automatically
uv run autodj serve --seed "Portishead" --port 8080 --open

# Bind to all interfaces (LAN access)
uv run autodj serve --host 0.0.0.0 --port 8080

# With preset and discovery
uv run autodj serve --preset wakeup --discovery-every 15

# Server-side audio output (legacy mode — default is browser-driven so
# the CLI player and the web UI never share an audio stream)
uv run autodj serve --server-audio
```

The web UI displays the currently playing track, the up-next track, playback state, and volume. All playback controls (pause/resume, skip, volume, mute) are available from the browser — keyboard controls continue to work in the terminal at the same time.

By default, **`autodj serve` is browser-driven**: the server picks tracks
and the browser's Web Audio graph plays them.  Skipping, volume, EQ, and
audio-device changes only touch the local browser.  Pass `--server-audio`
to fall back to the legacy mode where the server process streams audio
to its own soundcard (kept for headed hosts that want both).

The **◈ Discovery** button toggles discovery mode on/off from the browser (visible only when a discovery rate is configured).

Live state is pushed to the browser via a WebSocket connection every second — no polling, no page refreshes.

### Search and queue from the browser

The **Search Library** panel lets you search by artist or title. Each result has two buttons:

| Button | Action |
|--------|--------|
| **▶ Now** | Queue the track and immediately skip to it |
| **⏭ Next** | Queue the track to play after the current one finishes |

> **Note:** the web server has no authentication — only bind to `0.0.0.0` on a trusted LAN.

---

## Loudness normalisation (ReplayGain)

If your library is tagged with ReplayGain (most modern taggers — beets, MP3Tag, Mp3Gain, foobar2000 — write it), enable it in `config.toml`:

```toml
[replaygain]
enabled            = true       # default false (opt-in)
target_db          = -14.0      # output reference: -14 ≈ Spotify, -18 = original RG reference
max_clip_safe_gain = 1.0        # peaks never exceed this fraction of full scale
```

AutoDJ reads the embedded `replaygain_track_gain` + `replaygain_track_peak` tags and applies a clip-safe linear gain so every track plays at consistent loudness.  Tracks without tags play unchanged.

If your library is not tagged, generate tags once with:

```bash
# beets users
beet replaygain

# generic
loudgain -a -k -s e *.flac      # https://github.com/Moonbase59/loudgain
```

---

## Pro-DJ mixing layer (Deejay-style automix)

Five opt-in features that turn AutoDJ into a Deejay-grade automix:

```toml
[djmix]
harmonic_mixing      = true   # only mix into Camelot-compatible keys
beatmatch            = true   # pitch-stretch incoming track to match outgoing BPM (±8%)
beatmatch_max_stretch = 0.08
outro_intro_align    = true   # crossfade between detected outro of A and intro of B
phrase_align         = true   # snap crossfade start to nearest 8-bar phrase
phrase_bars          = 8
filter_sweep         = true   # low-pass sweep on outgoing tail during crossfade
filter_sweep_floor_hz = 250.0
```

Or override per-session from the CLI:

```bash
uv run autodj play --harmonic --beatmatch --align-outro --phrase-align --filter-sweep
```

**How it works:**

- **Harmonic mixing** uses the Camelot wheel.  Tracks already have their key + mode stored in the index, so this needs no re-analysis.  Same position, ±1 around the wheel, or relative major/minor pass.
- **Beatmatch** uses `librosa.effects.time_stretch` to pitch-preserve-stretch the incoming track to the outgoing BPM.  Refuses adjustments beyond the configured max stretch (default ±8 %, typical DJ practice).
- **Outro / intro alignment** auto-detects each track's intro end and outro start using a smoothed RMS envelope.  First time a track plays under this mode, detection runs and the result is cached in `index/dj_meta.json` (sidecar — never touches the main metadata file).  Subsequent plays are instant.
- **Phrase alignment** also runs on first play, extracting the beat grid via `librosa.beat.beat_track` and snapping the crossfade start time to the nearest 8-bar phrase boundary.
- **Filter sweep** rides a Butterworth low-pass on the outgoing tail (cutoff sliding from full-range down to the floor in 32 steps), giving the classic "filter-out" energy lift.

**3-band EQ** (low / mid / high) sliders in the web UI let you tweak the live mix in real time — Butterworth crossover at 250 Hz / 4 kHz, applied per audio output chunk via `sosfilt`.  Reset button restores unity.

The web UI also displays live **BPM**, **Camelot key**, **energy**, and the **current beatmatch stretch ratio** as badges in the now-playing card.  A polite live region announces "Key 8A, BPM 124, beatmatched 1.07 times" on every track change (separate from the title announcement to avoid clobbering).

---

## Where transitions are rendered

Two playback modes — same effect catalogue, different engine:

| Mode | Renderer | What's happening |
|------|----------|------------------|
| **CLI `autodj play`** + **server-rendered `serve`** | Python (`numpy` / `scipy` / `soundfile` / `librosa`) | Effects mutate raw audio bytes before they reach the soundcard. Full pipeline: ReplayGain → beatmatch (librosa time-stretch) → outro/intro align → phrase align → filter sweep → transition effect → EQ-ducked crossfade → 3-band EQ → sounddevice. |
| **Browser-playback `serve --no-playback`** | Browser **Web Audio API** (`AudioContext` + `AudioBufferSourceNode`, `BiquadFilterNode`, `DelayNode`, `ConvolverNode`, `WaveShaperNode`, `OscillatorNode`) | Effects routed as live audio nodes between `MediaElementSource` and the destination.  Real-time DSP, zero server CPU during playback. |

**Effect parity** — all 18 transition effects work in BOTH modes.  Some details differ by renderer:

| Effect | CLI | Browser | Notes |
|--------|-----|---------|-------|
| `echo_out` | numpy feedback delay | DelayNode + feedback gain | identical |
| `reverb_tail` | Schroeder reverb (numpy) | ConvolverNode + synth IR | browser is smoother |
| `highpass_sweep` / `lowpass_sweep` | scipy butter sweep | BiquadFilterNode + freq ramp | identical |
| `cross_eq_swap` | scipy butter pair | two biquads | identical |
| `tape_stop` | progressive resampling | playbackRate ramp 1.0→0.2 | browser floored at 0.2× to avoid HTMLMediaElement stutter |
| `gate_stutter` | numpy hard gate | scheduled gain on/off | identical |
| `noise_riser` / `noise_drop` | scipy filter sweep on synth noise | BufferSource + biquad sweep | identical |
| `bitcrusher` | quantising numpy | WaveShaperNode | identical |
| `flanger` | LFO delay (numpy) | DelayNode + LFO oscillator | identical |
| `pitch_swell` | resampling | playbackRate 1.0→2.0 | identical |
| `telephone` | scipy band-pass | cascaded biquads | identical |
| `backspin` / `forward_spin` | reversed buffer + accelerating resample | **fetch + decodeAudioData → AudioBufferSourceNode with reversed buffer** | true reverse playback in both modes |
| `distortion` (browser-only) | n/a — use `bitcrusher` for similar grit | WaveShaperNode + drive ramp | |
| `chorus` (browser-only) | n/a | 3 detuned LFO delays | |
| `submerge` (browser-only) | n/a | lowpass sweep + reverb | |
| `vinyl_wow` (browser-only) | n/a — server uses real time-stretch | playbackRate LFO modulation | |

> **Note:** `distortion`, `chorus`, `submerge`, `vinyl_wow` are browser-only at present.  CLI play falls back to `none` if these are configured.  Future: port to numpy/scipy on the CLI side.

> **Modern Web Audio (2026):** AutoDJ uses browser-native `AudioBufferSourceNode` (real reverse via decoded buffer + manual reverse copy — equivalent to the spec's negative `playbackRate`) plus standard `BiquadFilterNode` / `DelayNode` / `ConvolverNode` / `WaveShaperNode` / `OscillatorNode` graphs.  No third-party library (Tone.js, WAM, Howler) — adds runtime weight without enabling anything we need.  AudioWorklet is the path forward if we need custom DSP (e.g. stereo bitcrusher or a true-stereo backspin); not yet needed.

---

## Audio format support

| Format | CLI / server playback | Browser playback |
|--------|------------------------|------------------|
| MP3    | ✓ | ✓ all browsers |
| AAC (M4A) | ✓ | ✓ all browsers |
| FLAC   | ✓ | ✓ Chrome 56+, Firefox 51+, Safari 11+ |
| ALAC (M4A) | ✓ | **Safari only** — Chrome / Firefox cannot decode |
| OGG / Opus | ✓ | ✓ Chrome / Firefox; ✗ Safari |
| WAV    | ✓ | ✓ all browsers |

ALAC files in browser-playback mode auto-skip with a "Playback error" message naming the file.  Workarounds: re-encode to FLAC (`beet convert -f flac`) or use Safari.

---

## Transition effects

Each crossfade can apply one of twenty DJ-style flourishes (browser side
runs them through Web Audio + AudioWorklet for sample-accurate, click-free
fades; CLI side does the equivalent in numpy):

```toml
[transitions]
effect  = "rotate"   # cycle through all real effects
wet_mix = 1.0
```

| Effect | Sound |
|--------|-------|
| `none` | Standard crossfade only |
| `echo_out` | Feedback echo throw on outgoing |
| `reverb_tail` | Schroeder reverb on outgoing |
| `highpass_sweep` | Sweeps the highs out of the outgoing tail |
| `lowpass_sweep` | Sweeps the lows out of the outgoing tail |
| `tape_stop` | Vinyl stop on outgoing — true silence at the end |
| `gate_stutter` | Sample-accurate rhythmic chop with raised-cosine edges (AudioWorklet) |
| `noise_riser` | Synthesised white-noise build between tracks |
| `noise_drop` | Opposite of riser — descending sweep |
| `backspin` | True reverse playback of the outgoing tail |
| `forward_spin` | Pitched-up forward acceleration into the cut |
| `cross_eq_swap` | Outgoing keeps highs / incoming brings bass |
| `bitcrusher` | Real bit-crush + sample-rate reduction (AudioWorklet) |
| `flanger` | LFO-modulated short delay |
| `pitch_swell` | Slow pitch bend on outgoing |
| `telephone` | Band-limited, compressed |
| `distortion` | Soft-clip drive |
| `chorus` | Three detuned voices |
| `submerge` | Underwater-style cutoff dive |
| `vinyl_wow` | Pitch wow + flutter |
| `freeze` | Granular looper — captures the last 100 ms and loops with fade-out (AudioWorklet) |
| `glitch` | Random buffer slicing + reorder (AudioWorklet) |
| `random` | Pick uniformly per crossfade |
| `rotate` | Cycle through all real effects in order |

### Effect timing (industry-standard)

Each effect has a minimum duration sourced from commercial DJ-tool
defaults (Pioneer DJM, Reloop RMX, Numark, Mixxx).  When your
configured `crossfade_seconds` is shorter than the minimum, AutoDJ
automatically extends the outgoing-track runway so the effect doesn't
sound rushed.  The amplitude crossfade itself still respects your
configured length — the incoming track always lands at the same point.

| Effect | Minimum runway | Why |
|---|---|---|
| `tape_stop` | 4.0 s | Reloop default ~50% rate over 4 s |
| `backspin` | 2.5 s | Pioneer Backspin / Numark Reverse Roll |
| `forward_spin` | 2.5 s | Mirror of backspin |
| `reverb_tail` | 4.0 s | Mid-size hall decay |
| `noise_riser` | 4.0 s | 2-bar build at 120 BPM |
| `noise_drop` | 3.0 s | Drops feel snappier than risers |
| `freeze` | 4.0 s | Granular hold needs space |
| `glitch` | 3.0 s | Chaotic effects get tedious longer |
| `echo_out` | 3.0 s | 1/4-note feedback over 8 bars |
| (others) | = crossfade | Filter sweeps, EQ swap, distortion etc. follow user setting |

Real vinyl backspin uses a decelerating-rate envelope (`2.0× → 0.05×`,
quadratic curve) — not the linear acceleration earlier versions used.
The difference matches the actual physics of a record being pushed
back: hand impulse spins it fast, friction slows it to a stop.

### Daypart mood profiles

When you don't want to pick a preset every time you press play, the
daypart system picks BPM + energy targets from the local clock.  Same
idea as a radio station's clock-driven music rotation — gentler in
the morning, peak in the evening, chill late at night.

| Daypart | Hours | Target BPM | Target energy |
|---|---|---|---|
| morning | 06:00-10:00 | 80 | 0.04 |
| midday | 10:00-14:00 | 105 | 0.06 |
| afternoon | 14:00-18:00 | 115 | 0.07 |
| evening | 18:00-22:00 | 128 | 0.10 |
| night | 22:00-06:00 | 90 | 0.05 |

Enable in any of three ways:

```bash
uv run autodj play --daypart            # session override
uv run autodj serve --daypart           # web UI session
```

```toml
[playback]
enable_daypart = true                   # always on
```

```
☑ Daypart mood                          # web UI Settings card toggle
```

Custom windows in `config.toml`:

```toml
[dayparts.warmup]
start_hour    = 7
end_hour      = 9
target_bpm    = 70
target_energy = 0.03
bpm_weight    = 0.4
```

Daypart targets are **ignored when an explicit `--preset` is set** — a
preset's session BPM curve takes priority over the wall-clock baseline.

### Genre normalisation

Music libraries spell genres a hundred ways: "Electronic / EDM / IDM",
"Hip-Hop", "Hip Hop", "Rap", "Trip-Hop", "TripHop", "Indie Rock", "Alt
Rock"…  Preset `genres = [...]` filters now match against a canonical
form, so:

- `genres = ["Electronic"]` matches *Electronic, EDM, IDM, Synthwave,
  Trance, Chillwave, Ambient, Downtempo, Dubstep…*
- `genres = ["Rock"]` matches *Indie Rock, Alt Rock, Alternative,
  Post-Rock, Prog Rock, Garage Rock, Psychedelic Rock…*
- `genres = ["Hip-Hop"]` matches *Hip-Hop, Hip Hop, Rap, Trap*

Mapping table is in `src/autodj/genres.py` — adding a new alias is one
line.  Library tags don't have to be rewritten.

### Audio output device selection

Web UI Settings card has an **Audio output device** dropdown.

| Browser | Status |
|---|---|
| Chromium / Edge | Full support |
| Firefox 116+ | Full support |
| Firefox < 116 | Stuck on system default — `setSinkId` not exposed |
| Safari | Stuck on system default — Apple has not shipped `setSinkId` as of 2026-05 |

Browsers hide device **names** until you grant microphone permission once.
The Settings card shows a **Show device names** button when labels are
blank — clicking it triggers a brief microphone request, the stream is
released immediately (no audio captured), then the device list refreshes
with real names.  Generic `Output 1`, `Output 2` labels are shown without
permission.

Selection persists per-browser in `localStorage`.  Server-side CLI
playback has its own `--device` flag (see [Sound card selection — CLI](#sound-card-selection-cli)).

### Sound card selection — CLI

```bash
uv run autodj list-devices               # enumerate outputs with index + name
uv run autodj play --device 4            # by sounddevice index
uv run autodj play --device "USB Headphones"   # or by substring
```

Or set permanently:

```toml
[playback]
audio_device = "USB Headphones"          # int or substring
```

### Mobile-friendly web UI

Layout collapses to a single column on screens ≤ 720 px.  Touch
targets meet **WCAG 2.5.5** (≥ 44 × 44 CSS px) — every button, slider
thumb, and form field is finger-friendly.  Settings card stays
collapsed by default on phone, footer keyboard hint is hidden (no
keyboard).  Cover art shrinks to 96 × 96 (≤ 720 px) / 80 × 80
(≤ 480 px) but stays prominent.

### AudioWorklet effects

`bitcrusher`, `gate_stutter`, `freeze`, and `glitch` run in dedicated
AudioWorklet processors when played through the web UI.  This gives them:

- **Sample-accurate timing** — no 128-sample block-quantisation drift
- **Click-free seams** — raised-cosine fades on every gate / loop edge
- **Real audio-thread DSP** — operations like sample-and-hold rate
  reduction, granular loop-and-fade, and ring-buffer random reads that
  no built-in Web Audio node can produce.

Falls back to GainNode / WaveShaperNode equivalents when a worklet
fails to load (very old browsers).  CLI playback uses NumPy/SciPy
implementations of every effect.

Override per-session: `uv run autodj play --transition tape_stop`.

`wet_mix` controls how much of the effect is heard vs the dry crossfade (1.0 = full effect, 0.0 = inaudible).

---

## Pro-DJ crossfade (EQ ducking)

Two tracks playing simultaneously through a normal crossfade often sound muddy because their sub-200 Hz bass content sums.  Pro DJs solve this by manually cutting the bass on the outgoing track during the overlap.  AutoDJ does it automatically:

```toml
[playback]
crossfade_eq_duck         = true     # default false (opt-in)
crossfade_bass_cutoff_hz  = 180.0    # frequency below which outgoing track is progressively attenuated
```

Implementation: 4th-order Butterworth high-pass on the outgoing tail, mixed in with a quarter-sine envelope.  Tiny CPU cost, big perceived quality boost.  Falls back to plain linear crossfade if scipy is unavailable.

---

## Lyrics (LRC sidecars)

If a `<basename>.lrc` file exists next to an audio file, the web UI shows the full lyric list with the active line highlighted and announced via `aria-live` for screen-reader users.

LRC format example (`song.lrc`):

```
[ar:Portishead]
[ti:Glory Box]
[00:12.30]I'm so tired of playing
[00:18.55]Playing with this bow and arrow
[00:24.10]Gonna give my heart away
```

Tools that auto-fetch LRC files: `lrc-get`, `lyrics-finder`, `auddio`, or any beets plugin that fetches lyrics.

---

## Genre-aware presets

User presets accept an optional `genres` filter — only tracks whose genre matches (substring, case-insensitive) are eligible:

```toml
[presets.electronic_workout]
bpm_target = 145
bpm_weight = 0.4
genres     = ["electronic", "house", "techno"]
```

Combine with the existing BPM curves for tightly scoped sessions.

---

## Web UI features (recap)

In addition to the search + transport controls already documented, the web UI provides:

- **Album art** — embedded cover art (FLAC pictures, MP3 APIC, MP4 covr) shown on the now-playing card
- **Scrolling lyrics** — auto-scrolled, active-line highlighted, screen-reader-announced
- **Reorderable queue** — search → "Next" appends to a real queue; per-item Up / Down / Remove buttons (no drag-and-drop, deliberately, so it works for keyboard and screen-reader users)

All new features stream through the same WebSocket as the existing state push.

---

## Offline playlist generation

Generate an M3U playlist without playing anything:

```bash
# 20-track playlist to stdout
uv run autodj playlist

# 40-track playlist starting from a seed, saved to a file
uv run autodj playlist --seed "Portishead" --tracks 40 --output portishead.m3u

# BPM-shaped playlist
uv run autodj playlist --preset wakeup --tracks 30 --output wakeup.m3u

# Hard BPM filter
uv run autodj playlist --bpm-range 90-130 --tracks 20 --output house.m3u
```

---

## Library stats

```bash
uv run autodj stats
```

Displays a Rich overview of your indexed library: BPM distribution, top genres, decade breakdown, track lengths, top artists, key distribution (C–B), major/minor split, and energy histogram.

---

## Running tests

```bash
# Fast unit tests only (no model downloads, no audio hardware needed)
uv run pytest tests/unit/

# Integration tests (real FAISS, mocked model)
uv run pytest tests/integration/

# Smoke tests (CLI end-to-end, all heavy parts mocked)
uv run pytest tests/smoke/

# Full suite with coverage + property tests + lint
uv run pytest                # pytest + coverage + hypothesis
uv run ruff check .          # style + import + simplify lints
uv run mypy src/             # static type check
uv run bandit -r src/        # security scan (low-noise mode)
```

The full suite completes in about a minute. **~90 % line coverage** on
the default run; the threshold in `pyproject.toml` is `fail_under = 90`.
Uncovered code is exclusively the hardware-dependent paths that can't
be exercised without real devices:

- MuQ model load + GPU inference (requires the 1.5 GB checkpoint)
- `sounddevice.OutputStream` audio-out blocking loop
- ffmpeg ALAC transcode subprocess
- `librosa.beat.beat_track` + librosa internals
- WebSocket 1 Hz broadcast loop

These are marked `# pragma: no cover` with a one-line justification at
the call site so excluded code is auditable.

`sounddevice` is mocked globally in `tests/conftest.py` so all tests
pass on headless CI. `mutagen` tag readers are mocked at the module
level so audio file fixtures aren't needed. If you have a real beets
`library.db` at the project root, the integration tests will use it;
otherwise that test is automatically skipped.

---

## Manual model download

If the automatic model download fails (e.g. no internet access on the listening machine), download manually:

1. Visit [OpenMuQ/MuQ-large-msd-iter on HuggingFace](https://huggingface.co/OpenMuQ/MuQ-large-msd-iter)
2. Click **"Files and versions"** and download all files (~1.3 GB total)
3. Place them in `models/MuQ-large-msd-iter/` inside the AutoDJ project directory
4. Add to `config.toml`:
   ```toml
   [model]
   manual_path = "models/MuQ-large-msd-iter"
   ```

---

## Project structure

```
autodj/
├── config.toml          ← Edit this to set your music path and preferences
├── pyproject.toml       ← Dependencies and project metadata
├── src/autodj/
│   ├── cli.py           ← CLI entry point (index / play / serve / playlist / stats)
│   ├── config.py        ← Config loading from config.toml
│   ├── model.py         ← MuQ model loader + auto-download
│   ├── beets.py         ← Beets library.db reader
│   ├── indexer.py       ← Index builder: MuQ + librosa → FAISS
│   ├── similarity.py    ← FAISS query + next-song selection + discovery
│   ├── player.py        ← Crossfade playback + keyboard controls + M3U/history
│   ├── presets.py       ← BPM-shaping presets (built-in + user-defined)
│   ├── stats.py         ← Rich library statistics display
│   ├── server.py        ← FastAPI app + PlayerBridge + WebSocket broadcast
│   └── static/
│       └── index.html   ← Self-contained web UI (WCAG 2.2 AA)
├── tests/
│   ├── unit/            ← Fast, fully mocked tests per module
│   ├── integration/     ← Pipeline round-trip tests (real FAISS, mock model)
│   └── smoke/           ← CLI end-to-end tests
├── index/               ← Generated by `autodj index` (gitignored)
└── models/              ← MuQ checkpoint cache (gitignored)
```

---

## Troubleshooting

**`Index not found` when running `autodj play`**
→ Run `autodj index --limit 50` first.

**`Beets library not found`**
→ Set `beets_db` in `config.toml`, or leave it blank — AutoDJ will scan the filesystem instead.

**Model download fails**
→ See [Manual model download](#manual-model-download) above.

**Audio playback issues on Windows**
→ Ensure your default audio device is set in Windows Sound settings. AutoDJ uses `sounddevice`, which follows the system default.

**Very slow indexing on CPU**
→ Use `--limit N` to index in smaller batches, or index on the GPU machine (see [Indexing on a GPU machine](#indexing-on-a-gpu-machine)).

**Indexer skips all tracks / `0 new entries` when using beets**
→ `[library] music_dir` in `config.toml` does not match the local mount point of your beets library `directory`. Beets stores paths relative to that directory, so AutoDJ can't find the files. Set `music_dir` to the correct local path and try again.

**`NaN` or zero embeddings during indexing**
→ MuQ requires fp32 inference (this is the default). If you've modified `model.py` to use fp16, switch back to fp32.

---

## License

[MIT](LICENSE) — see the LICENSE file at the repo root.

## Project files

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to set up the dev env,
  run tests, and submit a PR.
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) — Contributor Covenant 2.1.
- [`SECURITY.md`](SECURITY.md) — how to report a vulnerability privately.
- [`CHANGELOG.md`](CHANGELOG.md) — release history.

---

## Credits

AutoDJ was built collaboratively by humans and AI assistants.  Each
contributor is named with the part of the work they led.

### Human contributors

- **[blindndangerous](https://github.com/blindndangerous)** — project
  vision, library design, requirements, UX direction (web UI flow, mode
  semantics, gapless feel), every accessibility decision, all real-world
  testing on a 10k-track library, every release call.
- **[jage9](https://github.com/jage9)** — additional contributions and
  feedback.

### AI assistants

- **Claude (Anthropic)** — paired-programming partner across the whole
  codebase: MuQ + librosa indexing pipeline, FAISS similarity engine,
  crossfade audio math + EQ-ducking, the 25 transition effects (CLI +
  AudioWorklet), the FastAPI + WebSocket web layer, the section-nav
  SPA, the gapless prefetch + silence detector, the `explain.py`
  music-genome reasoning, the harmonic Camelot rule set, the test
  suite (914 tests, ~90% coverage).  Every line was reviewed and
  guided by a human before it shipped.

If you contribute, add yourself here in the same shape as the rows
above.
