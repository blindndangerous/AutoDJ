# Changelog

What changed and when, written for the people who use AutoDJ.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

## [0.19.0] - 2026-09-29

### Added

- `[index] throttle_ms` sets a pause before each track that `autodj index` and `autodj analyse`
  process, to give network drives a rest.
- Settings, Browser access can rename a paired device, says when each device was paired and when
  it was last seen, and has **Show pairing code** for pairing another device from a device that is
  already paired. The code is said once, digit by digit; it changes by itself every five minutes
  and cannot be edited. A device that pairs while the code is shown is announced by name.
  `autodj devices rename DEVICE_ID NAME` does the same rename from the command line.
- Serato hot cues and saved loops are imported from the tags inside MP3, AIFF, FLAC and MP4/M4A
  files when `import_external_cues` is on, next to the Mixxx, Rekordbox and Traktor imports. The
  reader follows the published format notes of the serato-tags project and has only been tested
  with hand-made tags, not a real Serato library. Ogg files are skipped because Serato's layout
  there is not documented.
- `[server] ssl_certfile` and `ssl_keyfile` in `config.toml` or `config.local.toml` turn on HTTPS,
  like `--ssl-certfile` and `--ssl-keyfile`, which replace them when given. Both must be set, and
  AutoDJ refuses to start if either file is missing.
- A renewed certificate is picked up while AutoDJ runs: it checks the certificate and key files
  every five minutes and new connections get the new certificate, so a Let's Encrypt renewal
  needs no restart. A half-copied or mismatched pair is refused with a warning and the old
  certificate stays in use.
- `autodj doctor` has a `beets-db` check that warns when `[library] beets_db` names a missing
  file, and its FFmpeg warning now says that indexing skips `.m4a`, `.mp4` and `.aac` files and
  stream mode cannot start without FFmpeg.
- The operations guide has a section on HTTPS on a home network with a self-signed certificate,
  and the README covers FFmpeg for `.m4a` files, the Windows PowerShell folder command,
  `config.local.toml.example`, pairing-code lifetime, the firewall, and what plain HTTP from
  another device leaves out.
- The operations guide has a new section, "HTTPS with your own domain": a Let's Encrypt
  certificate through certbot's Cloudflare DNS plugin, copying it on renewal, home DNS so
  traffic stays on the LAN, and a Cloudflare Tunnel with Cloudflare Access for remote use.

### Changed

- The index is stored in a new, simpler format (manifest schema 3). An index made by an older
  AutoDJ is refused with a message to rebuild it: run `autodj index --force` once, on the machine
  that indexes, and copy the result again. `dj_meta.db` is kept and needs no new analysis.
- An index folder now holds only `index-manifest.json` and the two generation files it names,
  `tracks.g<number>.db` and `vectors.g<number>.index`, plus `dj_meta.db`, web settings and liners.
  The working `tracks.db` and `vectors.index` and `.index-publication-state.json` are no longer
  written. A script that copies an index to another machine should copy the two generation files
  and `dj_meta.db`, then `index-manifest.json` last, and no longer needs to create
  `.index-publication.lock`. The operations guide's "Copying an index to another machine" has the
  details.
- A running `autodj serve` loads a new index generation within 10 seconds whenever
  `index-manifest.json` changes, including one copied in from another machine, and logs a warning
  when the new generation cannot be loaded.
- `autodj doctor` checks the index by loading it the way `serve` does and reports either the
  generation and track count or the exact error `serve` would stop with. Its separate `tracks-db`
  check is gone and the `index-coherence` check is now called `index`.
- `autodj index` and `autodj analyse` print a plain progress line every 25 tracks instead of
  progress bars, which also reads cleanly in the web page's job log. `autodj analyse` analyses one
  track at a time.
- The MuQ model is now kept in Hugging Face's own cache layout inside `models/` (or your
  `[index] model_dir`), and `autodj index` asks Hugging Face for it on every run, which only
  downloads what is missing and uses the saved copy when offline. The first `autodj index` after
  upgrading downloads the model once more. Afterwards you can delete the old folder, named like
  `models/MuQ-large-msd-iter-e891ff924c0b7fe8`, and the `.lock` file beside it. To keep using that folder instead, set
  `[model] manual_path` to it.
- Playback settings out of range are refused instead of quietly adjusted: a negative crossfade,
  fade-in, liner trigger, artist repeat window or pick temperature, or a `pick_top_k` below 1, in
  `config.toml` stops AutoDJ with an error naming the key. Applying a saved profile with
  such a value, or with NaN or Infinity in it, now fails with an error and changes nothing.
- Deleting a voice liner asks in the page's own confirmation dialog, like deleting a profile,
  instead of the browser's pop-up. After the delete, focus goes to the next liner's Delete button,
  or to Upload when no liner is left.
- `autodj backup` and `autodj restore` are much simpler. A backup holds the same things as
  before and is safe to make while AutoDJ is serving, so the `--online` option is gone. Stop
  AutoDJ before you restore: restore cannot tell whether it is running. Restore now replaces the
  liners folder and `index/profiles` whole, so files added since the backup are deleted, and a
  restored index replaces every older index generation. It no longer checks free space or rolls
  back a half-finished restore; if a restore is interrupted, run it again. Restore still only
  accepts a backup made by the same major and minor version, so a 0.18 backup cannot be restored.
