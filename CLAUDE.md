# AutoDJ: local auto-DJ that listens to itself

## What it does

Indexes your music. Picks whatever sounds most like the song that's
playing. Plays it. Loops forever.

Fully offline. No cloud, no Spotify API, no scrobbling. Just your files
plus an optional beets database for richer tags.

## Approach: Pre-trained Embeddings + FAISS

### Stack
| Library | Purpose |
|---|---|
| `muq` | Pre-trained audio embeddings (`OpenMuQ/MuQ-large-msd-iter`, 1024-dim, 24 kHz, fp32) |
| `librosa` | Spectral / chroma features + resampling |
| `faiss-cpu` | Nearest-neighbor vector search (`IndexFlatIP`, exact cosine) |
| `numpy` | Vector math |
| `soundfile` / `sounddevice` | Audio decode + playback |
| `click` + `rich` | CLI + terminal UI |
| `fastapi` + `uvicorn` | Web UI server |
| `pynput` | Keyboard controls during playback |
| `mutagen` | ReplayGain tags, embedded album art, LRC sidecar parsing |
| `scipy` | Butterworth biquads for EQ-ducked crossfades |

### Model
- **MuQ-large-msd-iter** (Tencent, Jan 2025) — self-supervised music representation with Mel-RVQ tokenizer, ~300M params, SOTA on MARBLE music-understanding benchmarks.
- Replaced MERT-v1-330M (used in earlier versions) — MuQ is newer, designed to address MERT's heavy EnCodec tokenizer, and beats MERT on genre / instrument / structure tasks.
- Hard requirements: 24 kHz mono input, fp32 inference (fp16 may produce NaN).

## Architecture

```
library/
    song.mp3 ──► [MuQ]     ──► 1024-dim embedding
              ──► [librosa] ──► 16 spectral/chroma features
                            ──► concat + L2-normalize ──► 1040-dim FAISS vector

index/
    vectors.index   (FAISS IndexFlatIP)
    metadata.json   (per-track: path, title, artist, bpm, key, mode, energy, tempo_confidence, ...)

src/autodj/
    cli.py          CLI entry point (index / prune / play / serve / playlist / stats)
    config.py       config.toml + config.local.toml overlay; typed dataclasses
    model.py        MuQ loader + MuqWrapper.embed_array
    beets.py        Read-only beets library.db reader
    indexer.py      Build FAISS index; atomic save_index; prune_index w/ safety
    similarity.py   FAISS query, next-song selection, discovery, smart-shuffle
    player.py       Crossfade (linear or EQ-ducked) playback + keyboard + M3U
    audio_meta.py   ReplayGain tag read, embedded cover art, LRC parsing
    dj_meta.py      Intro/outro detect, beat grid, Camelot wheel, sidecar cache
    transitions.py  8 DJ transition effects (echo, reverb, riser, tape stop, ...)
    presets.py      BPM-shaping envelopes + optional genre filter
    stats.py        Rich library statistics
    server.py       FastAPI app + WS broadcast + queue/art/lyrics endpoints
    static/index.html  Self-contained web UI (now-playing, art, lyrics, queue)
```

## Pipeline
1. **Index** (one-time, slow): walk library → extract MuQ embedding + librosa features per track → concat + normalize → store in FAISS index. Incremental on subsequent runs (skips already-indexed paths).
2. **Query** (instant): look up the current track's stored vector in FAISS → top-N nearest neighbors → filter recently-played → optionally re-rank by preset BPM target → pick next.
3. **Playback loop**: play song → on finish (or hotkey skip) → query → crossfade into next.

## Beets path handling
Recent beets versions store track paths *relative* to the library `directory` setting (the `relative_path` migration). AutoDJ resolves them by prepending `[library] music_dir` for relative paths; absolute paths are used as-is. There is no NAS prefix-stripping config — `music_dir` must simply match the local mount point of the beets library root.

