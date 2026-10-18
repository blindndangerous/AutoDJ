# Listen through Sonos, VLC and other stream players

Start the stream, copy an address reachable from the player, configure access when needed, and diagnose connection errors.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Radio stream (Sonos, VLC and other players)

Stream mode serves the live AutoDJ mix as an MP3 radio station that any network audio player can
open, with AutoDJ's own crossfades, EQ, transition effects and voice liners already mixed in.
Speakers hear exactly what the web page would play in server-audio mode; there is only one mix.

Turn it on at startup with `autodj serve --stream`, combined with `--lan` so other devices on
the network can reach it:

```bash
uv run autodj serve --lan --stream
```

Choose **Stream** as the station's output in Settings > Stations. Existing installations
migrate legacy `[stream] enabled` and `AUTODJ_STREAM_ENABLED` settings into the default station
definition at startup; the old TOML key and environment variable are migration inputs only.
For automation, use `AUTODJ_STATION_DEFAULT_STREAM_*` variables for supported stream settings.
In Docker, change the default station's output in Settings and restart with `docker compose restart autodj`
when changing the process-level stream setup.

`uv run autodj doctor` warns, without failing, if stream mode is on but the server only listens
on loopback, since no other device could reach it.

## Finding the stream address

Open the web page and go to Settings, Stream. The "Stream address" field holds the full URL;
"Copy address" copies it. A "Download playlist file (.m3u)" link gives the same address as a
one-line playlist, for players that prefer to open a file rather than type a URL. The listener
count is shown below the address. The address is built from the address the page was opened
with, so a page opened as `localhost` or `127.0.0.1` gives a link only that computer can use (the
page says so); open AutoDJ by the network address in its start-up message before copying a link
for a speaker.

