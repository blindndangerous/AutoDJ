# Radio stream design

Date: 2026-09-26. Status: approved in conversation, awaiting review of this written spec.

## Goal

Let any network audio player, Sonos first, play the live AutoDJ set from a URL such as
`http://nas:8080/stream/<secret>.mp3`, with AutoDJ's own crossfades, transition effects, EQ and
voice liners, in stereo, and with the current artist and title shown on the player.

Success means:

- The owner adds the URL to Sonos as a custom radio station, presses play, and hears the non-stop
  set in stereo.
- The Sonos app shows "Artist - Title" for the track that is actually playing.
- Skip, queue and settings changes made on the web page reach the stream.
- The same setup works on a native install (Windows, Linux NAS) and in the container.

## Decisions taken with the owner

1. The server does all the mixing in stream mode. The web page is a remote control that can also
   listen to the stream. There are never two independent mixes.
2. Voice liners are mixed into the stream on the server, ducking the music.
3. The stream is MP3. The bitrate is chosen by the user from 128, 192, 256 and 320 kbps; the
   default is 320.
4. When no listener has been connected for 30 seconds the set stops. The next listener to connect
   starts a new set from the start of a track; it never resumes mid-song.
5. The stream is protected by a secret URL that allows listening only.
6. Stream mode is chosen at start-up (`autodj serve --stream` or `[stream] enabled = true`), not
   toggled at runtime. It may be combined with `--server-audio`.
7. EQ shapes the stream for every listener. The page's volume and mute affect only the page's own
   listening. Each speaker keeps its own volume.
8. Architecture: built into AutoDJ, with the mix feeding outputs. No Icecast server. Letting ffmpeg
   do the mixing from a playlist was rejected because it loses transitions, beat matching, EQ
   ducking and liners.

## Architecture

The server-side playback path in `src/autodj/player.py` today mixes each track into one mono
array (`_play_with_crossfade`) and plays it through a new `sounddevice.OutputStream` per track
(`_stream_audio`). That path is split into three units.

### Track renderer

The existing per-track logic: load, ReplayGain, lyrics, crossfade point, incoming load, beat
match, intro skip, outgoing filter sweep, transition effect, overlap mix. It keeps its current
behaviour with two changes:

- It is stereo. Audio loads as an `(n, 2)` float32 array at 44.1 kHz. A mono file is duplicated to
  both channels. Analysis that needs mono (beat grid, DJ meta, BPM) takes `audio.mean(axis=1)`, so
  indexing and analysis results are unchanged.
- It no longer plays audio. It returns the rendered track array to the mix bus, together with the
  track's identity and the sample offset at which the next track's metadata takes effect.

Every function in `src/autodj/transitions.py` accepts stereo. Effects are applied per channel.
Effects that use randomness receive one seed per transition and use it for both channels, so the
channels stay in step. Every effect preserves the input length.

### Mix bus

A new module, `src/autodj/mixbus.py`, owning one long-running thread that plays the set
continuously:

- It pulls rendered tracks from the renderer through a small queue (current track plus the next
  one, rendered ahead).
- It emits fixed blocks of 20 ms (882 frames at 44.1 kHz), paced by `time.monotonic()` with drift
  correction, so it does not depend on a sound card for timing.
- Per block it applies, in order: the 3-band EQ (reusing `make_eq_filters`, `apply_eq` and the EQ
  state logic now in `_stream_audio`), the liner overlay with ducking, then the skip fade.
- Pause emits silent blocks and holds the position.
- Skip applies a 150 ms fade-out and moves to the next track.
- It reports the playback position and the current track to `PlayerState`, as the sound-card path
  does today.
- It hands every block to each registered output. An output that raises is logged and removed; the
  bus keeps running.

The clock is injectable so tests run without real time.

### Outputs

An output has one method, `write(block)`, receiving a `(882, 2)` float32 array, plus `close()`.

- Sound card output (`--server-audio`): one long-lived stereo `sounddevice.OutputStream`, fed from
  a ring buffer of about 200 ms that absorbs drift between the sound card clock and the bus clock
  by padding silence on underrun and dropping the oldest block on overrun. It applies its own volume
  and mute from `PlayerState`. Playback becomes stereo and gapless between tracks.
- Stream output (`--stream`): described below.

## Stream output

A new module, `src/autodj/stream.py`.

- Encoder: one ffmpeg subprocess reading raw `f32le` stereo 44.1 kHz on stdin and writing
  constant-bitrate MP3 at the configured bitrate on stdout. It is started with an argument list,
  never a shell.
