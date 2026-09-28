# Changelog

What changed and when, written for the people who use AutoDJ.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

## [0.18.0] - 2026-09-27

### Added

- `autodj serve --lan` opens AutoDJ to your local network with one switch. It listens on all
  interfaces, allows only this machine's own names and addresses (hostname, `.local` name, IP
  addresses) plus any you configure, and keeps pairing on. If no access token is configured it
  creates one in `index/.access-token`. On Linux and macOS only you can read that file; on Windows
  it gets the index folder's permissions, so on a network share anyone who can read the share can
  read the token. Startup prints the addresses to open and the pairing code. `[server] lan = true` and `AUTODJ_LAN=1` do the same, `autodj doctor`
  shows the detected hosts, and `autodj devices pairing-code` works with the saved token. The
  Compose `lan` profile now uses it too. `--allowed-host`, `--allowed-origin` and
  `--access-token` still work but are hidden from `--help` as advanced overrides.
- `autodj serve --stream` (or `[stream] enabled = true`) turns AutoDJ into a radio station.
  Sonos, VLC and other network players can open the live mix directly, with AutoDJ's own
  crossfades, EQ, transition effects and voice liners already mixed in, and the artist and
  title shown on the player. The web page grows a "Listen here" button to play the same stream
  in the browser, and a Settings, Stream section with the stream address, a downloadable `.m3u`
  playlist, a "Make new link" button, a quality (bitrate) choice, and the listener count. The
  set starts when the first listener connects and stops 30 seconds after the last one
  disconnects. Combine it with `--lan` (`serve --lan --stream`) so other devices can reach it,
  or run the new Compose `stream` profile. See [Operations](docs/operations.md#radio-stream-sonos-vlc-and-other-players).

### Changed

- `--server-audio` now plays in stereo and gapless between tracks, using the same mix bus that
  drives stream mode instead of a fresh sound-card connection per track.
- Harmonic mixing now has one setting, `harmonic_mode`, which both turns it on and picks the
  rule: `off`, `compatible`, `strict`, `energy_boost`, `mood_change` or `neighbour`. The default
  is `off`. Check the `[djmix]` table in your `config.toml`:
  - Delete any `harmonic_mixing` line. It is now an unknown key and AutoDJ refuses to start
    with it.
  - A `harmonic_mode = "compatible"` line (the old example config had one) now turns key
    filtering on by itself. Change it to `harmonic_mode = "off"` if you want it to stay off.
  - Where you had `harmonic_mixing = true`, keep or set `harmonic_mode` to the rule you want.

  `autodj play` and `autodj serve` take `--harmonic-mode MODE` instead of
  `--harmonic` / `--no-harmonic`.
- A `harmonic_mode` in `config.toml` that is not one of those names, including `true` or
  `false`, now stops AutoDJ with an error naming the setting.
- `config.toml` and `config.local.toml` now reject unknown keys in every section, and unknown
  sections. A removed or misspelled setting, such as a leftover `harmonic_mixing` in `[djmix]`
  or `path_remap` in `[library]`, stops AutoDJ with an error naming the section and key instead
  of being ignored. Preset names under `[presets.NAME]` are still free-form; the keys inside
  each preset are checked by the preset loader as before.
- Saved web settings (`web_state.json`) from earlier versions are ignored, with one warning in the
  log. Your web settings return to their defaults until you change one in the web page, which
  saves a new file. Earlier files stored a harmonic mode even when harmonic mixing was off, and
  reading it now would quietly turn key filtering on.
- The web page no longer reads timed lyric lines aloud as they play. Screen reader users can
  read them on demand in the Lyrics card, where the line playing now is marked as current. The
  automatic reading only worked with the card open, which it is not by default.

### Removed

- The `harmonic_mixing` setting, in `config.toml`, in `web_state.json` and in `POST /api/djmix`,
  which now answers 422 when a request still sends it.
- `autodj serve --no-playback`. It never did anything and was deprecated in 0.17.0; passing it
  is now an error. Browser audio is still the default and `--server-audio` still opts into
  server audio.
- Presets have one layout per file. `presets.toml` takes only bare `[name]` tables, and
  `config.toml` takes only `[presets.name]` tables. A `[presets.name]` table inside
  `presets.toml` is no longer read as a preset; AutoDJ skips it with a warning, so rename it to
  `[name]`.
- The `bpm_low` and `bpm_peak` preset keys for `curve = "slide"`. Use `bpm_start` and
  `bpm_end`. A preset with any key AutoDJ does not know is now skipped with a warning naming the
  key, instead of loading with that key ignored.
- `autodj.player` no longer re-exports `apply_eq`, `make_eq_filters`, `make_eq_state` and
  `reset_eq_state`. Import them from `autodj.eq`.

- Upgrade note: an index made before the index manifest format (a folder with `tracks.db`
  and `vectors.index` but no `index-manifest.json`), or one that stores absolute track paths,
  must be rebuilt. AutoDJ stops with a message that names the folder and tells you to run
  `autodj index --force`. A DJ metadata cache (`dj_meta.db`) that stores absolute paths must
  be deleted; `autodj analyse` builds a new one.
- AutoDJ no longer moves an index from before 0.9 out of the index folder into
  `index/default/`, and no longer moves its `dj_meta.db`, `web_state.json` and
  `runtime_state.json` along with it.
- AutoDJ no longer reads an index folder that has no `index-manifest.json`. `autodj doctor`
  reports such a folder as an old index format instead of counting its "legacy vectors and
  rows".
- The `[library] path_remap` setting is gone. Track paths in `tracks.db` and `dj_meta.db`
  are always stored relative to `music_dir`, and `autodj index` skips beets tracks that sit
  outside `music_dir`, with a warning. A leftover `path_remap` line is an unknown key and
  stops AutoDJ; delete it.
- AutoDJ no longer rewrites absolute paths in `tracks.db` and `dj_meta.db` into relative
  ones, and no longer upgrades an older `tracks` table layout to the current one.
- Index rows with no `embedded_at` time are no longer stamped with the file's current time.
  The next `autodj index` run re-embeds them.
- `autodj index --reindex-modified-since`. It existed to catch files replaced before index
  rows had an `embedded_at` time; every row now has one, and `autodj index` already re-embeds
  any file whose modification time is newer than that. Passing the option is now an error.

### Fixed

- With `--server-audio`, each incoming track restarted from its very beginning on the sound
  card, replaying the few seconds of the outgoing track's crossfade and the incoming track's own
  skipped intro that had already played during the overlap.
- `/api/history` stayed empty for the whole session with `--server-audio`, because tracks were
  never recorded as played through that output path.
- In the web page, buttons that send a request (Play, Pause, Skip, Mute, Next, Refresh stats,
  Stop running job and others) no longer make NVDA say "unavailable" while the request runs. The
  button stays usable and a second press during the request is ignored.
- Play and Pause now say "Playing" or "Paused" after the button or the Space and K keys change
  the playback state. NVDA did not read the button's new label.
- Moving between the section tabs with the arrow keys reads each tab once instead of twice.
- Symbols that NVDA skips at its default punctuation level are now read in words: the question
  mark key in the keyboard shortcuts help, "plus or minus" and "plus" in the Harmonic mixing
  choices and descriptions, and the Beatmatch description. The text on screen is unchanged.
- The Transition effect list's description is now one sentence. The notes on each effect, about
  450 words that were read every time the list got focus, moved into a "Transition effect
  details" section below it.
- Stopping a library job with Stop running job now reports that the job stopped, instead of
  "exited with code 1".
- In stream mode with `--server-audio`, the page's volume slider and Mute now also set the
  volume and mute of the machine's own speakers. Before, they changed only the page's own
  listening, and the speakers kept whatever volume the server started with.

## [0.17.0] - 2026-09-26

### Security

- The voice-liner endpoints could read or delete any file, not just liners. The settings API let
  a client move the liner folder anywhere, for example to the folder holding `config.toml`, and
  the liner download and delete routes only checked that the name was a plain file name. A paired
  browser, or anyone on the LAN under `--insecure-lan`, could then download the config file with
  its access token or delete the track database. Liner download and delete now accept only audio
  file names, and the liner folder can only be set in the config file.
- Pairing codes are harder to guess. A device that enters ten wrong codes within one five-minute
  code window is refused until the window ends, and is told how long to wait; other devices can
  keep pairing with the same code. If fifty wrong codes arrive from all devices in one window,
  pairing pauses until the next code, and the pairing dialog says so. `autodj devices
  pairing-code` now also says how many seconds the code has left.
- Library jobs started from the web page now accept only the options the page itself sends
  (`index --limit N`). Anything else, such as `prune --force`, is refused. This replaces a check
  for shell characters that protected nothing, because jobs never run through a shell.
- The automatic API pages at `/docs`, `/redoc` and `/openapi.json` are no longer served. On
  loopback and under `--insecure-lan` they were open to anyone who could reach the server.
- Every GitHub Actions step is pinned to an exact commit instead of a movable tag, and the
  container's CPU-only PyTorch wheels are checked against recorded hashes before installing.

### Changed

- A repository-wide simplification pass removed about 1,900 net lines of
  duplicated or unused code across the Python backend, the browser frontend,
  the build scripts and the test suite, with no change in behaviour. Every cut
  is one commit with its reason in the message.
- The `dev` extra in `pyproject.toml` is gone; the same packages live in the
  `dev` dependency group that `uv sync` installs by default. `pip install .[dev]`
  no longer works, use `uv sync`.
- `.containerignore` was removed. It was a byte-for-byte copy of `.dockerignore`,
  which Podman reads when `.containerignore` is absent.
- Setup documentation now includes container-only indexing, native Windows backup and restore,
  and checking out a release before upgrading. It corrects offline and dependency claims and
  explains which model changes require a new index. MuQ remains the default model.
- Accessibility documentation identifies the missing published screen-reader sampling record
  for v0.16.1 and explains what must be recorded for the next release.
- `POST /api/discovery` with a rate now starts discovery immediately rather than only configuring
  it. Setting a rate while leaving the feature switched off is what made the Settings checkbox and
  the Now Playing button disagree.
- `autodj serve --no-playback` is deprecated. It could never be turned off, so it never did
  anything: server-side audio is opted into with `--server-audio`, which is unchanged. The flag
  still parses so existing deployments keep starting, is hidden from `--help`, and logs one
  informational line. The container image, the compose services and the workflows no longer pass
  it, and a test checks every flag those files use against the options the CLI actually declares.
- `pre-commit` now runs the same locked `ruff` and `mypy` that CI runs, instead of separately
  pinned mirrors that could disagree with it.
- Dependencies moved to their current releases. Direct ones are all minor or patch.
  Transitively the lock moved `torchvision` 0.28 to 0.29 (it arrives through MuQ's `x-clip`
  dependency), a 0.x minor that may change anything, and `evdev` 1.9.3 to 2.0.0 — a major; it is
  Linux-only, arrives as an sdist and is compiled on the machine that installs it, so Windows and
  macOS never see it and CI is what proves it — and the 0.x packages `tokenizers` 0.22.2 to 0.23.2, `numba` 0.66 to 0.67,
  `llvmlite` 0.48 to 0.49 and `ast-serialize` 0.8 to 0.10. `cloudpickle` is a new transitive
  dependency and `uc-micro-py` is no longer resolved. The lock pins `torch` 2.14.0; each platform
  wheel carries its own local version on top (a Windows CPU install reports 2.14.0+cpu).
- `config.toml.example` now lists every `[playback]` and `[model]` setting the loader accepts,
  including the liner triggers, the FX sync toggles and `dayparts_dir`.
- `POST /api/playback-settings` now rejects unknown fields and invalid choices with a 422 and
  changes nothing. It used to apply the valid part and then fail. An invalid liner pick mode, which
  used to be ignored silently, is now rejected too. Profiles are checked the same way when they are
  saved, and a stored profile with an invalid choice is refused with a 400 before anything changes.
- `mutagen` and `scipy` are now part of the base install instead of the `play` extra, because
  tag reading, album art, embedded lyrics and ReplayGain need them on every install. `pydantic`
  and `huggingface_hub`, which AutoDJ imports directly, are now declared rather than arriving by
  chance. The extras no longer repeat base packages, and Starlette may now take patch releases.
- The container image installs the CPU-only build of PyTorch, so it no longer downloads several
  gigabytes of CUDA libraries it cannot use. Native installs are unchanged and still pick up a GPU.
- The TLS guidance now agrees everywhere: AutoDJ terminates TLS itself with `--ssl-certfile` and
  `--ssl-keyfile`. A reverse proxy that terminates TLS in front of it is not supported, because
  the session cookie's `Secure` flag follows AutoDJ's own TLS setting. The operations guide now
  explains where `AUTODJ_ACCESS_TOKEN` comes from on a native install, since only Compose reads
  `.env`.
- The README lists every command, explains how to uninstall, and points to
  `presets.toml.example`. CONTRIBUTING describes how to cut a release.
- CI now lints and format-checks `scripts/`, runs the secret scan once over full history instead
  of twice, and pre-commit takes `bandit` and `vulture` from the lock file like CI does. The slow
  hooks (the full test suite, Trivy, the dependency audits and the code-quality scans) now run once
  per `git push` instead of on every commit, and `uv run pre-commit install` sets up all three hook
  types in one step.

### Added

- A tested Windows AMD ROCm setup guide and launcher for GPU indexing and web
  library jobs, with model-kernel caches kept inside the project.
- Keyboard shortcuts can be turned off under Settings, Keyboard. Single-key shortcuts can clash
  with a screen reader or with typing habits, and WCAG asks that they can be switched off. The
  choice is saved in the browser.

### Fixed

- The container image had no `mutagen`, so under Docker there was no album art, no embedded
  lyrics, no ReplayGain and no ALAC detection, and nothing said so.
- Applying a saved profile changed the settings but never saved them, so a restart put the old
  settings back.
- With `--server-audio`, adding a track to the queue during a song made Up Next name it, but it
  played one track later. The same edit could also pick a track from the web request while the
  audio thread was picking one, so the two disagreed.
- Reordering the queue kept only one copy of a track that was queued twice, and with
  `--server-audio` a track change landing in the middle of a reorder could play a track twice.
- With `--server-audio`, pressing "Play next" late in a song threw away the queued track that had
  already been lined up. It now plays straight after the new one.
- Starting a library job just after another finished could mark the new job as finished with exit
  code -2 and mix the old job's last lines into its log.
- On a slow disk or network share, deleting or playing a liner, saving a profile, or pairing a
  device stalled every other request and the live updates until it finished.
- Changing a setting, or pressing Pause, Mute, Shuffle, Search, a Library tools button or a liner
  button, threw keyboard focus to the top of the page while the request ran, because the control
  was disabled while it had focus. On a dropdown, the next arrow key went nowhere. Controls now
  keep focus and ignore repeat presses until the request finishes.
- A queue update from the server, such as the next queued track starting, threw focus off the
  Up, Down or Remove button you were on. Focus now returns to the same button, or the nearest one.
- Arrow keys on the seek slider jumped back to where the track was when the slider got focus,
  because they read a position that deliberately stops updating while the slider has focus. They
  now start from the real playback position, in exact seconds.
- A playback error replaced the track title on screen and never reached the status line, and an
  aborted load, which is harmless, was announced as an error. Errors now go to the status line and
  the title stays put.
- Changing the key notation announced into the Now Playing panel, which is hidden while you are on
  Settings, so nothing was heard. The dropdown already speaks its own value, so the extra message
  is gone.
- History paging lost focus at the first and last page, never said which page you were on, and
  showed "No tracks played yet" when the page was opened or reloaded straight onto the History tab.
- Pause had a changing label and a pressed state, so NVDA said "Pause, toggle button, pressed";
  Mute said "Unmute, pressed". Pause now just changes its label, and Mute keeps the name "Mute"
  with a pressed state.
- A track change was spoken as three separate messages in a confusing order: the new track, a bare
  next track, then the previous track's key. It is now one announcement, "Artist — Title, 128 BPM,
  key 8A", and Up Next is read on request with Shift+N.
- In Windows High Contrast the selected tab and pressed buttons looked like all the others. They
  are now drawn in the highlight colour with a thicker border.
- The volume was spoken twice after each change, once as the slider value and once as "Volume 95%".
  The slider now reads "95%" itself and the extra message only plays for the arrow-key shortcut.
- A server update arriving just after you moved an EQ slider could undo the change.
- Now Playing had no headings, so the H key could not reach Up Next or Lyrics. Every card title is
  now a heading.
- The Library tools output log had no name a screen reader would read.
- A pairing error was read twice.
- The playback buttons claimed to be a toolbar without supporting toolbar arrow keys; they are now
  a labelled group.
- The status message at the foot of the page could cover the control that had focus.
- Refresh stats gave no confirmation when the numbers had not changed.
- Pause, Mute and Discovery were rewritten every second even when nothing changed, which could
  make a screen reader re-read the focused button.
- MuQ model setup downloaded both safetensors and duplicate PyTorch weights,
  then rejected the cache because it contained two weight formats. Automatic
  downloads now keep only the safetensors checkpoint that MuQ loads by default.
- Music imports failed during MuQ inference with current Transformers, even when
  model loading and doctor checks passed. Adapt MuQ's Conformer configuration and
  final-layer output while retaining current, patched Transformers dependencies.
- Update Vitest to 4.1.11 to address GHSA-82fw-gwwq-j7x9.
- Refresh the container's PCRE2 runtime package to include Debian security fixes.
- Restore web keyboard shortcuts from focused buttons and sliders, including next track and
  measure seeking. Keep native control keys intact, allow status shortcuts on every tab, and
  let `?` close the shortcuts dialog from its focused Close button.
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
  beside it. Two more places had the same shape: the seek slider read out its new position once a
  second while it held focus, and a repeatedly failing background request repeated the same
  sentence every four seconds.
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
- A settings file saved by an older version, which stored the harmonic mixing mode as on/off
  rather than today's named modes, logged "ignoring invalid harmonic_mode" on every start and
  reset harmonic mixing to its default. The old on/off value is now carried over instead.
- The keyboard shortcuts list opened from the Settings, Queue, History or Library tab appeared as
  an empty, invisible dialog that screen readers could not read, because it lived inside the
  hidden Now Playing panel. It now opens on every tab.

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

[Unreleased]: https://github.com/blindndangerous/AutoDJ/compare/v0.18.0...HEAD
[0.18.0]: https://github.com/blindndangerous/AutoDJ/compare/v0.16.1...v0.18.0
[0.17.0]: https://github.com/blindndangerous/AutoDJ/commit/6b6834ca6079d0942867d59f3779cb284c41580a
[0.16.1]: https://github.com/blindndangerous/AutoDJ/compare/v0.16.0...v0.16.1
[0.16.0]: https://github.com/blindndangerous/AutoDJ/compare/v0.12.0...v0.16.0
[0.12.0]: https://github.com/blindndangerous/AutoDJ/releases/tag/v0.12.0
