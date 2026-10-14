# 5. A station owns its playout chain; the library is shared

Date: 2026-10-13
Status: Accepted. Builds on [ADR 4](0004-server-mix-bus-and-stream-mode.md).

## Context

Some operators run more than one station: a main station and a second one with its own
format, listeners and stream address. Until now each needed its own `autodj serve`, with its
own copy of the similarity index, its own writers on `library.db` and `dj_meta.db`, its own
pairing and port, and no way to see both stations from one page.

Everything in AutoDJ was "one per process": one player, one mix bus, one stream, one
scheduler, one set of state files in the active index folder.

## Decision

A station is one playout chain. Each station has its own player and play state, mix bus,
mastering process (Stereo Tool or the ffmpeg filter), MP3 encoder and relays, listener-driven
set controller, liner scheduler, recorder, listener access and bans, now-playing feed and
webhooks, scheduler, cart wall, time checks, WebSocket clients, saved settings and play
history. `autodj.stations.StationRuntime` holds them, and `StationRegistry` builds, starts and
stops them.

One process serves every station and shares what is not about one station: the music
library and its catalog, the similarity index, DJ metadata, related artists and tracks,
categories, analysis jobs and workers, pairing, sessions and tokens, the HTTP server, TLS and
the host and origin policy, `stream.trusted_proxies`, `server.station_timezone` and the
uploaded Stereo Tool presets.

- The root sections of `config.toml` are the default station, unchanged. Another station is
  a `[stations.<slug>]` table that starts from the defaults, never from the root sections, so
  it cannot quietly inherit the default station's relays, webhooks, listener password or
  recording folder.
- A station can also use `station.json` written from the web page. A matching `config.toml`
  table wins, and the station keeps its state folder.
- The default station keeps its files where they always were. Another station keeps the same
  file names under `<index_dir>/<name>/stations/<slug>/`. `StationPaths` is the only code that
  knows these locations. Nothing on disk moves and no conversion runs.
- Play history stays in the shared `library.db`, with a `station` column; the default station
  keeps `catalog_entries.last_played` and its `"clock"` setting, other stations use
  `station_plays` and `"clock:<slug>"`.
- Every route that acts on one station answers at `/api/...` for the default station and at
  `/api/stations/<slug>/...` for any station, with one WebSocket per station. Shared routes
  answer once. A test fails until a new route says which kind it is.
- Public listening is per station: `/stations/<slug>.mp3`, `.m3u`, `/stations/<slug>/status`
  and `/api/stations/<slug>/nowplaying`, public only for configured slugs.
- The web page controls one station at a time and switches inside the page, so Listen here and
  browser playback are not cut by a navigation.
- Backups and `autodj doctor` cover every station; restore refuses a station the
  configuration does not define.

## Consequences

- One library, index and DJ metadata cache in memory, whatever the number of stations, and one
  writer on each database.
- Each running station adds its own threads, two to four subprocesses and its own audio
  buffers. "Cost of each running station" in `docs/operations.md` estimates about ten threads
  and 150 to 250 MB; these are estimates, so measure on the machine itself with the soak test.
  An idle station still runs its mix bus, mastering and encoder; stopping it is the real saving.
- Every station shares one Python interpreter lock. A heavy render on one station could make
  another station's bus late; the soak test in `tests/integration/test_multi_station.py`
  measures it.
- Pairing is server-wide: a paired browser controls every station. Announcer tokens are the
  first credential bound to one station.
- Only the default station can play in the browser or on the server's sound card. Other
  stations always stream.
- One Stereo Tool licence covers one running copy, so at first only one station could use
  Stereo Tool at a time. #163 lifted that limit: each station may run its own Stereo Tool, with
  its own licence and executable copy (see "Stereo Tool instances and licences" in
  `docs/operations.md`).

## Alternatives considered

- Two `autodj serve` processes. Each would load the index, two processes would write to
  `library.db` and `dj_meta.db`, the browser would pair twice on two ports, and no page could
  switch between the stations.
- Stations that inherit the root sections. Inheritance would copy the default station's relays
  (pushing a second station to the first one's mount), webhooks, listener password and
  recording folder (whose retention would delete the other station's recordings).
- Moving the default station's files under `stations/default/`. It would need conversion code,
  a migration on every upgrade and a way back, for no gain.