The listening address is `/stream.mp3`, with a playlist at `/stream.m3u`; the AAC and Opus
streams, when on, are at `/stream.aac` and `/stream.opus` (see
[More formats](stream-formats-and-statistics.md#more-formats-aac-and-opus)). Anyone who can reach the server can listen without
pairing. Playback controls and settings still require
pairing when LAN authentication is enabled. Older addresses containing a stream key, such as
`/stream/<secret>.mp3`, no longer work: set any speaker or player saved with one to
`/stream.mp3`. AutoDJ no longer uses the `index/.stream-secret` file those versions made, and you
can delete it.

## Listener access controls

Listening stays public by default. To require HTTP Basic authentication, put these lines in
the station's private `secrets.json` and restart AutoDJ:

```json
{
  "version": 1,
  "stream": {
    "listener_user": "radio",
    "listener_password": "a long private password"
  }
}
```

Give a player `http://user:pass@host/stream.mp3` (or use `https://` when AutoDJ serves HTTPS).
The password protects `/stream.mp3` and `/stream.m3u`; the playlist itself never contains
credentials, so a player opening it must already know them. Some players, possibly Sonos, cannot
send HTTP Basic credentials. The page's **Listen here** button may also fail when a password is
set. HTTP sends the password across the network in plain text; use HTTPS if the network is not
trusted. Settings, Stream shows whether a password is set; edits to this private file require a
restart. For the default station, the file is `<index_dir>/stations/default/secrets.json`.

`max_listeners_per_address = 0` leaves the per-address limit off by default. A positive value
limits one IP address to that many simultaneous connections, in addition to `max_listeners`.
Change the value in Settings, Stream. If clients reach AutoDJ through a proxy or tunnel, list the **direct proxy
peer's** IP address or CIDR in `[server] trusted_proxies`, for example
`trusted_proxies = ["192.168.1.10"]`. Uvicorn then reads `X-Forwarded-For` from right to left
and uses the first untrusted address. It does not use the RFC 7239 `Forwarded` header. Without
this setting, AutoDJ uses the socket peer and ignores forwarded headers. Set trusted proxies
before enabling a per-address limit or bans behind a proxy or Cloudflare Tunnel; otherwise every
listener shares the proxy's address. A trusted proxy's `X-Forwarded-Proto: https` also keeps
playlist URLs on HTTPS. The trusted proxy setting changes client addresses on all routes in every
mode, not only in stream mode, including pairing, the listener request limits and audit logs.
Only trust a proxy that appends the address it saw to `X-Forwarded-For`, or overwrites the header
with it; never one that passes a client's own `X-Forwarded-For` through, which would let any
client choose its address. Without `trusted_proxies`, an existing `FORWARDED_ALLOW_IPS` environment
variable (Uvicorn's own setting) is honoured in every mode; with both, `trusted_proxies` wins and
the startup log says the variable is ignored. For each trusted proxy that is not on this machine,
the startup log warns that it must append or overwrite `X-Forwarded-For`, and that it must
terminate TLS and send `X-Forwarded-Proto: https` before machine tokens are accepted through it.
Restart after changing trusted proxies.

Settings, Stream lists each direct listener's address, player name and connection time. Use
**Disconnect** to end one connection, or **Ban** to block an address or CIDR for one hour, one day,
seven days or until removal. The form accepts an optional note. A ban disconnects matching
listeners immediately; blocked stream and playlist requests return 403. The **Bans** list removes
blocks. Bans survive restarts in the station's `listener_bans.json` and are included in
AutoDJ backups. Relayed listeners remain under the relay host's own controls.

## Adding the station to Sonos

In the Sonos app: Browse, then TuneIn, then My Radio Stations, then Add New Radio Station. Paste
the stream address and give the station a name. Sonos app versions vary, and some do not offer
Add New Radio Station directly. If yours does not:

- Add the station through the TuneIn app or the TuneIn website instead, then find it from Sonos
  under My Radio Stations, or
- Open the downloaded `.m3u` file with a player or file manager that can hand it to Sonos.

## Adding the station to VLC

Media, then Open Network Stream, then paste the stream address (or point VLC at the downloaded
`.m3u` file) and press Play.

## Troubleshooting

- **"Stream mode needs ffmpeg on the PATH"** — install ffmpeg and make sure it is on the PATH
  for the account running AutoDJ, then start again. `autodj doctor` reports this too, and the
  container image already includes ffmpeg. AutoDJ never falls back to browser-only mode
  silently; it refuses to start until ffmpeg is available or `--stream` is dropped.
- **The stream address gives a 403 or the player cannot connect** — the host name in the address
  is not in the allowed list. Start with `--lan` so this machine's own names and addresses are
  allowed automatically, or add the exact name the player uses with `--allowed-host` (and its
  origin with `--allowed-origin`) if it is a name `--lan` cannot detect, such as a router DNS
  entry.
- **A player gets a 503 "Stream listener limit reached"** — `max_listeners` (default 8) is full.
  It counts the listeners of every format (MP3, AAC and Opus) together. Raise the selected
  station's `stream.max_listeners` setting (or `AUTODJ_STATION_<SLUG>_STREAM_MAX_LISTENERS`) if you regularly have more
  simultaneous listeners.
- **The AAC or Opus address answers 404** — that format is off for the station. Turn it on in
  Settings, Stream, More formats. If the log says the Opus stream encoder keeps exiting, the
  ffmpeg on the PATH was built without libopus; install a build with it (the container image
  has one).
- **A relay says "the server rejected the user name or password"** — check the password in
  the station's private `relays.json` or its environment override and, for Icecast, the `user` and `mount`. Many hosts give a separate
  source password; the admin password is not always accepted.
- **A relay says "the mount point may already be in use"** — another source, perhaps an
  earlier AutoDJ that has not let go yet, is connected to that Icecast mount. The relay keeps
  trying and takes over once the mount is free.
- **A relay to Shoutcast cannot connect** — Shoutcast takes sources on the port above its
  listener port. Give the listener port in `port`, and make sure the one above it is open.
- **A player gets a 503 "stream encoder failed"** — ffmpeg crashed repeatedly (five times within
  a minute) and the encoder is cooling down for 60 seconds before it tries again. Check that
  ffmpeg is present and working (`ffmpeg -version`), then try the address again after a minute.


## Related tasks and contracts

- [Server playback controls and idle behavior](../../reference/operations/server-playback.md)
- [Enable stream formats and inspect listener statistics](stream-formats-and-statistics.md)
- [Relay a station to Icecast or Shoutcast](relays.md)
