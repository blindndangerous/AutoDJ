# Relay a station to Icecast or Shoutcast

Configure the station's private relay destinations and passwords, then check connection state and failure recovery.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Relaying to Icecast and Shoutcast

AutoDJ can push the same station to one or more hosted Icecast or Shoutcast servers, so a
station can use a host's listener capacity and directory listings as well as direct
connections. Relays run in stream mode only. Supported servers:

- Icecast 2.4 or later, which accepts a source over HTTP PUT.
- Shoutcast 1 (DNAS 1.9).
- Shoutcast 2 (DNAS 2), through its Shoutcast 1 source login with a stream ID.

In **Settings > Stream**, open the **Relays** card to add or edit relays, enable or disable them,
and check their connection status. Set the station name, genre, description and website there too; relays
inherit this metadata. Saving metadata reconnects enabled relays, but leaves direct stream
listeners connected. Changing one relay reconnects that relay only.

The complete relay list is stored in the station's `relays.json`, including an empty list if
every relay is removed. Startup migration moves old TOML definitions and passwords there once;
normal loading does not fall back to TOML. A blank password
keeps the saved one only when editing the same relay destination (host, port and type).
Environment passwords take precedence over both files and cannot be edited in the web form.
`relays.json` contains credentials and is deliberately excluded from AutoDJ backups; keep a
separate private copy if needed. Unreadable saved relay settings leave relays off and block web saves until the
file is repaired. Only the account AutoDJ runs as can read the file: it has mode 0600 on
Linux and macOS, and on Windows an access list granting that user alone, with nothing
inherited from the folder.
Station metadata is saved with the other web settings in `web_state.json`.

For initial configuration, describe each relay in the station's `relays.json` file:

```json
{
  "version": 1,
  "relays": [
    {"name": "main", "type": "icecast", "host": "radio.example.com", "port": 8000,
     "mount": "/live", "password": "the source password from your host"},
    {"name": "backup", "type": "shoutcast2", "host": "sc.example.net", "port": 8000,
     "stream_id": 1, "bitrate": 128, "password": "the second source password"}
  ]
}
```

Or set it in the environment as `AUTODJ_STATION_DEFAULT_RELAY_<NAME>_PASSWORD`, with the name
upper-cased and `-` written as `_`, for example `AUTODJ_STATION_DEFAULT_RELAY_MAIN_PASSWORD`.
Names containing spaces use an uppercase SHA-256 variable segment, for example the relay name
`Stream madness` uses `AUTODJ_STATION_DEFAULT_RELAY_NAME_SHA256_16DDB67590CA8DD1B551666647C75FEDC901ECD7A718195DCF3D6D96984604D1_PASSWORD`.
For another station, replace `DEFAULT` with its slug. Legacy
`AUTODJ_STREAM_RELAY_<NAME>_PASSWORD` values are copied into the default station's private
`relays.json` on startup migration only. A relay
without a password stops AutoDJ at startup with an error naming the table. Two relay names that
read the same variable, such as `a-b` and `a_b` or `Main` and `main`, stop AutoDJ at startup
when that variable is set, so one server's password is never sent to another; Settings refuses
such a name then too. While the variable is unset, each relay uses its own saved password.

The keys:

- `type`: `icecast`, `shoutcast1` or `shoutcast2`. Required.
- `host`: the server's host name or IP address, without `http://`, port or path. Required.
- `password`: the source password. Required.
- `port`: the server's listener port, default 8000. Shoutcast sources log in on the port above
  it (8001 for 8000), as Shoutcast expects, so give the port listeners use.
- `mount`: Icecast only, and required there: the mount point, such as `/live`.
- `user`: Icecast only: the source user name, default `source`.
- `stream_id`: Shoutcast 2 only: the stream ID, default 1.
- `bitrate`: 128, 192, 256 or 320. Leave it out to send exactly the stream's own audio, which
  needs no extra encoder and follows the Quality setting. A relay with its own bitrate gets a
  second FFmpeg encoder, shared by every relay with that bitrate.
- `public`: `true` asks the server to list the station in its public directories. Default
  `false`.
- `enabled`: `false` keeps the table but does not connect. Default `true`.

What a relay does:

- **It keeps the station playing.** While any relay is enabled, the station plays as if
  "Keep playing without listeners" were on, even with nobody connected to AutoDJ itself,
  because AutoDJ cannot see the relay's listeners. Pause still pauses everyone.
- **It sends the song title** to the server each time a new song starts, through Icecast's
  `/admin/metadata` or Shoutcast's `/admin.cgi`, timed to the audio like the stream's own
  titles. A failed title update is shown and logged, and the next song tries again.
- **It reconnects on its own.** When the server cannot be reached, refuses the login or drops
  the connection, the relay tries again after 1 second, then 2, 4 and so on, up to once a
  minute. A connection that lasted 30 seconds starts the count again. The audio sent after a
  reconnect is live; nothing is saved up while disconnected. A server too slow to take the
  audio (more than 5 seconds behind) is disconnected and tried again. Changing Quality makes
  the relays that send the stream's own audio log in again at the new bitrate. If the stream's
  encoder restarts at the same bitrate, after a crash say, the relay logs in again after the
  same growing wait as after a failure.
- **It never slows down the stream.** Each relay sends from its own queue and threads, so a
  stalled server cannot hold up the mix or the other listeners.

Settings, Stream, lists each relay with its state, read as one sentence each: "connecting",
"live", "not connected, trying again" with the last error, or "off in the configuration". The
list is plain text and does not interrupt the screen reader when it changes. The log has one
warning when a relay starts failing and a line when it is live again. `autodj doctor` checks
that each server answers on its source port, without sending the password, and shows the state
the running AutoDJ last reported (it writes `relay-status.json` in the index folder whenever a
relay changes state).

The password crosses the network to the server as the protocol requires: in an HTTP Basic
login for Icecast, and as plain text for Shoutcast, including in title updates. AutoDJ does
not use TLS for relays, so use a host you trust on networks you trust. See the threat model.
