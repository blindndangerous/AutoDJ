# Announcer API and uploaded-break lifecycle

Look up station-scoped authentication, context and upload payloads, response codes, states and file cleanup.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## API reference

The routes answer under `/api/announcer/` for the default station and under
`/api/stations/<slug>/announcer/` for any station by name; a token for another station gets 403.
Every reply is JSON and is not cached. In the examples `$TOKEN` holds the token and the server
listens on port 8080.

Songs are described as objects with `id` (the library's id for the song, stable across
restarts), `title`, `artist`, `album`, `year` and `duration_s`.

In stream mode, `slot` describes the currently airing song change and its selected successor;
`closes_in_s` is bounded by the current song's outro and may be earlier if existing speech or a
transition tail starts first. The server decodes/prepares an upload before atomically checking the
same pair, revision and cutoff at booking. Local server output continues to expose its advance
booked pair.

### GET context

What is playing, what is next, and the open slot.

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/api/announcer/context
```

```json
{
  "station": "default",
  "accepting": true,
  "reason": null,
  "now": {"id": "4be1…", "title": "First Song", "artist": "First Artist", "album": "First Album",
          "year": 1999, "duration_s": 212.4, "elapsed_s": 31.0, "remaining_s": 181.4},
  "next": {"id": "9a0c…", "title": "Second Song", "artist": "Second Artist", "…": "…"},
  "slot": {
    "slot_id": "5f2d0c9e8b7a6f5e4d3c2b1a09f8e7d6",
    "ending": {"id": "77aa…", "title": "Third Song", "…": "…"},
    "upcoming": {"id": "c3d4…", "title": "Fourth Song", "…": "…"},
    "closes_in_s": 181.4,
    "airs_in_s": 402.0,
    "upcoming_intro_s": 9.5,
    "pinned": false,
    "breaks_booked": 0
  },
  "history": [{"id": "…", "title": "…", "artist": "…", "album": "…", "played_at": 1760000000.0}],
  "requests": [{"id": "…", "title": "…", "…": "…"}],
  "limits": {"kinds": ["dj", "news", "weather", "music_news"],
             "extensions": [".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"],
             "max_bytes": 41943040, "max_seconds": 300.0, "max_queued": 6, "max_per_slot": 2,
             "default_expires_s": 1800, "max_expires_s": 7200, "dj_placement": "auto"}
}
```

- `slot` is `null` and `accepting` is `false` while no change is open: breaks are off, the station
  does not use saved-point playback, nothing is playing, or the next song is still being rendered.
  `reason` says which in words; ask again a few seconds later.
- `closes_in_s` is the remaining booking window; plan to upload well before then. For stream
  output this is bounded by the current song's outro, possibly earlier if speech or a tail begins
  first. `airs_in_s` is roughly when the song change itself happens.
- `upcoming_intro_s` is the upcoming song's intro in seconds, or `null` without an intro point:
  what a talk-up has to fit.
- `history` is the latest plays, newest first; `requests` is the queue.

### POST breaks

Upload one break for the open slot as `multipart/form-data`:

- `file` (required): the audio, one of the `limits.extensions` types, at most `limits.max_bytes`
  and `limits.max_seconds`.
- `kind` (required): `dj`, `news`, `weather` or `music_news`.
- `slot_id`, `ending_id`, `upcoming_id` (required): the slot and the ids of its two songs, as the
  context gave them.
- `placement`: `auto` (the default, following `dj_placement`), `talk_up` or `standalone`. Only a
  DJ break may ask for `talk_up`.
- `allow_outro`: `true` lets the voice start over the ending song's outro. Default `false`.
- `expires_s`: how long the break may wait, 30 seconds to `limits.max_expires_s`. A break whose
  song change would air later than that is dropped. Default `limits.default_expires_s`.
- `label`: a short description for logs and status, such as "Weather, 3 pm".

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  -F kind=dj -F slot_id=5f2d0c9e8b7a6f5e4d3c2b1a09f8e7d6 \
  -F ending_id=77aa… -F upcoming_id=c3d4… \
  -F label="Link into Fourth Song" -F file=@break.wav \
  http://127.0.0.1:8080/api/announcer/breaks
```

`201` books the break:

