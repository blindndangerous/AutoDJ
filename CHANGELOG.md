# Changelog

What changed and when, written for the people who use AutoDJ.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Fixed

- Every button in the Library tools panel — Index, Enrich, Prune, Stats — died about a second after
  being pressed. The job runner starts its child with `python -m autodj`, and the module that makes
  that work had never existed, so the only thing the job log ever showed was "No module named
  autodj.__main__". The child was also never told which config the server is running on, so once it
  did start, every job under a config file other than the default one failed with "Index not found".
- Screen readers described the cue points on the progress bar twice, once in the short form and
  once in full, because both copies were in the page for them to find.
- A saved audio output device that could no longer be selected re-announced its failure every time
  any USB device was plugged in or unplugged. It reports once now, and still reports again whenever
  you pick a device yourself.
- Lyrics stored in a file's own tag were classed as timestamped on the strength of a single
  bracket, which threw away every untimestamped line in the tag: a tag reading "Written by X /
  [00:00.00]Intro / Verse one / Verse two" showed only "Intro", and a lyric that merely mentions a
  time ("meet me at [10:30] tonight") had the time cut out of the middle. A tag is now judged as a
  whole, and anything that is not really an LRC file keeps all of its lines.
- The current-line highlight in the lyrics panel never fired, and the current line was never
  announced, because the position it keys off is reported by the server and the server's clock does
  not move while the browser is doing the playing. The panel follows the browser's own position now.
- A lost connection announced itself roughly three times every three seconds for as long as the
  outage lasted. It is now announced once when the link drops and once when it returns; the retry
  cycle in between is visible only, and retries back off from three seconds to a minute instead of
  hammering the server.
- Doing the same thing twice went unreported the second time: a second "Move up" on the same track,
  a repeated search returning the same number of hits, or a control that failed the same way twice
  all fell silent, which reads as the button not working.
- The History table pushed the page sideways on a phone, cutting off the Duration column.
- The Camelot wheel's ring key was too small to read, and the inner-ring numbers fell below the
  contrast floor when they landed on a highlighted wedge.
- On a phone each volume and EQ slider was drawn as a 44 px empty outlined box.
- The status message that appears at the foot of the page can now be dismissed with Escape, and
  failures are marked differently from confirmations.
- NVDA re-read the Library-tools status roughly every ten seconds for the whole life of a job,
  because the elapsed-seconds counter was inside the announcing region. The status now speaks once
  when a job starts and once when it finishes, whatever its length; the counter ticks on silently
  beside it. Three more places had the same shape: a lost connection re-announced itself on every
  three-second reconnect attempt, the seek slider read out its new position once a second while it
  held focus, and a repeatedly failing background request repeated the same sentence every four
  seconds.
- Every error and status message was invisible to sighted users — nine live regions were the only
  place a failure was reported, and all nine were clipped to one pixel. Clicking "Next" on a track
  that had been deleted from disk produced no visible response at all. The same message a screen
  reader announces is now also shown, once, in a status line at the foot of the page.
- Lyrics stored as timestamped text in a file's own tags (which is how most taggers write them)
  printed their raw `[00:17.49]` stamps on screen, arrived as one unscrollable block and could
  never highlight the current line. Embedded and beets lyrics are parsed the same way sidecar
  `.lrc` files always were, so they scroll and highlight; untimestamped prose is unchanged.
  Following the active line no longer scrolls the whole page.
- The Camelot wheel drew all twelve numbers half a sector out of position, on the boundary with
  the next wedge, so the wheel named the wrong key — and the highlighted number landed on an unlit
  neighbour where it measured 1.43:1 and was effectively invisible. Numbers now sit in their own
  sectors on both rings, the current one is readable on the lit wedge, and the middle of the wheel
  names the current key.
- Losing the connection wrote "Connection error" into the track-title slot while the artwork,
  metadata, badges, wheel and progress bar all carried on describing the previous track as live.
  The card says it is showing the last known state instead.
- The History view and the Library index-stats list shipped with no styling at all: a raw table
  with zero cell padding, so the time ran into the track title, and a definition list whose terms
  and values were indistinguishable. Both are styled now.
- Every `<select>`, number field and file picker rendered as a white 18 px system widget on the
  dark theme, a quarter the height of the buttons beside them.
- On a phone the settings help text was squeezed into a 162 px column indented 160 px from the
  left, queue and search rows truncated to identical strings, the volume and EQ sliders were 6 px
  tall with an egg-shaped knob, and the settings checkboxes were 18 px.
- Album art was removed from the layout when a track had none, so the title and the Camelot wheel
  jumped 111 px sideways on every track change.