- Fan-out: a reader thread reads encoded bytes and appends them to each listener's queue. The same
  encoded bytes go to every listener; audio is encoded once.
- Each listener queue is bounded at about 5 seconds of audio. A listener whose queue overflows is
  disconnected so a slow client never stalls the others.
- A new listener first receives the most recent 2 seconds of encoded audio, then live bytes, so
  playback starts quickly. The burst starts on an MP3 frame boundary.
- ICY metadata: when a client sends `Icy-MetaData: 1`, the response carries `icy-metaint: 16000`,
  `icy-name` (the configured station name) and `icy-br`, and a metadata block
  `StreamTitle='Artist - Title';` is inserted every 16000 bytes. The title changes at the byte
  position where the new track's audio enters the encoder output, not when the track is rendered.
  Quotes and control characters in titles are escaped or removed, and titles are truncated to fit
  the 4080-byte limit of one metadata block. Clients that do not ask get plain MP3.
- The response is `audio/mpeg` with `Cache-Control: no-store`, streamed until the client
  disconnects.
- Changing the bitrate restarts the encoder. Listeners are disconnected and reconnect on their own.
- If ffmpeg exits unexpectedly it is restarted with backoff, at most 5 restarts per minute. Listeners
  are disconnected; the failure is logged and shown in the web status line. After a fifth failure
  within one minute the encoder is left stopped for 60 seconds; during that time the stream URL
  returns 503 with the reason "stream encoder failed", and the next connection after the 60 seconds
  tries again.

## Stream URL, secret and security

- URL: `/stream/<secret>.mp3`, and `/stream/<secret>.m3u`, a one-line playlist containing the MP3
  URL built from the request's host, for players that prefer to open a file.
- Secret: 32 random bytes, URL-safe base64 (43 characters), created on first start in stream mode,
  stored in a file beside the paired-devices database, never in `config.toml`, and never equal to
  the access token.
- The secret allows listening only. The stream routes are exempt from the session cookie check,
  because Sonos cannot pair. Every other check in `SecurityMiddleware` still applies, including the
  Host check, so the host name typed into the player must be in the allowed hosts.
- A wrong secret returns 404, is compared in constant time, and counts toward the existing request
  rate limiter.
- "Make new link" (authenticated, `POST /api/stream/rotate`) replaces the secret, disconnects every
  current listener and makes the old URL return 404.
- `max_listeners` (default 8). A listener beyond it gets 503 with a reason; existing listeners are
  unaffected.
- `THREAT_MODEL.md` gains a stream section: the secret is a bearer credential for listening only;
  anyone with the URL can listen; rotation is the revocation mechanism; the stream carries no
  control surface.

## Set lifecycle

- In stream mode the server starts idle. No set is playing.
- The first listener to connect starts a new set from the start of a track: the first entry of the
  user queue if the queue is not empty, otherwise a new starting track picked the way Shuffle picks.
- While at least one listener is connected, the set plays continuously.
- A listener that disconnects and reconnects within `idle_grace_seconds` (default 30) finds the set
  still playing.
- When no listener has been connected for `idle_grace_seconds`, the set stops. A track cut short
  this way is not recorded as played in the session history. The next listener starts a new set as
  above.
- Pause holds the set regardless of listeners, so a paused set is not stopped by the idle timer.
  Resume continues from the paused position.

## Controls in stream mode

- Skip: immediate on the server with a short fade. Speakers hear it after their buffer, a few
  seconds later.
- Queue, play next, reorder and remove: as today, under the queue lock added in this review.
- Pause: the stream carries silence so speakers stay connected. Resume continues from the same spot.
- Seek: not available. The seek slider is hidden, and the comma and full stop shortcuts announce
  "Seeking is not available while streaming."
- EQ: applied by the mix bus, so every listener hears it.
- Page volume and mute: local to the page's own listening.
- Shuffle, settings and presets: unchanged.
- Liners: the mix bus fires them on the existing triggers (every N tracks, every N minutes, random
  window) and rotation, ducking the music under the liner. The Settings "Test liner" button plays the
  liner into the stream. Liner files are read through the existing pinned liner-root functions.

## Web page in stream mode

The page learns the mode from the state push (`stream_mode: true`).

- Now Playing shows a "Listen here" toggle button (`aria-pressed`, fixed name "Listen here") that
  plays the stream in a plain `<audio>` element. The browser mixing engine (decks, worklets,
  prefetch) is not started in stream mode.
