# Create, switch, edit and retire stations

Create or duplicate a station, inspect its saved settings while stopped, and choose which station the page controls.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Several stations

One AutoDJ server can play several stations. Every station has Start/Stop controls; the
default station starts with AutoDJ by default and can also be stopped separately. Stop releases a
station's audio resources; Start begins fresh playback. Pause holds playback
position. Stopping one station does not stop another. Scheduled events missed while stopped are
skipped when the station next starts.

Each station has its own settings, categories, queue, history, schedule, carts, clockwheels,
liners, shows, ads, requests and relays. Music metadata, analysis and cue points belong to the
shared library. Pairing, authentication, networking, TLS, trusted proxies and analysis workers
belong to the installation.

## Editing a stopped station

Select a stopped station to inspect and edit its saved settings, schedules, clockwheels,
relays, carts, shows, ads, liners, matched intros, requests configuration, and DJ accounts.
History and exports remain available. These editors use the same validation and saving logic
as a running station; opening an editor does not create an audio engine. Rotation tests and
show generation can run explicitly without broadcasting.

Commands that require playback, such as Skip, queue changes, firing a cart, and recording a
live voice transition, remain unavailable. Start builds a fresh player from the saved settings.
Edits are briefly unavailable while a station is starting or stopping.

## Creating a station from the web page

Use **New station** in the station switcher to create a station with AutoDJ defaults. Enter a
unique ID and display name. AutoDJ gives it `<root recording.folder>/<slug>` (typically
`recordings/nightowl`), creates it stopped, and adds it to the switcher without restarting or interrupting a
running station. Review its settings, then start it. `autostart` controls later server starts.

Use **Duplicate station** to copy a source station's written configuration and current web-saved
settings. You can also choose to copy its clockwheels, schedule, and carts. The copies live in
the new station's folder, so editing them does not change the source. A copied schedule starts
with no run history; an event that names a missing profile, cart, clockwheel, show, or liner
reports an error when it runs. An uploaded cart belongs to the new station. A cart slot that
names a liner in the source station's liner folder can still play it while its record and file
remain in the shared library. The liner file itself is not copied.

The new station starts with an empty queue and play history. AutoDJ does not copy active
playback, pending listener requests, listener sessions, bans or statistics, running jobs,
recording state, relays or their passwords, liner play dates, ads and reports, shows, matched
intros, profiles, liners, time-check clips, announcements, the source station's own Stereo Tool
licence key, or caches. Relays stay off until
configured for the new station. Music files, library records, analysis, song cue points, and
Stereo Tool preset files remain shared.

AutoDJ stores the definition at `<index_dir>/stations/<slug>/station.json` alongside
that station's state. A station's secrets (its stream relays, listener user and password,
webhooks, and its own Stereo Tool licence key) never go in `station.json`. They go beside it in
`relays.json` and `secrets.json`, which only your account can read and backups leave out. Startup migration reads legacy root
station sections and `[stations.<slug>]` tables from `config.toml` and `config.local.toml`, and writes
the canonical station files. The
legacy sections remain in the TOML file but normal loading ignores them; remove them after
migration to avoid confusion. A later edit to legacy TOML is not applied. To remove a web-created
station, stop AutoDJ and move its station folder away. There is no delete button yet.

## The station switcher

With more than one station configured, the banner of the web page has a **Station** button after
the AutoDJ heading, for example "Station: AutoDJ". It is hidden while the server has one station.
Pressing it shows a list of station buttons right after it, one for every station with its
state, such as "Night Owl Radio, on air, 3 listeners" or "Oldies Hour, off air". The station the
page controls has a tick and is read as current.

- **Keyboard:** Enter or Space on the Station button shows the list and moves focus to the
  current station. Up and Down Arrow, Tab and Shift+Tab move through the stations, and Home and
  End go to the first and last. Enter or Space switches to the station under focus, and the list
  closes once the switch is made; choosing the current station only closes the list. If the
  switch does not happen, for example when you answer No to leaving the station, focus stays on
  that station in the open list. Escape closes the list without switching, and so does
  pressing the Station button again. All three put focus back on the Station button. The list
  stays open when focus moves on or you click elsewhere. Moving through the list never switches;
  only pressing a station button does. Letters are the page's shortcuts, as on any button.
- **Shortcuts:** `c` moves focus to the Station button, Shift+C says which station the page
  controls, whether it is on air, its listeners and its song, and Shift+[ and Shift+] switch to the
  previous and next station.
- **What switches:** every control on the Now Playing, Queue & Search, Carts, Voice tracks, Track
  info, History and Settings tabs then acts on the chosen station, and they show its state: its
  song, queue, history, settings, carts, schedule, clockwheels, hour preview, liners, profiles,
  time checks, matched intros, stream, listeners, recording and webhooks. Library tools, search
  results, library folders,
  song points, Stereo Tool presets and paired devices are shared by all stations.
- **How it says so:** the heading becomes "AutoDJ: Night Owl Radio", the browser tab's title
  ends with "Night Owl Radio - AutoDJ", the page's main landmark is named after the station, and
  each station tab's name ends with it. The switch is announced once, for example "Night Owl
  Radio. Playing Artist — Title. On air, 3 listeners.", or "Now controlling Oldies Hour." for a
  station that is off air. A screen reader says
  "On Night Owl Radio" after each control that changes what is on air (Play or Pause, Skip,
  queue changes, carts, Run now, profile Apply, listener Disconnect and Ban, the stream settings
  and recording) and "Shared by all stations" after the Library tools tab and song points.
  Confirmations, such as Clear queue, end with the station's name.
