# Station definitions, routes and lifecycle

Look up canonical station configuration, public and paired routes, lifecycle states, failure recovery and log attribution.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Configuration

Every station has `<index_dir>/stations/<slug>/station.json`, including `stations/default/`.
Use Settings > Stations to create or duplicate a station and set whether it starts with the
server. The slug uses lower-case letters, digits and inner hyphens, up to 32 characters.
A server supports the default station and up to eight additional stations.

Startup migrates old root station settings and `[stations.<slug>]` tables into these definitions.
It also migrates station passwords and webhooks from `config.local.toml` and
legacy relay environment passwords when their matching relays exist. Normal loading reads the
migrated format; old TOML keys and default-only `AUTODJ_STREAM_*` variables are not fallback
settings. Station secrets live separately in `secrets.json` and `relays.json` and remain excluded
from backups. Server and library configuration remain in `config.toml`. Fresh installations
configure every station in its `station.json`; existing settings can also be changed in Settings,
Stations.

The top-level `station` object in `station.json` contains the station's settings sections. Its
nested `station` section holds output, time zone and upload size, for example:

```json
{
  "version": 1,
  "created": "2026-10-16T00:00:00+00:00",
  "source": null,
  "station": {
    "station": {
      "output": "stream",
      "server_audio_too": false,
      "timezone": "America/New_York",
      "liner_upload_max_mib": 50
    }
  }
}
```

Settings > Stations edits these options for the selected station, including while it is stopped.
Output can be `browser`, `server` (sound card), or `stream`. In stream mode, `server_audio_too`
also sends audio to the sound card. Stations may play on the same sound card simultaneously.
Output and timezone changes take effect on that station's next start. Time display format remains
shared across the interface. A station that cannot start reports its error while the interface
and other stations stay available.

Stations need separate recording folders and relay targets. Their definitions do not inherit
settings from the default station. A duplicate copies explicitly selected saved configuration;
it does not inherit the source's credentials or playback history.

The index folder holds `libraries/main/` for library data, `shared/` for shared assets,
`stations/<slug>/` for each station's files, and installation-wide logs and tools. Categories are
station-owned, in each station's `categories.json`. The earlier layout migration copied formerly
shared categories into each station's folder. A migrated default station keeps its old time-check
clip folder through an explicit setting; fresh stations use their own clip folder unless configured
otherwise.

A station keeps its state in `<index_dir>/stations/<slug>/`, the default station in
`stations/default/`, all with the same file names: `station.json` for its configuration, `web_state.json`, `schedule.json`, `carts.json` and `carts/`,
`time_checks.json`, `listener_bans.json`, `listener_stats.json`, `recording.json`, `relay-status.json`, `clockwheels/`,
`categories.json`, `liners/` and `profiles/`. Each station's `station.db` holds its history,
last-played values and clock setting independently of the library catalog.

Startup imports existing station history and settings from `library.db`, including records for
stations no longer configured. Each import commits with a completion marker, so interrupted imports
can be retried without duplicating history or replacing newer station values. The legacy source is
preserved. Track metadata, analysis and shared cue points stay in the library.

Whole-installation backups snapshot station databases through SQLite, including committed WAL data.
Restore rolls back a failed installation with the other restored files. Ledger-only folders for
removed stations are included. Create fresh backups after updating: older backup formats are not
supported. This does not add an individual-station backup command.



## Running

`autodj serve` starts stations whose definitions have `autostart = true`, including the
default station when enabled. Every station can use browser, sound-card or stream output.
A streaming station serves its mix at
`/<slug>.mp3`, with a one-line playlist at `/<slug>.m3u`; the startup log lists
the addresses. A station's AAC and Opus streams, when its `station.json` stream settings set
`aac_bitrate` or `opus_bitrate` (or Settings does), are at `/<slug>.aac` and
`/<slug>.opus`, each with its own encoders. These are public like `/stream.mp3`, and the station's own `listener_user` and
`listener_password` protect them. The default station keeps `/stream.mp3`, `/stream.m3u` and
`/status`, and also answers at `/default.mp3`. A station's stream needs ffmpeg.

Each station also has its own public status page and now-playing feed, for listeners and for
other sites:

- `/stations/<slug>/status`, the status page, with the station's name, song, listeners and links
  to its stream and playlist. It polls the station's own feed, and only a feed on this server.
