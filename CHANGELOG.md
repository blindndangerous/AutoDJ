# Changelog

What changed and when, written for the people who use AutoDJ.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Added

- `autodj setup --no-model` installs basic music import, browser playback and
  streaming without MuQ, PyTorch, Transformers or model weights. Local model
  analysis and worker execution remain disabled until a model-capable runtime
  is selected. Sound-card output packages are optional too. See
  [no-model setup](docs/setup.md#no-model-installation) for capabilities and upgrades.
- `autodj worker --idle-release MINUTES` keeps a GPU worker running between batches without
  holding the graphics card. It loads the model, and runs `--before-command`, only when songs
  arrive, and frees it and runs `--after-command` once the queue has been empty that long. Its
  heartbeat keeps the server's own CPU analysis paused, so a machine left running as a service
  analyses new songs whenever it is up, and the server analyses them itself when it is off.
- A related tracks entry can name its artist as `*` to mean every track with that title (#411).
  A medley written with `"* - Let It Snow! Let It Snow! Let It Snow!"` holds back every recording
  of the song in the library, including ones added later, instead of a line that names each one.
- Time check voice packs and clips are managed in **Settings**, **Time checks** instead of
  `config.toml` (#388). Every folder in the station's time checks folder is a voice pack, found
  without a restart and without a `voice_packs` line; packs still listed in `voice_packs` keep
  working, and **Look for new voice packs** finds folders copied in by hand. **Add a voice pack**
  uploads a zip of the pack's WAV, MP3, FLAC or OGG files (up to 256 MB, and 2000 files or 512 MB
  unpacked) and asks before it replaces a pack of the same name; any other file in the zip, a
  path outside the pack or a link refuses the whole zip, and nothing is left behind. A pack added
  here has a **Remove** button. **Recorded minute clips folder**, **Intro clip** and **Outro
  clip** are chosen from the files in the time checks folder, and **Upload a clip** adds one.
  Only the text-to-speech command stays in `config.toml`, because it runs a program on the
  server; `clips_folder`, `intro_clip` and `outro_clip` there are now starting values.
- Announcer breaks can be turned on and off for each station in the new Settings, Breaks tab,
  without editing `config.toml` or restarting. The tab also sets where a DJ break goes
  (automatic, talking up the next song, or between the songs), the shortest intro to talk up,
  and how far the music drops under a break. Turning breaks off drops the breaks still waiting
  for their song change, and the announcer program is told breaks are off.
- Listener requests can be turned on and off, and their approval set (you approve each one, or
  automatic), in the Listener requests card in Queue & Search, which now shows while requests
  are off. The request page opens or closes at once. While closed it says "Requests are closed",
  also to a listener whose page was open when requests were turned off.
- `[breaks]` and `[requests]` in `config.toml` stay in charge of these settings until you save
  them on the page for the station; saving another setting, such as the volume, does not change
  that. Once saved there, the page's values are kept with the station's other settings and win at
  every start, and the log says so once when they differ from `config.toml`. The break upload
  limits and quotas, and the request limits, stay in `config.toml`.
- Stereo Tool can be set up from Settings, Sound, with no configuration file. After you choose
  Stereo Tool and tick that you accept Thimeo's terms, **Download Stereo Tool** fetches Thimeo's
  Stereo Tool plugin (about 320 MB) straight from thimeo.com and keeps only the library the server
  needs (64-bit Linux PC or ARM, or 64-bit Windows) in the index folder; it then reads **Update
  Stereo Tool** and the page shows the installed version. AutoDJ still does not include Stereo
  Tool, and the container image has none: use the button, or copy the library into
  `tools/stereo-tool/` in the index folder. A station that keeps the default `stereotool_path`
  finds it there with no configuration change.
- **Stereo Tool licence key** in Settings, Sound saves your key where only AutoDJ can read it,
  outside backups, and never shows it again; the page says "Licence set" or "No licence", then
  whether Stereo Tool itself says the key covers the preset, and **Clear licence** removes it. One
  key serves every station, or tick **For this station only** to give one station its own.
  Installs without a browser can set `AUTODJ_STEREOTOOL_LICENSE_KEY` or
  `AUTODJ_STEREOTOOL_LICENSE_FILE`, or a station's `[playback] stereotool_license_file`. Stereo
  Tool restarts with a new key or a new library; a key for one station restarts only that station,
  and saving the same key again restarts nothing.

### Changed

- **Breaking:** plain package installs now omit MuQ and Transformers. Direct
  wheel installations that need local model analysis must select `index`,
  sound-card output needs `play`, and `all` includes both. Preserve the desired
  extra when upgrading. Guided default CPU/GPU setup and the container retain
  model analysis. In no-model mode, `autodj index` saves playable songs and
  reports pending analysis successfully; remote analysis requires an existing
  compatible server-side model cache. See [setup](docs/setup.md#no-model-installation).
- Liners and voice tracks use the same preparation loudness as music in Smooth and Stereo Tool.
  Produced IDs no longer receive an extra compressor; spoken breaks and matched intros use
  lighter 2:1 compression. AGC drive and master processing stay unchanged. **Music duck (dB)**
  in Settings, Liners defaults to 3 dB, uses smooth ramps, and also applies to scheduled IDs.
  Existing saved duck depths, including zero, are kept; break-specific ducking still takes
  precedence for spoken breaks. Old liner measurements refresh automatically (#402).

- **Breaking:** `[playback] stereotool_path` now names Thimeo's Stereo Tool plugin library
  (`libStereoTool_*.so` or `.dll`), not the command-line program `stereo_tool_cmd`. A
  configuration that points it at `stereo_tool_cmd` stops processing with Stereo Tool after the
  upgrade: the station plays on with its limited bypass and says the file "is not Thimeo's Stereo
  Tool plugin library". To fix it, delete the `stereotool_path` line and press Download Stereo
  Tool in Settings, Sound, or point the line at the library from Thimeo's plugin zip.
- Stereo Tool now runs Thimeo's plugin library (`libStereoTool`, as AzuraCast and Liquidsoap do)
  in a helper process of AutoDJ's own, instead of the command-line program. The licence key
  reaches it over that process's input, never a command line, and a crash in Stereo Tool ends only
  the helper. Linux on 64-bit PCs has been tested with Thimeo's real plugin; Windows and 64-bit
  ARM have not been tested with it yet.
- A station set to Stereo Tool without the library now says "Stereo Tool is not installed.
  Download it in Settings, Sound, with the Download Stereo Tool button." A station whose own
  `stereotool_path` names a missing library says that path is missing instead.
- A new index name, given with `--name`, may not contain `:` or end with a dot or a space,
  because Windows reads such a name as another folder: `D:music` is a folder on drive D:, and
  `chill.` is the folder of `chill`. An `[index] name` already in `config.toml` that does this
  still loads, with a warning asking you to rename the index folder and the setting. Only a name
  that would put the index outside `index_dir`, such as `D:music` on Windows, stops AutoDJ at
  startup, with a message saying so.
- Two relays whose names read the same password variable, such as `a-b` and `a_b`, stop AutoDJ
  at startup, and are refused in Settings, when that variable is set, so one server's source
  password is never sent to another. While it is unset, each relay keeps its own password.
- Long groups that are set once or read now and then are now cards, closed until opened: Repeats
  and Test rotation in Settings, Song picking; Relays, More formats, Now-playing feed and Listener
  statistics in Settings, Stream; Rules for every break and Ad report in Ads; and Uploaded clips in
  Carts. A field with an error opens its card before it takes focus. In Settings, Stream,
  Broadcast recording now comes before these cards, just after the general settings.

## [0.21.0] - 2026-10-14

AutoDJ 0.21.0 is the first release since 0.19.1. It also ships everything listed under 0.20.0
below, which was never released on its own. One server can now run several stations, each with
its own schedule, clockwheels, liners and stream, and stations can be created or duplicated from
the web page. A live DJ can take over a stream from BUTT or Mixxx, and the **Talk** button speaks
over the music from the browser's microphone. Listeners can request songs from a public page, and
AutoDJ can play up to three bridge songs to reach a distant request. The new Ads tab runs
scheduled ad breaks, clockwheels can hold ad-break steps, and both are reported. Intro rules play
a voice liner as a matching song starts. Hour fitting picks songs that end close to the top of the
hour, and the Hour preview lists what plays over the coming hour. Spoken track landmarks read a
song's structure with keys and speech instead of a waveform. Library tools can rescan the library
automatically, and the lists and tables share one way of moving with the arrow keys. Also new are
voice tracking from the browser, a cart wall, shows, more stream formats with listener statistics
and a light theme. The library is now kept in one catalog, `library.db`: a library from an older
release must be upgraded with `autodj migrate-index` before this version can use it. AutoDJ
is now licensed under the AGPL-3.0-or-later instead of MIT. The screen-reader sample for this release is in
[docs/accessibility-samples/0.21.0.md](docs/accessibility-samples/0.21.0.md).

### Changed

- Focus arriving in the Search text field in Queue & Search, or in the Shows song search, by
  keyboard or when the page puts it back selects the text already there, so typing replaces the
  old search. A click still places the caret where it lands. Enter in Search text runs the
  search, as before.
- Times the server writes on the page, such as the hour preview, now use 24-hour time like the
  rest of the server's text. Predicted ad breaks give their length as 1:00 rather than about 60
  seconds (#281).
- The hour preview's summary and backtiming lines are shorter. They keep the song names, fixed or
  predicted, how early or late, and how far a song runs over; the long notes under the table moved
  to the operations guide (#283).
- The History Track info button no longer reads a stray comma for a song with no artist (#283).
- AutoDJ is now licensed under the GNU Affero General Public License, version 3 or later
  (AGPL-3.0-or-later), instead of MIT. Anyone who runs a modified AutoDJ that people use over a
  network must offer them its source code. Releases up to and including 0.19.1 stay available under
  MIT (#276).
- Save tags in Track info now saves the tags in AutoDJ only and leaves the file alone. Check
  Also save the tags to the file to write them into the file as before; it starts unchecked
  for every track (#258).
- The main tabs are now Now Playing, Queue & Search, Track info, History, Carts, Voice tracks,
  Ads, Library tools and Settings, in that order. Alt+1 to Alt+9 open them (#256).
- The Keyboard shortcuts button is at the bottom of every page, above the version line, next to
  a new Changelog link (#253).
- Library tools can rescan the library automatically every so many hours and minutes (#249).
- DMCA checks on queued songs, listener request placement and show songs now use the hour
  preview's timing: the playing song's position, mixed handoffs, saved start and segue cues,
  overlapping liners and music speed, instead of whole file lengths (#217).
- A listener request is no longer placed where a queued song after it would then break a repeat
  rule or DMCA limit. Rules a queued song already broke do not count against the request (#218).
- The page title no longer shows the album. It names the station whenever the station is not
  called AutoDJ, even on a server with one station (#251).
- Windows setup offers to install missing uv and FFmpeg with WinGet, explains what each tool
  does, and gives manual installation steps when needed. The quick start uses one launcher
  command, with instructions for moving to another computer.
- History shows only the time on each play, with a date heading for each day instead of
  repeating dates on every row. Live updates keep existing rows and keyboard focus in place.
- History, Hour preview and the other action lists use the same arrow-key navigation as
  clockwheel steps. Ctrl+Home and Ctrl+End move to the first and last row. History and Hour
  preview now offer Track info on each song row, and narrow tables scroll inside their own area
  so the page stays readable (#250).

- Settings keep longer help available to read without repeating it on every field focus.
  Track counts, hour preview and matched-intro explanations are shorter (#235).
- Preview columns, show lengths and duration messages use numeric times for both visual
  and screen-reader output, with hours shown for durations of an hour or more (#235).

- New libraries start empty; add music folders in Library tools. Setup no longer asks for a music
  root, and startup never registers one implicitly. Legacy relative paths remain supported, with
  an explicit `migrate-index --music-dir` option for old index imports.

- Startup and backups use `library.db` without loading old index generations. Older libraries
  must be upgraded with `migrate-index`; leftover manifests no longer affect a current catalog (#223).

- Multiple stations can use independent Stereo Tool processing instances when configured with the
  required per-instance licences and executable paths (#163).
- Library Analyse, Enrich and Detailed Stats now read the current catalog. Analyse runs CPU DJ-meta
  work without MuQ, publishes automatic cues while preserving manual cues, and skips completed
  tracks across all registered folders. Existing DJ-meta cache entries are preserved. The web summary
  stats remain separate. The web no longer starts the legacy prune job.
- The Library panel can preview and confirm removal of tracks missing from available source folders.
  A clean scan and unchanged preview are required; more than 20% missing requires a second
  confirmation. Catalog cues and vectors are removed with the record, while audio files and play
  history remain. Disconnected and unreadable sources are protected.
- `prepared_path` is no longer part of the catalog schema API, serialization or change tracking.
  Existing SQLite columns are ignored without rebuilding the database.
- Hour preview rows now use the same compact timestamp and duration on screen and in screen-reader
  output. A refresh keeps a predicted choice while it remains eligible and tied for best, while
  still following the picker when eligibility or play history changes.

### Added

- Stations can be created or duplicated from the web page without restarting AutoDJ. New
  stations start stopped with their own state and recording folder (#233).
- Spoken track landmarks (#60): a song's structure through keys and speech, in place of a
  waveform. A new station route, `/api/landmarks`, merges the cue points, the DJ analysis (drops,
  breakdowns, build-ups, phrases, named DJ-software cues, the beat grid for bar numbers) and the
  synced lyrics (where the singing starts) into named landmarks, merged when they fall together
  and kept in a cache until the song's cue points, analysis or lyrics change. Track info lists
  them under **Song structure** ("Intro, 0:00 to 0:32", "Vocals from 0:45", "Drop, 1:35, bar 49"),
  with times numeric on screen and in speech alike. **Right bracket** and **Left bracket** go to the next or
  previous landmark and say it: in Track info they move the cue point preview ("Preview: Drop,
  1:35, bar 49"); elsewhere they say the landmark of the song on air ("On air: ...,
  in 0:12") and move the song only with the new **Bracket keys seek the song on air**
  setting (Settings, Keyboard; off by default). **Shift+U** says what is coming next on air ("On
  air: Drop in 0:12, 6 bars"). Each answer is said once, politely, without moving focus,
  and the keys follow Enable keyboard shortcuts.
- `POST /api/seek` takes an optional `path`: the song the seek is for. When another song is
  playing, the server refuses with 409 ("The song on air has changed, so it was not moved.").
  Pages that leave it out seek as before.
- The lyric timing the mix and the landmarks use is read again when a song's file or its `.lrc`
  sidecar changes, instead of being kept until restart.
- `autodj migrate-index` now completes the move from supported schema-3/4 published indexes into
  `library.db`, carrying vectors and catalog state without reanalyzing audio. It also explicitly
  upgrades version-1 web settings, old play-history JSON, and initial folder registrations, with a
  preview, recovery copy, and safe retry after interruption.
- Ad breaks as a clockwheel step (#51): the new **Ad break** step type plays spots from the Ads
  tab at the song change before the next song the clockwheel picks. It holds up to **Spots in the
  break** and **Longest break in seconds**, and can keep to one **Advertiser** or **Competing
  category**. Its spots are picked, booked and reported like any ad break's (flight dates, limits,
  separation, late and missed), with the source "Clockwheel step". When no spot can play, the
  step is skipped, the music goes on and the ad report says why. The hour preview shows each ad
  step as a predicted ad break with its expected length.
- Scheduled ad breaks with reports (#51): a new **Ads** tab keeps spots (an audio file with its
  advertiser, campaign, competing category, flight dates and daily and hourly limits) and ad
  breaks (the minutes past the hour they run at, such as :15 and :45, with hours, days and a date
  range, and how many spots and seconds they hold). A break also runs from a Schedule event with
  the new **Ad break** action, or from **Run now**. When a break falls due, its spots are picked
  (in flight, under their limits, the one heard longest ago first, never two from one advertiser
  or competing category in a break, and kept apart by the separation time) and booked on the
  open song change through the announcer breaks' slot model, so they play between two songs and
  never cut one; the render-ahead worker mixes them, nothing new runs on the audio thread. Every
  spot booked, aired or missed is appended to the station's ad play log, flushed line by line, so
  a crash loses at most the line being written. Late spots (past **Late after**) and missed ones
  (no song change in time, skipped, no spot could play, the computer slept, AutoDJ stopped) are
  logged with the reason in words, under one rule (**Missed after**) for late, slept, stalled and
  stopped time; breaks due while AutoDJ was down are logged at the next start, a long gap's extra
  breaks in one summary line, and a spot cut off on the air as "may have aired". Song changes are
  planned only from 20 minutes before a break is due, so with no break due songs are picked
  exactly as with ads off. **Ad report** shows, for chosen days, totals by advertiser and
  campaign and every spot, as tables with column and row headers, and exports either as CSV for
  an affidavit; the play log export lists aired spots as `kind` `ad`. The hour preview shows a
  booked ad break as fixed and a coming one as predicted, with its time as a fixed point to time
  to. Turn it on with `[ads] enabled = true` (saved-point playback with server audio or a stream).
  Spots and breaks are kept per station in `ads.json` (a bad entry is skipped and kept, a bad file
  set aside), the audio in `ads` and the log in `ad_plays`, all in backups. Uploads are capped by
  `[ads] max_upload_mb` before the body is read and decoded one at a time off the event loop with
  the breaks' bounded decoder. Routes: `/api/ads` and `/api/stations/<name>/ads`. See
  [Ad breaks](docs/operations.md#ad-breaks).
- Intro rules (#127): Library tools > Intro rules plays a voice liner as the next song starts
  when that song matches clockwheel conditions, such as an artist name drop, a genre, a decade, the
  first-release year, any Beets field, a folder or a saved category. Each rule chooses registered
  voice liners or a liner folder, with a chance, a wait between plays that holds across restarts,
  play dates and a priority next to the song's and artist's own matched intros (after them by
  default). Each station keeps its own rules in `intro_rules.json`, which backups include.
- More stream formats (#50): beside the MP3 stream, a station can serve the same mix in AAC
  (`/stream.aac`) and Opus (`/stream.opus`), and at `/stations/<name>.aac` and `.opus`, each at
  its own bitrate. Turn them on in Settings, Stream, **More formats** (AAC quality, Opus
  quality), or with `[stream] aac_bitrate` and `opus_bitrate`; Settings lists every format's
  address with its own Copy button. Turning a format on checks first that ffmpeg can encode
  it, and refuses with ffmpeg's reason when it cannot. Each format runs its own encoder on the
  same mix, only while someone listens to it (and a minute after), through its own short queue,
  so a slow encoder never holds up the mix or the other formats; each costs a few percent of one
  CPU core while it runs (measurements in the operations guide). AAC players get song
  titles as MP3 players do; Opus streams carry none. `max_listeners` now counts the listeners of
  every format together, and a listener on any format starts an idle set.
- Listener statistics (#50): Settings, Stream, **Listener statistics** says in words and in two
  tables how many are listening now on each format, each day's peak and when it was, the number
  of sessions, listening hours, the average and longest session, and how long sessions lasted,
  for the last 7, 30 or 90 days or every day kept. **Download statistics (CSV)** saves one row
  per day. Only daily counts are kept, never an address or player name, for
  `[stream] stats_days` days (90 by default; 0 keeps none), in each station's
  `listener_stats.json`, which backups include. Routes: `/api/stream/stats` and
  `/api/stream/stats/export`, and the same under `/api/stations/<name>/`.
- Shows (#106): Library tools has a **Shows** card where you plan a show such as "Remembering
  the 2000s" ahead of time as a list of songs. Choose where its songs come from (a category, so
  any genre, year or beets field, or a clockwheel with its step conditions) and a target length,
  then **Generate list**: a dry run of the station's own picker, the one the rotation tester
  uses, picks the songs under the repeat rules and DMCA limits without touching the air,
  optionally as if at a later date and time. The list is then yours: **Move up**, **Move down**
  and **Remove** buttons on every song (keyboard operable, focus stays on the moved song), songs
  added from a library search, and the total length against the target. A schedule event with
  the new action **Play a show** airs it at a date and time, once or repeating; hard timing plays
  its first song at once, fading out the song playing, and soft timing queues it to follow the
  song playing. On the air the list feeds the queue two songs
  ahead, so liners, time checks and breaks behave as with queued songs, and normal programming
  resumes when the list ends. Show songs are pre-approved for the repeat rules, but a song that
  would break a DMCA limit is skipped and logged, checked again at every song change. The card
  says which show is on the air and what was skipped, with **Stop show**. The hour preview lists
  the rest of a show on the air, and a scheduled show's songs from its time, as fixed, and a
  song the DMCA limits would skip as skipped. Shows are
  kept per station in `shows.json` (a damaged entry is skipped and kept, as with matched intros)
  and included in every station's backup. A show cut short by a restart resumes at its earliest
  unplayed song while its list would still be running; the card says so until then, and Stop
  show cancels it. `show_on_air.json`, which holds that place, is left out of backups and cleared
  by a restore. Queue & Search removes and moves the very entry pressed, so a listener's
  request and a show song of the same file never swap. While a show is on the air (or about to
  start) listener requests go after its songs and are told "after the station's show" rather
  than a time. A song may be listed twice, with a warning. Routes: `/api/shows` and
  `/api/stations/<name>/shows`, with request bodies capped for POST and PUT. See
  [Shows](docs/operations.md#shows).
- A schedule event whose action this version does not have, such as one a newer AutoDJ added,
  is now kept exactly as it was with every save instead of being set aside, so moving between
  versions loses no event. An event with settings its action does not accept is still set
  aside as before.
- Live DJs and talkover (#48): in stream mode a live DJ can take over a station from BUTT,
  Mixxx or another Icecast source client, connecting to AutoDJ's own address and port at the
  mount `live` (or `stations/<name>/live`) with their own username and password, added in
  **Settings > Live DJ** (passwords are stored as scrypt hashes and never shown).
  When the DJ's audio arrives AutoDJ crossfades to it and holds automation exactly where it
  was; when they leave, or go silent for 15 seconds, it comes back with the next song,
  rendered again for the time it airs. Hard-timed schedule events during a set take effect
  when the DJ hands back, and liners, time checks and carts are not played over them. The page
  says "Live: <name> since <time>" once when a DJ goes on or off, and the public feed, status
  page and stream title show the DJ. Each set is a History row and a `live` row in the CSV
  play log, left out of SoundExchange reports. Hold **Talk** on Now Playing (mouse, touch,
  Space or Enter on the button, or Alt+M anywhere) to speak over the music from the browser's
  microphone, with the duck amount and return time set per station; one person talks at a
  time. Sources need HTTPS or this machine, unless `[server] live_plain_http_lan = true` also
  allows plain HTTP from the local network. Backups keep the DJs but not their password
  hashes, so set DJ passwords again after a restore. See
  [Live DJs and talkover](docs/operations.md#live-djs-and-talkover).
- Settings > Stream can edit Icecast and Shoutcast relays while the station runs. Relay definitions
  and passwords are saved per station; environment-managed passwords remain read-only. Station
  name, genre, description and website URL are sent where each stream protocol supports them. The
  saved relay file is readable only by the account AutoDJ runs as, on Windows too, and backups
  leave it and its passwords out; a restore keeps each station's own relay settings.
- Listener requests can use up to three optional bridge songs to approach a distant request.
  Requests now check the active clockwheel music step and daypart at their projected start.
  Operators can choose whether a request with no smooth spot waits or uses the best legal spot;
  bridge status and the reason appear in queue, preview and request API data (#49). Queue &
  Search > Listener requests has a Smooth transitions form for these settings, and the queue
  marks each bridge "Bridge to <request>". Removing a request removes only its own bridges.
- Music speed (#162): Settings > Sound > Music speed has a slider and exact numeric field for
  10% slower to 10% faster. Speed and pitch change together. Browser music changes speed when
  safe; voice liners and previews stay at their normal speed. In stream and server-audio modes,
  the new speed takes effect at the next unrendered audio-buffer boundary. Cue points, lyrics,
  catalog durations and playback progress remain on the source-file timeline.
- Hour fitting (#126): **Fit songs to the hour** in Settings > Song picking chooses, near the
  top of the hour, a clockwheel restart or a hard scheduled event, the song whose end lands
  closest to it, out of up to 20 songs the station would allow anyway. A song that would end
  just short of the hour, leaving a gap the next whole song overruns, counts as a poor fit.
  **Minutes before to start fitting** is 10 by default (1 to 30). Off by default; in
  `config.toml` they are `[playback] hour_fit` and `hour_fit_window_minutes`. The rotation tester
  and the hour preview pick the same way.
- Hard schedule events have **Wait up to seconds** (0 to 300, default 0) in the schedule editor:
  when the song playing will end within that many seconds after the event's time, the event waits
  and runs at the song change without fading it out; if the song runs past the limit, the event
  runs then with the usual hard fade. The hour preview shows the wait. A schedule with no waits
  set is still read by older versions of AutoDJ.
- Daypart and hourpart song separation (#129): two new repeat rules in Settings > Song picking >
  Repeats keep a song from playing at the same time every day. **Prevent a song from playing in
  the same daypart** holds back a song that played in the daypart the next song starts in (such
  as morning drive, 6 to 10) earlier today or on up to 7 days before; the **Daypart start hours**
  are yours to set, 0, 6, 10, 15, 19 to start with. **Prevent a song from playing in the same
  quarter of the hour** holds back a song from the quarter of the hour its last 1 to 3 plays in
  the past week started in. Both are off until set, read the station's clock, count every version
  of a song with **Count versions as one song** on, and give way with the artist, album and title
  rules when nothing else fits, never the DMCA limits. The rotation tester and the hour preview
  pick with them as the station does, and the tester reports how often each was relaxed. In
  `config.toml` they are the `daypart` and `hourpart` tables of `[playback.repeat_rules]`; unknown
  keys there are ignored with a warning. See
  [Time of day](docs/track-selection.md#time-of-day).
- Saving edits to the active clockwheel can now apply at its next restart, immediately from
  step 1, or immediately continuing at the next step number. A song already playing and its
  committed handoff finish normally. With no restart minutes, deferred edits apply when the
  wheel wraps to step 1; the editor shows when an edit is waiting.
- **Why this track?** names the clockwheel step that selected the song, with its number and the same description shown in the clockwheel list. Music and category steps say whether they choose the least recently played song or choose at random.
- Matched intros (#127): Track info has a **Matched intros** section where you upload a short
  voice clip for that song, or for any song by its artist, such as "Here's the new one from
  Example Artist", and Listen to it or Remove it. When the song comes up, saved-point playback on
  server audio or a stream mixes the clip into its song change: it talks the song up when the song's intro is long enough
  for the speech, and otherwise plays just before the song. The song's own intros come first,
  then the artist's. Each intro has a chance, a wait between plays (60 minutes by default) and
  optional play dates. By default, a matched intro takes the place of the automatic liner at that
  change; Voice liners can instead limit matched intros to changes with no liner scheduled. A DJ
  break booked for the change wins over it. Uploads have the liner upload limits and are
  decoded like announcer breaks, at most one minute long. Intros are kept per station in
  `matched_intros.json` and the `intros` folder, included in backups; a clean library scan removes
  the intros of songs it found gone, and a share that is away loses none. The hour preview lists
  an intro once its song change is mixed. Routes: `/api/intros` and
  `/api/stations/<name>/intros`. See [Matched intros](docs/operations.md#matched-intros).

- An hour preview with backtiming (#126): Now Playing > **Hour preview** lists what plays over
  the next 30 minutes to 3 hours with each item's estimated start time, and says in words on
  every row whether it is fixed (on the air, ready to play, picked, a booked break, in the queue)
  or predicted. The prediction is a dry run of the station's own picker from where the fixed part
  ends, so nothing on the air changes. The card then says how early or late the song changes
  land against the top of the hour, each clockwheel restart and each scheduled event, and which
  song is the reason. The list is a table with column headers; times use numeric text such as
  "2:04:30 PM". It refreshes every 30 seconds while open, in place and silently; **Refresh preview**
  and the new **Shift+H** shortcut say the summary. Every station has its own, at
  `/api/hour-preview` and `/api/stations/<name>/hour-preview`. Previews run on one low-priority
  worker thread, stop after 80 songs or about 4 seconds, and are shared and reused for 45 seconds
  while the timeline and picking inputs stay current. Refresh preview always rebuilds from the
  current inputs; queued and predicted voice liners use saved overlap cues for their estimated
  placement.

- Listener requests (#49), off until `[requests]` turns them on, per station.
  - `/requests` (or `/stations/<slug>/requests`) lets anyone who can reach the server search the
    library by title, artist or album and request a song, with an optional name. It needs no
    pairing and no JavaScript, and each answer is a page whose title says what happened.
  - Every request is checked against the repeat rules and, in DMCA mode, the DMCA limits at the
    place it would play. It is also checked against blocked artists and folders, per-address
    limits, a daily cap and a cap on requests waiting.
  - A request goes to the place in the next `window_minutes` of the queue where it fits its
    neighbours best: sound, tempo (half and double time count), key and energy.
  - With `approval = "manual"` (the default), requests wait in a new Listener requests card in
    Queue & Search, which shows where each would fit and why. The card offers Approve, Play next
    and Turn down, says each result once and moves focus to the next request.
  - Listeners are only ever told roughly when a song may play. Songs are named by opaque ids, never
    paths. Each form carries a signed, time-limited token for its own song and must come from
    the server's own origin. Limits count an IPv6 address with its whole network, a /56 by default
    (`ipv6_prefix`). At most four
    searches or request checks run at once, and listener names are dropped 30 days after the
    decision.
- A public player page and an embeddable player (#52), each off until `[player_page]` turns it
  on, per station. `/player` (or `/stations/<slug>/player`) has a Play button that becomes Stop,
  a labelled Volume slider remembered in the browser, the song playing, the next song when that is
  shown publicly, and the recent songs. Nothing plays until Play is pressed, and Stop closes the
  connection so no listener slot is held. A stall says "Buffering." once, a stream that has not
  started 15 seconds after Play stops with a reason, and where the browser fixes the volume
  (iPhone and iPad) a note replaces the slider. `/embed` (or `/stations/<slug>/embed`) is a compact
  version for an `<iframe>` on another site: `embed_origins` lists the sites allowed to show it,
  and `?theme=light` or `?theme=dark` matches the site's colours. The song line is the only live
  region and speaks once per song; playback results are said once in a status line; both pages
  work at 320 px, in light, dark and forced colours, and without JavaScript through the
  browser's own audio controls. They carry nothing the public feed does not, send a strict
  Content-Security-Policy, and the full page cannot be framed. `GET /api/stations` lists each
  station's `player_path` and `embed_path`.
- Queue & Search uses one paginated library table with Music or Voice liners, folder filtering
  and text search. Folder management is shown as a table in Library tools; liner file maintenance
  is back in Settings > Voice liners. Search, folders and History share arrow-key navigation.
  Action cells focus their buttons and checkboxes so screen readers can announce their roles
  and checked state. Folder saves and table refreshes preserve focus.
- Categories and Music clockwheel steps share folder, genre, year, BPM, duration, text and Beets
  filters, with Match all / Match any and exclusions. Categories can be saved filter selections,
  while existing Beets-query and M3U categories keep working; category references remain available
  to steps, but categories cannot nest. Unknown metadata never passes an exclusion, and a genre
  exclusion rejects a track if any label matches. Both editors show a debounced unique-track count
  before playback restrictions; each settled count is announced once.
- Clockwheel Music steps can combine categories with folder and metadata conditions. Use
  Match any to combine categories, or Match all to narrow a category by year, BPM or other
  fields. Songs matching more than one condition are included once. Existing category steps
  open in the same condition editor and keep their selection and repeat settings. Category
  hours and the list of clockwheels using each category still apply.

- Count versions as one song (#177). Settings > Picking > Repeats has a **Count versions as one
  song** checkbox under the track limits, off by default. Versions of a song by the same lead
  artist, such as a radio edit and a remix, then come up as often as a song with one version:
  random picks choose a song before a version, least recently played dates a song by its latest
  played version, and similarity keeps the best version of each song. The track limits hold back
  every version of a song that played. The rotation tester counts the versions as one song.
- Related tracks (#101). Settings > Picking > Repeats has a **Use related tracks** checkbox, off
  by default, with its own Tracks and Minutes limits (20 tracks to start with). It holds back
  tracks that sound alike on air, such as a song and one that samples it, listed as
  "Artist - Title" groups in `index/<name>/related-tracks.toml`. Every track in a group holds
  back the others in both directions. AutoDJ copies a small sample there on first use and never
  replaces your copy; an invalid file is refused on save and skipped with a warning at startup.
  The rotation tester, `autodj doctor` and `autodj backup` include the file.
- Related artists have their own Tracks and Minutes limits under **Use related artists**.
  Settings saved before this keep using the artist limits.
- A "(w/ X)" note in a title now credits X, like "(feat. X)" and "(with X)", for the artist
  repeat rules, related artists and DMCA mode, and is ignored when titles are compared.
- Negative liner **Overlap start** values down to -15 seconds start the next song first, then place
  liner content within its intro when it fits. For example, if a -3 second request has one second
  available before vocals, the liner starts one second after the song. If the main content is longer
  than the intro, existing liner-first timing remains; the liner tail continues over the song.
  Track info announces when a negative value makes the cue-preview controls unavailable (#159).
- **Why this track?** names the clockwheel step that selected the song, with its number and
  the same description shown in the clockwheel list. Music and category steps say whether they
  choose the least recently played song or choose at random. The description stays with the
  selected song when the clock changes, including the opening song.
- Library Rescan can override saved fields with populated embedded tags; missing tags keep the
  saved values, cue points and analysis remain intact for unchanged files.

- Voice tracking from the browser (#128): a new **Voice tracks** tab, after Carts, records a DJ
  break with the computer's microphone for the open song change and books it, as an announcer
  program books one (#88). The tab names the ending song and the next one, the next song's intro,
  the time left to book and when the change airs, and where an Automatic voice track will go:
  over the intro as a talk-up, or between the songs. **Record** asks for the microphone only when
  pressed and lets it go when the recording stops; **Listen back** plays the take, **Record**
  again replaces it, **Discard recording** throws it away and **Book voice track** uploads it.
  Placement can be Automatic, Talk up or Standalone, and a talk-up may start over the ending
  song's outro. Booked voice tracks are listed with what became of them, and one still waiting can
  be cancelled. A take for a song change that has moved on to other songs is not booked: record
  again, or listen back to it and press Book again. Recording stops at three minutes (or the
  station's shorter limit), because booking decodes the whole take in the page, and it stops,
  keeping the take and letting the microphone go, when the tab is left or the page is hidden.
  Announcer tokens list, look up and cancel only announcer breaks, not voice tracks.
  - Keyboard and screen readers: every control is a named native control, and focus stays where
    it was through record, stop, listen, book and discard. **Alt+R** starts and stops recording
    from anywhere in the tab without moving focus. The recording state is said once, politely, at
    start and stop, then once a minute and 30 seconds before the length limit or the song change
    closing; the timer and the time left are on the page and never read out on their own. A
    blocked or missing microphone, a page not on a secure connection (HTTPS, or localhost on the
    same computer), a closed song change and a recording that is too long are said in plain
    words, shown on the page and linked to the control they are about.
  - Voice tracks go through the signed-in session, never an announcer token, with the page's
    origin checked as for every other change and the upload size capped before the body is read.
    They get the same checks, length limit, decoding and placement as announcer breaks, and share
    their caps and quota. The browser sends a mono WAV at the playout rate; the server decodes it
    off the event loop, one upload at a time, as it does a break. Each station has its own at
    `/api/stations/<slug>/voice-tracks`.
- Seasons for rotation categories (#140): under **When the songs may play**, a category can have a
  **Not before** date, a **Do not play after** date, or both, such as 1 to 26 December for
  Christmas songs. Outside them its songs are left out of every pick mode and a clockwheel step
  for it is skipped, as outside its hours; the list says the season in words and "Outside its
  dates now". Categories without dates are saved as before.
- Days and hours for clockwheel steps (#140): any step can keep to chosen days and hours in the
  new **When this step plays** fields of the step form. Outside them the step is skipped as an
  empty step is, with "outside its hours" in the clockwheel status and the server log; the hours
  are checked for when the song is expected to start. The step list says them after the step. A
  clockwheel whose every song step is outside its hours does what it does when no step has a
  song; it gets no new whole-library fallback. Steps without hours are written as before, so an
  older AutoDJ still reads the file.
- Dayparting messages name what holds the songs back: "outside its hours", "outside its days" or
  "outside its dates", in the clockwheel status, the server log and the category list (#140). The
  category list changes its words in place when a poll finds a category now inside or outside
  them, without moving focus.

- Play dates for time check voice packs (#140): in Settings, Time checks, **Voice pack play
  dates** gives a pack a **Not before** date, a **Do not play after** date, or both, such as a
  holiday voice in December. Outside them a ticked pack is passed over and the other ticked packs
  speak; a test of that pack still plays it. Each pack's description says its dates and when it
  is skipped now. The dates are kept in `time_checks.json` under `pack_dates`, only when a pack
  has some, so an older AutoDJ still reads the file. Keys this version does not know and dates
  it cannot read are kept as written, each save reads the file again first, a file that cannot
  be read refuses saves rather than lose what it holds, and a switch saved while dates are saved
  keeps both.
- Announcer breaks (#88): an announcer program on the station's machine can
  send a station DJ breaks, news, weather and music news through a new breaks API, with no
  other program in between. AutoDJ prepares each song change one song ahead, so it now also picks the
  song after the next one and offers that song change as the open slot; the program writes a
  break for that pair of songs and uploads it. A DJ break talks up the next song (the speech ends
  as its vocal starts, over its intro) when the song has an intro of at least two seconds, and
  otherwise plays between the songs; `[breaks] dj_placement` chooses. News, weather and music
  news play between the songs. The music drops by `[breaks] duck_db` under the voice, or by the
  liners' duck depth. A booked break keeps its next song whatever the queue does. AutoDJ trims the
  silence after the speech, reports each break as queued, rendered, on the air, aired, dropped or
  expired with the reason in words, and deletes the file afterwards. Breaks are off until
  `[breaks] enabled = true`, need saved-point playback, and work per station. Uploads are capped
  in size, length and number before their body is read, limited to 192 kHz and two channels,
  decoded away from the playout within a memory budget, and checked before they are accepted.
  Operations has the API with example requests.
- Announcer tokens: in Settings, Analysis workers, Machine tokens, **Token use** now offers
  Announcer, with a **Station** choice that shows only for it. An announcer token works for that
  one station and only for the breaks API.
  `autodj tokens create NAME --scope announcer --station SLUG` does the same, and
  `autodj tokens list` names a token's station.
- A rotation tester (#125): Settings > Song picking > **Test rotation**, or
  `autodj simulate --days 7`, plays a span of time (up to 30 days) or a number of songs (up to
  20,000) through the station's own song picker, with its clockwheel, categories and their hours,
  schedule and date ranges, repeat rules (related artists included) and DMCA limits, and reports
  plays per song, category and artist, songs never played, which rules had to be relaxed and how
  often, skipped clockwheel steps and why, DMCA conflicts and any moment with nothing to play.
  Nothing on air changes. The report comes in words (headings and short lines) or JSON
  (`--json`); a seed repeats a run exactly. Runs stop after ten minutes of work, with what they
  have. `autodj simulate` opens the library read-only, so it can run beside a server. In the page
  the run works in the background with a progress bar and a Cancel button, announces its end
  once, and shows the results as headings and lists. See
  [Operations](docs/operations.md#testing-the-rotation).

- Editing tags from Track info (#53). A Tags section has fields for title, artist, album, genre,
  year, BPM, key and ISRC. Save tags writes only the changed fields into the file with mutagen
  (MP3, FLAC, M4A, Ogg Vorbis, Opus, WAV and AIFF) and updates the library at once, keeping the
  song's analysis, cue points and loudness measurement, so nothing is analysed again; a key or BPM
  set by hand is kept when the song is analysed later. The file is tagged as a copy that is
  flushed to disk and replaces the original only once every changed field reads back, so a failed
  save leaves the original as it was; an MP3 keeps its ID3 version. A song playing or up next is
  refused until it has played, and only files inside the library folders are changed. When beets
  manages the library, the same fields can be written to the beets database too, in the same
  order as each file save. A refused field is marked invalid with its reason linked to it and
  focused; a save is said once and focus stays
  on Save tags. The dialog that asks to save before switching tracks now reads its explanation when
  it opens and has a Keep editing button, which does what Escape does.

- A separate output device for previews (#54). In Settings, Sound, **Preview output device**
  sends the cue point preview in Track info, which Queue & Search, History and the library open,
  to a device of its own, such as headphones, while the browser mix stays on the speakers.
  Previews never go through the station's mix, so they are never on the stream or in a
  recording. On a device of its own the preview keeps its own volume, and the page's volume and
  Mute leave it alone. The choice is saved in this browser. If the device is unplugged, it stays
  in the list as "not connected", previews move to the system default output and the page says so
  once, then they go back when it returns. Browsers that cannot choose a device, such as Chrome on
  Android, say so on the page and keep previews on the system default output. This is the first
  step toward headphone cueing.
- Play dates for voice liners and carts: a **Not before** date, a **Do not play after** date, or
  both, in the station time zone. Outside them, automatic liners, clockwheel liner steps and
  scheduled liner events skip the liner, and a cart plays nothing and says why. Browse voice
  liners has a Play dates button on each liner and says each liner's dates in words, with
  "Skipped now: expired after ..." when it is outside them; cart buttons end with "expired" or
  "not playable yet". Liner dates are kept in `liner_dates.json` and backed up; cart dates are
  kept in `carts.json`. Clips without dates are saved as before. The cart editor now shows a
  refused field's error on the page as well as saying it.
- Dayparting for rotation categories: a category can allow its songs only on chosen days, only
  between two times, or both, such as Friday and Saturday from 22:00 until 02:00, in the station
  time zone. Outside those hours its songs are left out of every pick mode (similar songs,
  discovery, random, least recently played and clockwheels), and a clockwheel step for it is
  skipped with the reason in the clockwheel status and the server log. Relaxing the repeat rules
  or the DMCA limits never brings them back; only when nothing else can play does one play, with a
  warning. A song you queue yourself plays at any time. A category from a beets query on a field
  such as `daypart` gives single songs their own hours. The categories list says the hours in
  words and when a category is outside them now. Categories without hours are saved as before.

- A station switcher in the web page (#70). With more than one station, a **Station** button in
  the banner opens a menu of every station with its state ("Night Owl Radio, on air, 3
  listeners"); Enter, Space or a click switches, and moving through the menu never does. The page
  then controls that station: every station control, panel and live update follows it, while the
  library, search, presets and pairing stay shared. The heading, the browser tab's title, the main
  landmark and the station tabs name it, the switch is announced, and screen readers hear "On
  Night Owl Radio" after each control that changes what is on air and "Shared by all stations"
  after shared ones. `?station=<slug>` in the address opens a station, and each browser
  remembers the last one chosen. `c` moves to the switcher
  and Shift+C says which station the page controls. Listen here keeps playing the station it was
  started on. A stopped station shows an off-air note with Start, and Settings, Stream can stop a
  station other than the default one. Hidden with one station, so a single-station page is
  unchanged. See "The station switcher" in `docs/operations.md`.
- Groundwork for running several stations on one server (#70). Every control address also answers
  under `/api/stations/default/...`, and the play log, last-played times and active clockwheel are
  kept per station in `library.db`. Existing plays belong to the default station, so nothing
  changes for a single station. `autodj export-plays --station NAME` exports one station's plays.

- `[stations.<slug>]` tables in `config.toml` describe more stations (#70), each with its own
  `playback`, `stream`, `recording`, `dmca`, `time_checks`, `presets` and other station sections.
  A station starts from the defaults rather than copying the default station's settings. Its
  listener credentials, relay passwords and webhooks belong in `config.local.toml` (relay
  passwords can also come from `AUTODJ_STATION_<SLUG>_RELAY_<NAME>_PASSWORD`). Names are checked,
  `default` is reserved, two stations cannot share a recording folder or a relay target, and
  errors name the station. See "Several stations" in `docs/operations.md`. A configuration
  without these tables behaves exactly as before.

- More than one station can run on one server (#70). Each `[stations.<slug>]` station has its own
  player, mix bus, mastering, encoder, relays, recorder, schedule, carts, time checks,
  clockwheels, liners, profiles and saved settings, under `stations/<slug>/` in the index folder.
  It streams at `/stations/<slug>.mp3` (and `.m3u`), its controls answer under
  `/api/stations/<slug>/`, and `GET /api/stations` lists every station. `POST
  /api/stations/<slug>/start` and `/stop` start and stop one; `autostart = false` leaves a station
  stopped until then. Its log lines start with `[<slug>]`. Only one station can use Stereo Tool
  at a time (one licence), and a Stereo Tool preset any station uses cannot be deleted.
- Each station has its own public status page and now-playing feed (#70): `/stations/<slug>/status`
  and `/api/stations/<slug>/nowplaying`. Each shows only that station's name, songs and
  listeners, and the page links to that station's stream and playlist and keeps itself current
  from that station's feed. The default station keeps `/status` and `/api/nowplaying`. Only
  configured stations have them; any other name needs a paired browser and then is not found.
  While a station is stopped, its page, feed and stream all answer "off air" (HTTP 503); its page
  still shows its name, says it is off air, and shows the song again once the station is back.
  `GET /api/stations` now gives each station's status page, feed, stream and playlist addresses
  beside its state. On every status page the uptime is now labelled "Uptime" instead of "AutoDJ
  running for", since each station counts its own, and a stopped station's says "Not on air".
  In Safari and other WebKit browsers, the status page's Pause updates button is now readable in
  the dark theme; its text had too little contrast.
- `autodj backup` and `autodj restore` cover every station (#70). Each other station's settings,
  schedule, carts, time checks, listener bans, recording state, liner dates, liners, cart clips,
  time check clips, clockwheels and profiles are stored under `stations/<slug>/` in the archive,
  and the default station's keep their names. A folder two stations share is stored once.
  Restore puts each station's files back in its own folder, and refuses a backup holding a
  station the configuration does not define ("add [stations.<slug>] to config.toml first")
  before it changes anything. A shared folder restored where the configuration keeps it apart is
  restored once, with a warning. Folders that hold one another, or a station's own folder, are
  refused, and a station's `liners_folder` or `clips_folder` that holds its own folder is a
  configuration error.
- Backup and restore are safer for every installation (#70). Restore is all or nothing: every
  file and folder it replaces is set aside first and put back if any replacement fails, so a
  failure part way no longer leaves a mix of old and new files. It checks the archive's size
  against a 256 GiB cap and the free space before unpacking, and refuses file names Windows
  reserves for devices, holding `* ? " < > |` or ending in a dot or a space. Each step is
  written to a journal first: a restore stopped by a crash is reported by `autodj doctor`, which
  offers to roll it back (asking first and moving rather than deleting), and `autodj serve`
  refuses to start until it is. Restore refuses while `autodj serve` holds its new lock on the
  index folder. SQLite journal and WAL files beside a replaced database are set aside with it. Old
  copies that cannot be deleted at the end no longer fail a finished restore. A backup copies every
  station's state files with the index generation under the index's lock, checks the free space
  first and skips AutoDJ's own temporary files; restored files keep their saved permissions.
  `autodj doctor --station` runs only that station's repairs, and an index rebuild is always asked
  for, even with `--fix`. A link that leaves its folder,
  including a Windows junction, makes the backup fail with its name instead of copying outside
  files (#193); a link within the folder is saved as a file. Restore refuses links in an archive
  and destinations that a link takes elsewhere. Hard-linked files are refused because their other
  names may be outside the backup folder.
- `autodj doctor` checks every station (#70). Each other station gets its own lines, starting
  "Station <slug>," (and a `station` field in `--json`): its folder, saved settings, stream,
  relays, time checks and voice packs, DMCA mode, recording folder and liner folder.
  `--station <slug>` checks one station (`default` for the default one) beside the server
  checks. New for every station: `recording` (can its folder be written) and `liners` (is a
  configured liner folder there). New server checks: `config-keys` lists the keys and tables in
  the config files that AutoDJ ignores, including those under `[stations.<slug>]`, and
  `station-folders` names station folders no `[stations.<slug>]` table uses any more, which are
  never deleted and not backed up. Doctor offers to clean up a station's saved settings (with a
  backup) and to make a station's folder, each only after asking, or with `--fix`.

- Schedule events can have a date range: an optional start date and end date, both included, in
  the station time zone. Outside the range an event does not run, and Next events skips it. An
  active window that crosses midnight belongs to the day it starts, so a Friday 18:00 until 02:00
  window still runs to 02:00 after its end date. Every-few-minutes events can also keep to chosen
  days. The events list says the range in words ("Every day at 00:00, from 27 November to 26
  December 2026") and says when an event has no runs left, and Next events gives the year when it
  is not this one. The date fields are native, labelled date inputs, and a form error is now
  linked to the field it names. Existing schedule files load unchanged. Operations describes a
  holiday clockwheel season as an example.

- A light theme. **Theme** under Settings, Display and keys offers System (the default, which
  follows the device's light or dark setting), Light and Dark. The choice is saved in this
  browser, applied before the page is drawn so the other theme never flashes, and used by the
  public `/status` page too. Every text, control edge, focus ring, meter, cue tick and Camelot
  wheel edge meets WCAG 2.2 AA contrast in both themes, and Windows High Contrast still uses the
  system colours. Browsers too old for the CSS `light-dark()` function (before Chrome 123,
  Firefox 120 and Safari 17.5) keep the dark theme.

- The status message at the foot of the window has a **Dismiss** button that works by mouse,
  touch and keyboard, and stays on screen while the pointer is over it or focus is on its button.
  Escape still closes it. Only the message and the button take the pointer, so a click on the
  rest of the strip still reaches the page underneath. What screen readers hear is unchanged: the
  button is not part of the message, and the message never takes focus.

- `autodj worker --idle-exit MINUTES` keeps asking for new songs every half minute and stops only
  once the queue has stayed empty for that long, so a worker started alongside a Rescan does not
  exit before the scan has found the new music. The before- and after-commands still run once
  each, around the whole wait. `--once` is the same as `--idle-exit 0`.

- Clockwheel music and liner steps can combine conditions with Match all or Match any. Add
  folders, genres, year, BPM, duration, artist, title or album in the step editor; artist, title
  and album also accept simple wildcard patterns. Beets fields, including custom fields, are
  available when a Beets database is configured. Folder steps open as one condition, and
  category steps keep working. Missing metadata does not match, and repeat-rule relaxation
  stays within the matching tracks.

- Rotation categories for clockwheels (Library tools, Categories), such as A, B, C and D or
  "Remembering the 2000s". A category's songs come from a beets query or from an M3U or M3U8 file
  in the playlists folder (`[library] playlists_folder`, by default `playlists/` in the index
  folder), with each line matched to a library track and the lines that match none counted and
  named. Queries use the common beets syntax (`field:value`, ranges such as
  `original_year:2000..2009`, `^` to leave out, quoted phrases) on any beets field, flexible
  attributes included, read from the beets database opened read-only; regular expressions,
  alternatives and other syntax AutoDJ does not run are refused in words. Year filters use
  `original_year`, falling back to `year` when beets has none, and the list, Check and
  `autodj doctor` count the songs that used the fallback. A new clockwheel step, "Music from a
  category", picks the least recently played or a random song of a category under the repeat
  rules and the DMCA limits, like a folder step. Categories are worked out again only when the
  categories, beets, a playlist file or the library change. An empty or deleted category is
  logged and skipped, and a clockwheel whose category steps all come up empty plays a song from
  the whole library instead of going silent. Categories are kept in `categories.json` and in
  backups; `autodj doctor` names the empty ones and the missing playlist tracks.
- Remote analysis workers: a computer with a graphics card can analyse new music for a server
  that has none. Create a token in Settings, Analysis workers, then run
  `autodj worker --server https://HOST:PORT` on that computer with the token. The worker claims
  songs, downloads each one from the server, analyses it with the same model and code as
  `autodj index` and sends the result back; nothing is copied by hand and paths need not match.
  The server checks every result against its own model weights (the downloaded snapshot's commit,
  not the branch name) and analysis version and refuses one for a song that changed, then stores it in the live library, which picks it up within seconds.
  Claims run out ten minutes after a worker stops reporting, so a worker that dies strands no
  songs, and the server's own processor analysis pauses while a worker is connected and resumes
  after it leaves. `--once` stops when the queue is empty, Ctrl+C stops after the current song,
  and `--before-command` and `--after-command` run a program around the work, such as stopping a
  text-to-speech container that shares the GPU. Settings shows each worker, its rate and the
  queue, and creates and revokes tokens; a token is shown once, with a Copy button and a spoken
  warning. See "Using a GPU machine" in the operations guide.

- Machine tokens: named, scoped bearer tokens for programs, stored as a SHA-256 digest, shown
  once, revocable, with the time each was last used. They work only over HTTPS or from the server
  machine, and only on the routes their scope allows. The analysis worker is the first scope.
  `autodj tokens list|create|revoke` manages them on a server without a browser.

- Several named clockwheels (Library tools, Clockwheels): create, duplicate, rename, delete,
  activate one or turn the clockwheel off. Each is a versioned JSON file in `clockwheels/` in the
  active index folder, included in backups. The step editor has numbered names with Edit, Move up,
  Move down, Duplicate and Delete buttons. Up/Down navigate names, Enter edits, and the Delete key
  asks before it removes a step; Alt+Up/Down still move the focused step without changing the
  playback shortcuts. Steps
  can play music or liners from a library folder or a folder inside it, a specific track, or
  switch preset. Profiles remember the active clockwheel, and the schedule's
  "Change song picking" action can switch to a clockwheel by name. Import .PAL turns a SAM
  Broadcaster `.PAL` file into a new clockwheel, and now accepts folders inside a registered
  library folder. Liner steps have an option to play only when there is room for the following
  song before the next reset. If the pair will not fit, the liner is skipped and the song follows
  the usual clock rules. The check uses saved cue points and overlap, and is off by default.

- A cart wall (the Carts tab) with four pages of twelve numbered carts for station IDs, jingles
  and sound effects. Assign a voice liner or an uploaded clip, then play it with its button or,
  with focus on the wall, keys 1 to 9, 0, minus and equals; Page Up and Page Down change the
  page. A cart ducks the music, plays over it, or replaces it until the cart ends, in browser
  playback, server audio and stream mode. Every open page announces when a cart starts and ends.
  The wall and the uploads are kept on the server and in backups, profiles remember the page
  shown, and the scheduler can play a cart.

- Spoken time checks (Settings, Time checks) use a recorded `HH_MM` clip or a cached external
  TTS command, with optional intro and outro clips. They play from a Schedule action or after the
  song that crosses the hour, in the station time zone, with the voice liner duck and talk-up; the
  check after the hour takes the place of that song change's automatic liner. Settings choose
  12- or 24-hour speech; doctor reports missing recordings, and backups include the clips and the
  saved switches. `scripts/time_check_sapi.ps1` speaks with a chosen Windows SAPI voice.

- Time checks can speak from voice packs: folders of hour and minute files, named the way
  RadioBOSS names them (`hr_05.mp3`, `hr_05o.mp3`, `m_56.mp3`, optional intro, outro, am and
  pm), listed in `[time_checks] voice_packs`. AutoDJ trims each piece's silence, matches their
  loudness and joins them with a 40 ms equal-power crossfade, so the time sounds like one
  sentence, then plays it through the voice liner path. Settings, Time checks lists the packs as
  checkboxes with their coverage, takes turns between ticked packs or picks one at random, and
  can test one pack. A pack that lacks the minute falls back to the next pack, the recorded
  clips, then text to speech. Doctor reports each pack's missing hours, exact hours and minutes
  and the files it does not use; packs kept in the `time_checks` folder are in backups.

- DMCA mode (`[dmca] enabled = true`): every pick mode, the clockwheel included, keeps to the
  US statutory webcasting licence's performance complement (at most 3 songs from one album, 2 in
  a row, and 4 by one artist or from one box set, 3 in a row, in any three hours; featured
  artists count). When nothing keeps every limit, a song breaking the fewest plays and a warning
  names the limit. Queueing a song that would break a limit asks first, with the limit in words
  and Queue anyway or Cancel. The public feed, the status page and webhooks never show the next
  song, Settings, Stream says the mode is on, and `autodj doctor` names conflicting settings.

- Stereo Tool presets can be uploaded, selected and deleted in Settings, Sound. The included
  preset remains read-only; uploaded presets survive restarts and are included in backups.
  A scheduled event can switch the Stereo Tool preset (Settings, Schedule).
- Stereo Tool input trim is adjustable from -12 to +6 dB. Quiet song openings can receive up
  to 4 dB of peak-safe lift by default, returning to normal within eight seconds. The limit
  is adjustable from 0 to 6 dB, with 0 disabling the lift. This intro-only lift applies to
  ReplayGain playback; Smooth and Stereo Tool use their continuous output processors.

- Broadcast recording saves the encoded stream, including liners and processing, as dated MP3
  files. Settings, Stream and scheduled actions control recording and splits. A free-space floor
  pauses recording without stopping listeners; retention removes old dated recordings. Running
  state and settings survive restarts. Backups include that state but not the MP3 files.

- A scheduler for timed events (Settings, Schedule). Events run once, daily, on weekdays, on
  chosen days, or every N minutes within an optional window, in the station time zone
  (`[server] station_timezone`). They switch profiles, presets or song picking, play liners,
  start or stop the stream, or turn discovery on or off, with hard or soft timing. The panel
  lists the next events and recent runs; the schedule is saved in `schedule.json` and backed up.

- A public now-playing feed at `/api/nowplaying`: JSON with the station name, the song playing, recent
  songs and the listener count, for web pages, overlays and other services. It needs no pairing and
  never includes file paths.
  - Track-change webhooks, set up in `config.local.toml`, send the same details as JSON or as a
    Discord message. Delivery runs in the background and retries, so a slow destination never
    holds up playback.
  - The next song stays out of the feed unless you turn on **Show the next song publicly** (or
    `[nowplaying] show_next`), since some webcasting licences forbid announcing songs in advance.
  - The feed also reports today's peak listener count (since midnight in the station time zone, reset when AutoDJ
    restarts) and how long AutoDJ has been running. Its schema version is now 2.
  - A public status page at `/status` shows the station name, the song playing, the next song when
    allowed, listeners now, today's peak and the uptime, as plain text that works without
    JavaScript and updates every 15 seconds when it can. Screen readers hear the song line only
    when the song changes.
  - In stream mode, Settings, Stream has a **Now-playing feed** group: the feed's link and the
    status page's link, each with a Copy button, the show-next checkbox, and a list of webhooks
    by host, format and last result, each with a **Send test** button that says whether the test
    arrived.

- Stream operators can set an optional listener password in `config.local.toml`, limit
  connections per address, and disconnect or ban addresses from Settings, Stream. Bans survive
  restarts and are included in backups. Trusted proxy addresses control when forwarded client
  addresses are used.

- Track info buttons in Now Playing, Queue, Search results, History, library browse and voice
  liners open the same details and cue editor. The Return to History, Return to Queue & Search
  and other view-specific buttons restore focus to the source item, or explain when it has gone.
  An old play with no saved path, or a recording removed from the library, gets a spoken explanation.
- Richer repeat rules for picking songs.
  - An artist credit counts for every artist it names: "feat.", "ft.", "featuring" and "vs."
    always split it, and "&", "and", "x", "with" and commas split it when the parts look like
    separate artists. "Simon & Garfunkel", "Earth, Wind & Fire" and "Mumford & Sons" stay one act.
  - Song titles match in any version, so "Jingle Bells", "Jingle Bells (Live)" and
    "JINGLE BELLS - Remastered 2009" are one song.
  - Settings > Picking > Repeats has separate artist, album, track and title rules.
    Each accepts tracks, minutes or both; zero turns off that limit. Save applies changes
    to future picks, uses saved play history and remembers the settings across restarts.
    Fresh installations default to 50 tracks for the same track and 8 for the same artist;
    album, title and all minute limits start at zero. Artist, album and title track
    limits are capped at 5000.
  - Least-recent picks, including clock slots, check songs from oldest to newest and stop
    at the first eligible song. Ties stay random; rules relax only after none qualify.
  - Use related artists applies the artist limit to links in an editable TOML file.
    AutoDJ copies the sample to `index/<name>/related-artists.toml` on first use and never
    overwrites an existing copy during upgrades. Save repeat settings to reload file edits.
    The fixed file is included in backups and capped at 1 MiB.
  - An invalid related-artist file is refused when saving. At startup the other rules still
    work, and the Repeats panel, log and `autodj doctor` identify the file problem.
- Export the play log for royalty reports, from "Export play log" in the History tab or with
  `autodj export-plays --from DATE --to DATE`. The CSV has one row per play: start and end time,
  seconds played, artist, title, album, ISRC, label, kind, file and performances. ISRC and label
  are read from the file tags when you export, so no re-index is needed; an ISRC is checked and
  written as 12 characters, and a malformed one counts as missing. `--missing` lists the songs
  that lack an ISRC, label or other reporting field, and marks the ones SoundExchange may not be
  able to match because they have neither an ISRC nor both an album and a label.
- `--format soundexchange` (or the format choice in the History tab) writes a SoundExchange Report
  of Use for US webcasters: songs only, one line per recording, with actual total performances from
  the performance counts. See "Reporting for webcasting licenses" in the operations guide.
- Each play now records its performances, the listeners who heard any part of it: in stream mode,
  the stream listeners connected when it starts plus each listener who connects while it plays,
  with a quick reconnect from the same player counted once; otherwise 1. While an Icecast or
  Shoutcast relay is enabled, plays get no count, because the relay's listeners are not visible.

- More settings can be changed on the Settings page instead of in `config.toml`. They are saved
  on the server and survive a restart:
  - the EQ duck cutoff
  - how many of the closest songs a similarity pick chooses among, and how randomly
  - the beatmatch stretch limit, and how many bars a beatmatched or key-shifted song takes to glide back
  - the filter sweep floor
  - the ReplayGain peak limit
  - whether the browser loads the next song early, and whether it crossfades when a song goes quiet
  - in stream mode, the station name that players show
- Settings that have no effect are hidden:
  - with saved segue points, the transition effect and every DJ-mix option
  - the effect level and effect sync while no transition effect is chosen
  - drop mixing and the beatmatch stretch limit while Beatmatch is off
  - the glide while both Beatmatch and Key shift are off
  - the EQ duck cutoff while EQ ducking is off
  - the filter sweep floor while the sweep is off
  - the pick spread outside Similarity picking
- Stream mode can relay the station to hosted Icecast 2.4, Shoutcast 1 and Shoutcast 2 servers,
  set up as `[stream.relays.<name>]` tables in `config.toml` with the password in
  `config.local.toml` or `AUTODJ_STREAM_RELAY_<NAME>_PASSWORD`. Each relay sends the stream's
  own audio, or a separate encoding at its own bitrate, sends the song title on every new song,
  and reconnects on its own with growing waits up to a minute. While a relay is on, the station
  keeps playing with nobody connected to AutoDJ itself. Settings, Stream lists each relay's
  state and last error, and `autodj doctor` checks that each server answers and shows the state
  AutoDJ last reported. Passwords never reach the browser, the logs or `web_state.json`. See
  "Relaying to Icecast and Shoutcast" in the operations guide.

### Fixed

- Tables no longer say "row header" when focus lands on a track's name (#302).
- In tables, a cell with a button such as Track info takes focus itself, so it is read once.
  Enter or Space on the cell presses the button (#294).
- Up and Down Arrow in a table, such as the hour preview, move between rows without changing the
  volume (#290).
- Enter on a track's name in History, the hour preview or Search and browse opens its Track
  info (#291).
- The footer Changelog link, the build link, Dismiss, and the row buttons for clockwheel steps,
  categories, shows and listener requests each read as one line in NVDA browse mode (#288).
- A slow request-head deadline no longer logs an asyncio error when a TLS connection has already
  closed or detached (#277).
- The track repeat-rule cap warning appears once per station for each setting and library size,
  instead of on every pick (#278).
- **Prevent a song from playing in the same quarter of the hour** reads quarters on the
  station's clock. Across Lord Howe Island's half-hour clock change it compared UTC quarters, so
  a song could return to the quarter it last played in. Listener request checks read it on the
  station's clock too (#219).

- Saving cue points in Track info no longer fails with "end exceeds track length" on some
  tracks when the End was left as loaded. Track info shows cue times to the nearest 0.01 s,
  which could round the End just past the song's length. The catalog now accepts a cue that
  rounding pushed up to 0.01 s past its limit (the length, Start, End, or the Overlap start
  room) and moves it onto that limit; anything further is still rejected (#237).
- A matched intro reserved for a handoff is available again if playback skips that handoff before the intro starts. Intros already heard keep their cooldown.


- The web page footer said "build unknown" in Docker, because the image has no git. Build it
  with `AUTODJ_COMMIT=$(git rev-parse --short HEAD) docker compose build` (or `up -d --build`)
  and the footer names that commit. Once paired, the commit in the footer, from git or from
  the image, links to that commit on GitHub in a new tab. A value that is not 7 to 40
  hexadecimal digits is ignored. See [Build stamp](docs/operations.md#build-stamp).
- Positive liner overlap points now respect the next song's intro, just like zero and
  negative values. A cold opening waits until the liner's segue; its remaining tail can
  still overlap. Some songs now start later where the old overlap would have covered
  their vocals. Stored cue values and outgoing fades are unchanged (#194).
- Track info preview audio stops when leaving the tab or saving cue points, while keeping
  its position available for further editing (#186).
- Audio prepared before a clockwheel edit no longer restores the old step position or restart
  time when it begins playing (#153).
- Clock skip logs now include the step number and reason for every skipped step, including
  folders blocked by repeat filters (#188). Repeated reasons are logged once and reported again
  after a pick has no skips.

- A `[model] manual_path` folder whose weights are rewritten at the same size within one tick of
  the filesystem clock (two seconds on FAT and exFAT drives) gets a new digest instead of keeping
  the old one, so the server and a remote worker can no longer look as if they run the same
  weights when they do not. A digest is only remembered once every weight file is more than two
  seconds old.
- Backups refuse links to files outside a configured backup folder and linked subfolders,
  including Windows junctions, for both default and named stations. Restore also checks
  linked destinations before replacing any files (#193).

- Several stations on one server (#70, #141):
  - Restarting audio processing releases finished process objects and their Windows handles
    during playback instead of retaining them until the station stops.
  - Stopping a station takes effect at once. While its player finishes, the station is listed
    as "stopping", its controls answer 409 straight away, its open pages are told at once, and
    starting it again is refused at once instead of waiting up to 30 seconds. Other stations can
    start and stop meanwhile. If the stopping station still holds Stereo Tool, a station asking
    for it is told that the other station is stopping and to try again in a few seconds.
  - A station stops and closes only once, even when a stop request arrives during server
    shutdown. Every step of a stop is tried even if an earlier one fails, so shutdown always
    stops every station and closes the library.
  - A station whose player does not finish within 30 seconds of a stop is listed as "stuck
    stopping", and the log says so every minute. It keeps its mastering process and Stereo Tool
    until the player ends and the mastering process has really ended, instead of closing them
    under a running player.
  - Stopping a station now says what went wrong: the reply has a `warnings` list in plain words
    (empty after a clean stop), and the log line is a warning instead of "stopped".
  - On a normal shutdown, AutoDJ kills any mastering process still running and lets none start
    again. On Windows, mastering processes also end when AutoDJ is killed outright or crashes.
  - The check that only one station uses Stereo Tool can no longer miss a station that is
    starting or stopping at that moment.
  - A station that fails part of the way through starting now stops everything it had started:
    its encoders, relays, liner worker, mastering process and player. If its player does not
    finish in time, the player and its mastering process are left running and the log says so.
  - A station with no `stream.station_name` is called by its slug, such as `nightowl`, instead of
    "AutoDJ", so it no longer shares the default station's name in stream metadata. Two stations
    given the same name are warned about at startup.
  - When one station's liner folder is inside another's, each station keeps to its own liners.
    The outer station no longer plays the inner station's liners in clockwheel liner steps or
    offers them as cart clips. Folders reached through a symbolic link or a Windows short name
    are matched too.
  - Stopping a station cancels a cart's end timer, so a cart can no longer announce its end
    after the station has stopped.
  - The multi-station soak test now runs for a few seconds in every test run, stops and starts
    the second station while both render, and checks that nothing is left behind (the bus timing
    is judged only in a long run). It can run for hours and write memory and processor samples to
    a file. See "Soak test" in `docs/operations.md`.

- Clockwheel Delete and Undo leave an open step edit alone. Duplicating or moving steps
  keeps the edit attached to its original step and updates its displayed number (#146).

- Repeat rules that use minutes now keep and load up to 50,000 recent plays, so fast song rotations
  retain more of the selected time window across picks and restarts.
- Dayparting checks a category's hours for when the song is expected to start rather than when
  it is picked (#140), so a song no longer starts a few minutes after its window has closed. The
  rotation tester checks them the same way, and so does the song change planned a song early for
  announcer breaks (#88), for the song after the next one.
- Picks with dayparting are cheaper (#140): which songs are outside their hours is worked out once
  a minute rather than on every pick, and the closed songs are no longer sorted for the
  similarity search on every pick. The answers are the same.
- Without `[server] station_timezone`, the system time zone is looked up once rather than on
  every pick, and a host with no known zone logs its warning once (#140).
- When every song is outside its hours, the warning is logged once, not twice per pick, and again
  only after the hours change (#140).
- A category with all seven days ticked and no hours says "Plays every day, at any hour" rather
  than "Plays only every day" (#140).
- Sequential liner rotation keeps its place when a registered liner is outside its play dates
  (#140): the liner after it still comes next, rather than the order shifting by one. A
  discarded pick gives back every liner it passed over, and talk-up liners planned ahead pass over
  liners outside their dates as well.
- "expired" on cart buttons and "Skipped now" in the liner list follow the station's day on a page
  left open past midnight, in place, without moving focus or announcing anything (#140).
- A library scan forgets the play dates of liners deleted outside AutoDJ, and a liner uploaded
  under a free name starts without dates, so a new file no longer inherits an old one's (#140).
  A scan that could not read a folder marks no track gone and forgets nothing, a liner folder
  that cannot be listed keeps its dates, and so does a library ID no longer in the library. A
  `liner_dates.json` that cannot be read just now, or whose folder is away, is no longer set aside
  or written over; saves are refused until it can be read, a new upload waits rather than take
  an old liner's dates, and a delete says when the dates could not be forgotten. An empty liner
  folder forgets dates only after two scans in a row, names compare without case on Windows, and
  a file listed but not reachable is not marked gone.
- Liner play dates are judged by the day the liner airs: a talk-up planned before midnight for a
  song after it, and a clockwheel liner step for the next song. **Test** on the server refuses a
  liner outside its dates and says why (#140).
- In browser playback, dayparting times a pick from what is left of the song playing, and a pick
  that replaces a song ending now is timed now (#140).
- A cart editor date refused by the server is shown under the editor and linked to its field, as
  the page's own checks are (#140). Only that 422 answer names the field; every other endpoint's
  422 is unchanged.
- A liner list redrawn at the station's midnight puts focus back on the same liner's button (#140).
- The schedule form hides **Date range** for one-time events, unless the event was saved with one
  (#140).
- In Safari and other WebKit browsers, Settings, Time checks no longer scrolls sideways at 320 px
  wide: a drop-down list whose choice is too long for its width now wraps the text onto a second
  line instead of widening the page. Clicking Dismiss on the status message also leaves keyboard
  focus on the control that had it; WebKit does not focus a clicked button, so focus used to fall
  to the page itself. The visual audit passes in WebKit as well as Chromium and Firefox, and the
  contributing and accessibility-testing guides say how to run it there.

- Rotation tests now count repeat-rule violations on the opening song and after a fresh start,
  including steps configured to ignore repeat rules. Those picks could previously repeat a song
  while the report said no rules had been relaxed.

- The web page works better for people who see it. In Windows High Contrast (forced colours) the
  selected section tab and Settings subtab now show their names; they were blank. Queue &
  Search no longer scrolls sideways when a music folder has a long path, and no view scrolls
  sideways at 320 px wide or with text at 200 %. Library tools, Browse library, the clockwheel
  editor, the address-ban form and the new worker token lay out each label above its field
  instead of running them together, and each clockwheel step is one row. Checkboxes and radio
  buttons are 24 px everywhere. Pressed toggles such as Mute and Record broadcast show a tick as
  well as the orange fill, cue points are listed in words under their colour key, compatible
  Camelot keys get an outline, and the phone slider track is visible. The Play or Pause button
  shows the words it is called by, for speech input. Time, date and address-ban fields match the
  other fields, list bullets stay inside their cards, a status message no longer stays over the
  next view after you change tabs, and a control reached with Tab near the window's edge is
  scrolled fully into view.
- Voice pack exact-hour files may have a space, `_` or `-` before the `o`, as in `hrs05_o.mp3`,
  so packs named that way say "it's 5 o'clock" on the hour instead of skipping the top of the hour.

- The Camelot wheel's wedge edges were faint (1.9:1). Every edge now reaches 3:1 against the
  wedges beside it in both themes, the compatible keys' outline is heavier and drawn on top, and
  the current key has the heaviest edge as well as its fill and its name in the middle. In
  Windows High Contrast the wheel uses system colours. Cue ticks keep a high-contrast ring there.

- Startup now loads the current library and shows its song and analysis counts, rather than
  the count from an older index. Doctor and list-indexes also use the current catalog. Existing
  catalogs no longer depend on the old index manifest; legacy indexes are imported on first use.

- Public now-playing history and its webhooks include only artist, title, duration and play time;
  local file paths and internal event IDs remain available in operator history.
- Hard scheduled liners and time checks prepare their audio before skipping the current song.
  Missing clips or failed speech generation leave the song playing and report the failed event.
- The library scan no longer runs out of memory on very long files such as multi-hour mixes. A
  track longer than `[playback] server_max_track_minutes` is refused early when its header gives
  a reliable duration. Estimated MP3/AAC lengths use the bounded decode instead, so an estimate
  alone does not reject a playable song. The decode still stops a second past the limit.
- A voice liner, cart or time check being decoded no longer holds up the mix bus. Liners are now
  decoded without the scheduler's state lock, the bus never waits for that lock when a planned
  liner is heard, and carts and time checks play without waiting for a liner decode. A sequential
  liner displaced by an announcement stays in rotation rather than being skipped unheard.
- Play now and every song change start sooner on server audio and the stream. Each song is now
  decoded once instead of twice (the render that mixes a song in keeps its audio for the render
  that plays it), the outgoing and incoming songs decode side by side when neither is ready, and a
  song's first loudness measurement happens in the same FFmpeg run as its decode. Saved-point
  renders also build the track in one buffer instead of copying it again. A file or processing
  change during decoding prevents reuse of the old audio. The audio processing is unchanged.
- Server audio and the stream are less likely to drop out while the library scan, a track pick or
  another busy task runs. The mix bus, its EQ and duck, and the Smooth and Stereo Tool processor feeds no
  longer give up Python's interpreter lock in the middle of a block, and a block that comes out late
  is made up straight away (up to eight blocks, 160 ms) instead of being lost from every listener's
  buffer. The sound is unchanged. Startup can compile the audio helpers without caching when
  no writable compilation-cache location is available.
- Clock music slots with repeat rules disabled still skip silent tracks and songs already
  playing or being prepared.
- A clock whose slots hold only songs just played or being prepared repeats one of them, with a
  warning, instead of leaving the station silent. The song playing never follows itself.
- Automatically chosen first songs now follow repeat rules and protected-track checks, including
  restored play history. When the rules leave no choice, they relax without stopping playback.
- Unknown or removed configuration settings now produce warnings and are ignored instead of
  blocking startup. Saved browser settings follow the same rule, preserving valid repeat limits
  alongside obsolete keys. Invalid values for supported configuration settings still fail validation.
  Doctor offers optional cleanup with a backup; nonempty old related-artist paths need manual review.
  Failed `serve` commands offer to run Doctor in a terminal with the same configuration and named
  index. `autodj doctor --name` also lets you select that index yourself.

- Folder imports requested while a library scan measures loudness now publish their tracks
  before that scan continues measuring the remaining tracks.

- When a liner's segue moves earlier, move any later saved overlap back to the segue.
  Track Info now matches playback, and subsequent cue edits can be saved.

- Brief playout stalls no longer trigger a burst of catch-up audio that can empty the
  output buffer and unnecessarily restart Smooth or Stereo Tool.
- Low, mid and high EQ settings now survive server restarts.
- Radio streams use HTTP framing that works with HTTP/1.0 players such as Winamp,
  avoiding chunk markers that can disrupt audio and track titles.
- Moving between tabs no longer makes screen readers say "selected" twice.
- The ReplayGain target loudness setting shows again when ReplayGain is chosen; it was always hidden.
- A setting with two descriptions now hides both when it does not apply, not just the first.
- A clockwheel you select stays selected. The player could save its place in the old
  clockwheel just after the selection, so the clockwheel page and the next restart went back
  to the old one. The new clockwheel also starts from its own place and restarts at its own
  times: a song already lined up when you switch no longer gives it the old clockwheel's
  place or restart time. Reselecting, renaming or editing the active clockwheel keeps the
  place it reached most recently.

### Changed

- The operations guide walks through adding a station to a server that runs one, step by step,
  from the backup and configuration to moving its listeners and retiring a station (#70).
  ADR 5 records the design: a station owns its playout chain, and the library is shared. The
  README introduces several stations.

- Analysis worker status counts waiting songs without sorting or building a list of their
  paths (#166).

- History exports load details only for the requested dates, while retaining the next play
  needed to calculate duration (#165). Reports use a separate database snapshot so they do not
  hold up play-history writes. Stored history remains unlimited.

- Clockwheel picks on a large library take less work, the same songs as before: in DMCA mode a
  pick checks each song against only the few albums and artists at a limit (and none while nothing
  is), the songs a clock can pick from are kept between picks until the library or the hours
  change, and least-recently-played steps reuse their last-played lookup when retrying with
  looser repeat limits (#167).

- The clockwheel's reset timing now measures every song from its start cue to its end cue (or its
  segue or outro, when earlier) for all clockwheels, not only for liner steps with "Only play if
  the following track fits before the next reset" ticked. Silence before the start cue and an
  unused tail after the end cue no longer count toward a song's length when AutoDJ decides
  whether to restart the clock at the next song handoff.

- `autodj index` now adds songs from registered folders and saves analysis in the current
  library, matching Library tools. Limits apply to advanced analysis; songs are added first.
  Force replaces analysis without clearing cues or history, and Enrich updates the current
  catalog. The old published-index format is deprecated; first-use imports and migration
  tools remain available, and original index files are kept for recovery.
- Large libraries stay responsive. Plays, the play log, settings, cue edits and loudness
  measurements no longer make the server reload the whole library and rebuild its similarity
  index, which took minutes per song on a 77,000-track library and held up the web page, the
  stream and the API. The library now reloads only when tracks, vectors or folders change,
  including changes from the analysis job or the command line. Starting the server no longer
  re-imports the index generation into an existing library, so a restart takes seconds instead
  of minutes. Browse library, Search, the check for tracks to analyse and the state pushed to web
  pages are fast at that size, and the state is built off the server's main loop.
- A file deleted while AutoDJ runs leaves the library at the next scan (every start, or Rescan)
  rather than within seconds; if it is picked first, playback skips it as before. A library
  folder whose share is not mounted marks none of its tracks gone, and they return with it.
- Library scans measure loudness only when loudness processing is Smooth or Stereo Tool, the
  modes that use it.
- `library.db` uses SQLite's WAL journal, so it can have `-wal` and `-shm` files beside it while
  AutoDJ runs. Backups and restores already handle them.

- The included Stereo Tool preset recovers more slowly after drum hits, keeping snare tails
  less prominent while retaining the default 7 dB AGC drive for quiet passages.

- Browse library and the voice liner list moved to the Queue & Search tab, after Search, as
  **Browse library** and **Browse voice liners**. Tab goes from Search to Browse library to the
  voice liners, and Track info's Return to Queue & Search puts focus back on the button that
  opened it, even after the list is drawn again. Adding, removing and rescanning folders stays in
  Library tools, which now says where browsing went, and uploading liners stays in Settings,
  Voice liners. Play next and Add to queue from Browse library are now spoken, since the queue's
  status region is on the same tab; Delete on a voice liner says its result beside the list and
  moves focus to the next liner, or to the list when none is left.
- Old repeat keys in `config.toml` and `web_state.json` are ignored with a warning.
  Re-enter any limits that still use those keys in Settings > Picking > Repeats.
- Random walk, least recently played and clock picks now hold back the same album and the same
  song title as well as the same artist, like similarity picks always did. Discovery picks now
  follow the artist, album and title rules too.
- Liners start at the outgoing song's segue by default. Track info has an "Allow start over
  outro" checkbox for voicetracks that may begin earlier.
- A blank liner Overlap start uses its Segue, or End if Segue is blank. Set it to 0 to let
  the next song's intro overlap the liner when timing allows. Overlap start cannot be later
  than the liner's segue.
- Smooth and Stereo Tool play original music and liner files. AutoDJ saves small loudness
  measurements in the library and processes audio in memory, instead of writing a second
  audio library. The old `prepared` folder inside the active index folder is no longer
  used and is safe to delete. It holds uncompressed 32-bit float WAV copies, about 21 MB
  per minute of audio (roughly 85 MB for a four-minute song), so on a large library it
  can free tens of gigabytes. After upgrading, a library scan measures loudness in the
  background; a song played before it is measured is measured as it loads, so its first
  start takes longer. Later plays reuse the saved measurement.
- Stereo Tool's **AGC drive boost** replaces **Boost quiet sections**. It starts at 7 dB
  and adjusts how strongly the processor lifts quiet passages throughout a song.
  Smooth keeps its built-in compressor and limiter, without the old quiet-section lift.
  Neither mode adds the separate intro-only lift. An uploaded preset without a single
  `Drive` line under `[Pre Compressor]` plays unchanged, and Settings and the status API
  say the AGC drive was not applied.
- Stereo Tool output gains 5.8 dB through a final peak limiter, with a -1.8 dB ceiling,
  5 ms attack and 100 ms release. The included preset remains the starting point for the AGC drive boost.
- Liners play 1.5 dB lower with Smooth or Stereo Tool processing, reducing their intended
  lift above music from 4.5 dB to 3 dB. Cue points are retained.
- Radio listening uses `/stream.mp3`, with `/stream.m3u` for players that need a playlist.
  Anyone who can reach the server can listen without a key; browser controls still require
  pairing when LAN authentication is enabled.
- The Settings page is split into subtabs: Transitions, DJ mixing, Song picking, Sound, Display
  and keys, Voice liners, Stream (in stream mode only) and Profiles and access. They work like the
  section tabs, and the page remembers the last one used in this browser.
- Indexing, DJ analysis and server playback now share one audio loader. FFmpeg decodes MP3, AAC,
  M4A and MP4 files everywhere, and soundfile decodes WAV, FLAC and the other formats, with FFmpeg
  as the fallback. A file that indexes now also plays, and analysis hears songs at the same level
  as playback: FFmpeg's own mono mix was about 3 dB louder. Server playback of MP3 files now
  needs FFmpeg, like indexing already did.
- A file FFmpeg repairs while decoding is reported as "decoded with warnings", and a file that
  fails is reported as skipped. `autodj index`, `autodj analyse` and `autodj analyse-library`
  end with one line counting both and save the details to `decode-indexing.log`,
  `decode-analysing.log` or `decode-library.log` in the index directory. FFmpeg's routine "Estimating duration from bitrate" note is no longer shown.

### Removed

- **Breaking:** the single clockwheel saved by earlier versions is no longer read. Library tools
  and the server log say so. Import the `.PAL` file again in Library tools, Clockwheels, under a
  name, then press Activate.
- **Breaking:** the old keyed stream addresses, `/stream/<secret>.mp3` and `/stream/<secret>.m3u`,
  and the `POST /api/stream/rotate` route that made a new one. A speaker or player saved with a
  keyed address must be set to `/stream.mp3` or `/stream.m3u` instead. AutoDJ no longer creates
  or reads `index/.stream-secret`. It does not delete an existing one; the file is unused and you
  can delete it.
- The librosa file-loading fallback. Librosa still detects beats, tempo and audio features.

### Security

- `[stream] trusted_proxies` now applies in every mode, not only in stream mode, so pairing, the
  listener request limits and the audit log see a proxied client's own address there too. Trust
  only a proxy that appends or overwrites `X-Forwarded-For`. Without `trusted_proxies`, an
  existing `FORWARDED_ALLOW_IPS` is honoured, and the startup log warns about every trusted proxy
  that is not on this machine.
- No page AutoDJ serves can be shown inside another site's frame any more, so a hostile page
  cannot overlay the web UI or a public page to trick a click (clickjacking). Every response now
  carries `Content-Security-Policy: frame-ancestors 'none'` (added to the page's own policy where
  it has one) and `X-Frame-Options: DENY`. The one exception is the new embeddable player, which
  may be framed only by the sites in `[player_page] embed_origins`.
- FFmpeg may now open only local files and pipes whenever AutoDJ asks it to read a song, a liner,
  a cart clip or a break (`-protocol_whitelist file,pipe`). A file that FFmpeg took for a playlist
  could otherwise make it fetch addresses on the network.
- A machine token (an analysis worker's or an announcer's) is refused on plain HTTP when the
  request carries a `Forwarded`, `X-Forwarded-*` or `X-Real-IP` header, even from the same
  machine: a reverse proxy there may have carried it in from the network unencrypted. Through a
  proxy, machine tokens need TLS at the proxy and `X-Forwarded-Proto: https` from a proxy the
  server trusts. With `[stream] trusted_proxies` empty, a streaming server now trusts a proxy on
  its own machine (`127.0.0.1`, `::1`), as a server that does not stream always has, so a TLS
  reverse proxy on the same host keeps working; a list you set is used as it is.
- AutoDJ now needs python-multipart 0.0.32 or later, which refuses a form part with more than
  eight headers or a header over about 4 KiB.

## [0.20.0] - 2026-10-11 (not released on its own; shipped in 0.21.0)

### Added

- `autodj analyse-library` runs advanced analysis for songs added through Library folders
  with advanced analysis enabled. The web interface normally starts this job in the background.
- `autodj migrate-index` previews a schema-3 to schema-4 conversion; `--apply` saves a verified
  recovery copy before converting, keeping the existing analysis. Interactive `autodj serve`
  offers to do this and continue startup. Services print instructions without waiting for input.
- Add multiple music and liner folders from Library tools, with separate choices for
  subfolders and background analysis. Songs can play before advanced analysis finishes.
  Browse by folder and see analysis progress. Confirmed file moves keep their saved data;
  removing a folder leaves its files and cues intact.
- **Use saved segue points** gives stream and server audio playback station-style fades.
  Liners fit around the songs' Outro and Intro cues. **Overlap start** controls when music
  can enter a liner or jingle, while its Segue cue keeps reverb tails from delaying the song.
  The queue shows which handoff is already prepared.
- **Track info** brings together metadata, analysis status, last-played time and cue editing
  for songs and liners. Open it from Now Playing, the library, search or the queue. Preview
  cues without changing station playback; Save or Discard protects edits when switching tracks.
- **Least recently played** chooses songs that have waited longest. **Clockwheel** follows
  music and liner folder slots, choosing each track as needed. Import supported PAL folder
  rotations and restart the rotation at a song handoff near the hour or other chosen minutes.
  Only tracks that actually start playing update their last-played time.
- Play history survives restarts and is kept without an expiry limit. The history viewer
  shows the latest 500 entries; each track also retains its last-played time for selection.
- **Output processing** offers Off, ReplayGain, Smooth and Stereo Tool for stream and server
  audio. Smooth balances source levels and compresses the continuous mix. Stereo Tool uses
  the same source preparation with a bundled free preset and a separately installed executable.
- **Keep playing without listeners** lets the stream run unattended.
- `autodj index` now recognises tracks that moved, for example after `beet move`, and keeps their
  vectors and DJ cues instead of embedding them again. The index format changed (schema 4).
  Convert a schema-3 index with `autodj migrate-index --apply`; `docs/operations.md` explains how.
- In stream mode and with server audio, the new **Drop mixing** setting (`drop_mix` in the
  `[djmix]` section of `config.toml`, off by default) mixes songs drop to drop. When both songs
  have a reliable drop and **Beatmatch** can lock their beats, the new song's build-up plays under
  the end of the old song, and its drop lands on the downbeat that starts the old song's next
  section, just as the old song has faded out. With the Auto transition effect a noise riser
  builds up to the drop. Drops you marked in Rekordbox, Traktor, Serato or Mixxx are used first; a
  drop AutoDJ found itself is used only when it is unmistakable. Songs that do not fit, for example
  because the drop is more than 90 seconds in or the old song would be cut more than 32 seconds
  short, are mixed the usual way. A drop mix keeps its timing even with **Keep vocals apart** on,
  since moving the crossfade would take the drop off the beat.
- In stream mode and with server audio, the new **Key shift** setting (`key_shift` in the
  `[djmix]` section, off by default) makes clashing keys mix. When the new song's key clashes with
  the old one but would fit one or two semitones higher or lower, the new song plays that much
  higher or lower during the crossfade, at the same tempo, then slides back to its own key over
  the next few bars, so it plays in its true key from then on. The Auto transition effect treats
  such a pair as matching keys.
- In stream mode and with server audio, Skip can now end the song the way a DJ would. **Skip
  style** in Settings (`[playback] skip_style`) offers Echo out (the music cuts and its echoes ring
  out), Backspin (the record spins backwards to a stop) and Loop roll (a short loop repeats, rolling
  faster as it fades), as well as the quick fade it always used, which stays the default. Each
  effect lasts one to two beats, at most 2 seconds, and ends on a beat when the song's tempo is
  trusted; then the next song starts exactly as before. Browser playback keeps its own skip.
- Voice liners can now talk up to the vocal, the way a radio presenter "hits the post". With
  **Talk up to the vocal** on (`[playback] liners_talk_up`, off by default), a liner due as a
  song starts is timed to finish just before the singing comes in: at the first line of the song's
  synced lyrics, or else at its detected intro end. A liner too long for the intro starts during the
  crossfade instead when that fits, and otherwise plays as the song starts, as it did before. The
  music ducks under it as usual. This works in stream mode and with server audio; liners played by
  the browser are not timed this way.
- In stream mode and with server audio, crossfades now keep two singers from overlapping. When both
  songs have synced (LRC) lyrics, the crossfade is shortened, or moved a little, so the song that
  is ending finishes its last line before the next song starts singing. The fade is never made
  longer, or shorter than 1 second, and when nothing avoids the clash it stays as it was. It is
  on by default; turn off **Keep vocals apart** in Settings (`[djmix] vocal_guard`) to mix as before.
- In stream mode and with server audio, **Beatmatch** now lines up the beats as well as the
  tempos. When both songs have a confident tempo and an analysed beat grid, the crossfade starts at
  the moment that puts the new song's first downbeat on a downbeat of the song that is ending, so
  the kicks land together instead of flamming. The crossfade moves by less than a bar to do this;
  with **Phrase align** on, the new song's downbeat lands on the phrase boundary when one fits. A
  first-downbeat cue or intro start marked in Rekordbox, Traktor or Mixxx tells AutoDJ which beats
  are downbeats. The new song still comes in at the same point as before, so no more of its intro is
  cut. Songs whose tempo or beat grid is uncertain are still matched by BPM, as before.
- **Beatmatch** now mixes songs at half or double tempo, such as 70 BPM into 140 BPM, as long as
  the stretch stays within `beatmatch_max_stretch`. They used to be mixed without beatmatching.
- After a beatmatched crossfade the new song now eases back to its own tempo over a few bars
  instead of jumping back at the end of the fade. `beatmatch_glide_bars` in the `[djmix]` section
  of `config.toml` sets how many bars this takes (8 by default, at most 30 seconds; 0 returns
  straight away).
- Phrase align now works for crossfades played in the browser, not only when the server does the
  mixing. With **Phrase align** on, a crossfade in the browser starts on the phrase boundary
  nearest the point where it would otherwise begin, so the next track comes in at the end of a
  phrase rather than mid-bar. **Phrase length (bars)** sets how long a phrase is, as it does for
  the server mix. Beat-synced transition effects count the boundary as their first beat. The
  crossfade keeps its usual start when the track has no detected beat grid,
  when no boundary is within half a phrase, or when starting on the boundary would leave too
  little of the track for the whole fade.
- In stream mode and with server audio, the rhythmic transition effects now follow the tempo of
  the song that is ending: echo out repeats on eighth notes, dub delay on quarter notes, gate
  stutter and transformer chop in sixteenth notes, stutter build speeds up from quarter to
  thirty-second notes, beat repeat loops an eighth-note slice, scratch works a one-beat slice, and
  sidechain pump ducks on every beat. The gates, loops and pump start on the song's next downbeat.
  Air horn, dub siren and ring modulator are tuned to the songs' keys. This matches what the
  browser already did. A tempo is only used when beat tracking was confident about it; otherwise
  the effects keep their old fixed timings. The **Beat-sync** and **Key-sync** settings now apply
  to the server mix as well.
- A new transition effect, **auto**, picks an effect to suit each pair of songs: a backspin, dub
  siren or vinyl rewind into a discovery pick; an echo, tape stop, reverb, backspin or low-pass
  sweep to hide tempos more than 8 % apart or keys that clash; a noise riser or stutter build into
  a clearly louder song; a cross-EQ swap, high-pass sweep or plain fade between songs in the same
  key and tempo; otherwise a filter sweep, cross-EQ swap or echo. There is still some variety
  within each kind. The page's state now names the effect coming up for the next crossfade.
- Intro and outro markers set in Mixxx, Rekordbox or Traktor now move the mix. Mixxx's intro start,
  intro end and outro start, and Rekordbox's and Traktor's fade-in and fade-out points, replace
  the intro and outro AutoDJ detects, so the crossfade starts and ends where you marked it. The cue
  strip lists them as intro start, intro end, outro start and outro end. Songs analysed before
  this pick the markers up the first time they play.

### Changed

- Stereo Tool defaults to `./tools/stereo-tool/stereo_tool_cmd.exe` on Windows and
  `./tools/stereo-tool/stereo_tool_cmd` elsewhere. Custom executable paths still work.
- Track info follows Queue & Search in the tabs and keyboard navigation.
- Settings help uses plainer language and explains which controls apply to saved segue
  playback. The processing selector is labelled **Output processing**.
- `autodj serve --host ADDRESS --port PORT` now works with a network address: with no allowlists,
  access token or `--insecure-lan`, it serves there in LAN mode, with pairing and the machine's
  detected names allowed, instead of stopping with "LAN binding requires an access token". Without
  `--host`, `autodj serve` still listens only on this machine.
- Docker runs AutoDJ in LAN mode on the host network: `docker compose up -d --build` starts one
  `autodj` service that listens on all of the machine's interfaces, prints the machine's own
  addresses, and requires each browser to pair. The server secret is created on first start and
  kept in the index folder. It reads `config.toml` and `config.local.toml` from the checkout, so
  HTTPS, extra host names and stream mode are set there, as on bare metal. `.env` sets the music,
  index and model folders, `AUTODJ_UID` and `AUTODJ_GID` (the folders' owner, so no `chown` is
  needed), and `AUTODJ_BEETS_PATH` for a beets library. The health check also works with HTTPS.
  Bare-metal `autodj serve` stays local-only unless you pass `--lan`.
- **Beatmatch** in stream mode and with server audio now stretches only the part of the new song
  that plays during the crossfade and the glide after it, not the whole song. On a 4-minute song
  this cut the work from about 5.5 seconds of processor time and 1.5 GB of memory to under one
  second and about 350 MB, so a small server keeps up much more easily. Drums in the stretched part
  also stay sharper.
- Crossfades the browser plays now use the transition effect AutoDJ chose for that pair of songs,
  the same one the page's state names for the next crossfade. **auto** now works in the browser
  too, where it used to play a plain crossfade, and **random** and **rotate** no longer make a
  separate choice in the browser. With **Phrase align** on, the browser checks that the chosen
  effect fits before the end of the song, instead of allowing for the longest effect. Over plain
  HTTP, an effect the browser cannot play there (gate stutter, bitcrusher, freeze, glitch) is
  swapped as before: random and rotate pick another one, and the others play a plain crossfade.
- A pairing code works only after someone asks for one, and each one pairs one device. Before,
  the current code always worked, whether or not anyone had asked, and any number of browsers
  could pair with it. Ask with `autodj devices pairing-code` or **Show pairing code** in a paired
  browser. Startup prints a code only while no browser is paired, so once one is, the log never
  holds a working code. When a device pairs while the code is shown, the page says so and hides the used
  code.

### Removed

- `autodj setup-lan` and the Compose `lan` and `stream` profiles. The Docker service detects the
  machine's addresses itself, so there is no host or origin to write into `.env`; delete
  `AUTODJ_LAN_HOST`, `AUTODJ_LAN_ORIGIN` and `AUTODJ_LAN_BIND_ADDRESS` from an existing `.env`.

### Fixed

- Container builds refresh `perl-base` from Debian's package repositories to pick up security fixes.
- Backups now include the station library database, preserving registered folders, cue edits,
  last-played times and full play history. Restores check database integrity before replacing files.
- Output processing keeps audio playing through a fallback while the processor restarts,
  then returns to processed sound when it recovers.
- Older version-1 web settings are migrated on upgrade, preserving the harmonic-mixing
  preference and saving the original settings as `web_state.json.v1.bak`.
- Listen here opens a fresh stream URL on every connection, preventing Firefox from
  reusing an old media offset after a page refresh. Shared station links stay unchanged.
- Adding a folder scans and counts that folder instead of the whole library. Additions queued
  during a scan are combined for the next pass; Rescan still checks all registered folders.
- Folder actions announce success or failure once. Import progress remains available as
  plain text without repeating its counts and filenames through the screen reader.
- Adding folders accepts quoted Windows paths copied from File Explorer. Failed additions
  keep their error visible and focus the folder field so the reason can be read.
- Neutral EQ no longer colours the server mix, and moving EQ sliders changes the sound smoothly.
- Replacing audio at an existing path no longer leaves stale cues that can break a rescan.
  Changed file contents reset cue and analysis caches, including when a file is retagged;
  unchanged files and confirmed moves keep their cues.
- Cue times round correctly across second and minute boundaries, and oversized values are
  rejected instead of silently clearing a cue. Random walk now displays correctly in Settings.
- Signing out clears folder, clockwheel and Track info data, including previews and unsaved
  drafts. Responses from the old session cannot put that data back on screen.
- In stream mode and with server audio, a beatmatched song no longer jumps back to its own tempo,
  by up to 8 %, the moment the crossfade ends.
- In stream mode and with server audio, "beatmatched 1.04 times" now describes the song that just
  started. It used to give the stretch planned for the song after it, so a song that came in
  untouched could be announced as beatmatched and the other way round.
- In stream mode and with server audio, the music no longer stops for good when no song fits the
  settings. A BPM range or harmonic mode that no song matches, or a library reload that removed the
  song coming up, made every pick fail, and the station played silence until a restart. AutoDJ now
  plays a random song instead, avoiding recent songs as far as the library allows, logs one warning,
  and goes back to normal picks as soon as they work. The random song stays inside your BPM range
  whenever any song is in it; the warning says when none is. "Why this track" says it was a random
  pick.
- When every song in a small library has played recently, the auto-DJ no longer goes back to the
  song that is starting or the one before it. The no-repeat window also follows the library size
  after an index reload, so a library that started empty and then filled is not stuck repeating a
  few songs.
- In stream mode and with server audio, editing the queue while the next song is ready no longer
  lets the song after it be the one that is just starting, which could make two songs alternate.
- In stream mode, a queued song is no longer lost when the set stops just as that song starts: it
  goes back to the top of the queue. The History page no longer loses an earlier play of a song
  when a set stops as that song starts again.
- In stream mode, **Play now** and **Random** no longer lose the chosen song when the set stops at
  the same moment because nobody is listening; the next set starts with it.
- The web page no longer stops updating when a stream set stops while the page's state is being
  built. One failed update now only logs an error, and the next update runs a second later.
- Cue points, intro and outro markers written by **Analyse** in the Library panel now show up for
  songs AutoDJ had already looked up, within a few seconds, without restarting the server.
- When the DJ meta database cannot be opened at startup (for example because it is locked), AutoDJ
  tries again every 30 seconds instead of running without cue points until a restart.
- Changing the no-repeat window in Settings while music plays can no longer drop the song that is
  starting from the recently played list.
- Stopping the server while a song is being analysed for the server mix no longer logs an error
  about the closed DJ meta database; shutdown waits for that work like it does for other analysis.
- In browser playback, the first song's Up next and length are no longer overwritten when the page
  advances while the server is still picking at startup.
- Listening no longer stops when the page's connection to AutoDJ drops for a moment. A Cloudflare
  tunnel in front of AutoDJ closes that connection every few minutes, and each time the page
  stopped Listen here (and browser playback) without a word, said "Disconnected, retrying" and
  then "Live" as if all was well, and the server ended the set 30 seconds later. The music now
  plays on through the drop: Listen here has a connection of its own, and browser playback plays
  from what it has loaded and asks for the next track again once the connection is back. If a
  track runs out or stops loading while AutoDJ cannot be reached, the page says once that the music
  stopped and carries on when the connection returns. Playback still stops at once when the page
  learns that its session has ended (it expired or this browser was signed out), and a server that
  comes back louder than the page was playing is still held at the page's level.
- When AutoDJ stays out of reach while you are signed in through Cloudflare Access, the page now
  says once, in plain words, that you may need to reload the page to sign in again, and keeps
  trying in the background. Before, an expired Cloudflare sign-in left the page retrying silently
  forever after a single "Disconnected, retrying".
- Listen here no longer stops with "The stream stopped responding." while the stream is still
  playing. A pause in the download started a 15-second countdown that only a fresh "playing"
  event cancelled, and none comes while the buffer still covers playback. The stream now counts
  as stopped only when playback itself has not moved on. A dropped stream also keeps trying for
  up to about half a minute while AutoDJ cannot be reached, instead of giving up after one try.
- In browser playback, the music no longer stops dead at the end of a track after you have paused
  and resumed, with a crossfade of 0 seconds, the Fixed transition mode, or no next track lined up.
- In browser playback, the page no longer fades back to the track it has just left. A status update
  sent just before the server moved on could arrive after the page had already faded into the next
  track and start a fade back to the old one, saying its name again. The page now waits for the
  server to confirm the move, and tells it which track it is moving on from.
- In browser playback, a track that fails to load because the connection dropped is no longer
  skipped and marked as bad. The page loads it again once the connection is back, and only a
  second failure with the connection up counts as a bad file.
- Voice liners set to play every few songs in browser playback no longer talk over the
  transition between tracks or break its fade. They wait until the transition has finished, and
  they now turn down both tracks during a transition instead of only one.
- Moving or removing a queued track no longer drops keyboard focus to the top of the page when a
  status update redraws the queue before the change is saved. Focus lands on the moved track's
  button, or on the row that took a removed track's place.
- A checkbox in Settings, the volume slider or an EQ slider no longer jumps back to its old value
  while your change is still being saved. On a slow connection the page took the old value from
  the next status update; it now keeps your change until the server confirms it, and puts the old
  value back only if the save fails.
- Beatmatch on skip in browser playback now holds the new track's tempo for the whole transition,
  however long the transition effect runs, and then eases back over a few seconds instead of
  jumping. Like the server mix, it no longer stretches a tempo that is more than 8 percent away.
- In stream mode and with server audio, the auto-DJ no longer plays two songs back and forth. It
  picks each next song in the background as soon as a song starts, but the song that was starting
  was recorded as played a moment later, so the pick could choose it again; two songs that were
  each other's closest match then alternated for as long as the set ran. The song that is starting
  now counts as played for that pick.
- The History page adds each new song to the top of the table while page 1 is shown, without
  Refresh. The rows already there are left as they are, so a screen reader reading the table keeps
  its place, and nothing is said.
- ALAC and other `.m4a`, `.mp4` and `.aac` files are no longer skipped by the server mix and
  stream mode, and they now get cue points and intro and outro markers. `autodj index` already
  decoded them through FFmpeg, but the server mix and the background analysis that runs when a
  track starts only tried soundfile and librosa, which cannot read MP4 files, and logged
  "Format not recognised". Both now decode these files through FFmpeg too.
- In the Full intro and outro and Fixed (skip silence) transition modes, and with Outro and intro
  align on, the next track no longer starts partway into its first verse. AutoDJ skipped
  everything before the point where a track first got loud, so a verse sung over a quiet intro was
  cut, often in the middle of a word. The next track now starts at its first sound, and its intro
  plays under the end of the outgoing track, as in Mixxx. In Full intro and outro mode the fade is
  sized from that first sound to the intro's end. Tracks analysed before this release have their
  first sound measured when they come up to play; the rest of their analysis is kept.
  `dj_meta.db` gains an `intro_start_s` column the first time this release opens it.

## [0.19.1] - 2026-09-29

### Fixed

- `autodj index` no longer tries the same missing files on every run when the beets library still
  lists tracks that were deleted from disk. It skips them and prints one warning with the count, a
  few example paths and a hint to run `beet update -M` so beets drops them. Before, each run pruned
  them from the index, queued them again as new and failed every one in ffmpeg.

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
- Shift+K on a track with no detected key said only "unknown". It now says "Key unknown". For such
  a track, the announcement when the track changes now leaves the key out instead of ending in
  "key unknown".
- Changing the stream Quality no longer writes a "ValueError: I/O operation on closed file"
  traceback to the server log. The old encoder's output ended while it was being read, which is
  expected there; the stream carried on normally before and still does.
- The first model download could fail on Windows without Developer Mode with "[WinError 1314] A
  required privilege is not held by the client". Hugging Face copies files where it cannot make
  symlinks, but its parallel downloads tried a symlink before it had found that out. AutoDJ now
  downloads the model files one at a time, which avoids that. The model folder layout is
  unchanged.
- `autodj index --limit N` without a beets database read the tags of every file in the music
  folder before applying the limit, which took more than 30 minutes on a 76,000-track network
  share. It now stops looking once it has found N tracks that are not in the index yet. Every
  `autodj index` run also skips reading the tags of tracks that are already indexed.

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

- 1296 Python tests pass.  26 JavaScript module tests pass.  Cross-browser audit (Chromium / Firefox / WebKit) verified end-to-end on a network-storage deployment.

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

[Unreleased]: https://github.com/blindndangerous/AutoDJ/compare/v0.21.0...HEAD
[0.21.0]: https://github.com/blindndangerous/AutoDJ/compare/v0.19.1...v0.21.0
[0.20.0]: https://github.com/blindndangerous/AutoDJ/commit/5f2fbd208ded518d7572dce755faaae186add889
[0.19.1]: https://github.com/blindndangerous/AutoDJ/compare/v0.19.0...v0.19.1
[0.19.0]: https://github.com/blindndangerous/AutoDJ/compare/v0.18.2...v0.19.0
[0.18.2]: https://github.com/blindndangerous/AutoDJ/compare/v0.18.1...v0.18.2
[0.18.1]: https://github.com/blindndangerous/AutoDJ/compare/v0.18.0...v0.18.1
[0.18.0]: https://github.com/blindndangerous/AutoDJ/compare/v0.16.1...v0.18.0
[0.17.0]: https://github.com/blindndangerous/AutoDJ/commit/6b6834ca6079d0942867d59f3779cb284c41580a
[0.16.1]: https://github.com/blindndangerous/AutoDJ/compare/v0.16.0...v0.16.1
[0.16.0]: https://github.com/blindndangerous/AutoDJ/compare/v0.12.0...v0.16.0
[0.12.0]: https://github.com/blindndangerous/AutoDJ/releases/tag/v0.12.0
