# Changelog

What changed and when, written for the people who use AutoDJ.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.15.0] - 2026-05-07

The "make it feel like a real radio station" release.

### Added

- **Voice liners.**  Drop spoken clips into the configured folder; AutoDJ ducks the music and plays one over the top every now and then.  Pick how often (every N tracks, every N minutes, or a random window).  Upload + delete from the web UI.
- **Profile bundles.**  Save a whole session config (preset, BPM range, harmonic mode, transition mode, sync flags, voice-liner triggers) under a name, then load it back with one click.  Different from the `--name` flag, which scopes a separate music library.
- **Per-file dayparts.**  Each daypart can declare which library names it applies to, so a workout schedule only kicks in when the workout library is selected.
- **Beat- and key-synced transition effects.**  Rhythmic effects (echo, dub delay, beat repeat, gate stutter, sidechain pump, scratch) snap to the beat grid and span whole bars.  Oscillator effects (air horn, dub siren, ring modulator) tune to the song's root note.
- **Web seek bar.**  Click, drag, or arrow-key the progress bar.  Left/Right ±5 s, Shift+Arrow / PageUp+Down ±15 s, Home/End.
- **Beatmatch on skip.**  When enabled, pressing Skip mid-track pitches the incoming song to match the outgoing tempo, so manual interventions still groove.
- **Modular web UI.**  The 4700-line `app.js` was split into focused ES modules (lyrics, queue, hotkeys, transitions, audio engine, ...) and run through Vite for a minified production bundle.  Dev still works without Node.
- **Container quickstart.**  `Containerfile` + `compose.yaml` in the repo root.  `podman compose up` (or `docker compose up`) after `git clone` boots the web UI.
- **Background cue analysis.**  Cue points (drop, breakdown, phrase markers, intro / outro downbeats) now appear shortly after every track starts in the default browser-driven mode.
- **Default-on info logging.**  `autodj serve` prints a friendly ready banner, WebSocket connect / disconnect lines, and external-cue import results.  Pass `-v` to drop to debug.
- **Vitest unit tests for the JS modules.**  `npm test` runs them.
- **Build-stamp footer.**  The bottom of every page now shows the AutoDJ version, the short git commit, and when the bundled JS was built.  Useful when a browser is caching an old version and you want to confirm what the server is actually serving.
- **`/api/version` endpoint.**  Same three values as the footer, in JSON.  Tail the log or hit `curl /api/version` to verify the running build.
- **Advance log banner.**  Every track change prints one line: outgoing track, incoming track, BPM and Camelot key for both, and which picker mode was used (similarity / queue / shuffle / discovery).  Closes the gap where cue points were logged but tempo and key on track change were not.
- **BPM and key in the background-analysis log.**  When AutoDJ analyses a track in the background, the resulting log line now includes its BPM and Camelot key alongside the cues and intro / outro markers.

### Changed

- **Lyrics card lives on the Now Playing tab.**  It used to sit on the Settings tab; now it sits where the music is.
- **Hotkeys only fire on the Now Playing tab.**  On Settings or Library, arrow keys go back to navigating dropdowns.  The `?` shortcut still opens the help dialog from any tab.
- **Voice-liner options collapse together.**  The whole option pane hides until you tick the master "Enable voice liners" checkbox; no orphan settings on screen.
- **Library tools panel.**  Run index / enrich / prune / stats jobs from the web UI without leaving the page.
- **Clean Ctrl+C shutdown.**  `autodj serve` prints a single "Shutting down... / Server stopped cleanly." pair on exit and no longer leaves stack traces from the asyncio teardown on the screen.
- **Internal: `server.py` split.**  `PlayerBridge` moved into a private `autodj._bridge` module to keep both files under the 2000-line working budget.  Public API unchanged.

### Fixed

- Hotkeys no longer hijack arrow keys when focus is on a dropdown.
- Lyrics card no longer stays hidden in the default `serve` mode.
- Volume / EQ / search status messages no longer pile up at the bottom of the page; each one wipes itself a few seconds after it speaks.
- The `librosa` audio library is now a required dependency; missing it used to silently disable cue detection.
- Fresh installs no longer need to set the no-repeat window manually for tiny libraries; AutoDJ adjusts on its own and warns when the library is too small.

### Tests

- 1296 Python tests pass.  26 JavaScript module tests pass.  Cross-browser audit (Chromium / Firefox / WebKit) verified end-to-end on a NAS deployment.

---

## [0.14.0] - 2026-05-06

### Added

- **Multi-library support.**  Pass `--name workout` to build a separate index for a subset of your music.  Switch between named libraries on every command.
- **Mood arc.**  AutoDJ can warm up at the start of a session and cool down at the end, looping over a configurable number of hours.
- **Cue points from external DJ software.**  AutoDJ can import drop / breakdown / first-downbeat markers from Mixxx, Rekordbox (XML export), or Traktor on the same machine.
- **Smart shuffle and pure shuffle.**  Smart shuffle deliberately picks tracks that sound very different from what is playing.  Pure shuffle is plain random, ignoring similarity.
- **Mobile-friendly web UI.**  Single-column layout under 720 px wide; touch targets sized for fingers.
- **3-band EQ in the web UI.**  Real-time low / mid / high gain knobs and a Reset button.

### Changed