- The cue markers on the progress bar were colour-coded with no visible key.
- The Settings discovery checkbox and the Now Playing Discovery button read different fields and
  routinely showed opposite states.
- Cards had no gap on desktop, the queue list and the empty-queue message started outside their
  card's padding, the "Refresh stats" button inherited the card's padding and sat out of line, the
  keyboard-shortcuts dialog opened in the top-left corner of the screen instead of centred, the
  voice-liner file list showed a heading followed by nothing, and Search Library was collapsed by
  default on the tab whose empty-queue message tells you to search.
- The search results list never said it had been capped at the server's first 100 matches.
- The History table announced itself as "Recently played tracks", which contradicted its visible
  "History" label and collided with the list of that name on the Queue tab.
- The web UI could be taken down for the rest of the session by one bad number. A settings request
  carrying `NaN` or `Infinity` was stored as-is, and every later status, settings and WebSocket
  update then failed to encode. Numeric settings requests are now rejected with a 422 and the
  player refuses non-finite values.
- Ten transition effects the Settings dropdown offered (halftime, dub delay, phaser, ring
  modulator, wow + flutter, stutter build, dub siren, transformer, vinyl rewind, pitch fall) were
  accepted by the server and then ignored, so the dropdown snapped back a second later. Every
  effect in the catalogue now works from the web UI and from `--transition`, and an unknown name
  returns 400 instead of being swallowed.
- `GET /api/history?per_page=0` returned a 500 and a negative page size returned an empty page.
  Pagination is now range-checked.
- Listing voice liners walked the whole configured folder tree on the event loop, so every other
  request waited for it. The walk now runs on a worker thread.
- Indexing compared the indexing host's clock against the file server's, so a NAS clock running
  ahead made every freshly embedded track look replaced and re-embedded the library on every run.
  Both sides of that comparison now come from the file.
- The server-side 3-band EQ restarted its filters on every audio block, which put a click at every
  block boundary as soon as a band left unity gain. Its filter memory is also cleared whenever the
  EQ starts working again after a stretch at unity gain, so re-engaging it no longer splices in a
  fragment of wherever it was last active.
- A library job whose output could not be decoded by the system locale stopped being read, filled
  its pipe and left the job slot busy until Stop was pressed. Job output is now read as UTF-8.
- Beets and Mixxx databases stored under a path containing `#` failed to open.
- Lyrics: enhanced-LRC per-word timestamps were read out literally by screen readers, and UTF-16
  `.lrc` sidecars (common from Windows tools) decoded to nonsense.
- The library search box carried `aria-expanded`, so screen readers announced "collapsed" and
  "expanded" on a plain search field with no popup. The result count still announces normally.

### Changed

- `POST /api/discovery` with a rate now starts discovery immediately rather than only configuring
  it. Setting a rate while leaving the feature switched off is what made the Settings checkbox and
  the Now Playing button disagree.
- `autodj serve --no-playback` is deprecated. It could never be turned off, so it never did
  anything: server-side audio is opted into with `--server-audio`, which is unchanged. The flag
  still parses, is hidden from `--help`, and logs one informational line, because the container
  image, both compose services and three workflows still pass it. A test now checks every flag
  those files use against the options the CLI actually declares.
- `pre-commit` now runs the same locked `ruff` and `mypy` that CI runs, instead of separately
  pinned mirrors that could disagree with it.
- Dependencies moved to their current releases. Direct ones are all minor or patch, except
  `torchvision` 0.28 to 0.29, which is a 0.x minor and so may change anything. Transitively the
  lock also moved `evdev` 1.9.3 to 2.0.0 — a major; it is Linux-only, arrives as an sdist and is
  compiled on the machine that installs it, so Windows and macOS never see it and CI is what
  proves it — and the 0.x packages `tokenizers` 0.22.2 to 0.23.2, `numba` 0.66 to 0.67,
  `llvmlite` 0.48 to 0.49 and `ast-serialize` 0.8 to 0.10. `cloudpickle` is a new transitive
  dependency and `uc-micro-py` is no longer resolved. The lock pins `torch` 2.14.0; each platform
  wheel carries its own local version on top (a Windows CPU install reports 2.14.0+cpu).
- `config.toml.example` now lists every `[playback]` and `[model]` setting the loader accepts,
  including the liner triggers, the FX sync toggles and `dayparts_dir`.

## [0.16.1] - 2026-08-16

### Fixed

- Documented that the default MuQ model weights are CC-BY-NC 4.0. AutoDJ never redistributes them,
  but commercial use needs a different checkpoint or permission from the publisher. The copyleft
  dependencies pulled by the `play` and `all` extras are now disclosed too.
