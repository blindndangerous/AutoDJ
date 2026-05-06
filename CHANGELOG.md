# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.12.1] - 2026-05-05

### Fixed

- **Volume slider arrow-key snap-to-0.**  The WebSocket state echo
  broadcasts the *post-curve* perceptual gain (50 % slider ≈ 0.0316
  gain), but the client was writing `Math.round(gain * 100)` straight
  back into the slider.  Every Up/Down arrow press kicked off a
  POST → WS-echo round-trip that snapped the fader to ~0.  The echo
  is now run through `_gainToSlider` (inverse of the fader curve)
  and ignored for 600 ms after a user-initiated change so the
  in-flight POST cannot fight the input.
- **NVDA "same-page link" announcement on the section nav.**  The
  in-page section switcher used `<a href="#now">` etc., so NVDA
  announced every tab as a same-page link even though the SPA
  swapped panels in place.  Switcher is now an ARIA tablist
  (`role="tablist"` + `<button role="tab">` + `role="tabpanel"`)
  with roving tabindex and Left / Right / Up / Down / Home / End
  navigation per the APG.  `aria-selected` replaces `aria-current`;
  `hashchange` is preserved so deep links + browser back / forward
  still work.  Volume Up/Down shortcut now skips when focus is on a
  tab so the tablist owns its own arrows.

### Changed

- **Per-effect transition lengths driven by the outgoing track's
  outro.**  Server surfaces `outro_len` on each track dict (track
  length minus the DJ-meta `outro_start_s` when the sidecar has
  analysed the track).  Browser uses a per-effect outro-fraction
  table to size each effect to the actual outro instead of the
  fixed crossfade window:  reverb / echo / risers fill ~80 % of the
  outro, punctuating effects (scratch, air horn, glitch) take
  ~25–35 %.  Result is clamped to 1.0–12.0 s and never falls below
  the existing `_MIN_FX_DURATION_S` floor.  Falls back to the static
  table when no outro is known (track not yet DJ-meta analysed).

### Tests

- 967 passing / 8 skipped after the tablist conversion + outro-len
  surfacing.  No new failing paths.

---

## [0.12.0] - 2026-05-05

### Added

- **`serve` now defaults to browser-driven playback** so the web UI and
  the CLI `play` loop never share an audio output.  Skipping / volume /
  device changes only touch the local browser process.  Pass
  `--server-audio` (or run `autodj play` separately) to opt back into
  server-side audio.
- **Gapless prefetch.**  As soon as the server picks the next track,
  the standby deck calls `<audio>.load()` so the bytes are buffered well
  before the crossfade fires.  `preload="auto"` on both decks.  A new
  Web-Audio analyser tap on each deck spots silence past the half-way
  mark and triggers the crossfade early — no more dead air at the end of
  long fade-outs.
- **Pure-shuffle mode** (`--pure-shuffle`, web *Pure shuffle* checkbox,
  `/api/playback-settings`).  Random next pick, ignores similarity.
  Toggle off mid-set and similarity resumes from the song that's
  playing — so you can wander into a track you like, then lock in.
- **Lyrics display toggle** (`--show-lyrics/--no-show-lyrics`,
  `[playback] show_lyrics`, web *Show lyrics* checkbox).  Default on.
  Resolution order: sibling `.lrc` → beets `lyrics` field →  embedded
  ID3 USLT / Vorbis LYRICS / MP4 `©lyr` tags.  Plain lyrics now also
  print to the CLI panel once per track.
- **Browser titlebar** updates to `AutoDJ - artist - title - album` on
  every track change (also feeds the OS Media Session).
- **Auto-skip unplayable tracks.**  Browser advances on any audio-element
  error; the CLI player keeps moving when `load_audio` fails.
- **Audio device fix.**  Output device selection now uses
  `AudioContext.setSinkId` (Chromium / Edge) so the live Web-Audio
  crossfade graph actually switches outputs (element-level `setSinkId`
  is bypassed once Web Audio is in play).  Element-level fallback
  remains for Firefox 116+.  The mic-permission button is recoverable —
  no more one-click lockout when the user denies the prompt; the
  Permissions API listens for re-grants from the browser's site
  settings.
- **Harmonic mixing combo box** replacing the old single bool.  Modes:
  `off`, `compatible`, `strict`, `neighbour`, `mood_change`,
  `energy_boost`.  Picks how strict the Camelot wheel filter is.

### Tests

- 27 new tests covering the new lyric tag fallbacks, harmonic-mode
  rules, pure-shuffle pick path, lyrics toggle, and `set_djmix`
  branches.  866 passing / 8 skipped, coverage ≈ 89.9 %.

---

## [0.11.2] - 2026-05-04

### Fixed

- **Web audio device dropdown stuck on system default in Firefox.**  The
  selector now detects `HTMLMediaElement.prototype.setSinkId` support
  upfront and disables itself with a tooltip explaining why on browsers
  without it (Safari, old Firefox).  On supported browsers without
  microphone permission the device dropdown shows generic `Output 1`,
  `Output 2` labels plus a **Show device names** button — one click
  triggers a brief mic permission request (immediately released, no
  audio captured) so real labels populate.  `setSinkId` calls also use
  Firefox-strict argument handling (empty string then `undefined` fall-back).
- **Effects-list description block in the web UI** was missing entries
  for `noise_drop`, `forward_spin`, `chorus`, `submerge`, `vinyl_wow`,
  `freeze`, `glitch`, `scratch`, `beat_repeat`, `sidechain_pump`,
  `reverse_reverb`, and `air_horn`.  All 25 effects are now documented
  in the dropdown's `aria-describedby` text.