- Voice liner uploads and deletes now work in a liners folder that other users or a group can
  write to, such as a shared NAS folder; they used to be refused. A liner file name can be at most
  200 bytes long.
- Running AutoDJ no longer needs Node.js or npm. The server sends the web page's files from
  `src/autodj/static` as they are, gzip-compressed for browsers that accept it. Node.js is only
  needed to run the JavaScript tests, lint and browser audits.
- The page's scripts and stylesheet are now checked with the server on each page load and reused
  from the browser cache when unchanged, instead of being downloaded again every time.
- Settings saved by the web page (`web_state.json`) are checked on restart by the same rules the
  settings page's requests must pass, one setting at a time: a bad value is skipped with a
  warning and the other saved settings still apply. A file with any `schema_version` other than
  2 is ignored instead of partly applied. `prefetch_next_track` and `silence_trigger_crossfade`
  are no longer saved there, since the page cannot change them, so the values in `config.toml`
  apply; an older file that still has them logs a warning for each until the page next saves.
- An unknown harmonic mode sent to `POST /api/djmix` is refused with an error instead of being
  ignored.
- The Now Playing Discovery button now uses `POST /api/discovery/toggle`, which needs a paired
  session like the other controls, instead of a message on the WebSocket. The WebSocket only sends
  state to the page and ignores anything the page sends on it. The button also works while the
  WebSocket is reconnecting.
- The gate stutter, bitcrusher, freeze and glitch transition effects in crossfades the browser
  plays need the page to be opened over HTTPS or on localhost, where the browser allows
  AudioWorklet. On a plain-HTTP page they are skipped: that crossfade plays without an effect, and
  random and rotate leave them out. Settings says so under the transition effect. Stream mode and
  server audio are not affected.

### Removed

- `autodj analyse -j/--workers`. Analysis runs one track at a time; `autodj index -j` still sets
  how many tracks are decoded ahead while embedding.
- `autodj backup --online`. Every backup now reads the index and the DJ metadata safely while
  AutoDJ runs.
- `GET /api/settings` and `GET /api/profiles/{name}`. The web page never used them; the settings
  are in `GET /api/status` under `settings`.
- The `autodj play` terminal player, with its keyboard keys (a global keyboard hook that also
  caught keys typed in other windows), its terminal status panel and its terminal lyrics. To play
  on the machine's own speakers, run `autodj serve --server-audio` and control it from the web
  page; choose the output device with `[playback] audio_device`. The `play` extra no longer
  installs `pynput`.
- The web bundle build (`npm run build`) and the `src/autodj/static_dist` folder it produced.
  Delete a leftover `src/autodj/static_dist` folder: AutoDJ ignores it, but Git now lists it as
  untracked. `autodj doctor` no longer has a `frontend-bundle` check.
- The `/static/` URLs. The page never used them; it loads its files from `/`, `/app.css`,
  `/app.js`, `/modules/` and the worklet URLs, which stay.
- The request-rate limit on pairing (five tries a minute per address) and on stream links. The
  wrong-code lockout still stops guessing: ten wrong pairing codes lock an address out until the
  code changes, and fifty from all addresses pause pairing. A wrong stream link now always
  answers "not found"; its 256-bit secret cannot be guessed.
- The GPU beat-grid path and the `AUTODJ_GPU` and `AUTODJ_DJMETA_GPU` environment variables. Beat
  grids are always found by librosa on the CPU, so every machine finds the same grid for the same
  file. MuQ embedding still uses a CUDA or ROCm GPU whenever PyTorch sees one. Beat grids already
  saved in `dj_meta.db` by the old GPU path stay in use until that track is analysed again; AutoDJ
  does not recompute them by itself. The `index` extra no longer lists `torchaudio`; MuQ still
  installs it.
- The session logs: `serve --export-m3u`, `serve --history-file`, `[playback] history_file` and
  the `autodj playlist` command. A `config.toml` that still sets `history_file` is refused; delete
  the line. The History tab in the web page stays. Backups no longer hold a play history file.
- The playback and DJ-mix options of `autodj serve`: `--preset`, `--bpm-range`,
  `--discovery-every`, `--smart-shuffle`, `--pure-shuffle`, `--anchor-seed`, `--show-lyrics`,
  `--import-external-cues`, `--beat-sync-fx`, `--key-sync-fx`, `--harmonic-mode`,
  `--transition-mode`, `--beatmatch`, `--phrase-align`, `--align-outro`, `--filter-sweep` and
  `--transition`, with their `--no-` forms. Set these in the web page, which saves them, or in
  `config.toml`. `serve` keeps its server options (`--seed`, `--lan`, `--host`, `--port`,
  `--insecure-lan`, `--open`, `--name`, `--server-audio`, `--stream` and the TLS options).