- **`autodj serve` defaults to browser-driven playback.**  The browser owns the audio output; the CLI player and the web UI never share a sound card.  Pass `--server-audio` for the old behaviour.
- **Logarithmic volume curve.**  Sliders now behave the way ears expect (−60 dB / −30 dB / 0 dB at 0 / 50 / 100 %).
- **Per-track checkpoint during indexing.**  Every successful embed is durable immediately; old behaviour saved every 500 tracks and could lose the last batch on a crash.

### Fixed

- HTTPS support via `--ssl-certfile` / `--ssl-keyfile` so AudioWorklet effects unlock on remote browsers.
- Cover art no longer logs a console error for every track without embedded art.

---

## [0.13.0] - 2026-05-05

### Added

- **Pro-DJ mixing layer.**  Harmonic mixing using the Camelot wheel (only mix tracks in compatible keys).  Beat-matching across the crossfade.  Outro / intro alignment.  Phrase alignment (snap to 32-bar boundaries).  Filter sweep.
- **Live BPM / key / energy / beatmatch badges** in the Now Playing card.
- **DJ metadata sidecar cache** so analysis runs once per track.

### Changed

- **35 transition effects** (echo, reverb tail, high-pass sweep, low-pass sweep, tape stop, gate stutter, noise riser, noise drop, backspin, forward spin, EQ swap, bitcrusher, flanger, pitch swell, pitch fall, telephone, chorus, submerge, vinyl wow, freeze, glitch, scratch, beat repeat, sidechain pump, reverse reverb, air horn, vinyl rewind, transformer, dub siren, stutter build, wow flutter, phaser, ring modulator, dub delay, halftime).  Plus random and rotate meta-modes.

### Fixed

- Effects that route through filters (high-pass / low-pass sweep) now stay audible across the crossfade.

---

## [0.12.0] - 2026-05-05

### Added

- **Reorderable queue in the web UI.**  Up / Down / Remove buttons on every queue row.  Keyboard accessible.
- **Genre-aware presets.**  Each preset can declare a `genres = [...]` filter so a Workout preset only picks workout-genre tracks.
- **Embedded album art** in the web UI.

### Fixed

- Text-style settings descriptions stripped of verbose `aria-describedby` so screen readers announce just the label.

---

## [0.11.0] - 2026-05-04

### Added

- **LRC lyric sidecars.**  If a `<basename>.lrc` file is next to an audio file, the web UI shows the full lyric list with the active line highlighted and announced via aria-live for screen readers.
- **EQ-ducked crossfade.**  An optional Butterworth high-pass filter on the outgoing track during the overlap, so two basslines do not fight each other.

---

## [0.10.0] - 2026-05-04

### Added

- **Web UI** (FastAPI + WebSocket).  Album art, scrolling lyrics, live BPM / key / energy badges, queue management, search across the library.
- **Stats command.**  `autodj stats` prints library size, average BPM, key distribution, energy histogram.

---

## [0.9.0] - 2026-05-04

### Added

- **Discovery mode.**  Every N tracks, AutoDJ deliberately picks something sonically distant from what is playing so the set does not calcify.
- **Hard BPM filter.**  `--bpm-range 90-130` excludes anything outside the window.
- **M3U export.**  Save the played history as a playlist file.
- **Play history file.**  Tab-separated record of every track played, with an ISO timestamp.

---

## [0.8.0] - 2026-05-04

### Added

- **Presets.**  Built-in profiles (`wakeup`, `chill`, `party`, `workout`).  Each preset shapes the BPM curve over time so a Wakeup set starts slow and ramps up.
- **Custom presets** in `presets/<name>.toml`.

---

## [0.7.0] - 2026-05-04

### Added

- **Beets integration.**  AutoDJ reads `library.db` for richer metadata (artist, title, genre, BPM, year) instead of re-scanning every file.
- **Cross-machine index portability.**  Build the index on a GPU box, copy it to a NAS, play from any other machine.

---

## [0.6.0] - 2026-05-04

### Added

- **Crossfade.**  Two-deck overlap with a configurable crossfade length (default 5 s) instead of a hard cut at end-of-track.
- **Keyboard controls** for the CLI player (Space, N for next, ← / → to seek).

---

## [0.5.0] - 2026-05-03

### Added

- **Prune command.**  `autodj prune` drops index entries whose audio files were deleted or moved.

---

## [0.4.0] - 2026-05-03

### Added

- **MuQ-large-msd-iter** as the default music model.  Replaces MERT-v1-330M.  Better scores on the MARBLE benchmark, no need for the heavy EnCodec tokenizer.

---

## [0.3.0] - 2026-05-03

### Added

- **Spectral and chroma features** appended to the model embedding (16 extra dimensions: tempo, chroma, spectral centroid, spectral rolloff, zero-crossing rate).  Helps the picker re-rank when raw cosine ties.

---

## [0.2.0] - 2026-04-30

### Added

- **FAISS index** with `IndexFlatIP` exact cosine search.  Scales to ~100 000 tracks without approximate-neighbour tuning.

---

## [0.1.3] - 2026-04-11

### Added

- First public release.
- Walks a music folder, extracts a per-track embedding, picks the next song by cosine similarity to the one currently playing.

---

## A note on accessibility

AutoDJ is built and maintained by a blind developer.  Every change to the web UI runs through an accessibility review before it ships.  If you find a screen-reader bug or a keyboard trap, please file an issue.