- **The address keeps the station:** the page adds `?station=nightowl` to its address, so a
  reload or a bookmark opens the same station. Each browser also remembers the last station
  chosen and opens it when the address names none; the address wins. An address naming a
  station that is not configured opens the default station and says so; if the station list
  cannot be loaded, the page keeps the station the address names and loads the list again.
- **Listen here** keeps playing the station it was started on. While the page controls another
  station, a line under the controls says which, for example "Playing AutoDJ"; pressing Listen
  here stops it, and pressing it again plays the station the page now controls.
- **Browser playback:** when the selected station plays in the browser, only that
  page's own decks play it. Switching away asks first, because the page then stops playing it;
  the station itself goes on for everyone else.
- **A stopped station:** choosing one, or having the station stopped while the page controls it,
  shows "Night Owl Radio is off air." with a **Start Night Owl Radio** button on Now Playing. The
  page looks every 10 seconds whether the station is back. **Settings, Stations** has Stop and
  Start controls for every station, including the default.

Every page has its own choice, so two browsers can control two stations at once.

## Adding a station, step by step

This adds Night Owl Radio (`nightowl`) to a server that runs one station today. Nothing on disk
moves: the default station keeps its files, and Night Owl starts with empty ones of its own.

1. **Check the server can carry it.** Each running station costs threads, two to four
   subprocesses and a few hundred megabytes of memory (see
   [Cost of each running station](station-soak.md#cost-of-each-running-station)). To measure it on the
   machine itself, run the long soak test in [Soak test](station-soak.md#soak-test) while the server is idle.
   One Stereo Tool licence runs one copy, so give the new station
   `playback.loudness_processing = "smooth"` unless you have a second licence.
2. **Stop AutoDJ and take a backup:** `uv run autodj backup backups/before-nightowl.zip`.
3. **Restart AutoDJ, then create the station** in Settings > Stations. Set its display name, output, and whether it
   starts with the server. Review its Stream, Playback, and Recording settings; they are saved in
   `<index_dir>/stations/nightowl/station.json`. The station folder is created automatically.
   Its secrets are stored separately in `secrets.json` and `relays.json`. For deployment
   automation, edit those canonical files or use station-scoped environment variables; do not
   add a new `[stations.nightowl]` TOML table, which is accepted only as legacy migration input.
4. **Liners and time checks.** Night Owl plays the liners in every registered liner folder,
   and liners uploaded for it go in `<index_dir>/stations/nightowl/liners/`. Its time
   checks folder is `<index_dir>/stations/nightowl/time_checks/` unless you set
   `time_checks.clips_folder` in its `station.json`; add voice packs and clips to it from
   **Settings**, **Time checks** while Night Owl is the station the page controls. It also finds
   the packs in the shared `<index_dir>/shared/time_checks/` folder.
5. **Containers only:** make sure the new recording folder, and any liner folder outside the
   index, is mounted and writable. The station's own folder is inside the `/index` volume.
6. **Check it:** `uv run autodj doctor --station nightowl`. A configuration error (a recording
   folder or relay target another station uses, or an invalid station definition) stops the
   configuration from loading, and doctor says which. Doctor offers to
   make the station's folder; AutoDJ also makes it when the station first starts.
7. **Start Night Owl** in Settings > Stations (and start AutoDJ first if you stopped it again). The startup log names each streaming station's stream and status page, such as
   `/nightowl.mp3`. Run `uv run autodj doctor` again: each station now has its own
   lines.
8. **Set it up in the web page.** Choose Night Owl Radio with the Station button in the banner
   (see [The station switcher](#the-station-switcher)), then choose its clockwheel, DMCA mode
   and processing, and build its schedule, carts and liners. To start from the default
   station's clockwheels or profiles, copy files from `stations/default/clockwheels/` or
   `stations/default/profiles/` into `stations/nightowl/clockwheels/` or `stations/nightowl/profiles/`
   while AutoDJ is stopped.
9. **Move the listeners.** Publish `https://<your host>/nightowl.mp3`. If listeners
   already use an address on a hosted Icecast or Shoutcast server, add a relay from Night Owl to
   that server (see [Relaying to Icecast and Shoutcast](relays.md#relaying-to-icecast-and-shoutcast)) and
   stop whatever fed it before, so the address listeners use does not change.
10. **Take a new backup** and check that its `stations/nightowl/` files are in it (see
    [Backups and doctor](backup.md#backups-and-doctor)).

To retire a station, stop it (Settings > Stations > Stop, or `POST /api/stations/<slug>/stop`),
stop AutoDJ, then move its canonical station definition out of the station folder before
restarting. Editing old TOML station tables does not remove a migrated station. Its folder stays: doctor names
it as no longer configured, and backups leave it out. Move or delete it by hand once a backup
that holds it is safe.


## Related tasks and contracts

- [Station definitions, routes and lifecycle](../../reference/operations/stations.md)
- [Measure multi-station resource use](station-soak.md)
- [Back up the installation and its stations](backup.md)