- Daypart and Mood arc, which steered the tempo of picks by the time of day or over a three-hour
  arc. `[playback] enable_daypart`, `enable_mood_arc` and `mood_arc_hours` are refused in
  `config.toml`; delete them. The settings routes refuse these fields too. Every profile saved by
  0.18 holds them, so it no longer applies: delete it and save it again. Saved web settings still
  load; the three old fields are skipped with a warning in the server log. The Wall-clock daypart,
  Mood arc and mood arc length settings are gone from the web page's Settings, and a profile saved
  from the page no longer includes them.
- The `genres` filter of presets. A preset that still sets `genres` is skipped with a warning
  naming the unknown key, like any other invalid preset.
- `presets.toml`. Define presets as `[presets.NAME]` tables in `config.toml`. AutoDJ refuses to
  start while a `presets.toml` sits next to `config.toml`: move each `[NAME]` table into
  `config.toml` as `[presets.NAME]`, then delete the file. `presets.toml.example` is gone too.
- The Auto-import DJ-software cues switch in the web page's Settings. The settings route no longer
  takes `import_external_cues` and the settings in `GET /api/status` no longer show it; set
  `[playback] import_external_cues` in `config.toml`.
- The page's stand-in versions of the gate stutter, bitcrusher, freeze and glitch effects used when
  AudioWorklet was missing.
- The Entropy walk pick mode, which picked the track least like the one playing and so wandered
  away from the seed. Pick mode now offers Similarity and Random walk. A set saved with Entropy
  walk now follows Similarity. The settings route and saved profiles no longer take
  `smart_shuffle`: a profile that still holds it is refused when applied, so delete it and save it
  again, and `smart_shuffle` in `config.toml` is refused like any unknown key; delete the line.
  Saved web settings still load, and the old field is skipped with a warning in the server log.

### Fixed

- Harmonic mixing set to energy boost picked keys two steps down the Camelot wheel as well as two
  steps up, so the set could drop instead of lift. It now picks only two steps up, and the key
  wheel on the page marks only those keys.
- `liners_folder` in `config.toml` now expands `~` to the home folder, as the example shows; it
  used to look for a folder literally named `~`.
- Cue points imported from Mixxx, Rekordbox, Traktor and Serato only reached tracks the player
  analysed itself, never tracks analysed by `autodj index` or `autodj analyse`, which analyse the
  whole library. Those commands now merge them too when `[playback] import_external_cues` is on in
  `config.toml`. Tracks analysed before
  keep the cues they have; to redo them with imported cues, delete `dj_meta.db` and run
  `autodj analyse`.
- `autodj analyse --limit N` deleted the DJ data (intro, outro, beat grid and cues, imported cues
  included) of every indexed track past the first N. It now keeps all of it and analyses at most N
  of the tracks that still need analysis. A limit below 1 is refused.
- `autodj serve` flags now win over the settings saved from the web page, as the README says.
  `--preset`, `--bpm-range`, `--discovery-every`, `--transition`, the DJ-mix flags and the other
  playback flags used to be replaced by the saved value on start. Settings not given on the
  command line are still restored from `web_state.json`.
- The liner ducking level (Duck depth) now only accepts -30 to 0 dB. A positive value used to
  boost the music, not quieten it, while a liner played. AutoDJ refuses to start with
  `liners_duck_db` outside that range in the config file, the server refuses it, a saved value
  outside that range is ignored on restart, and the web page's Duck depth field refuses it and
  says why, like the other number fields, instead of sending it.
- A Duck depth of 0 (no drop) is now used as 0 when the web page plays a liner, not as minus 12.
- The random and rotate transition choices in the server-side mix (the radio stream) now include
  backspin, as the web page already did.
- Liner clips in subfolders of the liners folder no longer show up in the list. They could not
  be played, because liners are opened by name from the folder itself.
- Mixxx cue import uses each track's own sample rate, so cues on 48 kHz tracks no longer land
  about 9% late. Mixxx outro cues now count as outro markers (they were read as intro markers),
  and Mixxx's hidden "audible sound" range is no longer imported as an outro.
- Discovery stays off after a restart when you turned it off with the Discovery button. AutoDJ
  saved only the discovery rate, so a saved rate turned discovery back on every time it started.
- Crossfade seconds set to 0 now cuts from one track to the next in the web page too, as it
  already did in the server mix. The page used to play a 3 second fade instead. With 0 there is no
  transition effect either. In the Full intro and outro and Outro fade modes, tracks with detected
  markers still fade for 1 to 12 seconds, in the page and in the server mix alike.
- Error messages on the command line keep their config section names. A misspelled key printed
  "unknown  keys: ['bogus_key']" with the section missing; it now prints
  "unknown [playback] keys: ['bogus_key']". The same applied to the enrich and list-devices hints.
- `autodj serve --lan` printed `http://0.0.0.0:8080` as the Web UI address, which the Host
  allowlist refuses, and `--open` opened it. It now prints and opens `http://localhost:8080`, and
  says that other devices use the addresses listed below it. An IPv6 bind address is shown in
  brackets.
