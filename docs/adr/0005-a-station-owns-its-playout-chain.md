# 5. A station owns its playout chain; the library is shared

Date: 2026-10-13
Status: Accepted; construction and configuration details updated by #366 and #415. Builds on [ADR 4](0004-server-mix-bus-and-stream-mode.md).

The original decision below describes the first multi-station release. Current behavior uses
`stations/<slug>/station.json` for every station, including the default, and migrates legacy TOML
settings at startup. Every station can stop/restart and use browser, sound-card or stream output.
Categories and scheduling timezone are station-owned; trusted proxies remain server-owned.
See [Several stations](../operations.md#several-stations) for the current configuration.

> Current scope and implementation evidence are in the dated amendment
> at the end of this record. Original sections below are retained as history.

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

## Amendment, 2026-10-16: current station and shared ownership

The station-owned playout decision remains accepted. The original Decision and
Consequences sections above describe the first multi-station release. In
particular, their root-TOML precedence, old path layout, shared history/categories,
shared timezone, and default-only output restrictions are historical.

Current constraints:

- Every station, including default, has a definition at
  `stations/<slug>/station.json`; stopped stations retain editable validated
  settings without a live Player. Legacy TOML station configuration is migrated
  at startup, not used as an ongoing competing definition.
- Station state/history uses its own `station.db`, settings, and asset paths.
  Categories and scheduling timezone belong to the station. `StationPaths` and
  the configuration path properties define current locations.
- The shared music catalog, analysis infrastructure, related-track/artist data,
  pairing/token registry, HTTP/TLS security policy, and trusted proxies stay
  outside the station's playout ownership. Shared/library folders are distinct
  from `stations/<slug>/`; do not construct paths from the historical layout.
- Every station can stop/restart and choose supported browser, sound-card, or
  stream output. A stopped station is not simply an unknown station: saved-state
  endpoints can remain available while runtime-only endpoints refuse work.
- Station routes use explicit station resolution; shared routes stay shared.
  Default and named aliases preserve authorization and body limits. Browser
  pairing remains server-wide, not a per-station operator role system.
- Duplication copies only selected allowed assets/settings and omits destination
  credentials. Backup/restore and diagnostics must address every configured station.

Implementation: [runtime and paths](../../src/autodj/stations.py),
[settings](../../src/autodj/station_settings.py), [definitions](../../src/autodj/station_creation.py),
[station store](../../src/autodj/station_store.py), [ledger](../../src/autodj/station_ledger.py),
[routes](../../src/autodj/station_routes.py), and [layout migration](../../src/autodj/layout.py).
See [station operations](../operations.md#several-stations) and
[configuration precedence](../operations.md#configuration-precedence) for procedures.

Verification criteria: default and other station paths use the same ownership
model; settings/history stay isolated; stopped settings are editable; every route
is classified; duplication omits secrets; lifecycle failures retain resource ownership.
Use [config tests](../../tests/unit/test_station_config.py),
[settings tests](../../tests/unit/test_station_settings.py),
[creation tests](../../tests/unit/test_station_creation.py),
[ledger tests](../../tests/unit/test_station_ledger.py),
[route tests](../../tests/integration/test_station_routes.py),
[stopped-state tests](../../tests/integration/test_stopped_station_editing.py), and
[lifecycle tests](../../tests/unit/test_station_runtime.py).

Independent station authorization and extra library owners remain outside this
decision. Revisit if either becomes required. Resource figures in the original
record remain estimates requiring load evidence, not admission guarantees.
The new storage/access records are Proposed, not silently accepted replacements.
Record actual checks and their limits in the relevant PR.

## Amendment, 2026-10-22: programme and listening ownership

[ADR 16](0016-one-programme-with-browser-listening-routes.md) makes the station's
server mix authoritative regardless of its outputs. The runtime owns private
browser-monitor subscriptions and closes them on shutdown. Per-browser listening
routes and levels do not modify its programme or other browsers. Saved Browser
and Server output settings normalize to local playback with the prior server
speaker choice preserved; stream broadcasting remains explicit.

## Current contracts

IDs are stable and are never recycled. Automated means an existing regression
test is mapped; structural validation does not execute it, prove platform
coverage, or accept this ADR. Manual and gap rows remain explicit obligations
or evidence limits. Narrative above retains rationale and scope.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0005-01` | Each runtime receives independent station configuration and default station paths use the same ownership model. | automated | `tests/unit/test_station_runtime.py::test_each_run_gets_a_fresh_copy_of_the_station_config`; `tests/unit/test_station_runtime.py::test_default_paths_use_the_same_station_factory_path` |
| `ADR-0005-02` | Station histories, last-played values, and clock state remain separate after persistence. | automated | `tests/unit/test_station_ledger.py::test_two_ledgers_on_one_catalog_keep_history_last_played_and_clock_apart` |
| `ADR-0005-03` | Stopped settings load/edit/save without a Player and retain running-setter validation. | automated | `tests/unit/test_station_settings.py::test_offline_settings_load_edit_save_without_player`; `tests/unit/test_station_settings.py::test_offline_settings_use_running_setter_validation` |
| `ADR-0005-04` | Every route declares shared/station ownership and named station operations target that station. | automated | `tests/integration/test_station_routes.py::test_every_route_is_classified_as_shared_or_station`; `tests/integration/test_station_routes.py::test_a_named_station_route_acts_on_that_station`; `tests/integration/test_station_routes.py::test_named_station_monitor_sends_pcm_and_detaches_on_disconnect` |
| `ADR-0005-05` | A station whose Player will not stop retains ownership of its mastering process and licence. | automated | `tests/unit/test_station_runtime.py::test_a_station_whose_player_will_not_stop_keeps_its_master_and_licence` |

## Related proposal — 2026-10-17

Prepared edition manifests and immutable source recordings are station-owned through StationPaths. Their source-asset and lifecycle proposal is [ADR 15](0015-prepare-speech-for-specific-show-editions.md). This cross-reference does not accept that proposal or change this record's status.
