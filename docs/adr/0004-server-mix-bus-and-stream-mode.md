# 4. Server mix bus for --server-audio and stream mode

Date: 2026-09-27
Status: Accepted. Supersedes part of [ADR 3](0003-browser-driven-playback.md).

## Context

ADR 3 made the browser the default audio host and kept `--server-audio`
as an opt-in. By 0.17.0 `--server-audio` opened a fresh sound-card
connection for every track. That replayed the overlap of each
crossfade, played in mono, left gaps between tracks and never recorded
history.

People also wanted to hear AutoDJ on Sonos speakers, VLC and other
network players. Those players cannot run the page's Web Audio graph.
They can only open an audio address, so the mixing has to happen on
the server.

## Decision

One clock-paced stereo mix bus (`autodj.mixbus.MixBus`) does all
server-side playback. It plays rendered tracks in 20 ms blocks and hands
each block to every attached output. `RenderAhead` renders the next
track (decode, beat-match, transition effect, crossfade overlap) on a
background thread so the bus never waits. EQ, volume and voice liners
are applied on the bus.

- `--server-audio` attaches one long-lived sound-card output
  (`SoundDeviceOutput`).
- `--stream` attaches an MP3 stream output (`StreamOutput`). One ffmpeg
  encoder feeds every HTTP listener, with ICY metadata carrying the
  artist and title. The address carries a listen-only secret instead of
  pairing, because speakers cannot pair. The station starts a set when
  the first listener connects and stops it 30 seconds after the last
  one leaves.

Browser playback from ADR 3 stays the default for `autodj serve`.

## Consequences

- `--server-audio` is a supported mode, not a legacy one. It is stereo,
  gapless, and records history.
- Crossfades, transition effects, EQ and liners exist twice: in the
  browser's Web Audio graph and on the server mix bus. A change to one
  playback effect has to be made, and tested, in both places.
- In the server modes the browser is a remote control. The page's "Test
  liner" and "Listen here" use the server; seeking is refused while
  streaming, since every listener hears the same mix.
- Stream mode needs ffmpeg on the server and more CPU than browser
  playback, because the server decodes, mixes and encodes the whole set.
- The stream address is a second credential beside pairing. It is kept
  in `index/.stream-secret`, and "Make new link" replaces it.

## Alternatives considered

- Keep the per-track sound-card connection for `--server-audio` and add
  a separate path for streaming. Two server playback paths would drift
  apart, and the per-track path already had the gap and replay bugs.
- Stream from the browser. A browser tab cannot serve an address to a
  Sonos speaker, and the stream would stop whenever the tab closed.