- `autodj enrich` with a `[library] beets_db` file that does not exist said the index was
  already in sync with beets. It now fails with "Beets library not found", and `autodj index`
  reports "Enrich failed" with the same message.
- Every command started with faiss logging a `ModuleNotFoundError` while it picked its CPU build,
  and `autodj index` logged each model-download request. Those lines now show only with `-v`.
- Crossfade seconds and Fade-in seconds in Settings saved any length, such as 99 seconds, without
  a word, although the fields go up to 20. The page now refuses a value outside 0 to 20 and says
  why, like the other number fields, the server refuses it too, a saved profile or web setting
  outside that range is not applied, and AutoDJ refuses to start with either one outside 0 to 20
  in `config.toml`.
- Why this track could get its BPM sums wrong, such as "BPM lifts 129 to 133, up 3.", because it
  rounded the two tempos and their difference separately. The difference is now worked out from
  the two numbers it says.

## [0.18.2] - 2026-09-29

### Fixed

- AutoDJ no longer picks silent tracks by itself. Some libraries hold silent files, such as the
  short "[silence]" tracks some albums use as a gap before the next song. In the Entropy walk pick
  mode those sound as far from music as anything can, so every other track was silence; in
  Similarity mode one silent track led to more. Now no pick mode chooses a silent track, and
  neither do discovery, Shuffle, the first track of a set or the playlist command. You can still
  play or queue one from search. Entropy walk is meant to drift across the library; if you want
  the set to stay close to the track you started with, choose Similarity as the Pick mode.
- The discovery rate you saved in Settings works again after a restart. AutoDJ remembered the rate
  but left discovery switched off, so no discovery tracks played and the Discovery button and the
  Settings checkbox both showed it off.

## [0.18.1] - 2026-09-28

### Fixed

- Play now on a search result starts the music on a page that has not pressed Play yet. With the
  browser playing the music, the server switched to the chosen track but the page stayed silent
  until you went back to Now Playing and pressed Play. Play now now counts as pressing Play on that
  page: it starts the chosen track at the page's volume, unpausing the station if it was paused,
  and says "Playing" and the track name once the sound starts. Play next and Add to queue still
  start nothing, and neither does the media Play key on a page where Play was never pressed.
- Keyboard shortcuts no longer act while a dialog is open. With the Clear queue confirmation, the
  Make new link confirmation or the sign-in dialog showing, pressing N skipped the track and the
  other playback and status keys worked on the page behind the dialog. Now no shortcut acts until
  the dialog closes; ? still closes the shortcut list.

## [0.18.0] - 2026-09-28

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
- Shift+L in the web page speaks the current lyric line once, on request. During an
  instrumental break it says "Instrumental" and the next line to be sung; a track without lyrics
  says "No lyrics for this track." Lyrics are still never read aloud on their own.
- New status keys in the web page, each spoken once on request from any tab: Shift+E says the
  elapsed and total time, Shift+V the volume and whether it is muted, Shift+Q how many tracks
  are queued and the first one, and Shift+J the library job status: the running job, how long it
  has run and its percentage when it shows one, or how the last job ended.
- Settings, Announcements, "Announce track changes on every tab". Off by default, which keeps
  today's behaviour of speaking a new track only while Now Playing is showing. Saved in this
  browser only.
- The keyboard shortcuts list and the README now say that NVDA+F2 passes the next key through
  to the page in browse mode.
- Each track in the state that `/api/status` and the WebSocket send now carries `key_spoken`,
  the key spelled out for speech in the chosen notation: "F sharp minor" and "B flat minor"
  instead of "F#m" and "Bbm", which NVDA reads as "F number m". Camelot keys such as "8A" are
  sent as they are.
- Search results have three buttons: Play now, Play next (plays straight after the current
  track) and Add to queue (adds it to the end). The old "Next" button said it queued the track as
  the next one but added it to the end.
- The queue has a Top button on each track and a Clear queue button, which asks before clearing.
  Moving a track now says where it landed, such as "Moved Alpha to position 2 of 5", instead of
  only "Moved Alpha up".
- Settings, Profiles saves the current settings under a name, lists saved profiles, and applies
  or deletes them (deleting asks first). These are the same profile files `GET /api/profiles`
  already served.
- Settings, Browser access (when the server pairs browsers, as with `--lan`): "Sign out this
  browser", and a list of paired devices with their last use and a Revoke button each. Revoking
  this browser's own entry signs it out. New `GET /api/devices` and `DELETE /api/devices/{id}`
  need a paired session.
- Settings can now change how many tracks pass before a song or an artist may repeat
  (`no_repeat_window`, `artist_repeat_window`), the phrase length used by phrase align
  (`phrase_bars`), the filter sweep (`filter_sweep`), the transition effect level (`wet_mix`) and
  the ReplayGain target loudness (`target_db`). The number fields accept the same ranges as the
  server and say the allowed range in words when a value is refused. The values are saved in
  `web_state.json` with the other settings.
- Library tools has an Analyse button, which runs `autodj analyse` to fill in intro, outro, beat
  grid and cue points. When any job finishes, the page loads its whole log instead of only the
  last 25 lines, so a Stats report is no longer cut off, and the Index stats refresh.