---

## [0.11.1] - 2026-05-04

### Fixed

- **Auto-migrate pre-0.9 flat-layout indexes.**  Older builds wrote
  `<index_dir>/vectors.index` + `<index_dir>/metadata.json` directly.
  Post-0.9 expects `<index_dir>/<name>/...`.  `load_index` now slides
  the old files into `<index_dir>/default/` automatically on first
  read so users don't have to re-index after upgrading.  Sidecars
  (`dj_meta.json`, `web_state.json`) move with them.
- **Clearer "index not found" error.**  Now spells out the expected
  paths and points at `autodj list-indexes` so the named-index layout
  is less of a surprise.

---

## [0.11.0] - 2026-05-04

### Added — concurrent index + serve

- **Hot-reload watcher** in `serve` polls `metadata.json` mtime every
  10 s.  When `autodj index` writes a new track, the running server
  picks it up automatically — no restart, the next track pick consults
  the fresh index.
- **`SimilarityIndex.reload_from_disk()`** — re-reads vectors +
  metadata in place, refreshes the path-to-index lookup.
- **`PlayerBridge.reload_index_from_disk()`** — server-side wrapper
  that the watcher calls on a worker thread.

### Added — sound card selection

- **`autodj list-devices`** — enumerates every sounddevice output
  device with index, name, channels, sample rate.  Marks the system
  default with `*`.
- **`--device` flag** on `autodj play`.  Accepts an int (sounddevice
  index) or a substring of the device name (e.g. `"USB Headphones"`).
- **`[playback] audio_device`** config — permanent default for CLI playback.
- **Web UI device dropdown** uses `navigator.mediaDevices
  .enumerateDevices()` + `audio.setSinkId()`.  Selection persists in
  `localStorage` per browser; the dropdown refreshes on USB plug /
  unplug via the `devicechange` event.

### Added — ID3 tag fallback

- **`autodj.audio_meta.read_file_tags`** — when no beets DB is
  configured the indexer now reads ID3 / Vorbis / MP4 atoms via
  mutagen for title / artist / album / genre / BPM / year / length.
  Tracks without tags fall back to filename for the title field only.
  BPM, energy, and key are still derived per-track by librosa during
  indexing — these are *audio analysis*, not tag lookups.

---

## [0.10.0] - 2026-05-04

### Added

- **Presets sidecar** — user BPM presets now live in `presets.toml`
  next to `config.toml` instead of nested under `[presets.*]` in
  the main config.  Easier to share / version independently.
  Bare top-level tables OR legacy `[presets.<name>]` wrapped form
  both supported; falls back to inline form in `config.toml` if
  the sidecar is missing.  See `presets.toml.example`.

### Changed

- **CLI / web player decoupled.**  The persisted state file used by
  the web UI is renamed `web_state.json` (was `runtime_state.json`)
  and the CLI `play` command no longer reads it.  CLI playback is
  driven entirely by config + command-line flags so toggles in the
  browser can never leak into terminal sessions.
