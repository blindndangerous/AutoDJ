# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Added
- Web UI search results now have **▶ Now** and **⏭ Next** buttons — queue any
  indexed track from the browser to play immediately or after the current track
- `POST /api/play-next` endpoint — accepts `{"path": "...", "now": bool}`;
  sets `PlayerState.queued_next` and optionally skips the current track

### Fixed
- Crossfade slice bug: `_play_with_crossfade` was passing
  `audio_b[:crossfade_samples + len(audio_a)]` to `_apply_crossfade`, causing
  every track to play at ~2× its actual length then replay from the start;
  corrected to `audio_b[:crossfade_samples]`

---

## [0.1.0] - 2026-04-11

### Added

**Core pipeline**
- `autodj index` — one-time library scanner that extracts a 784-dimensional audio fingerprint per track (768-dim MERT-v1-330M embedding + 16 librosa spectral/chroma features) and stores them in a FAISS flat index
- `autodj play` — continuous playback loop: picks the nearest sonically similar neighbor from the FAISS index, crossfades into it, and repeats indefinitely
- Incremental indexing — subsequent `autodj index` runs only process files not already in the index
- `--limit N` flag to index a small test batch before committing to a full library scan
- `--force` flag to rebuild the index from scratch

**Similarity engine**
- FAISS `IndexFlatIP` (cosine similarity on L2-normalized vectors) for sub-millisecond next-song lookup even at 100 k+ track scale
- No-repeat window — configurable sliding window that excludes recently played tracks from the candidate pool; falls back gracefully when the window is larger than the index

**Playback**
- Crossfade between tracks (configurable duration, default 3 s)
- Seed track support — `--seed "artist or title"` for fuzzy-matched starting point
- Dry-run mode (`--dry-run`) — prints track picks without playing audio
- Keyboard controls:
  - `Space` — Pause / Resume
  - `N` — Skip to next track
  - `Q` — Quit
  - `←` / `→` — Seek −10 s / +10 s
  - `↑` / `↓` — Volume up / down (5 % per step)
  - `M` — Mute / Unmute
- Rich Live status bar pinned to the bottom of the terminal showing now-playing, up-next, seek position, volume, and control hints; refreshes twice per second

**Configuration**
- `config.toml` with inline documentation for all options
- `beets_path_prefix` — strips the NAS internal path prefix from beets library paths and replaces it with the local `music_dir` mount point, fixing silent track-skip on NAS setups
- Optional HuggingFace token support (`hf_token`) for gated model downloads

**Model management**
- Automatic download of MERT-v1-330M checkpoint via HuggingFace Hub on first run
- Retry-with-timeout: up to 3 download attempts, 300 s timeout per attempt, 5 s delay between retries
- `manual_path` config option for air-gapped / pre-downloaded model checkpoints
- Automatic resampling of any input audio to 24 kHz before MERT inference (supports 44.1 / 48 / 96 kHz sources)

**Beets integration**
- Reads artist, title, genre, BPM, and year from a beets `library.db` for rich display and metadata-aware indexing; falls back to filesystem scan if beets is not configured

**Developer experience**
- Full test suite: unit, integration, and smoke tests (124 tests, all passing)
- Property-based tests for vector math (hypothesis)
- Windows UTF-8 fix — stdout/stderr reconfigured to UTF-8 at startup to prevent `UnicodeEncodeError` on box-drawing characters
- `pyproject.toml`-based project with `hatchling` build backend and `uv` lockfile