- Voice liners have a "Replace existing file" checkbox for uploading over a liner with the same
  name, and each trigger field now says that blank or 0 turns it off.
- History has a Refresh history button, and shows the date next to the time when the page holds
  tracks from before today.

### Changed

- Server-side mixing (`--server-audio` and `--stream`) now skips tracks longer than
  `[playback] server_max_track_minutes` (default 15), with one line in the log naming the track,
  and moves on to the next. The server decodes each track whole, at about 21 MB per minute, and
  holds a few at once, so an hour-long DJ mix could use several gigabytes and exhaust a small
  machine such as a NAS. Raise the setting if you have the memory. Browser playback has no limit.
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
- The web bundle's `build-info.json` now also records a fingerprint of the web sources it was
  built from. In a source checkout, the server refuses to start and `autodj doctor` fails when the
  bundle was built from different sources, even at the same version (for example after a
  `git pull` without `npm run build`). Run `npm run build` to fix it. A bundle built by an earlier
  version has no fingerprint and is refused the same way. Installed packages, which ship the built
  bundle without its sources, are not affected.
- Saved profiles (the JSON files in the `profiles` folder next to your index) with a key AutoDJ
  does not know are now refused with an error naming the key, instead of the key being kept and
  ignored. Saving a profile through `/api/profiles` with an unknown field is refused the same way.
  Profiles saved by earlier versions all contain an empty `"extra": {}` entry: delete that line
  from each file, or save the profile again.
- Saved web settings (`web_state.json`) from earlier versions are ignored, with one warning in the
  log. Your web settings return to their defaults until you change one in the web page, which
  saves a new file. Earlier files stored a harmonic mode even when harmonic mixing was off, and
  reading it now would quietly turn key filtering on.
- When pairing is on, as it is under `--lan`, `/healthz` answers only `{"status": "ok"}` and
  `/api/version` only the version number until the browser is paired. The track count, commit
  and build time need a session. Container health checks still work, since they only need the
  answer.
- The web page no longer reads timed lyric lines aloud as they play. Screen reader users can
  read them on demand in the Lyrics card, where the line playing now is marked as current. The
  automatic reading only worked with the card open, which it is not by default.
- Web page shortcuts other than Space and the Up and Down arrows now work from every tab, so n,
  k, m, s, comma and period no longer need Now Playing to be showing. Space and the arrows still
  act only on Now Playing, where they do not scroll the page.
- `POST /api/play-next` and `POST /api/queue/add` answer 404 with "That track is no longer in
  the library." when the path is not in the index, instead of 200 with `"ok": false`.
- Request failures in the web page are said in plain sentences, such as "The AutoDJ server is
  not reachable.", instead of "Failed to fetch", a request address or a bare HTTP status.
- Cue points are read as minutes and seconds ("drop at 1:35"), the way the seek slider says
  the position, instead of a count of seconds.
- The web page no longer fills in settings the server did not send with guesses meant for older
  servers, and no longer writes a browser console warning when the no-repeat window is bigger
  than the library. The server log already reports that.
- `autodj stats` prints each histogram row as words, such as "120 to 129: 45 tracks, 12
  percent", when its output is not a terminal, which includes the web page's library job log.
  Screen readers read the bar characters as noise. In a terminal the bars stay and each row gains
  a percentage. Ranges are written with "to" and keys as "C sharp" in both.

- "Sign out this browser", and `POST /api/logout`, now revoke this browser's paired device as
  well as deleting its cookie, so the device leaves the paired devices list and a copied cookie
  stops working.
- Applying a profile whose preset no longer exists now fails with a message naming the preset,
  and changes nothing. Before, the preset was skipped without a word and the rest applied.
- Pressing a Library tools Run button while a job is running starts nothing and says which job
  is running. The buttons stay enabled.
- Clearing a voice liner trigger field in the web page now turns that trigger off. Before, a
  blank field left the old value in place on the server.
- The History pager buttons are named "Previous page", "Next page" and "Go to page".

### Removed

- The Recently Played card on the Queue & Search tab. It kept five tracks in the browser, emptied
  on reload and repeated the History tab.
- The "weighted" voice liner rotation mode. It behaved exactly like random. `liners_pick_mode =
  "weighted"` in `config.toml` now stops AutoDJ with an error, a saved `web_state.json` value is
  ignored with a warning, and profiles and `POST /api/playback-settings` refuse it. Use `random`
  or `sequential`.
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
- The `index_name` field of a saved profile. It was stored but never applied. `POST
  /api/profiles` now answers 422 when a request still sends it, and a profile file that still has
  it is refused when it is loaded or applied; save the profile again.
- The empty `web` extra. The browser UI's dependencies have been in the core set for a while,
  so install `autodj` or `autodj[all]` instead of `autodj[web]`.
- `autodj index -a`, `-e`, `--analyse` and `--enrich`. They only restated the default: indexing
  already runs the analyse pass and, when `[library] beets_db` is set, the beets enrich pass.
  Passing any of them is now an error; `--no-analyse` and `--no-enrich` still skip the passes.
