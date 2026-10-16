# Streaming and relays

Start a stream, set its sound and listener access, then send it to a hosted server if needed.

## What stream mode does

Without stream mode, the browser plays the music and AutoDJ picks the songs. In stream mode,
the server mixes songs, crossfades, voice liners, time checks, and output processing into an
MP3 stream that network players can open.

Stream mode needs FFmpeg. Windows setup offers to install it, and Docker includes it. On Linux
and macOS, install it yourself (see [Install AutoDJ](install.md)).

## Turn on the stream

Choose one of these:

- Start AutoDJ with `--stream`, for example `./autodj serve --lan --stream` (on Windows,
  `.\autodj.cmd serve --lan --stream`).
- To make it permanent for the default station, add these lines to `config.toml` beside the launcher, then restart
  AutoDJ:

  ```toml
  [station]
  output = "stream"
  ```

- In Docker, put the same lines in the `config.toml` beside `compose.yaml`, then run
  `docker compose up -d` again.

`--lan` lets other devices on your network reach AutoDJ. Without it, AutoDJ answers only on the
computer it runs on, so nobody else can listen. The [Install guide](install.md#use-autodj-from-other-devices)
explains LAN mode and pairing.

Each running named station streams. The default station can use browser playback. See
[Run several stations](several-stations.md).

## Find the stream address

1. Open the web page and go to Settings, Stream.
2. **Stream address** shows the address. Press **Copy address** to copy it.
3. **Download playlist file (.m3u)** saves a small playlist file that holds the same address.
   Some players prefer it.

The default station's usual address ends in `/stream.mp3`, for example
`http://radio.example.com:8080/stream.mp3`. A second station with the ID `nightowl` streams at
`/nightowl.mp3`. The default station also has `/default.mp3`.

An address with `localhost` or `127.0.0.1` works only on the AutoDJ computer. To share the
stream, open the page through a network address and copy it again.

Add the address to your player as a radio station or network stream. In VLC, use Media, Open
Network Stream. Listening never needs pairing.

## Name the station and choose the quality

In Settings, Stream:

- **Station name** is what players show, up to 100 characters. Players pick up a new name the
  next time they connect.
- **Genre**, **Description** and **Website URL** are sent to players and to relays. Press
  **Save station metadata** after changing any of them. Saving reconnects any relays that are
  turned on.
- **Quality** sets the MP3 bitrate: 128, 192, 256 or 320 kbps. The default is 320. Changing it
  makes every listener reconnect for a moment.
- **Keep playing without listeners** keeps the station running when nobody is listening. When it
  is off, the station starts when the first listener connects. It stops 30 seconds after the last
  listener leaves. A relay that is turned on, or broadcast recording, also keeps it playing.

### More formats: AAC and Opus

Open the **More formats** section of Settings, Stream to add a second or third format beside the
MP3 one:

- **AAC quality**: Off, 64, 96, 128, 192 or 256 kbps. AAC sounds better than MP3 at the same
  bitrate and plays on most phones, browsers and smart speakers.
- **Opus quality**: Off, 48, 64, 96, 128 or 192 kbps. Opus sounds good at low bitrates, which
  suits mobile data. Some smart speakers and older players cannot play it, and Opus streams do
  not carry song titles.

Both are off by default. Each format that is on runs its own encoder on the AutoDJ computer. The
section lists the address of each format, ending in `/stream.aac` or `/stream.opus`.

## Decide who can listen

Anyone who can reach the AutoDJ address can listen. These controls limit that:

- **Connections per address (0 for unlimited)**, under Listeners in Settings, Stream, limits
  how many streams one network address may open at once. The default is 0, no limit.
- **Add address ban** blocks one address or a range of addresses (written as a CIDR, such as
  `203.0.113.0/24`). Choose 1 hour, 1 day, 7 days or Until removed, and add a note if you like.
  The **Bans** list shows each ban, and you can remove it there.
- The Listeners list shows who is connected now. Each listener has **Disconnect** and **Ban**
  buttons.
- The total number of listeners, across every format, is limited to 8 by default. Change it with
  `max_listeners` in the `[stream]` section of `config.toml` (from 1 to 100). Above the limit, a
  new listener is turned away.

### Listener password

You can make listeners type a user name and password. This is set in a file, not in the page,
because it is a secret:

1. Open `config.local.toml` beside `config.toml` (create it if it is missing).
2. Add:

   ```toml
   [stream]
   listener_user = "listener"
   listener_password = "a long password you choose"
   ```

3. Restart AutoDJ.

For a named station, put the same two keys in that station's `secrets.json`
instead (see [Run several stations](several-stations.md#where-a-station-keeps-its-files)).
Some players cannot send a password. Over plain HTTP, the password travels unencrypted.

## Relay to Icecast or Shoutcast

A relay pushes your stream to a hosted Icecast or Shoutcast server. The host's server carries
the listeners and can list the station in public directories. While a relay is turned on, the
station keeps playing even when nobody listens to AutoDJ directly.

You need these details from your host: the server type, host name, port, source password, and
for Icecast the mount point.

1. In Settings, Stream, open **Relays** and press **Add relay**.
2. Fill in the form:
   - **Name**: a short name of your own, using letters, digits, `-` and `_` (up to 32).
   - **Server type**: Icecast, Shoutcast 1 or Shoutcast 2.
   - **Host**: the server's name or address, such as `radio.example.com`. Do not add `http://`
     or a port.
   - **Listener port**: the port listeners use. The default is 8000. For Shoutcast, AutoDJ
     connects to the port one above it, as Shoutcast expects.
   - **Source password**: from your host. Saved passwords are never shown again. When you edit a
     relay, leave this blank to keep the saved password.
   - **Mount point** and **Username** (Icecast only): the defaults are `/live` and `source`.
   - **Stream ID** (Shoutcast 2 only): the default is 1.
   - **Bitrate**: **Use station stream quality**, or 128, 192, 256 or 320 kbps.
   - **Public directory listing**: asks the host to list the station publicly.
   - **Enabled**: untick it to keep the relay but not connect.
3. Press **Save relay**. The change takes effect at once.

The page warns: "Relay TLS is unsupported; the source password is sent without encryption."
Use a host and network you trust.

The Relays table shows each relay's status and its last error. The same status also appears
under the stream address. A relay that loses its connection keeps trying again on its own, and
the station keeps playing. A station can have up to 8 relays. Two relays, on any stations, may
not push to the same server and mount.

### Relays in configuration files

You can also describe relays in `config.toml`, with each password in `config.local.toml`, as
`[stream.relays.<name>]` tables. The [Settings reference](../settings-reference.md#relays) lists
every key. Once you save a relay in the page, AutoDJ keeps the whole relay list in the station's
`relays.json` file and stops reading the relay tables in `config.toml`.

## Record the broadcast

Broadcast recording, in Settings, Stream, saves what listeners hear as dated MP3 files.

- **Record broadcast** starts and stops recording. While it records, the station keeps playing
  even without listeners.
- **Start a new file**: **Every clock hour**, or **For each show**. With For each show, a new file
  starts when you press **Split recording now**, when a scheduled recording event runs, or when
  a profile is applied.
- **Days to keep recordings**: older recordings are deleted about once an hour. 0, the default,
  keeps them forever.
- **Minimum free space in MB**: recording pauses when the drive has less free space than this.
  The default is 100. The stream keeps playing.
- Press **Save recording settings** after changing them.

Recordings go in `recordings` by default and are not backed up.

## Share what is playing

AutoDJ has public pages and feeds that need no pairing. Find their links in the **Now-playing
feed** section of Settings, Stream.

- **Now-playing link** is a JSON feed (`/api/nowplaying`) for web pages, overlays and other
  services. It lists the current song, recent songs and the listener count.
- **Status page link** is a plain page for listeners (`/status`), updated every 15 seconds.
- **Show the next song publicly** adds the upcoming song to both. It is off by default, because
  some webcasting licences do not allow announcing songs in advance.

A public request page and a listening page are also available. Both are off by default. The
request page is switched on in the **Listener requests** card in Queue & Search. The listening
page and its embeddable version are switched on in `config.toml`; see `[player_page]` in the
[Settings reference](../settings-reference.md#player-page).

## Related pages

- [Run several stations](several-stations.md)
- [Troubleshooting](troubleshooting.md#streaming)
- [Settings reference: Stream](../settings-reference.md#stream)
