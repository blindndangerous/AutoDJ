# AutoDJ

An AI-powered local music continuity player. Give it your music library and it plays songs that sound like each other, forever — no cloud, no subscriptions, fully offline.

Uses [MERT-v1-330M](https://huggingface.co/m-a-p/MERT-v1-330M) (a music-specific deep learning model trained on 160K hours of music) and [FAISS](https://github.com/facebookresearch/faiss) nearest-neighbor search to select and queue the next most sonically similar song.

---

## How it works

1. **Index** — AutoDJ scans your library, extracts a 784-dimensional audio fingerprint per song (768-dim MERT embedding + 16 librosa spectral/chroma features), and stores them in a FAISS index.
2. **Play** — It picks a seed song, finds its closest sonic neighbors, and plays them back-to-back with a configurable crossfade. Recently played songs are excluded from the candidate pool.

---

## Why MERT instead of CLAP?

[CLAP](https://github.com/LAION-AI/CLAP) aligns audio with *text descriptions* — great for text-to-audio retrieval, but the embeddings are optimized for matching audio to words, not music to music. [MERT](https://huggingface.co/m-a-p/MERT-v1-330M) is trained with music-specific self-supervised objectives (inharmonicity prediction, chord recognition, beat tracking) on 160K hours of music. Its embeddings capture musical structure directly, producing better music-to-music similarity.

---

## Prerequisites

- **Python 3.13+** — [Download here](https://www.python.org/downloads/)
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** — Fast Python package manager
  ```bash
  # Windows (PowerShell)
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```
- **~2 GB disk space** for the MERT model (downloaded automatically on first run)
- A music library in **MP3, FLAC, or M4A** format

**Optional but recommended:**
- [beets](https://beets.io/) — if you use beets, AutoDJ reads your `library.db` for rich metadata (artist, title, genre, BPM, year) without re-scanning files.

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/yourname/autodj
cd autodj

# 2. Install AutoDJ and its dependencies
uv sync

# 3. (Optional) Install dev/test dependencies
uv sync --extra dev
```

---

## Configuration

Edit `config.toml` to point to your music library. Every key has an inline comment explaining its purpose.

```toml
[library]
# Path to your music folder — local drive or NAS mapped drive letter
music_dir = "Z:/Music"

# Path to your beets SQLite library database (optional but recommended)
# Run `beet config | grep library` to find yours
beets_db = "C:/Users/you/.config/beets/library.db"

[playback]
crossfade_seconds = 3.0   # Crossfade duration between tracks (0 = instant cut)
no_repeat_window  = 50    # Don't replay any of the last N tracks

[model]
name = "m-a-p/MERT-v1-330M"  # Model used for audio embeddings
```

See `config.toml` for all options with full inline documentation.

### NAS / beets path remapping

If you use beets and your music lives on a NAS, beets stores paths using the NAS's internal path (e.g. `/volume1/Mike/Beetsmusic/...`), but your Windows machine mounts it differently (e.g. `Z:\Beetsmusic\...`). AutoDJ will silently skip every track unless you tell it how to remap the prefix.

Add `beets_path_prefix` to `config.toml`:

```toml
[library]
music_dir         = "Z:/Beetsmusic"
beets_db          = "C:/Users/you/.config/beets/library.db"

# Strip this prefix from beets paths, then prepend music_dir
# NAS internal path  → /volume1/Mike/Beetsmusic/Artist/Album/song.flac
# After remap        → Z:/Beetsmusic/Artist/Album/song.flac
beets_path_prefix = "/volume1/Mike/Beetsmusic"
```

The prefix is stripped from the beets path and the remainder is appended to `music_dir`. Forward slashes and trailing slashes are normalized automatically.

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

---

## Playing music

```bash
# Start from a random seed song
uv run autodj play

# Start from a specific song or artist (fuzzy search)
uv run autodj play --seed "Portishead"

# Override crossfade duration for this session
uv run autodj play --crossfade 5

# Override the repeat-prevention window for this session
uv run autodj play --no-repeat 100

# Dry run — print track picks without playing audio (good for testing)
uv run autodj play --dry-run
```

### Keyboard controls during playback

| Key       | Action                         |
|-----------|--------------------------------|
| `Space`   | Pause / Resume                 |
| `N`       | Skip to next track             |
| `Q`       | Quit                           |
| `←` / `→` | Seek −10 s / +10 s             |
| `↑` / `↓` | Volume up / down (5% per step) |
| `M`       | Mute / Unmute                  |

### Status bar

While playing, a persistent status panel is pinned to the bottom of the terminal:

```
  Now playing : Portishead — Glory Box                    [playing]  1:23 / 4:50
  Up next     : Massive Attack — Teardrop                 Vol: ████████░░  80%
  Controls: Space=Pause/Resume  N=Skip  Q=Quit  ←/→=Seek±10s  ↑/↓=Volume  M=Mute
```

The panel refreshes twice per second. All other output (skip messages, volume changes) prints above it so the panel is always visible at the bottom.

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
```

The web UI displays the currently playing track, the up-next track, playback state, and volume. All playback controls (pause/resume, skip, volume, mute) are available from the browser — keyboard controls continue to work in the terminal at the same time.

Live state is pushed to the browser via a WebSocket connection every second — no polling, no page refreshes.

---

## Running tests

```bash
# Fast unit tests only (no model downloads, no audio hardware needed)
uv run pytest tests/unit/ --no-cov

# Integration tests (real FAISS, mocked model)
uv run pytest tests/integration/ --no-cov

# Smoke tests (CLI end-to-end, all heavy parts mocked)
uv run pytest tests/smoke/ --no-cov

# Full suite with coverage report
uv run pytest
```

---

## Manual model download

If the automatic model download fails (e.g. no internet access on the listening machine), download manually:

1. Visit [m-a-p/MERT-v1-330M on HuggingFace](https://huggingface.co/m-a-p/MERT-v1-330M)
2. Click **"Files and versions"** and download all files (~1.3 GB total)
3. Place them in `models/MERT-v1-330M/` inside the AutoDJ project directory
4. Add to `config.toml`:
   ```toml
   [model]
   manual_path = "models/MERT-v1-330M"
   ```

---

## Project structure

```
autodj/
├── config.toml          ← Edit this to set your music path and preferences
├── pyproject.toml       ← Dependencies and project metadata
├── src/autodj/
│   ├── cli.py           ← CLI entry point (index / play / serve subcommands)
│   ├── config.py        ← Config loading from config.toml
│   ├── model.py         ← MERT model loader + auto-download
│   ├── beets.py         ← Beets library.db reader
│   ├── indexer.py       ← Index builder: MERT + librosa → FAISS
│   ├── similarity.py    ← FAISS query + next-song selection
│   ├── player.py        ← Crossfade playback + keyboard controls
│   ├── server.py        ← FastAPI app + PlayerBridge + WebSocket broadcast
│   └── static/
│       └── index.html   ← Self-contained web UI (WCAG 2.2 AA)
├── tests/
│   ├── unit/            ← Fast, fully mocked tests per module
│   ├── integration/     ← Pipeline round-trip tests (real FAISS, mock model)
│   └── smoke/           ← CLI end-to-end tests
├── index/               ← Generated by `autodj index` (gitignored)
└── models/              ← MERT checkpoint cache (gitignored)
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
→ Your beets database stores NAS paths that don't match your local mount point. Set `beets_path_prefix` in `config.toml` to the NAS internal prefix — see [NAS / beets path remapping](#nas--beets-path-remapping) above.

**Sampling rate warnings during indexing**
→ Warnings like `"The model was trained using a sampling rate of 24000"` are expected and harmless — AutoDJ automatically resamples your audio (44.1/48/96 kHz) to 24 kHz before feeding it to MERT. You can safely ignore these warnings.

**`use_return_dict is deprecated` warning**
→ This has been fixed in the current version. If you see it, make sure you're running the latest code (`git pull` then `uv sync`).