- The `[playback] dayparts_dir` setting. Nothing ever loaded that folder (the picker always uses
  the five built-in dayparts), so `autodj backup` no longer archives it and a leftover
  `dayparts_dir` line is an unknown key that stops AutoDJ; delete it. A backup that holds a
  `dayparts` folder can no longer be restored.

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

- A page where Play was never pressed could start playing on its own. The operating system's
  media Play key or media overlay can reach any open AutoDJ tab, and that tab then started its
  deck at the page volume and unpaused the server. In release testing a Firefox page that had
  never been told to play played for about 45 seconds. The media Play key now plays only on a
  page where Play (or Space or K) was pressed earlier in the session, and in stream mode it
  starts Listen here only on a page where Listen here was pressed. The media Pause key only
  stops: on a page that is not playing it does nothing, where before it could unpause the
  server.
- After Delete profile or Revoke is confirmed, focus now goes from the confirmation straight to
  the next row, the Profile name field or the Refresh button. The action now runs while the
  confirmation is still open, and focus moves as it closes. Before, focus went back to the
  button that opened the confirmation first, and NVDA read a line from the top of the page and
  that button before the new focus. Clear queue works the same way and keeps focus on its
  button.
- With `--server-audio`, each incoming track restarted from its very beginning on the sound
  card, replaying the few seconds of the outgoing track's crossfade and the incoming track's own
  skipped intro that had already played during the overlap.
- `/api/history` stayed empty for the whole session with `--server-audio`, because tracks were
  never recorded as played through that output path.
- In the web page, buttons that send a request (Play, Pause, Skip, Mute, Next, Refresh stats,
  Stop running job and others) no longer make NVDA say "unavailable" while the request runs. The
  button stays usable and a second press during the request is ignored.
- Play and Pause now say "Playing" or "Paused" after the button or the Space and K keys change
  the playback state. NVDA with Chrome did not read the button's new label, while Firefox read it
  as well as the state ("Playing", "Pause"). The button is now always named "Play or Pause", with
  only its symbol showing the state, so every browser says the new state once.
- Moving between the section tabs with the arrow keys reads each tab once instead of twice.
  Each tab named its panel with `aria-controls`; showing that panel as the tab took focus made
  NVDA with Chrome speak the tab a second time. The tabs no longer carry `aria-controls`, and
  each panel is still named after its tab.
- Symbols that NVDA skips at its default punctuation level are now read in words: the question
  mark key in the keyboard shortcuts help, "plus or minus" and "plus" in the Harmonic mixing
  choices and descriptions, and the Beatmatch description. The text on screen is unchanged.
- The Transition effect list's description is now one sentence. The notes on each effect, about
  450 words that were read every time the list got focus, moved into a "Transition effect
  details" section below it.
- Stopping a library job with Stop running job now reports that the job stopped, instead of
  "exited with code 1".
- More symbols that NVDA skips at low punctuation levels now have words for screen readers,
  with the text on screen unchanged: the arrow, comma and period keys and the slashes in the
  keyboard shortcuts list; plus, times, arrow, at, equals and minus signs in the setting
  descriptions and Transition effect details; the slashes and sharp sign in the Harmonic mixing,
  Transition mode and Key notation choices; the dash that stands for no value in Up Next and the
  Library stats; and the note shown for an instrumental break in the Lyrics card.
- The Library tools output log now holds each line as its own line on the page, so NVDA's
  browse mode reads it one line per Down Arrow. Before, the whole log was one block of text.
- In stream mode with `--server-audio`, the page's volume slider and Mute now also set the
  volume and mute of the machine's own speakers. Before, they changed only the page's own
  listening, and the speakers kept whatever volume the server started with.
- A new track was never spoken while the Queue, History, Settings or Library tools tab was
  showing, because the announcement lived inside the hidden Now Playing tab. It now comes from
  the page itself (see Settings, Announcements).
- The m, comma and period keys, and Space or k in stream mode, now say what they did ("Muted",
  "1:36 of 3:20", "Listening") when focus is not on the control they act on.
- Skip no longer moves focus to the Skip button after every skip, and a failed Play, Shuffle,
  Mute or Discovery no longer pulls focus to its button. Pressing n or m while on the seek
  slider, an EQ slider or the lyrics now leaves focus there.
- Search results, profiles and voice liners stop moving focus after a request too.
  Play now, Play next, Add to queue and Apply leave focus where it is when they finish, and so
  does a failed liner delete. Deleting a profile or a liner, or revoking a device, still moves
  focus to the next row because the one you were on is gone, but only if focus has not already
  moved somewhere else while the request ran.
- With Why this track open, a track change no longer reads the whole reasons list after the
  title. Pressing Reset EQ twice in a row now confirms both presses.
- The track-change announcement and Shift+K say the key in words, such as "F sharp minor",
  instead of a label like "F#m" that NVDA reads as "F number m".
