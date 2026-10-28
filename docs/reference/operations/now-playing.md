# Now-playing feed, status page and webhook contracts

Look up public payload fields, status-page behavior, private webhook configuration and delivery limits.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Now-playing feed and webhooks

`GET /api/nowplaying` is public and needs no paired browser. It accepts GET and HEAD, sends
`Cache-Control: no-cache` and `Access-Control-Allow-Origin: *`, and reports the last cached
track or listener change. The response schema is version 2:

```json
{
  "schema": 2,
  "station_name": "AutoDJ",
  "current_track": {"artist": "Artist", "title": "Song", "album": "Album", "duration": 180.0, "started_at": "2026-10-11T12:00:00+00:00", "elapsed": 12.0},
  "next_track": {"artist": "Artist 2", "title": "Next song", "album": "Album 2", "duration": 200.0},
  "history": [{"artist": "Artist 3", "title": "Earlier song", "duration": 190.0, "played_at": "2026-10-11T11:56:00+00:00"}],
  "listener_count": 2,
  "peak_listeners_today": 5,
  "uptime_seconds": 7260,
  "stream_path": "/stream.mp3"
}
```

Version 2 added `peak_listeners_today` and `uptime_seconds`. Clients that read version 1 keep
working, since no field was removed or renamed.

- `station_name` is the name on the air: the **Station name while active** of the show on the
  air or of the clockwheel picking the songs, else the station's **Station name**.
- `listener_count` is the number of players connected to this server's stream now (0 outside
  stream mode). Listeners of a relay to another server are not counted.
- `peak_listeners_today` is the most listeners connected at once since midnight in the station
  time zone (`station.timezone in the station definition`, the one the scheduler uses; the computer's time zone
  when it is not set). It is kept in memory only, so it starts again from the current count when
  AutoDJ restarts.
- `uptime_seconds` counts whole seconds since this station was last started, including when the default station is stopped
  and started again without restarting the server.

Each station has its own feed, with its own name, songs, listeners and stream path:
`/api/nowplaying` for the default station, `/api/stations/<slug>/nowplaying` for any station
(the default one is `default`). See [Several stations](../../guides/operations/stations.md#several-stations).

`current_track` and `next_track` are `null` when unavailable. History is newest first and
lists songs, liner files played as tracks and live DJ sets; it never lists the liners, ads and
other items History shows with **Show liners**, or a show's speech segments.
The feed never contains local file paths, internal IDs, credentials or pairing state.
`next_track` is `null` unless `[nowplaying] show_next = true` or **Show the next song
publicly** is ticked in Settings, Stream. It is off by default because some webcasting
licences, such as the US statutory licence, forbid announcing specific songs in advance, and
it is always `null` in [DMCA mode](../../explanation/operations/dmca.md#dmca-mode).
`history_limit` defaults to 5 and accepts 0 through 20.

Put webhook URLs in the station's private `secrets.json` only. Each webhook accepts an HTTP or
HTTPS URL, `format` of `generic` or `discord`, and optional `events` containing `track_change`.
Up to ten destinations are accepted:

```json
{
  "version": 1,
  "webhooks": [
    {"url": "https://example.com/secret-endpoint", "format": "generic"},
    {"url": "https://discord.com/api/webhooks/your-secret", "format": "discord"}
  ]
}
```

The generic POST body is the feed object above plus `"event": "track_change"`.
The Discord preset sends one embed with the station, artist, title and album.
Delivery runs outside playback, on one background thread per destination, with a
five-second request timeout. A track change is tried three times, waiting about 1 and then
4 seconds, or the destination's `Retry-After` up to 30 seconds; a newer track change replaces
one still waiting. A test send is tried once. The paired `GET /api/webhooks` route shows only the destination host, format
and last result; `POST /api/webhooks/{index}/test` sends the current snapshot.
Webhook URLs are never returned to the browser. The private station secret files are excluded
from AutoDJ backups; preserve them separately if you need to restore secrets.

## Status page

`GET /status` is a public page for listeners, in the spirit of an Icecast status page: the
station name, the song playing, the next song when **Show the next song publicly** is on, the
number of listeners now, today's peak and how long the station has been on air (**Uptime**, which
says "Not on air" while the station is stopped). Every station
has one at `/stations/<slug>/status`; `/status` is the default station's. It needs
no pairing and does not load the paired app. The server writes every value into the page as
plain text, so it works without JavaScript; with JavaScript, `/modules/status-page.js` polls
the station's own feed every 15 seconds (skipping polls while the tab is hidden) and updates the
text. Only the now-playing line is a live region: screen readers hear it, politely, when the
song changes, and nothing else on a poll. A **Pause updates** toggle stops the polling until
pressed again. In stream mode the page also links to the stream and
its playlist file. It sends a Content-Security-Policy that allows only its own scripts,
stylesheet and feed requests. It uses the system's light or dark colours, or the Light or Dark
theme chosen under Settings, Display and keys, Theme in a browser that opened the web page at the
same address (`/modules/theme-init.js` reads that choice before the page is drawn).

## Settings

In stream mode, **Settings, Stream** has a **Now-playing feed** card, closed until you open it:

- **Now-playing link** shows this page's address for the feed, with **Copy now-playing link**.
  Opened as `localhost`, the page notes that the link only works on that computer.
- **Status page link** shows the address of `/status`, with **Copy status page link** and an
  **Open the status page (new tab)** link.
- **Show the next song publicly** turns `show_next` on or off and is saved like the other
  station settings. In
  [DMCA mode](../../explanation/operations/dmca.md#dmca-mode) it is unavailable, and says why.
- **Track-change webhooks** lists each destination by number, host, format and last delivery
  result (Not sent, Delivered or Delivery failed). Its **Send test** button sends the current
  snapshot once and says whether it was delivered. With no webhooks configured, the group says so
  and points to that station's private `secrets.json`; webhooks are added or changed there and
  take effect on restart.


## Related tasks and contracts

- [Enable a public player or embedded player](../../guides/operations/public-player.md)
- [How DMCA mode checks programming](../../explanation/operations/dmca.md)