```json
{"break_id": "0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a", "kind": "dj", "label": "Link into Fourth Song",
 "state": "queued", "reason": null, "slot_id": "5f2d…", "ending_id": "77aa…",
 "upcoming_id": "c3d4…", "placement": "auto", "mixed_as": null, "allow_outro": false,
 "speech_s": 11.8, "created_at": 1760000000.0, "expires_at": 1760001800.0, "eta": null,
 "aired_at": null, "updated_at": 1760000000.0, "airs_in_s": 401.2}
```

Refusals:

- `409` with `"reason": "slot_closed"` (the slot was rendered or replaced) or `"pair_changed"`
  (the ids do not match the slot's songs), and `"slot"` holding the slot that is open now, or
  `null`. Write the break again for that slot. `409` with `"slot_full"` when the slot already
  holds `max_per_slot` breaks, and a plain `409` while breaks are off.
- `429` when the station already holds `max_queued` waiting breaks, or the token sent more than
  `requests_per_minute` requests; wait for `Retry-After` seconds.
- `413` for a file over `max_upload_mb` (refused from the declared `Content-Length` before the
  body is read), `415` for a file type that is not allowed, `422` for a file AutoDJ cannot decode,
  one whose contents do not match its extension, one over 192 kHz or with more than two channels,
  one longer than `max_seconds`, one with less than half a second of sound, a missing file, an
  unknown field or a field out of range.
- `503` with `Retry-After` while other breaks are being decoded; `507` when the waiting files
  would pass `quota_mb`; `408` when the body takes longer than 60 seconds plus one second per
  256 KiB of its declared length to arrive.
- `401` for a missing, wrong or revoked token, `403` for a token for another station, a request
  from another machine over plain HTTP, or a request with an `Origin` header.

A refused upload leaves no file behind.

### GET breaks and GET breaks/{break_id}

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8080/api/announcer/breaks/0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a
```

`GET breaks` lists the announcer's breaks still waiting, then the finished ones, newest first, as
`{"breaks": [...]}`. Voice tracks booked in the web UI are not listed, and an announcer token
cannot look them up or cancel them (404). A break's `state` is one of:

- `queued`: booked, waiting for its song change to be rendered.
- `rendered`: mixed into the song change; it will air. `mixed_as` says `talk_up` or `standalone`
  and `eta` when it should air.
- `on_air`: its voice is playing.
- `aired`: its speech has finished; `aired_at` is when it started.
- `dropped`: it will not air; `reason` says why, for example "cancelled by the announcer", "the
  song before it changed", "the station jumped to another song", "skipped before it aired" or
  "its file could not be decoded".
- `expired`: it was not on the air in time; `reason` says so.

AutoDJ keeps the last 200 finished breaks. Breaks do not survive a restart: after one, ask for the
context again.

### DELETE breaks/{break_id}

Drops a break that is still `queued`, or removes its speech from an unheard current/ready stream
handoff when the safe boundary remains open; the reply is the dropped break. Once the affected
speech has started, it cannot be taken back: the reply is `409` with its state in `reason`.

```bash
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8080/api/announcer/breaks/0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a
```

## What happens to the files

Uploads go to the station's `announcements` folder (`<index_dir>/stations/<slug>/announcements`)
under a name AutoDJ makes up. That
folder is never part of liner rotation. A break's file is deleted once it has aired, been dropped
or expired, and any file left by a previous run is deleted when the station starts (an upload
still arriving is left alone). Room for an upload (its place in `max_queued` and `max_per_slot`
and its bytes in `quota_mb`) is held from before its body is read until it is booked or refused,
so uploads sent together cannot pass the limits together. Decoding an upload happens away from the
playout, one upload at a time, so a burst of uploads never holds up the audio or the next render.

A break is decoded with limits a library song does not need: the file's header is read first, and
a file over 192 kHz, with more than two channels, or longer than `max_seconds` is refused before
any sample is decoded. FFmpeg is told the format from the extension instead of guessing it, may
open only local files and pipes, and its output (stereo at 44.1 kHz) is read against a byte
budget. The break is decoded again the same way when it is mixed in, and where its speech starts
and ends is found again in the audio that plays, so Smooth or Stereo Tool processing cannot move
the voice off its marks. A break mixed into a render that is thrown away unplayed (the station
jumped, or the render was replaced) is dropped at once; one that has still not started ten
minutes after its expected air time expires.

The log records each break by the first eight characters of its id: when it is booked, mixed in,
aired, dropped or expired, and why.