## Index portability (cross-machine)
Track paths in `metadata.json` are stored RELATIVE to `music_dir` (forward-slashed) so a single index built on one host runs on any other machine that mounts the library at a different absolute path. Per-machine overrides go in a sibling `config.local.toml` (gitignored). Legacy absolute paths can be remapped on the fly via `[library] path_remap`. Per-machine venv via `UV_PROJECT_ENVIRONMENT` keeps OS-specific binary wheels off the shared NAS tree.

## Safety
- `save_index` writes to `*.tmp` then `os.replace()` — partial writes (common on SMB / NFS) leave the existing on-disk index intact.
- `prune_index` raises `PruneSafetyError` when more than 20 % of the index would be removed in one pass (almost always indicates a misconfigured `music_dir`). Override with `--force`.

## Recommended setup
- **Python 3.13+**
- **Package manager**: `uv`
- **Indexing host**: machine with NVIDIA GPU recommended (CUDA accelerates the
  MuQ embedding pass massively).  CPU works but is much slower.
- **Listening host**: any machine with a sound device.  Can be the same as the
  indexing host, or a separate one that shares the index over a network mount.
- **Library on a network share**: fully supported.  See "Index portability"
  above for the per-machine overlay pattern.

## Status
- [x] Project scaffolded
- [x] Dependencies pinned in `pyproject.toml`
- [x] Index builder, similarity engine, crossfade player
- [x] Presets, discovery mode, BPM filter, M3U export, play history
- [x] Web UI (FastAPI + WebSocket) with search and queue
- [x] Stats command
- [x] 400+ tests, ruff + mypy + bandit + pre-commit
- [x] Migrated from MERT-v1-330M to MuQ-large-msd-iter
- [x] **Prune subcommand + auto-prune + safety threshold + atomic writes**
- [x] **Cross-machine index portability + `config.local.toml` overlay + `path_remap`**
- [x] **ReplayGain loudness normalisation (`[replaygain]` config, opt-in)**
- [x] **EQ-ducked crossfade (Butterworth high-pass on outgoing, opt-in)**
- [x] **LRC lyric sidecars (web UI scrolling + active-line aria-live)**
- [x] **Embedded album art in web UI**
- [x] **Genre-aware presets (`genres = [...]` filter)**
- [x] **Smart-shuffle mode (`--smart-shuffle`, opposite of similarity)**
- [x] **Reorderable web queue (Up/Down/Remove buttons, no drag-drop)**
- [x] **Pro-DJ mixing layer**: harmonic (Camelot), beatmatch, outro/intro align, phrase align, filter sweep
- [x] **3-band EQ in web UI** (low/mid/high real-time gain) + Reset
- [x] **Live BPM / key / energy / beatmatch badges in now-playing card**
- [x] **DJ-meta sidecar cache** (`index/dj_meta.json`) — lazy detect, never re-index
- [x] **26 transition effects** (echo_out, reverb_tail, highpass_sweep, lowpass_sweep, tape_stop, gate_stutter, noise_riser, noise_drop, backspin, forward_spin, cross_eq_swap, bitcrusher, flanger, pitch_swell, pitch_fall, telephone, chorus, submerge, vinyl_wow, freeze, glitch, scratch, beat_repeat, sidechain_pump, reverse_reverb, air_horn) + random / rotate meta-modes
- [x] **Non-worklet fallbacks for AudioWorklet effects** — `bitcrusher` falls back to a WaveShaper amplitude quantiser, `freeze` to a BufferSource grain loop, `glitch` to a random-slice BufferSource scheduler when `AudioContext.audioWorklet` is undefined (non-secure context, e.g. LAN HTTP).  Worklet effects stay audible without TLS at the cost of sample-accurate timing
- [x] **HTTPS support** — `autodj serve --ssl-certfile X.pem --ssl-keyfile X-key.pem` flips uvicorn into HTTPS so AudioWorklet unlocks on remote browsers.  Recommended generator: `mkcert` (installs a local CA, produces a trusted leaf cert per host)
- [x] **Crossfade ordering fix** — `applyTransitionFx` runs after `startCrossfade` schedules the baseline gain ramps so effect overrides on `deck.gain` are no longer wiped by `cancelScheduledValues(t0)`.  Filter-sweep effects override deck.gain to keep the affected track loud while filter character remains perceptible
- [x] **Cross-browser audit harness** — `tests/playwright/{transition_audit,health_audit}.mjs` drive Chromium / Firefox / WebKit via `@playwright/test` (Node).  `AUTODJ_URL` env var picks the target.  Reports gitignored (LAN paths)
- [x] **Cover-art probe via fetch()** — `loadCoverArt` no longer uses `<img>.onerror` (which logs a console error on every 404).  Tracks without embedded art now hide silently
- [x] **AudioWorklet** for sample-accurate effects: bitcrusher, gate_stutter, freeze, glitch
- [x] **`autodj enrich`** — refresh title/artist/album/genre/bpm/year/length/key from beets without re-embedding
- [x] **Genre normaliser** (`autodj.genres`) — preset filters now match across spelling variants (Electronic = EDM = IDM = Synthwave …)
- [x] **Mobile-friendly web UI** — single-column layout ≤ 720 px, WCAG 2.5.5 touch targets
- [x] **Multi-token search** across title + artist + album, default limit 100, `?limit=` clamped to 500
- [x] **Named indexes** — `<index_dir>/<name>/` layout, `--name` flag on every subcommand, `autodj list-indexes` enumerator
- [x] **Logarithmic volume curve** — perceptual dB-spaced fader (−60/−30/0 dB at 0/50/100 %)
- [x] **Per-track checkpoint** — every successful embed durable immediately (was every 500)
- [x] **Keyboard scope** — pynput global hook disabled in `serve` (browser handles controls)
- [x] **90 % test coverage** (`fail_under = 89` in pyproject after the 2026-05 feature push — effective coverage ≈ 89.9 %), hardware paths excluded via `# pragma: no cover`
- [x] **`serve` defaults to browser-driven playback** (`--no-playback` is default; `--server-audio` opts back into server-side audio output) — fully decouples web UI from CLI player so skip / volume / device changes only affect the local browser
- [x] **Gapless prefetch** — standby deck fetches the next track as soon as the server picks it; `<audio preload="auto">`; analyser-driven silence detector triggers the crossfade early when the active track ends in dead air
- [x] **Pure-shuffle mode** (`--pure-shuffle` CLI / web checkbox) — random next, ignores similarity.  Toggle off mid-set and similarity resumes from the current song
- [x] **Lyrics toggle** (`--show-lyrics/--no-show-lyrics`, `[playback] show_lyrics`, web checkbox) covering LRC, beets `lyrics` field, and embedded ID3 USLT / Vorbis LYRICS / MP4 ©lyr tags
- [x] **Auto-skip unplayable tracks** — browser auto-advances on any audio element error; CLI player silently moves to the next pick when load fails
- [x] **Browser titlebar updates** with `AutoDJ - artist - title - album` on every track change (also feeds OS Media Session)
- [x] **Audio device fix** — selection now uses `AudioContext.setSinkId` (Chromium / Edge) so Web-Audio-routed playback actually switches outputs; element-level `setSinkId` remains a Firefox fallback; permission-denied flow is recoverable (button re-enables, Permissions API auto-detects re-grants)
- [x] **Harmonic mixing combo box** — `harmonic_mode` config / dropdown with `off`, `compatible`, `strict`, `neighbour`, `mood_change`, `energy_boost`
- [x] **Outro-driven transition lengths** — server surfaces `outro_len` (length − `outro_start_s` from the DJ-meta sidecar) per track; browser sizes each transition effect to a per-effect fraction of that outro (reverb / echo / risers ~80 %, scratch / air horn / glitch ~25–35 %), clamped 1.0–12.0 s, with the static `_MIN_FX_DURATION_S` floor honoured.  Falls back to the legacy fixed-window behaviour when no outro is known
- [x] **Tablist section nav** — in-page section switcher converted from anchor links (which NVDA announced as "same-page link") to an ARIA tablist + tabpanels with roving tabindex, Left / Right / Up / Down / Home / End arrow navigation per APG, `aria-selected` instead of `aria-current`, and `hashchange` preserved for deep links + browser back / forward
- [x] **Volume slider WS-echo lockout** — fader now ignores WebSocket state echoes for 600 ms after a user-initiated change and inverts the perceptual gain curve (`_gainToSlider`) before writing the slider value, so arrow-key nudges no longer snap the fader to ~0
- [x] **Reverb IR cache + `_disconnectAll` helper** — synthetic impulse responses for `reverb_tail` / `submerge` / `reverse_reverb` are now memoised per `AudioContext` in a `WeakMap`, eliminating the ~1.5 MB stereo fp32 alloc + RNG fill on every crossfade.  Verbose multi-node `try { a.disconnect(); b.disconnect(); ... }` teardowns collapsed to per-node best-effort `_disconnectAll` so one failing disconnect cannot leak the rest.  Pattern lifted from `chat_grid`'s `client/src/audio/effects.ts`
- [x] **Mixxx-style `transition_mode`** (`[playback] transition_mode`) — four crossfade alignment modes: `full_intro_outro` (default; aligns outgoing outro_start with incoming intro_end, fade length = `min(outro_len, intro_end)` clamped 1-12 s), `outro_fade` (begin at outro_start, length = outro_len), `fixed_skip_silence` (fixed length, skip leading silence on incoming), `fixed` (legacy).  Server surfaces `intro_end_s` + `outro_start_s` per track so the browser and CLI player share the same logic
- [x] **Camelot wheel SVG** in now-playing card — 12-spoke / 2-ring (B outer, A inner) decorative visual aid showing the current key + harmonically compatible neighbours per the active `harmonic_mode`.  `aria-hidden` (live region already announces key); active sector marked with both fill *and* stroke width so it doesn't rely on colour alone (WCAG 1.4.1).  Honours `prefers-reduced-motion`
- [x] **Web + CLI players fully decoupled** — `_run_headless` parks on `_skip_event` indefinitely; no server-side auto-advance after `duration * 2.0`.  Browser owns every state transition.  `PlayerBridge.advance_now()` mutates state synchronously in dry-run mode; `/api/skip`, `/api/advance`, `/api/random-track` return the fresh state in one round trip
- [x] **Cue points** — `Cue` dataclass + `cues` list on `DjMeta`.  `detect_cues()` auto-emits `first_downbeat`, `drop`, `breakdown`, `phrase`, `outro_downbeat` from raw audio.  `autodj.dj_cues_import` imports cues from Mixxx (`mixxx.db`), Rekordbox (`Library.xml` export), Traktor (`collection.nml`); auto-discovers libraries on first cache use.  Conflicts resolve by source priority (user > DJ-software > auto) within a 250 ms window.  Surfaced in `/api/status` and rendered as decorative cue strip on the web progress bar with screen-reader summary in `#badges-announce`
- [x] **Mood arc** (`autodj.mood_arc`) — set-relative warmup → peak → cool envelope, anchored to enable time, loops every `mood_arc_hours`.  Six interpolation anchors mapped to `(low_bpm, high_bpm)` and `(low_energy, high_energy)` ranges
- [x] **Daypart restored** (`autodj.daypart`) — wall-clock built-in profiles morning / midday / afternoon / evening / night.  Picker target priority: explicit preset > mood arc > daypart


## Running
```bash
uv sync                              # install deps (per-machine; UV_PROJECT_ENVIRONMENT recommended for shared NAS code)
uv run autodj index --limit 50       # smoke-test on a small batch
uv run autodj index                  # full library (slow on CPU; run on GPU machine overnight)
uv run autodj prune                  # drop entries whose files were deleted/moved
uv run autodj play                   # start the auto-DJ
uv run autodj play --smart-shuffle   # invert similarity for genuinely surprising sequences
uv run autodj serve                  # web UI at http://127.0.0.1:8080
```
