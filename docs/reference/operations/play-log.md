# Play-log fields, exclusions and listener counts

Look up CSV columns, spreadsheet escaping, omitted plays and the meaning and limitations of performance counts.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

The CSV has one row per play, in this column order:

- `start` and `end`: local time with its UTC offset, for example `2026-09-01T14:03:00+02:00`.
- `duration_played`: whole seconds from the start of the play to the start of the next one,
  capped at the song's length. A crossfade's overlap counts toward the incoming song, and a skipped
  song shows how long it played. The song still playing counts its full length.
- `artist` and `title`, as shown when the song played, and `album` from the library.
- `isrc` and `label`, read from the file's tags when you export, so you do not need to re-index
  after tagging. ISRC comes from `ISRC` (FLAC and Ogg), `TSRC` (MP3) or the iTunes `ISRC` atom
  (M4A). It is written as 12 characters, without hyphens or spaces and in capitals. A tag that is
  not a valid ISRC (2 letters, 3 letters or digits, then 7 digits) counts as missing, so the song
  shows up in `--missing`. Label comes from `LABEL`, `ORGANIZATION` or `PUBLISHER` (FLAC and Ogg), `TPUB` or
  `TXXX:LABEL` (MP3), or the iTunes `LABEL` atom (M4A).
- `kind`: `song`, `liner`, `ad` for an aired ad spot, or `live` for a live DJ's set (see "Live DJs and talkover"),
  which has no path and is never in a SoundExchange report. With **Include liners** checked, the
  export also has `matched_intro`, `time_check`, `cart`, `voice_track`, and the announcer breaks
  `dj`, `news`, `weather`, `music_news` and `speech`. Their `duration_played` is their own length.
- `path`: the file's path inside its music or liner folder, with `/` between folders.
- `performances`: how many listeners heard the play; see "Listener counts" below. Empty for plays
  logged before AutoDJ counted listeners.

The file is UTF-8 without a byte order mark, because reporting tools and scripts would read a byte
order mark as part of the first column name. To open it in Excel, use Data, From Text/CSV, and
choose UTF-8 (65001); LibreOffice asks for the character set when it opens the file. A cell that
starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading `'`, so a spreadsheet
shows it as text instead of running it as a formula.

What the play log holds and leaves out:

- By default the log holds the songs, liner files and live sets the mixer starts. A file played
  from a liner folder is marked `liner`. In stream and server audio modes, AutoDJ also logs the
  liners mixed over a handoff, matched intros, time checks, carts, voice tracks and breaks as they
  air. They are in the export only with **Include liners** checked, and never in a SoundExchange
  report, the repeat rules or a song's `duration_played`. History lists them when **Show
  liners** is checked; this browser remembers the choice. Liners that the browser plays in
  browser playback mode are not logged.
- Ad spots that aired are merged from the station's ad play log and exported with `kind` `ad`,
  the advertiser as artist and campaign as album. There is no separate `id` kind for station
  IDs; keep IDs in a liner folder so they are not reported as songs.
- A song cut short when a stream set stops, because the last listener left, is dropped from the
  log, as it is from the History page.

## Listener counts

Each play records its performances: how many listeners heard any part of it, each counted once.
That follows SoundExchange's definition, where a performance is each time any part of a recording
is transmitted to one listener, so one play heard by 100 listeners is 100 performances.

- In stream mode (`serve --stream`), the count is the stream listeners connected when the song
  starts, plus each listener who connects while it plays. A listener who joins partway through
  counts. When one listener leaves and another joins, both count, unlike a count of the most
  listeners connected at once.
- Players drop and reopen the stream, for example on a Wi-Fi blip or when speakers regroup, so
  AutoDJ treats a listener as a client address with a User-Agent (the name the player sends). A
  new connection from a pair already counted for the song is the same listener, not another
  performance, when that pair is still connected or its last connection ended at most 10 seconds
  earlier. A pair that comes back after more than 10 seconds counts again.
- The rule can undercount: two players behind one public address that send the same User-Agent,
  such as two speakers of the same model in one home, count once while both are connected. Behind
  a reverse proxy such as a Cloudflare Tunnel, the address is the one the proxy passes on in
  `X-Forwarded-For` only when the direct proxy peer is in `[server] trusted_proxies`. Without
  that setting, every listener has the proxy's address and only the User-Agent tells them apart.
- Addresses and User-Agents are kept in memory only, for the song that is playing and the 10
  seconds after a disconnect. The play log stores only the count.
- While a relay to Icecast or Shoutcast is enabled (see "Relaying to Icecast and Shoutcast"),
  plays get no count. AutoDJ cannot see the relay's listeners, and a count of only its own
  listeners would understate the performances. Those plays show up in `--missing`, and their
  SoundExchange lines leave ACTUAL_TOTAL_PERFORMANCES empty; take the listener figures from the
  relay server's statistics instead.
- In every other mode the count is 1: the person at the computer or in the browser.
- Plays logged before AutoDJ counted listeners have no count. They are listed by `--missing`.