- **Index name validation.**  `--name` now rejects path separators
  (`/`, `\`), `..`, and leading `.` with a clear error message.
  Previously `--name index/metadata.json` silently produced
  `index/index/metadata.json` which then failed obscurely.

---

## [0.9.0] - 2026-05-04

### Added — named indexes

- **Multi-index support.**  AutoDJ now stores each named index in its
  own sub-directory: `<index_dir>/<name>/vectors.index` etc.  Build a
  "workout" index of high-BPM tracks alongside a "chill" evening one
  and switch between them per session.  Each named index gets its own
  FAISS file, metadata, runtime state, and dj-meta cache.
- **`--name <NAME>` flag** on `index`, `play`, `serve`, `prune`,
  `enrich`, `playlist`, `stats`.  Default name = `default`.
  `[index] name` in `config.toml` sets the always-on default.
- **`autodj list-indexes`** — enumerates every named index found
  under `[index] index_dir` with track count + active marker.

### Changed — playback feel

- **Logarithmic / perceptual volume curve.**  Slider 0-100 % now maps
  through a standard audio-fader dB curve (−60 dB at 0, −30 dB at 50,
  0 dB at 100) instead of a linear amplitude.  The 0-50 % range is
  now actually useful — humans hear loudness as log of amplitude.
  Server receives the perceptual gain so CLI + browser stay synced.

### Fixed — keyboard scope

- **Global key hook disabled in `serve` mode.**  pynput's listener is
  a system-wide hook — keys typed in OTHER apps / browser tabs were
  pausing / skipping / muting the player.  Browser is the intended
  control surface in `serve`, so the listener is now skipped.  CLI
  `play` mode keeps the hook (terminal control still works).
- **Checkpoint cadence** dropped from 500 tracks → every track.  Atomic
  writes (tmp + `os.replace`) make every successful embed durable
  immediately, so a parallel `serve` process can read newly-indexed
  tracks the moment they land.

---

## [0.8.0] - 2026-05-04

### Added — quality-of-life polish

- **Daypart mood profiles** (`autodj.daypart`).  When
  `[playback] enable_daypart = true`, AutoDJ picks BPM + energy targets
  from the local clock (morning / midday / afternoon / evening / night).
  Same idea as a radio station's clock-driven music rotation.  Custom
  windows in `[dayparts.<name>]` TOML.  Disabled if an explicit
  `--preset` is set so preset session curves still take priority.
  Surfaced via `--daypart` flag (play + serve), web UI settings toggle,
  persisted across restarts.
- **Genre normaliser** (`autodj.genres`).  Free-text genre strings are
  folded into canonical buckets ("Electronic / EDM / IDM" → `electronic`,
  "Indie Rock" / "Alt Rock" / "Alternative" → `rock`, etc.).  Preset
  `genres = [...]` filters now match across all spelling variants
  without rewriting library tags.
- **Mobile-friendly web UI**.  Layout collapses to single column on
  screens ≤ 720 px.  WCAG 2.5.5 compliant — every button, slider thumb,
  form field is ≥ 44 × 44 CSS px.  Settings card collapsed by default on
  phone, keyboard-hint footer hidden (no keyboard), cover art shrinks
  but stays prominent.
- **Server URL hint by host.**  `autodj serve` now prints a host-aware
  hint below the URL — `127.0.0.1` says "this machine only", `0.0.0.0`
  says "all interfaces, use the actual LAN IP from a remote browser".

### Changed

- **Default `playback.no_repeat_window`** raised 50 → 150.  Avoids the
  "stuck in 20 tracks" feel on tightly-clustered libraries.
- **Default similarity `n_candidates`** bumped 10/25 → 30/50.  Wider
  candidate pool gives the chooser more options.

---

## [0.7.0] - 2026-05-04

### Added — AudioWorklet effects

- **`stutter-worklet.js`** — sample-accurate gate stutter with raised-cosine
  fades on every edge.  Replaces the GainNode-scheduling implementation of
  `gate_stutter`, eliminating clicks at hard 1→0 transitions.  Parameters:
  `rate` (1–32 Hz), `duty` (0.05–0.95), `edgeMs` (0.5–20).
- **`freeze-worklet.js`** — new transition effect.  Captures the last
  ~100 ms of the outgoing track on first audio-thread frame, then loops
  the captured grain with a seam crossfade and configurable fade-out.
  Worklet-only (no native Web Audio equivalent).
- **`glitch-worklet.js`** — new transition effect.  Maintains a 2 s ring
  buffer of recent input; on each slice boundary picks either pass-through
  or a random offset within the buffer for the next slice, with seam
  crossfades.  Worklet-only.
- Python ports of `freeze` and `glitch` in `transitions.py` so CLI
  playback gets parity with the browser.
- New `freeze` / `glitch` entries in TransitionFx enum, web UI dropdown,
  CLI `--transition` Click choice, server validator allowlist, and
  app.js `_resolveTransition` real-effect catalogue.

### Changed

- Coverage threshold raised: `fail_under = 90` in `pyproject.toml`
  (was 80).  Hardware-only paths now use `# pragma: no cover` with a
  one-line justification at each call site.
- `_play_with_crossfade` split into 9 focused helpers — orchestrator
  is now ~30 readable lines instead of 270.
- `runtime_state.save_from_player` lost its unused `player` parameter.
- Broad `except Exception` clauses narrowed at module boundaries
  (audio_meta, beets, server) to catch only the actual exceptions
  those libraries raise.
- `apply_eq` / `make_eq_filters` now take/return `dict[str, Any] | None`
  instead of `object` for proper type-checking.

### Added — five new transition effects

- **`scratch`** — turntablist back-and-forth sweep over a 250 ms slice,
  4 passes alternating forward/reverse with sine-shaped rate envelope.
  The classic "wikka wikka" sound.
- **`beat_repeat`** — Pioneer DJM "Loop Roll" / Mixxx "Beat Loop" —
  captures a 250 ms slice and stamps it 8 times across the tail.
  Each retrigger has sharp attack + decay so hits punch.
- **`sidechain_pump`** — rhythmic 4-on-the-floor amplitude duck at
  120 BPM with exponential recovery between beats.  Models the EDM
  side-chain compressor sound without needing a kick drum source.
- **`reverse_reverb`** — reverse'd reverb tail that swells INTO the
  cut point.  Wash crescendos right up to where the new track lands.
- **`air_horn`** — synth dub-siren with 220-880 Hz square-wave pitch
  sweep, lowpass at 3.5 kHz.  Mixed with the music, not replacing it.

### Changed — search

- **Multi-token query support** — `/api/search?q=` now splits on
  whitespace; every token must appear somewhere in
  `title + artist + album`.  Examples:
  - `q=portishead+mysterons` → matches title=Mysterons artist=Portishead
  - `q=dummy+sour` → matches album=Dummy title=Sour Times
  - `q=thom+yorke+amok` → matches Atoms For Peace album=Amok
- **Result limit** — default raised 20 → 100, configurable via
  `?limit=` (clamped to 500 server-side).

### Removed

- **`distortion` effect** — dropped at user request (felt redundant
  with bitcrusher).  Removed from `TransitionFx`, dispatcher,
  `transitions.distortion` Python function, CLI Click choices, server
  validator, web UI dropdown, app.js branch, tests.

### Fixed (worklet wiring + media-element bugs)

- **Per-worklet readiness flags.**  `Promise.all` over four worklet
  loads meant a single 404 silently disabled all four effects.  Now
  each worklet has its own `_workletReady.<name>` flag set
  independently — bitcrusher works even if glitch failed to load.
- **`vinyl_wow` and `pitch_swell` now actually shift pitch.**  The
  HTMLMediaElement `preservesPitch` property defaults to **true** in
  every browser — that's why setting `playbackRate` was just changing
  tempo, not pitch.  Both effects now flip `preservesPitch=false`
  on entry and restore on teardown.
- **Freeze worklet was capturing only one 128-sample audio block (~3 ms)
  instead of the configured 150 ms grain.**  Process is called per-
  block, not per-grain — original code grabbed the first block then
  looped it forever (effectively silent on most material).  Capture
  now accumulates across ~50 blocks until the grain is full, passing
  input through during capture so the start of the freeze isn't a
  sudden silence.
- **Glitch worklet was outputting silence for the first 0.5 s** because
  its ring buffer was zero-initialised and random-pick slices read
  uninitialised data.  Slices now force straight-pass until the ring
  has accumulated enough audio to source from.
- **Freeze browser handler set `audio.muted=true`,** which silences the
  signal feeding `MediaElementSource` upstream — so the worklet
  captured silence and looped nothing.  Removed the `muted=true` line;
  zeroing `deck.gain` (downstream of the source tap) is sufficient.
- **`reverb_tail` wet path** now has a +6 dB pre-convolution send and
  longer-decay 4 s IR; previous mix was inaudible.

### Fixed (browser audibility)

- **Effects were getting silenced by the crossfade gain ramp.** All
  outgoing-tail effects (bitcrusher, distortion, telephone, vinyl_wow,
  reverb_tail, echo_out, freeze, glitch, spin, tape_stop) routed
  through `deck.gain` which fades to 0 over the crossfade.  Result:
  user heard at most the first 30 % of the effect before silence took
  over.  Two-pronged fix:
  1. **Wet / synth paths bypass `deck.gain`** — reverb tail, echo
     throw, freeze, glitch, tape_stop and backspin/forward_spin now
     route directly to `ctx.destination` with their own gain envelopes
     so the effect peaks at full volume.
  2. **Modulating effects peak in first 50 %** — bitcrusher and
     distortion ramp to maximum intensity at `fadeSec * 0.5` and
     hold, so the heaviest crush is heard while the deck is still
     loud rather than at the silenced end.
- **Pre-decode standby track on load** — `setSrcOnDeck` kicks off a
  background `_decodeFor(path)`.  Spin / tape_stop fire instantly when
  the crossfade starts instead of waiting 500 ms-2 s for the fetch +
  decode round-trip to complete.
- **Telephone effect** had only a static HP+LP band — now adds tanh
  saturation + drive, narrower band (500-2800 Hz, Q=3), genuinely
  sounds like a phone now.
- **Vinyl wow** modulation depth doubled (5 %→25 % vs old 2 %→12 %),
  poll rate raised from 33 Hz to 60 Hz so wobble is unmistakable.
- **Distortion** drive ceiling raised from 4× to 6×, curve k from 25
  to 50 — was barely audible at the old levels.

### Changed

- **Track-loop variety.**  `playback.no_repeat_window` default raised
  50 → 150 (avoids cycling through the same 20-track sonic island).
  `n_candidates` in similarity search bumped 10/25 → 30/50 so the
  pick has more options to draw from.

### Fixed

- **Backspin direction** — was a linear-accelerating rate (0.7→2.5),
  which sounds like an upward pitch sweep, not a vinyl backspin.  Now
  uses decelerating quadratic envelope (2.0→0.05) matching real
  friction physics.  Browser side mirrors the same envelope.
- **Per-effect minimum runway** — long-tail effects (tape_stop,
  backspin, reverb_tail, noise_riser, freeze) were rushed when the
  user's crossfade was 2-3 s.  Both CLI and browser now extend the
  outgoing runway to the industry-standard minimum (Pioneer DJM /
  Reloop / Numark defaults — see README "Effect timing" table).
- **`autodj serve` URL message** — host-specific hint added below the
  URL: `127.0.0.1` says "this machine only", `0.0.0.0` says "all
  interfaces, use the actual LAN IP from a remote browser".
- ID3 ReplayGain peak lookup was rewriting both `gain` tokens in the
  frame key (`replaygain_track_gain` → `replaypeak_track_peak`), so MP3
  ReplayGain peaks were silently never read.  Now uses suffix-only swap.
- AudioWorklet routes now register **before** the `/static` mount so
  the cache-busting `Cache-Control: no-cache` headers actually apply
  (previously StaticFiles intercepted them).
- Worklet served at `/bitcrusher-worklet.js` / `/stutter-worklet.js` /
  `/freeze-worklet.js` / `/glitch-worklet.js` instead of nested under
  `/static/` — cleaner URLs + cache-control control.

---

## [0.6.0] - 2026-05-04

### Added — browser playback (server can run headless)

- **Web Audio API two-deck crossfade** in the browser.  Two `<audio>`
  decks feed `MediaElementSource → GainNode → destination`; near
  end-of-track the standby deck pre-loads `next_track` and the gains
  ramp linearly for a real DJ-style transition (not the browser's
  default hard cut).  Crossfade length follows `[playback]
  crossfade_seconds` from server state.
- **Media Session API** — wires up OS media keys, lock-screen art, and
  the notification-shade transport on Chromium / WebKit / Firefox.
  Cover art comes from the `/api/art` endpoint.
- **Keyboard shortcuts** in the browser UI: `Space` = Pause/Resume,
  `N` = Skip, `M` = Mute, `Up`/`Down` = Volume ±5 %.  Inputs and
  selects keep their normal behaviour.
- **Auto-detect headless mode** — when `serve` boots without
  `sounddevice` / `soundfile` (typical on a NAS), the player flips to
  browser-driven mode automatically and prints a single-line note
  instead of spamming load errors per track.
- **`--no-playback`** flag for `serve` to force headless mode even on a
  host that has audio deps installed.
- **Static-file split** — `index.html` / `app.css` / `app.js` are
  separate now (was a 60 KB monolith).  Served via FastAPI
  `StaticFiles` mount + top-level `/app.css` and `/app.js` aliases.

### Added — UI

- **Settings card** mirrors every CLI flag: preset picker, transition
  effect picker, harmonic-mixing / beatmatch / phrase-align /
  outro-intro-align / filter-sweep toggles, EQ-duck, smart shuffle,
  ReplayGain, crossfade seconds, BPM range, discovery rate.  All
  changes POST to dedicated REST endpoints and reflect via WebSocket
  state push.
- **Collapsible cards** — every section is now a native
  `<details>`/`<summary>` so the page stays clean.  Now Playing,
  Controls, Up Next, Lyrics, Queue open by default; EQ, Search,
  Settings, Recently Played start collapsed.
- **Unified Play / Pause / Resume button** — replaces the previous
  separate "Enable playback" gate.  In browser-playback mode the first
  click both unlocks the AudioContext (browser autoplay policy) and
  starts the active deck; subsequent clicks toggle pause/resume.
- **Pause-button state fixed** — disabled with "▶ Play" label until a
  track is loaded and playback is unblocked.

### Fixed — accessibility

- Removed `aria-live` from the per-second progress timer and volume
  percent — those updated every WebSocket tick and made screen readers
  announce the timer infinitely.  A separate `#vol-announce` polite
  region writes "Volume X %" only when the user moves the slider,
  debounced 250 ms.
- TDZ bug in `connectWS()` — `let _ws = null` was declared after the
  function that referenced it; first WebSocket open threw silently and
  left the page stuck on "Connecting…".  Declaration moved to the top.

### Internal

- New endpoints: `GET /api/audio?path=...` (HTTP Range-supporting
  audio stream), `POST /api/advance` (browser end-of-track signal),
  `GET /api/settings`, `POST /api/preset`, `POST /api/transition`,
  `POST /api/djmix`, `POST /api/playback-settings`, `POST /api/bpm-range`,
  `POST /api/discovery`.
- New `Player._run_headless()` — track-picking loop with no audio
  output, advances on the browser's `/api/advance` signal.
- Heavy deps (`librosa`, `soundfile`, `sounddevice`, `pynput`, `tqdm`,
  `model.MuqWrapper`) deferred to inside the functions that need them
  so minimal installs (NAS) don't import them.
- `sounddevice` import moved to `_stream_audio` so `Player` can be
  constructed without audio libs (headless serve mode).

---

## [0.5.0] - 2026-05-03

### Added — transition effects (8 selectable styles)

A new module `autodj.transitions` adds DJ-style flourishes layered on top of every crossfade.  Pick one in `[transitions] effect = "..."` (or per-session `--transition <name>`):

| Effect | What it does |
|--------|--------------|
| `none` | Standard crossfade only |
| `echo_out` | Feedback-delay echo tail on outgoing track |
| `reverb_tail` | Schroeder reverb (4 combs + 2 allpass) on outgoing |
| `highpass_riser` | High-pass cutoff sweeps DOWN on incoming intro — "filter-in" |
| `tape_stop` | Vinyl-stop ramp-to-zero on outgoing |
| `gate_stutter` | Rhythmic amplitude gate (1/16-note default) on outgoing |
| `noise_riser` | Synthesised band-passed white-noise build layered on top |
| `backspin` | Reversed pitch sweep on outgoing — turntablist backspin |
| `cross_eq_swap` | Outgoing keeps highs / incoming brings bass — mirror of EQ-duck |

Plus two **meta-modes**:
- `random` — uniform random pick per crossfade (every transition feels different)
- `rotate` — cycle through all real effects in order (predictable variety)

`[transitions] wet_mix` is the outer wet/dry between dry overlap and effect-processed overlap.  Server state exposes `last_transition_fx` so the web UI can show what just played.

### Added — beets enrich expansion

`autodj enrich` (was: keys-only) now refreshes a curated set of fields from beets:

- **Text fields** (`title`, `artist`, `album`, `genre`) — overwritten when beets has a non-empty value.
- **Numeric fields** (`bpm`, `year`, `length`) — overwritten when beets has a positive value.
- **Key / mode** — overwritten from `initial_key` (keyfinder plugin) when parseable.

Fully backwards-compatible: missing optional columns are silently skipped via `_items_columns()` schema introspection.  Users who index from filesystem first and add `library.db` later get the full beets metadata pulled in by a single `autodj enrich` run.

### Internal

- New module `autodj.transitions`.  All effects are stateless functions with graceful scipy-missing fallbacks.
- `TransitionsConfig` dataclass + `[transitions]` TOML section.
- New CLI flag `--transition <name>` available on `play` and `serve`.
- `Player._last_transition_fx` exposed in `PlayerBridge.get_state()` payload.
- `enrich_from_beets` dynamically builds its SELECT clause based on present columns; never assumes plugin columns exist.

---

## [0.4.0] - 2026-05-03

### Added — pro-DJ mixing layer

- **Harmonic mixing** — Camelot-wheel filter on next-track candidates.  Same position, ±1 around the wheel, or relative major/minor.  Reuses the `key` + `mode` already in the index — no re-analysis.  Enable via `[djmix] harmonic_mixing = true` or `--harmonic`.
- **Beatmatch (tempo align)** — incoming track is pitch-stretched (up to ±8 % by default) so its BPM matches the outgoing track during the crossfade.  Configurable max stretch.  Enable via `[djmix] beatmatch = true` or `--beatmatch`.
- **Outro / intro alignment** — auto-detects each track's outro start + intro end on first play; crossfade is positioned so the outgoing outro mixes into the incoming first downbeat (no more cold-cut intros).  Detection is cached in `index/dj_meta.json` (sidecar; never touches main metadata).  Enable via `[djmix] outro_intro_align = true` or `--align-outro`.
- **Phrase-aligned crossfade** — uses cached beat grid to snap the crossfade start time to the nearest 8-bar phrase boundary.  Configurable phrase length.  Enable via `[djmix] phrase_align = true` or `--phrase-align`.
- **Filter sweep** — low-pass swept-cutoff biquad rides the outgoing tail during crossfade (full-range → 250 Hz floor by default), giving the classic "filter-out" energy lift.  Enable via `[djmix] filter_sweep = true` or `--filter-sweep`.
- **3-band EQ** — low / mid / high gain knobs in the web UI, applied in real time inside the audio output callback (Butterworth split + sosfilt).  REST endpoint `POST /api/eq`.  Reset button.  Sliders use `aria-valuetext` for screen reader friendliness.
- **Energy-ramp planner** — `find_next_for_path` accepts a `target_energy` + `energy_weight`, blended into the score alongside cosine + BPM.  Plumbed through `Player._target_energy` (set by future preset/CLI hookups).
- **Live BPM / Camelot key / energy / beatmatch ratio** badges in the now-playing card.  Visual-only badges + a separate polite live region announces "Key 8A, BPM 124, beatmatched 1.07 times" on track change (per a11y review tweaks: spelled-out wording, fires only on track change, separate region so it doesn't collide with title).

### Added — internals

- New module `autodj.dj_meta` exposing `detect_intro_outro`, `detect_beat_grid`, `nearest_phrase_boundary`, `harmonic_compatible`, `camelot_position`, `camelot_label`, `analyse_audio`, and `DjMetaCache`.
- New module functions in `autodj.player`: `beatmatch_incoming`, `apply_filter_sweep`, `make_eq_filters`, `apply_eq`, `_time_stretch`.
- `IndexEntry`-driven harmonic + energy filters added to `SimilarityIndex.find_next` / `find_next_for_path`.
- `[djmix]` config section with full type-checked dataclass.
- New CLI flags: `--harmonic / --no-harmonic`, `--beatmatch / --no-beatmatch`, `--phrase-align / --no-phrase-align`, `--align-outro / --no-align-outro`, `--filter-sweep / --no-filter-sweep`.  All available on both `play` and `serve`.
- `dj_meta.json` sidecar cache (atomic temp+rename writes, batched flush every 10 entries).

### Notes

- DJ metadata (intro/outro times, beat grid) is detected lazily on first play of a track when a feature requiring it is enabled.  Once cached, subsequent plays hit the cache instantly.  No re-indexing of the FAISS index is required.
- All DJ-mix features are off by default — basic crossfade behaviour from v0.3.0 is unchanged unless you opt in.

---

## [0.3.0] - 2026-05-03

### Added — index portability and safety

- **`autodj prune` subcommand.**  Drops indexed entries whose audio files no longer exist on disk (e.g. after deleting, moving, or renaming files in your library).  Auto-runs at the start of every `autodj index` so your index stays in sync with reality.
- **Cross-machine index portability.**  Track paths in `metadata.json` are now stored RELATIVE to `[library] music_dir` (forward-slashed), so a single index built on one host is usable on any machine that mounts the library at a different path.  Old indexes with absolute paths are migrated automatically on next prune/index run.
- **`[library] path_remap`** config option — list of `[from_prefix, to_prefix]` pairs applied to legacy absolute paths from another host.  Useful as a one-time bridge before the migration completes.
- **`config.local.toml` overlay.**  If a sibling file exists alongside `config.toml`, its keys are deep-merged on top.  Keep a shared `config.toml` (per project) and per-machine overrides (paths, `music_dir`) in a gitignored `config.local.toml`.
- **Prune safety threshold.**  `prune_index` refuses if more than 20% of the index would be removed, raising `PruneSafetyError`.  Override with `autodj prune --force`.  Prevents accidental wipes from a misconfigured `music_dir`.

### Added — features

- **ReplayGain loudness normalisation.**  When `[replaygain] enabled = true`, AutoDJ reads embedded RG track-gain + peak tags (FLAC, MP3, M4A) and applies a clip-safe linear gain so all tracks play at consistent loudness.  Default target `-14 dB` matches Spotify / YouTube.
- **EQ-ducked crossfades** (pro-DJ style).  When `[playback] crossfade_eq_duck = true`, the crossfade applies a Butterworth high-pass sweep on the outgoing track during the overlap, eliminating bass-clash mush in the sub-200 Hz range.  Cutoff configurable via `crossfade_bass_cutoff_hz`.
- **LRC lyrics support.**  If a `<basename>.lrc` sidecar exists next to the playing audio file, the web UI renders the full lyric list with the active line highlighted (and announced via aria-live for screen readers).
- **Album art in web UI.**  Embedded cover art (FLAC `METADATA_BLOCK_PICTURE`, MP3 `APIC`, MP4 `covr`) is shown on the now-playing card.
- **Genre-aware presets.**  Presets accept an optional `genres = ["electronic", "house"]` list — only tracks whose genre matches (substring, case-insensitive) are eligible.
- **Smart-shuffle mode.**  `--smart-shuffle` flag (or web UI counterpart) inverts the similarity engine to pick the most sonically DISTANT next track, for genuinely surprising sequences.
- **Web queue with reorder + remove.**  New Queue card lists upcoming user-added tracks; per-item Up / Down / Remove buttons.  Drag-and-drop deliberately not used — keyboard / screen-reader-friendly buttons instead.

### Added — internals

- New module `autodj.audio_meta` exposing `read_replaygain`, `read_cover_art`, `load_lrc_for`, `parse_lrc`, `current_lyric`, `replaygain_multiplier`.
- `mutagen >= 1.47` dependency (ReplayGain tags, cover art, LRC sidecar parsing).
- `scipy >= 1.13` dependency (Butterworth biquads for EQ-ducked crossfade).
- New REST endpoints: `GET /api/art`, `GET /api/lyrics`, `POST /api/queue/add`, `POST /api/queue/remove`, `POST /api/queue/reorder`.
- `PlayerState.queue: list[IndexEntry]` — popped front-to-back by `_pick_next` ahead of similarity selection.

### Changed

- **Atomic writes** for `metadata.json` and `vectors.index` — `save_index` writes to `*.tmp` then `os.replace()`, so a failed write (common on SMB / NFS) leaves the existing on-disk index intact.
- `prune_index` skips the migration rewrite when storage is already in the target relative form (idempotent — no needless re-writes of the FAISS file every run).

### Documentation

- README sections added for prune, cross-machine setup, ReplayGain, EQ ducking, lyrics, smart-shuffle, queue, per-machine venv via `UV_PROJECT_ENVIRONMENT`.

---

## [0.2.0] - 2026-04-30

### Changed (breaking)
- **Embedding model swapped from MERT-v1-330M to MuQ-large-msd-iter** (`OpenMuQ/MuQ-large-msd-iter`). MuQ is a 2025 self-supervised music model with a Mel-Residual Vector Quantization tokenizer; it outperforms MERT on the MARBLE music-understanding benchmark (genre, instrument, structure, singer ID).
- Embedding dimension grew from 768 → **1024** per track. Combined FAISS feature vector dim grew from 784 → **1040**.
- MuQ requires **fp32** inference (fp16 risks NaN per the model authors); the previous fp16-autocast path on CUDA has been removed.
- Sample rate stays at 24 kHz (same as MERT).
- **No backwards compatibility**: any existing FAISS index built with MERT must be discarded — re-run `autodj index` from scratch.

### Fixed
- **Beets relative-path resolution.** Recent beets versions store track paths relative to the library `directory` (the `relative_path` migration). The previous `_remap_beets_path` only handled NAS-style absolute paths with a stripped prefix, causing AutoDJ to silently skip every relative-path track. Replaced with `_resolve_beets_path`, which prepends `[library] music_dir` for relative paths and leaves absolute paths alone.
- Updated indexer error message to point users at `music_dir` instead of the removed prefix option.

### Removed
- `[library] beets_path_prefix` config option — no longer needed; the new resolver handles relative paths automatically. Existing configs containing this key continue to load (the unknown key is ignored), but the value has no effect.
- `MertWrapper`, `MERT_SAMPLE_RATE`, MERT-specific transformers loader code in `model.py`.
- `transformers` dependency (no longer needed; MuQ's own `MuQ.from_pretrained` handles loading).
- `nnaudio` dependency (was unused dead code).
- `bandit` skip for `B603` (no subprocess code in the project).
- "NAS / beets path remapping" section from `README.md` — replaced with simpler "Beets paths" guidance.

### Added
- `muq>=0.1.0` dependency for the new embedding model.
- `huggingface_hub>=0.23.0` listed explicitly (was previously transitive through `transformers`).
- Optional commented hints in `config.toml` for `playback.history_file` and `playback.discovery_every`.

### Internal
- `_combine_features` parameter renamed `mert_vec` → `embedding_vec`.
- `EMBEDDING_DIM` constant exported from `autodj.model` and used by indexer + tests.
- All MERT references purged from source, tests, configs, and docs.

---

## [0.1.3] - 2026-04-11

### Added
- **ruff** — linter + formatter (replaces flake8/isort/black); configured in `pyproject.toml` with `E`, `W`, `F`, `I`, `UP`, `B`, `C4`, `SIM`, `RUF` rule sets
- **mypy** — static type checker; `disallow_untyped_defs = true`, `ignore_missing_imports = true` for untyped third-party libs (torch, faiss, librosa)
- **bandit** — security linter; `B311` (non-security `random.choice`) and `B603` (no subprocess usage) skipped globally; HuggingFace download findings suppressed with `# nosec B615`
- **pre-commit** — git hooks running ruff → bandit → mypy automatically before every `git commit`; install with `uv run pre-commit install`
### Fixed
- `raise ... from err` added to BPM range parse error in `cli.py` (B904)
- `zip(..., strict=False)` made explicit in `similarity.py` (B905)
- Redundant `int(round(...))` simplified to `round(...)` in `player.py` (RUF046)
- `sys.stdout/stderr.reconfigure` guarded with `isinstance(sys.stdout, io.TextIOWrapper)` instead of bare `try/except AttributeError` in `cli.py`
- `TYPE_CHECKING` guards added to `cli.py`, `player.py`, and `server.py` for cross-module imports that would create circular dependencies
- `from __future__ import annotations` added to `server.py`; stale comment about FastAPI annotation compatibility removed
- Unused unpacked variables renamed to `_` across test suite (RUF059)
- `pytest.raises(Exception)` tightened to `pytest.raises(sqlite3.DatabaseError)` in `test_beets.py` (B017)
- Import ordering and `Optional[X]` → `X | None` modernisation applied project-wide (ruff auto-fix)

---

## [0.1.2] - 2026-04-11

### Added

**Presets — BPM-shaping envelopes**
- 10 built-in presets: `wakeup`, `winddown`, `sleep`, `morning`, `slide`, `party`, `workout`, `chill`, `focus`, `driving`
- User-defined presets via `[presets.NAME]` sections in `config.toml`; support `bpm_target`, `bpm_start`/`bpm_end`, `curve` (`linear` / `slide`), `bpm_weight`, `horizon_tracks`, `discovery_every`
- `--preset NAME` flag on `play`, `serve`, and `playlist` commands
- `presets.py` — `Preset` dataclass, curve constructors (`constant_curve`, `linear_curve`, `slide_curve`), `get_preset()`, `load_user_presets()`

**Discovery mode**
- `--discovery-every N` flag on `play` and `serve` — injects a sonically distant track (bottom-quartile cosine similarity) every N tracks
- Runtime toggle: press `D` in the terminal, or click **◈ Discovery** in the web UI
- Discovery rate and enabled state are separate — a preset can ship with a rate while the user controls whether it fires
- `SimilarityIndex.find_distant()` — queries full index, returns a random pick from the bottom 25% by cosine score; falls back to any non-excluded track
- `PlayerState.discovery_enabled` (runtime toggle) + `Player._discovery_every` (rate)
- WebSocket `{"type": "toggle_discovery"}` message for browser-side toggle

**Extended metadata (extracted during `autodj index`)**
- `IndexEntry` now stores `energy` (RMS loudness), `key` (chromatic 0–11), `mode` (1=major, 0=minor), `tempo_confidence` (0–1) — all extracted by librosa during indexing at no extra cost
- Key/mode estimated via major/minor chromatic template matching against chroma features
- Tempo confidence = detected beat frames / expected beats at estimated tempo

**M3U export**
- `--export-m3u FILE` on `play` and `serve` — appends `#EXTINF` + path entries in real time as tracks play
- `autodj playlist` subcommand — simulates the selection loop offline and writes an M3U without playing audio; supports `--seed`, `--tracks`, `--preset`, `--bpm-range`, `--output`
- `write_m3u()` and `_append_m3u_entry()` helpers in `player.py`

**Play history**
- `--history-file FILE` on `play` and `serve` — appends one JSON Lines record per played track: `{"timestamp", "path", "title", "artist", "album", "bpm", "length"}`
- Configurable via `history_file` in `[playback]` section of `config.toml`

**BPM range filter**
- `--bpm-range MIN-MAX` on `play`, `serve`, and `playlist` — hard filter that excludes tracks with known BPM outside the given range; tracks with unknown BPM (0.0) always pass
- Accepts both ASCII hyphen and en-dash separators
- Falls back to unfiltered with a warning if the range excludes all candidates

**`autodj stats` subcommand**
- Displays a Rich overview: BPM histogram, top genres, decade breakdown, track lengths, top artists, key distribution (C–B), major/minor split, energy histogram
- Reads only `metadata.json` — no FAISS index or model needed

**Web UI — discovery toggle**
- **◈ Discovery** button added to the control panel; visible only when a discovery rate is configured
- State synced via WebSocket broadcast (`discovery_enabled`, `discovery_available` keys in state JSON)

### Changed
- `_extract_librosa_features()` returns a 4-tuple `(features, audio, sr, extra_meta)` — callers in `build_index()` updated to unpack and store the extra metadata fields on `IndexEntry`
- `IndexEntry` fields `energy`, `key`, `mode`, `tempo_confidence` are now required (no defaults) — fresh index required

### Fixed
- SQLite connection leak in `beets.py._open_db` — a failed fast-fail `SELECT 1` validation call left the connection open; it is now closed in the exception handler before re-raising

### Removed
- `autodj enrich` subcommand and `enrich_index()` function — the extended metadata fields are now extracted as part of the normal `autodj index` run, so a separate enrichment pass is no longer needed

### Testing
- Test suite grown from 124 to **414 tests** (88% line coverage, up from 49%)
- New test modules: `test_stats.py`, `test_presets.py`, `test_cli.py`; extended `test_player.py`, `test_indexer.py`, `test_server.py`, `test_similarity.py`, `test_model.py`
- `tests/conftest.py` — `sounddevice` mocked at `sys.modules` level so the full suite runs on headless CI without audio hardware
- `pytest-asyncio` (`asyncio_mode = "auto"`) added for FastAPI/WebSocket async tests; `pytest-cov` added for coverage reporting
- Windows NAS path remapping covered by 8 edge-case tests in `TestRemapBeetsPath`
- Real `library.db` integration test (skipped automatically when the database file is absent)
- FastAPI tested via `httpx.AsyncClient` + `ASGITransport` — no live server process required

---

## [0.1.1] - 2026-04-11

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