- The library job clock can be read again, and a job that fails says why, taken from the last
  line the job printed, instead of only its exit code. The job runner's own closing line
  ("autodj-jobs exit 1 (elapsed 1.0s)") was being spoken as the reason; it is now skipped, so a
  failed Enrich says "No beets_db in config — enrich requires beets."
- Shift+J during a job read its raw progress bar, block characters and all ("Pruning: 43 ████▎
  32919 76728 00:22 00:29, 1477.44file s"). It now says the job, the time in words and the
  percentage: "prune running, 27 seconds elapsed, 43 percent done." Job times in the finish
  message are in minutes and seconds too, and "1 seconds" is now "1 second".
- NVDA no longer calls the queue and search result lists "clickable".
- A search result's Play now failure is no longer reported as "Could not update queue".
- A setting that fails to save goes back at once and says so, for example "Could not save
  Beatmatch; it is still on.", instead of flipping back silently a second later. The Preset and
  Harmonic mixing lists no longer reset while they have focus.
- The pairing dialog's instructions are part of the dialog's description only, no longer of the
  code field as well, and when a paired browser is signed out the dialog says so. NVDA with
  Chrome still reads the dialog's name and instructions twice when it opens, on load and after
  signing out: that browser and screen reader read any named dialog twice as focus moves into it.
  The dialog keeps its name, which it needs.
- The Play, Skip and Mute buttons no longer carry a tooltip that NVDA read as a description
  repeating the button name. Skip's said "the next sonically similar track", which was wrong
  with a queue or in random mode.
- The section tabs are now inside a navigation landmark named Sections.
- Voice liners' Test now button asks the server to play the liner whenever the server mixes the
  audio, with `--server-audio` as well as in stream mode. Before, with plain `--server-audio` it
  tried to play the liner in the browser, which is not playing anything, so nothing was heard.
- Voice liners never played with plain `--server-audio` (without `--stream`): only stream mode
  started the server's liner scheduler. They now play on the same triggers, with the same ducking,
  while a track is playing and not paused.
- With `--server-audio` or `--stream`, a track starting no longer waits for the server to finish
  choosing the track after it. The choice works on a copy of the play history, so the page and
  the stream title update the moment the new track begins.
- `autodj stats` no longer shows an always-empty "180 to 189" BPM row next to "180+".
- A preset genre that AutoDJ does not know, such as a misspelling in `genres = ["rock",
  "vaporwav"]`, was dropped without a word, which narrowed the filter or, when no name was
  known, turned it off. The preset is now skipped with a warning that names it, the unknown
  genres and the genres AutoDJ knows. A `genres` value that is not a string or a list is
  refused the same way instead of meaning no filter.
- In Firefox's open drop-down lists, NVDA read the Harmonic mixing choices without their symbols
  ("same side,  2 jump"), because Firefox reads an option's text there and ignores its
  `aria-label`. The Harmonic mixing, Transition effect, Transition mode and Key notation choices
  now say it in words on screen too, such as "plus or minus 1", "slice and reorder" and "Musical
  (C, A minor, F sharp minor)", and carry no `aria-label`.
- Why this track now says each change in words: "Camelot key 8A to 12A", "Energy similar (0.25
  to 0.25)", "BPM lifts 100 to 130, up 30" and "relative major or minor flip". NVDA skipped the
  arrow at its default punctuation level, so the two values ran together.
- Deleting the last saved profile dropped focus to the page for a moment, so NVDA read the page
  title three times and the banner before reaching Profile name, and revoking a device did the
  same before reaching the next Revoke button. Moving focus before removing the row was not
  enough: the browser told NVDA about the removal and the new focus in one update, the removal
  first. The deleted row now stays, hidden and out of the tab order, for a moment after focus has
  moved to the next row or to Profile name, and only then goes. Deleting a voice liner works the
  same way.
- Voice liners' Test now said nothing and did nothing while browser playback was stopped, and
  played silently while it was paused. In browser playback it now plays the liner at the page
  volume whether or not music is playing, before the first Play and while paused included. Only
  Mute keeps it silent, and then it says "Muted, so the liner was not played." Scheduled liners
  still play only over music that is playing.
- In browser playback, un-pausing while muted left the music stopped until Mute was turned off.
  Muted music now keeps playing, silently, like on the server.
- With the page opened as localhost in stream mode, the note that the stream address only works
  on this computer is now part of the description of the address field and Copy address, so it is
  heard when tabbing to them. Before, it was only found by reading on in browse mode.
- Tabbing to the Library tools output log made NVDA read the whole log at once, about 3,400
  characters for a Stats run. The log no longer takes focus and is no longer a scroll box; it
  grows with the page and is read in browse mode a line per Down Arrow, as before. Its name now
  carries the line count, such as "Output, 42 lines".
- Browser playback started at full volume after the page loaded, whatever the volume slider
  showed, and stayed there until the slider was moved. Voice liners and the noise sweeps of some
  transition effects also played at full volume. They now all play at the volume the slider
  shows, from the first sound, and again after a reconnect.
