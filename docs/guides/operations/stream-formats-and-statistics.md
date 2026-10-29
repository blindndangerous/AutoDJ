# Enable stream formats and inspect listener statistics

Choose AAC or Opus alongside MP3, check encoder behavior, and inspect or export the station's listener statistics.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## More formats: AAC and Opus

Beside the MP3 stream, a station can serve the same mix in AAC, in Opus, or both, each at its
own address and bitrate. Turn them on in Settings, Station, Broadcasting, **More formats** (a card closed until you open it), with **AAC
quality** and **Opus quality** (Off, or a bitrate). In `station.json`, these keys are in the
station's `stream` section:

```json
{
  "stream": {
    "aac_bitrate": 96,
    "opus_bitrate": 64
  }
}
```

A format that is on answers at `/stream.aac` or `/stream.opus` for the default station, and at
`/<slug>.aac` or `/<slug>.opus` for any station; a format that is off answers
404. Settings, Station, Broadcasting lists the address of every format that is on, each with its own Copy
button. The addresses are public like `/stream.mp3`, and the station's listener password, bans
and per-address limit apply to them too. The choice made in Settings is saved in
`web_state.json` and wins over the station definition at the next start, as the MP3 quality does.
Changing a format's bitrate makes only that format's listeners reconnect.

- **AAC** (AAC-LC in ADTS frames, `audio/aac`) sounds better than MP3 at the same bitrate and
  plays on most phones, browsers and smart speakers. Players that ask for ICY titles get the
  song titles, as with MP3.
- **Opus** (in Ogg, `audio/ogg`, resampled to 48 kHz) sounds good at low bitrates, which suits
  mobile data. Browsers and players such as VLC play it; some smart speakers and older players
  do not. Song titles are not sent inside an Opus stream; players can read them from the
  now-playing feed.

`max_listeners` counts listeners on every format together: with the default 8, five MP3 and
three Opus listeners fill the station. A listener on any format starts an idle set, as an MP3
listener does. Relays and broadcast recordings stay MP3.

Turning a format on first has ffmpeg encode a moment of silence in it. If this ffmpeg cannot
(for example one built without libopus), the setting is refused with ffmpeg's own words and
nothing changes; when several changes are saved together, none is made unless all can be.

A format that is on runs its own ffmpeg encoder on this computer, fed the same mixed audio, but
only while someone listens to it: its first listener starts the encoder, and it stops once nobody
has listened for a minute. So a format nobody uses costs nothing. (The MP3 encoder runs whenever
the station streams, as before.) The mix never waits for an encoder: each has its own queue of at
most 2 seconds of audio, and one that falls behind loses its oldest audio, with one warning in
the log, while the mix, the MP3 stream and the other format carry on; the song title changes
never wait on an encoder starting or stopping either. A listener who joins a running encoder
starts on a whole frame (or, for Opus, the stream's header and then a whole page) from the last
2 seconds of audio; the first listener, or one who joins before an Opus stream's header is
complete, gets the stream from its start. When an encoder exits, the log gives its last words,
and Settings shows them beside its address while it runs.

What an encoder costs, measured with ffmpeg's `-benchmark` on 120 seconds of audio, as a share
of one CPU core (an Intel Core i7-8809G at 3.1 GHz under Windows 11, ffmpeg 8.1):

- MP3 (the stream that is always on): 2.1 % at 320 kbps, 2.9 % at 128 kbps.
- AAC: 3.5 % at 96 kbps, 4.9 % at 256 kbps.
- Opus: 1.3 % at 64 kbps, 1.9 % at 192 kbps.

Those are for music (two minutes of a DJ demo track). Pink noise under a chord gave 1.1 to
1.5 % for MP3, 2.1 to 2.2 % for AAC and 1.3 to 1.6 % for Opus. The whole AutoDJ process with its
encoders, on an idle station playing silence, went from 1.9 % of one core with MP3 alone to
3.9 % with AAC at 96 kbps and Opus at 64 kbps both running, as when each has a listener.

So each extra format costs about as much as the MP3 stream already does, a few percent of one
core, and only while it has listeners (and a minute after). Several stations each run their own
encoders, so multiply by the stations whose format is being heard.

## Listener statistics

Settings, Station, Broadcasting, **Listener statistics**, a card closed until you open it, shows, for the station
the page controls:

- a summary in words: how many are listening now on each format, today's peak and when it was,
  today's sessions and listening hours, and the same for the chosen period with its peak day
  and longest session;
- a table **Listeners by format**: listening now, the peak, sessions and listening hours of
  MP3, AAC and Opus;
- a table **Listeners by day**, newest first: the peak and when it was, sessions, listening
  hours, the average and the longest session;
- **How long sessions lasted**: how many sessions were under 1 minute, 1 to 5 minutes, 5 to 15
  minutes, 15 minutes to 1 hour, 1 to 3 hours, and 3 hours or more.

**Show** picks the last 7, 30 or 90 days, or every day kept. The view changes only when you open
Settings, press **Refresh statistics** or change Show, so a screen reader reading a table is
never interrupted. **Download statistics (CSV)** saves every day kept as a spreadsheet file,
one row per day, with the same numbers for each format and each session length band.

A session is one connection, from connect to disconnect, and counts on the day it began, in
the station's time zone (`station.timezone in the station definition`). The peak is the most listeners connected
at once. Listeners of a relay's server are not counted here; that server keeps its own figures.

Nothing in the statistics identifies a listener. AutoDJ keeps no address, player name or
connection time for them: only the daily counts and totals above, in the station's
`listener_stats.json`, written about once a minute and when the station stops, and included in
backups. **Days to keep statistics** (`[stream] stats_days`, 90 by default, 0 to 3650) deletes
older days; 0 keeps none and deletes those kept. The connected listeners' addresses and player
names in the Listeners list above are shown only while they are connected, as before.

Routes: `GET /api/stream/stats?days=30` (JSON) and `GET /api/stream/stats/export` (CSV), and
the same under `/api/stations/<slug>/`. Both need a paired browser.


## Related tasks and contracts

- [Listen through Sonos, VLC and other stream players](stream-listening.md)
- [Server playback controls and idle behavior](../../reference/operations/server-playback.md)