- `/api/stations/<slug>/nowplaying`, the now-playing feed, in the same form as `/api/nowplaying`
  (see [Now-playing feed and webhooks](now-playing.md#now-playing-feed-and-webhooks)).
- `/stations/<slug>/requests`, the listener request page, when the station's
  station settings turn it on (see [Listener requests](../../guides/operations/listener-requests.md#listener-requests)).
- `/stations/<slug>/player` and `/stations/<slug>/embed`, the player page and embeddable player,
  when the station's settings turn them on (see
  [Player page and embeddable player](../../guides/operations/public-player.md#player-page-and-embeddable-player)).
- Both are public without pairing, like the stream, but only for a configured station: any
  other name needs a paired browser and then answers 404. The station's listener password
  protects only its stream, not these.
- The default station keeps `/status` and `/api/nowplaying`, and also answers as `default`
  (`/stations/default/status`, `/api/stations/default/nowplaying`).
- While a station is stopped, its stream, its feed and its status page all answer 503, "off air".
  The page still shows the station's name and says it is off air right now; an open page notices
  when the station is back on air and shows the song again. The feed answers
  `{"detail": "That station is off air"}`.
- Each station sends its own track-change webhooks, kept in its `secrets.json`, with only
  that station's feed.

Every control route of a station answers under `/api/stations/<slug>/`, for example
`/api/stations/nightowl/status`, `/api/stations/nightowl/skip` or
`/api/stations/nightowl/queue/add`; the default station keeps its `/api/...` addresses. These
need a paired browser, like the default station's.

- `GET /api/stations` lists every configured station: its slug, `defined_in` (`config` or `web`), name, state (`stopped`,
  `stopping`, `stuck stopping`, `idle`, `playing` or `paused`), listener count and current
  song, and its public addresses: `status_path`, `nowplaying_path`, `stream_path`, `m3u_path`,
  `player_path` and `embed_path`. The stream and playlist are `null` for a station that
  does not stream, and the player page and embed are `null` while they are off. For example:

  ```json
  {"slug": "nightowl", "defined_in": "config", "name": "Night Owl Radio", "state": "playing", "listeners": 3,
   "title": "Artist — Song", "status_path": "/stations/nightowl/status",
   "nowplaying_path": "/api/stations/nightowl/nowplaying",
   "stream_path": "/nightowl.mp3", "m3u_path": "/nightowl.m3u",
   "player_path": "/stations/nightowl/player", "embed_path": null}
  ```
- `POST /api/stations` creates a stopped station. Its JSON body requires string `slug` and
  `name`; optional `source` is an existing slug or `null`. Optional Boolean
  `copy_clockwheels`, `copy_schedule`, and `copy_carts` require a source and default to `false`.
  Optional Boolean `autostart` defaults to `false`. The response is 201 with the station entry
  and `copied` and `not_copied` lists. Invalid bodies return 422, an unknown source returns 404,
  a duplicate name or ID, existing state folder or play history, station limit, or conflicting
  recording or relay location returns 409. A server without the shared library returns 503.
  The route requires a paired browser and an allowed Origin, and caps the body at 4 KiB.
- `POST /api/stations/<slug>/start` builds and starts a stopped station. Starting a running
  station changes nothing.
- `POST /api/stations/<slug>/stop` stops a station completely: its listeners are disconnected,
  its encoder, relays, recorder and mastering process are closed and its player ends, so it uses
  no playback resources. Playback commands then answer 409 and its stream answers 503, "off air".
  Saved configuration and content remain available while stopped.
  This starts as soon as the stop is asked for: while the player finishes its work, which can
  take several seconds, the station is listed as `stopping`, its routes already answer 409, a
  start request is refused at once with 409, and a second stop request returns at once. Its
  open pages are told at once. Other stations can be started and stopped meanwhile. If the
  stopping station used Stereo Tool, it keeps it until it has stopped, and another station asking
  for it is told to try again in a few seconds. This applies to the default station too.
- The stop reply is the station's entry, as in `GET /api/stations`, plus `warnings`: a list of
  what went wrong, in plain words, such as "Closing its recorder failed; the server log has the
  details.". It is empty when everything stopped cleanly. The station is stopped either way.
- If a station's player has not finished 30 seconds after a stop, the station is listed as
  `stuck stopping`, and the stop's `warnings` say so. Its player and mastering process keep
  running, because closing the mastering process under a running player is unsafe, so it keeps
  Stereo Tool, and it cannot be started again. The log says every minute that it is still stuck;
  only a restart of AutoDJ frees a player that never ends. When the player ends, the mastering
  process is closed, and closed again every few seconds until it has really ended; only then is
  the station `stopped` and Stereo Tool free. The same holds for a station that failed to start.
- On a normal shutdown, and when Python exits, AutoDJ kills any mastering process still running,
  after making sure none can start again. On Windows each mastering process (Stereo Tool and its
  FFmpeg limiter, or the FFmpeg master) is also tied to AutoDJ's own process with a job object,
  so it ends even when AutoDJ is killed outright or crashes. On Linux and macOS, a hard kill of
  AutoDJ (such as `kill -9`) can leave one running; stop it by hand.

A station that cannot start, with the server or when asked, says why: in the log at startup, and
in the reply to a start request. Whatever it had already started (its player, mastering process,
encoders and relays) is stopped again. The default station and the other stations keep running.

Each station starts with its saved settings (its `web_state.json`), its active clockwheel and its
own play history. The first time a station runs, its liner folder is added to Library folders as
a liner source. Registered liner records are shared: every station can use every registered
liner, with its own play-date rules. A nested folder such as `liners/nightowl` does not make
its registered liners exclusive to Night Owl. This replaces the older folder-isolation
description for automatic liners, clockwheel liner steps, the Liners list and cart clips;
station settings, uploads and saved dates still have their own station paths.

## Logs

A log line from a station starts with its slug, such as `[nightowl] Stream station tick failed`
or `[default] Stream station tick failed`. Station threads carry extra station slugs in their
names (`autodj-player-nightowl`, `autodj-stream-writer-nightowl`); default thread names are
unchanged.


## Related tasks and contracts

- [Measure multi-station resource use](../../guides/operations/station-soak.md)
- [Back up the installation and its stations](../../guides/operations/backup.md)