- Any volume below about 23 percent came back from the server as 0: Shift+V and the slider said
  0, and the slider jumped back to 0 a moment after each arrow press. The server rounded the
  volume to two decimal places, and on the slider's loudness curve every setting below 23 percent
  is under 0.005. The server now keeps and sends the exact value, so 1 to 22 percent can be set,
  are spoken and stay put.
- Transition effects no longer play louder than the music. In browser playback, echo and reverb
  tails, noise sweeps, horns, sirens and replayed audio went straight to the speakers, past the
  volume, and kept sounding through Mute and Pause. Every sound the page makes, voice liners
  included, now goes through one master volume, and each effect peaks at or under the deck it
  works on: the dub delay, echo out and reverse reverb had been up to 8.5, 6.5 and 4 dB over the
  music, several others 1 to 3 dB over, and the telephone effect about 6 dB louder than the
  track. The Transition effect level setting now applies in browser playback too. With
  `--server-audio` or `--stream`, the noise sweeps, air horn and dub siren were fixed levels that
  stayed as loud when ReplayGain turned the music down, which put them 3 to 11 dB over it; they
  are now set against the music they play over. The reverse reverb there was about 17 dB over the
  music and clipped, and no server effect now peaks above the music it treats.
- The m shortcut worked once and then did nothing when NVDA passed the keys to the page, until
  the browser window lost focus. The page waited for m's own key release, and keys passed through
  NVDA are released under the name "Unidentified". Any key release now lets every shortcut fire
  again, and so does switching away from the tab.
- In Windows High Contrast and other forced-colours modes, the volume and EQ slider thumbs now
  take the system highlight colour and a text-coloured border as intended. The rule named the
  Chrome and Firefox thumbs in one selector list, so each browser dropped the whole rule.
- In browse mode NVDA read the Play or Pause button as one word, "PlayorPause", in Chrome, and as
  three buttons, "Play", "or" and "Pause", in Firefox. Its name was built from the visible words
  and a hidden " or " between them. The button is now named "Play or Pause" outright, and its
  symbol and visible "Play/Pause" are hidden from screen readers, so both browsers read the one
  name.
- After the AutoDJ server restarted it came back at full volume, and a page that had been
  playing took that volume and started playing again by itself within a few seconds. The server
  now keeps the volume and Mute in its saved settings and restores them when it starts. The page
  never starts playing on its own after its connection drops, and neither do voice liners: press
  Play (or Listen here in stream mode) and the current track starts from its beginning. As a
  safety net, after a dropped connection the page does not take a louder volume from the server
  than the one it was playing at until you change the volume on the page; a quieter one still
  applies.
- The first Play after pairing could say "Playing" while nothing played, when the server had
  been left paused; the second press worked. Seen in Firefox and Edge. Play now unpauses the
  server before it starts the music, says "Playing" only once the music has really started, and
  otherwise says "The browser did not start playback. Press Play again."
- With `--server-audio` or `--stream`, the level jumped where a transition effect began or
  ended. Holding an effect under the music turned its whole tail down whenever any part of it
  peaked too high, so a dub delay's tail started 1.5 to 3 dB under the audio just before it, and
  several effects started at their own level: echo out dropped 9 dB at the join, the telephone,
  sidechain pump and reverse reverb 7 to 8 dB. A treated tail now starts as the untouched track
  and blends into the effect over a quarter of a second, a treated head blends back into the track
  the same way, and only the moments that peak too high are turned down.
- In browser playback, the crossfade that starts early when a track ends in silence stopped
  working after the first transition that used an effect. Setting up and removing an effect cut
  the deck off from the level meter that listens for the silence. The meter now stays connected.
- In stream mode, Listen here could play at full volume while the volume slider showed a low
  level, until the slider was moved. A page left at 5 % when the server restarted in stream mode
  played the stream at 100 %, and a page opened in stream mode showed 100 % whatever the server
  had saved. The page's volume and Mute now reach every sound it plays, the stream included,
  before it starts and after every change, reconnect or update from the server. A page opened in
  stream mode starts at the server's saved volume and Mute. The rule that a dropped connection
  never brings back a louder volume still applies.

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

[Unreleased]: https://github.com/blindndangerous/AutoDJ/compare/v0.19.0...HEAD
[0.19.0]: https://github.com/blindndangerous/AutoDJ/compare/v0.18.2...v0.19.0
[0.18.2]: https://github.com/blindndangerous/AutoDJ/compare/v0.18.1...v0.18.2
[0.18.1]: https://github.com/blindndangerous/AutoDJ/compare/v0.18.0...v0.18.1
[0.18.0]: https://github.com/blindndangerous/AutoDJ/compare/v0.16.1...v0.18.0
[0.17.0]: https://github.com/blindndangerous/AutoDJ/commit/6b6834ca6079d0942867d59f3779cb284c41580a
[0.16.1]: https://github.com/blindndangerous/AutoDJ/compare/v0.16.0...v0.16.1
[0.16.0]: https://github.com/blindndangerous/AutoDJ/compare/v0.12.0...v0.16.0
[0.12.0]: https://github.com/blindndangerous/AutoDJ/releases/tag/v0.12.0