- Released SBOMs listed no components. The generator scanned the built `dist/` directory, saw two
  opaque archives, and found no package manifests; it now scans the checkout.
- Documented the published release artifacts and how to install a tagged wheel with uv.

### Changed

- CI now rejects AGPL-licensed Python dependencies instead of only printing a license inventory.

## [0.16.0] - 2026-08-15

### Added

- Added browser pairing for LAN servers. Operators share an 8-digit code instead of the server
  secret. Each browser receives its own device record and 90-day HttpOnly session, with CLI
  commands to list, revoke, or reset paired browsers.
- Added `autodj doctor` with text and JSON reports, token redaction, and read-only checks for
  configuration, storage, index consistency, dependencies, model cache, network exposure, and web
  bundle identity.
- Added versioned `autodj backup` and `autodj restore` workflows for stopped or online backups,
  classified archive manifests, staged restoration, and post-restore doctor validation.
- Added [operations guide](docs/operations.md) for no-config startup, container ownership,
  loopback and authenticated LAN deployment, Windows PowerShell, recovery, and upgrades.

### Changed

- LAN authentication no longer accepts the server secret from a browser or accepts old session
  cookies. Existing browsers must pair again after upgrading.
- Container now runs as UID/GID 10001, publishes default service on host loopback, and requires
  explicit authenticated or acknowledged-insecure LAN configuration.
- CI enforces 99.1% line coverage and 94.7% branch coverage, both Python type checkers, locked
  frontend installation, and container scanning.
- The release workflow reruns CI and security checks, then verifies tag, project, changelog, and
  wheel identity before publishing.

### Fixed

- Corrected the threat model, which still described the removed token-exchange login endpoint. It
  now documents pairing-code derivation, device-bound sessions, and immediate device revocation.

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
- **Key-notation picker.**  Settings tab now carries a <em>Key notation</em> combo box.  Pick <em>Camelot</em> (8A / 8B, the DJ-software default) or <em>Musical</em> (C, Am, F#m).  When Musical is on, a sibling <em>Use flats</em> checkbox switches accidentals between sharps (C#) and flats (Db).  The Camelot wheel SVG below the now-playing card always renders in Camelot positions because the wheel is Camelot-shaped — only the badge text + advance log line follow your choice.

### Changed

- **Lyrics card lives on the Now Playing tab.**  It used to sit on the Settings tab; now it sits where the music is.
- **Hotkeys only fire on the Now Playing tab.**  On Settings or Library, arrow keys go back to navigating dropdowns.  The `?` shortcut still opens the help dialog from any tab.
- **Voice-liner options collapse together.**  The whole option pane hides until you tick the master "Enable voice liners" checkbox; no orphan settings on screen.
- **Library tools panel.**  Run index / enrich / prune / stats jobs from the web UI without leaving the page.
- **Clean Ctrl+C shutdown.**  `autodj serve` prints a single "Shutting down... / Server stopped cleanly." pair on exit and no longer leaves stack traces from the asyncio teardown on the screen.
- **Internal: `server.py` split.**  `PlayerBridge` moved into a private `autodj._bridge` module to keep both files under the 2000-line working budget.  Public API unchanged.
- **Pick mode.**  The <em>Entropy walk</em> + <em>Random walk</em> checkboxes in Settings collapsed into a single <em>Pick mode</em> combo box (Similarity / Entropy walk / Random walk).  Same picker behaviour, fewer ways to get into a contradictory state.
- **Settings descriptions trimmed.**  Removed the "Default: on / off" trailers from checkbox descriptions — the checkbox itself is the state, the tail of the sentence was noise.  Discovery checkbox now mentions that the Now Playing tab's <em>Discovery</em> button mirrors it.
- **Dropped the <em>Clear BPM filter</em> button.**  Empty Min and Max fields now clear the filter on their own; no extra button needed.
- **Internal: integration test split.**  `tests/integration/test_server.py` crossed the 2000-line working budget.  Recent additions (version stamp, advance log banner, key notation, repick failure paths, bridge re-export) extracted into `test_server_recent.py`.  Shared mock-builders and the `client` / `bridge` fixtures live in `_helpers.py` + `conftest.py` so neither file duplicates setup.
- **Internal: dead-code + dep-audit + JS lint hooks.**  Added `vulture` (Python dead-code), `deptry` (Python dep-declaration audit), and `eslint` (JS lint) to the pre-commit suite.  All three are wired into `[dependency-groups.dev]` / `package.json` and run on every commit alongside `ruff` / `mypy` / `bandit`.  Run individually with `uv run vulture src/autodj`, `uv run deptry src/autodj`, `npm run lint`.

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