- While idle the page shows "Waiting for a listener. Press Listen here, or start the AutoDJ station
  on a speaker." Pressing Listen here connects, which starts a new set.
- Track, artist, art, Up Next, BPM, key and the queue come from the server as today.
- Announcements: a track change is announced once, in the "Artist — Title, BPM, key" form. Idle,
  set started, set stopped and stream link changed are each announced once, never on a timer. The
  listener count is visible text and is not announced when it changes.
- Hotkeys: all existing shortcuts keep working and honour the keyboard shortcuts on/off setting.
  Space and K toggle Listen here instead of pausing the station; the Pause button pauses for
  everyone.
- Lyrics and progress follow the server position minus a stream delay. With Listen here on, the
  delay is measured from the page's audio element; otherwise it is estimated at 3 seconds. Lyric
  highlighting on a speaker can be a second or two early or late.
- Settings gains a "Stream" section: the stream URL in a read-only field with a Copy button, a link
  to the `.m3u` file, a "Make new link" button with a native confirmation dialog, a bitrate select,
  and the listener count. All controls are real labelled form controls following the existing
  Settings patterns.
- In browser mode (no `--stream`) the page is unchanged; the Stream section and Listen here button
  are not rendered.

## Configuration

A new `[stream]` table:

- `enabled = false`
- `bitrate = 320` (one of 128, 192, 256, 320)
- `idle_grace_seconds = 30`
- `max_listeners = 8`
- `station_name = "AutoDJ"`

Each key gets an environment override following the existing `AUTODJ_*` pattern in
`ENVIRONMENT_OVERLAY`. `--stream/--no-stream` on `serve` overrides `enabled`. The bitrate can also
be changed from the Settings page (`POST /api/stream/settings`) and is persisted in the runtime
state like other playback settings. `config.toml.example` documents every key.

## Deployment

- Native: ffmpeg must be on the PATH. `autodj doctor` checks for ffmpeg and for a LAN allowed host
  when stream mode is enabled.
- Container: the image already contains ffmpeg. `compose.yaml` gains a `stream` profile that runs
  `serve --stream` on the authenticated LAN setup, so speakers can reach it.
- Documentation: `docs/operations.md` explains enabling stream mode, finding the URL, and adding it
  to Sonos (Browse, TuneIn, My Radio Stations, Add New Radio Station, in app versions that offer it,
  with the alternatives for versions that do not), VLC and other players. README gets a short
  section.

## Error handling

- ffmpeg missing when stream mode is requested: `serve` exits with a clear message. It never falls
  back to browser mode silently.
- ffmpeg crash: restart with backoff as above.
- Unreadable track: skipped as today; the stream continues.
- Listener limit reached: 503 with a reason.
- Slow listener: disconnected when its queue passes 5 seconds.
- Shutdown: listener connections are closed cleanly and ffmpeg is terminated and reaped.

## Testing

- Mix bus: unit tests with a fake clock and fake outputs for pacing, pause silence, skip fade, liner
  ducking, EQ, stereo and output failure isolation.
- Transitions: a parametrised test that every effect accepts stereo, preserves length and keeps the
  channels in step for a fixed seed.
- Stream output: tests with a fake encoder for fan-out, slow-listener drop, the listener limit, the
  ICY metadata layout (metaint spacing, block length byte, escaping, truncation), title timing, and
  the start burst on a frame boundary.
- Endpoint: integration tests for a wrong secret (404), a correct one (200, `audio/mpeg`, ICY
  headers when asked), rotation invalidating the old URL, the `.m3u` file, and the Host check.
- Lifecycle: idle at start, first connect starts a set, reconnect within the grace period
  continues, a new set after the grace period, pause holds the set, a cut-short track is not
  recorded as played.
- Real ffmpeg: one end-to-end test encodes 2 seconds and checks the MP3 frame headers; skipped when
  ffmpeg is absent.
- Browser: vitest tests for the Listen here toggle, the idle message, the Stream settings section,
  announce-once behaviour and hotkeys in stream mode.
- Container: `scripts/container_smoke.sh` fetches one second of the stream in stream mode.
- Manual, by the owner: add the station on Sonos, and check the page with NVDA in Firefox and
  Chrome.

## Out of scope

- Internet-facing hosting and TLS-terminating proxies (unchanged policy).
- AAC, Opus, FLAC or multiple simultaneous bitrates.
- Per-listener mixes or per-listener EQ.
- Sonos control through its API (grouping, volume) and Sonos-specific discovery.
- Porting the browser's liner or effect worklets; the server uses its existing numpy effects.
