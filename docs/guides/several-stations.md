# Run several stations

One AutoDJ server can run more than one station from the same music library. Each station has
its own sound, schedule and stream. This guide shows how to add a station, switch between
stations, and look after them.

## What stations share and what they own

Shared page controls are music folders, library jobs, saved cue points, the time format, and creating stations. Categories, clockwheels, shows, and intro rules belong to each station.

Each station has its own:

- settings (transitions, song picking, sound, liners, breaks),
- queue, history and listener requests,
- categories, clockwheels, shows and intro rules,
- schedule, carts, ads and profiles,
- stream, relays, listeners, statistics and broadcast recordings,
- live DJ accounts.

The station AutoDJ starts with is the **default station**. Its configuration lives in
`index/stations/default/station.json`. Old station sections in `config.toml` are moved there
once at startup, then ignored. New named stations use stream output by default. Stream output
needs FFmpeg.

You can add up to 8 stations besides the default one. Each running stream station mixes and
encodes its own audio, so each one adds to the work the computer does.

## Add a station

1. Open Settings, Stations.
2. Under **New station**, choose **Start from**:
   - **AutoDJ defaults** starts with AutoDJ's own settings.
   - A station's name copies that station's saved settings, including changes made in the page.
3. Type a **Name**, such as Night Owl Radio. Listeners see this name. It must differ from every
   other station's name.
4. Type a **Station ID**, such as `nightowl`, or leave it empty to have one made from the name.
   Use lower-case letters, digits and hyphens, up to 32. The ID names the station's addresses
   and its folder, and you cannot change it later.
5. When you start from another station, **Also copy** offers **Clockwheels**, **Schedule** and
   **Carts**. Tick the ones you want. The copies belong to the new station, so changing them
   never changes the original.
6. Tick **Start with the server** if the station should start whenever AutoDJ starts.
7. Press **Create station**.

Creating a station never starts it, and no other station stops. The page then offers
**Switch to the new station**.

A copy never takes these from the original station: relays, the listener password, webhooks,
the recording folder, the queue and history, requests, listeners, bans and statistics, ads,
shows, intros, profiles and liners. Set those up again for the new station.

## Start and stop stations

The **Stations on this server** table in Settings, Stations lists every station with what it is
playing and how many people are listening. For each station:

- **Start with the server** sets whether it starts when AutoDJ starts.
- **Start** or **Stop** starts or stops it now. Confirm **Stop station** to take it off air.

The default station starts with the server. You can stop any station and still select it
to edit its saved settings and content. Its stream is unavailable. Enabled public pages report
that it is off air. Stopping one station leaves the server and other stations running.

## Switch which station the page controls

With two or more stations, the top of the page shows a **Station** button that names the station
this page controls, such as "Station: Night Owl Radio". Press it and choose another station from
the menu. The page heading also names the station.

Music folders, library jobs, saved cue points, time format, and creating stations affect every
station. Categories, clockwheels, shows, and intro rules in **Library tools** belong to the
selected station. Shared controls say "Shared by all stations" for screen readers.

## Set up the new station

Switch to the new station, then work through its settings as you did for the first one:

1. Settings, Stream: give it a station name, quality and any relays. See
   [Streaming and relays](streaming-and-relays.md).
2. Library tools: make its categories and clockwheels. The music folders are already shared.
3. Settings, Song picking: choose its pick mode and repeat rules.
4. Settings, Schedule: add its events.

## Each station's addresses

Replace `nightowl` with the station's ID. The default station uses the shorter form shown second.

- Stream: `/nightowl.mp3` (default station: `/stream.mp3`). AAC and Opus end in `.aac` and
  `.opus` when they are on.
- Playlist file: `/nightowl.m3u` (default: `/stream.m3u`).
- Status page: `/stations/nightowl/status` (default: `/status`).
- Request page: `/stations/nightowl/requests` (default: `/requests`).
- Listening page: `/stations/nightowl/player` (default: `/player`).
- Now-playing feed: `/api/stations/nightowl/nowplaying` (default: `/api/nowplaying`).
- Live DJ mount: `/stations/nightowl/live` (default: `/live`).

When AutoDJ starts, it prints each station's stream address and status page.

## Time zone, time format, and output

In Settings, Stations, **Time for the selected station** holds **Time zone**. It controls that
station's schedule, ad breaks, and clockwheel restarts. A change takes effect when that station
next starts. **Time format for all stations** changes the display between 24 hour and 12 hour
time. Press **Save time settings** after changing either.

**Output for the selected station** holds **Playback output**: Browser, Server sound card, or
Radio stream. **Also play the radio stream on the server sound card** works with Radio stream.
**Maximum liner upload size (MiB)** limits uploads for that station. Output changes take effect
when the station next starts.

## Where a station keeps its files

Each station has a folder inside AutoDJ's data folder, `index/stations/<id>/`. The default
station's folder is `index/stations/default/`. You rarely need to open it. The files that matter
to an admin are:

- `station.json`: the station definition and configuration. Every station has one, including
  `default`. Use the page to change settings, even while a named station is stopped.
- `web_state.json`: the settings you saved in the page.
- `station.db`: that station's play history, last-played times, and rotation state. The shared
  `libraries/main/library.db` holds song metadata and cues, not this station's history.
- `relays.json`: the station's relays, with their passwords. Only the owner of the AutoDJ
  process can read it.
- `secrets.json`: optional, for a station's listener password and webhooks. Only the owner
  should be able to read it.

A `secrets.json` that sets a listener password looks like this:

```json
{
  "version": 1,
  "stream": {
    "listener_user": "listener",
    "listener_password": "a long password you choose"
  }
}
```

Restart AutoDJ after changing the file. The default station uses its own `secrets.json` too; see
[Streaming and relays](streaming-and-relays.md#listener-password).

Stations from older versions that were written as `[stations.<id>]` tables in `config.toml` are
moved into their own `station.json` the first time a newer AutoDJ starts. Relay definitions and
passwords move into `relays.json`; listener credentials and webhooks move into `secrets.json`.
The old tables are then ignored. Remove them after checking the migrated files.

## Remove a station

The page cannot delete a station yet. To retire one, stop it and untick **Start with the
server**. It then stays stopped and uses no resources.

## Stereo Tool licences

Stations can share one Stereo Tool licence key or have their own. With several stations,
Settings, Sound shows **For this station only** beside the licence key. Ticked, the key is saved
for the current station. Unticked, it is for every station that has no key of its own.

## Backups

`autodj backup` saves every station: its settings, schedule, carts, categories, clockwheels,
liners, ads and the rest. It never saves secrets:

- `relays.json` and `secrets.json` are left out, so relay passwords, listener passwords and
  webhooks are not in the backup.
- Live DJ passwords are left out. After a restore, set each DJ's password again.
- Recordings are left out.

Keep a separate, safe copy of those passwords.

`autodj doctor` checks every station. Add `--station nightowl` to check only one.

## Related pages

- [Streaming and relays](streaming-and-relays.md)
- [Settings reference](../settings-reference.md)
- [Troubleshooting](troubleshooting.md)

AutoDJ and this page were developed with AI assistance.
