# 4. Server mix bus for --server-audio and stream mode

Date: 2026-09-27
Status: Accepted. Supersedes part of [ADR 3](0003-browser-driven-playback.md).

> Current scope and implementation evidence are in the dated amendment
> at the end of this record. Original sections below are retained as history.

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
background thread so the bus never waits. EQ and voice liners are
applied on the bus. Volume and mute are applied only in the sound-card
output, so stream listeners get the full level.

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
- In the server modes the browser is a remote control. The page's liner
  "Test now" and "Listen here" use the server; seeking is refused while
  streaming, since every listener hears the same mix.
- Stream mode needs ffmpeg on the server and more CPU than browser
  playback, because the server decodes, mixes and encodes the whole set.
- Originally, the stream address was a second credential beside pairing,
  kept in `index/.stream-secret`. Listening now uses public `/stream.mp3`
  and `/stream.m3u` addresses; the secret remains for legacy links.

## Alternatives considered

- Keep the per-track sound-card connection for `--server-audio` and add
  a separate path for streaming. Two server playback paths would drift
  apart, and the per-track path already had the gap and replay bugs.
- Stream from the browser. A browser tab cannot serve an address to a
  Sonos speaker, and the stream would stop whenever the tab closed.

## Update, 2026-10-11: keyed stream links removed

The keyed addresses `/stream/<secret>.mp3` and `/stream/<secret>.m3u`, the
`POST /api/stream/rotate` route and the stream secret are gone. Listening
uses only the public `/stream.mp3` and `/stream.m3u` addresses, so the
stream is no longer a second credential beside pairing. AutoDJ no longer
creates or reads `index/.stream-secret`; an existing file is unused and
can be deleted. `THREAT_MODEL.md` describes what public listening exposes.

## Amendment, 2026-10-16: output, preparation, and lifecycle boundaries

The bus/render-ahead decision remains accepted. The original single MP3 stream
and listener-idle description records the initial implementation; current
formats, continuous/idle policy, and public addresses are defined by
[stream operations](../operations.md#radio-stream-sonos-vlc-and-other-players).
The 2026-10-11 removal of keyed links above remains applicable.

Current boundaries: server audio is stereo float32 at 44.1 kHz; expensive decode,
beat matching, and track preparation happen outside the bus callback. The
existing render-ahead soft-refresh path keeps a usable fallback while replacing
stale prepared audio. This does not promise zero silence after a hard reset or
under arbitrary resource pressure. Stream formats fan out encoded audio to
listeners; slow consumers must not block other listeners. Sound-card volume/mute
is device-local. Mastering, live input/talkover, relays, and recording are
station-owned output-chain components under [ADR 5](0005-a-station-owns-its-playout-chain.md).

Implementation: [mix bus](../../src/autodj/mixbus.py),
[render ahead](../../src/autodj/render_ahead.py), [mastering](../../src/autodj/mastering.py),
[stream output](../../src/autodj/stream.py), [format mounts](../../src/autodj/stream_mounts.py),
[live source](../../src/autodj/live.py), and [recorder](../../src/autodj/recorder.py).
Audio effects still have browser/server paths; changes must consider both.
Scope excludes a new DSP backend or a hard real-time performance guarantee.

Verification criteria: paused/idle bus behavior and track boundaries are stable;
soft refresh preserves fallback; slow listeners are isolated; output formats and
station lifecycle retain ownership. Evidence entrypoints are
[bus tests](../../tests/unit/test_mixbus.py), [render tests](../../tests/unit/test_render_ahead.py),
[stream tests](../../tests/unit/test_stream_output.py),
[format tests](../../tests/unit/test_stream_output_formats.py),
[live tests](../../tests/integration/test_live_source.py),
[recording tests](../../tests/unit/test_recorder.py), and
[station cleanup tests](../../tests/unit/test_station_runtime.py).

See [Proposed ADR 8](0008-bound-background-work-outside-playout.md) for the
reconstructed wider workload boundary. Multi-station load needs the documented
[soak test](../operations.md#soak-test); unit tests are not a hardware timing
certification. Revisit for a changed audio clock, mix topology, or output ownership.
Record actual checks and their limits in the relevant PR.

## Current contracts

IDs are stable and are never recycled. Automated means an existing regression
test is mapped; structural validation does not execute it, prove platform
coverage, or accept this ADR. Manual and gap rows remain explicit obligations
or evidence limits. Narrative above retains rationale and scope.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0004-01` | Paused server playout emits silence while holding track position. | automated | `tests/unit/test_mixbus.py::test_pause_emits_silence_and_holds_position` |
| `ADR-0004-02` | A soft refresh keeps the stale prepared track usable until its replacement is ready. | automated | `tests/unit/test_render_ahead.py::test_stale_ready_track_stays_available_until_replaced` |
| `ADR-0004-03` | A slow stream listener is dropped without preventing other listeners receiving audio. | automated | `tests/unit/test_stream_output.py::test_slow_listener_is_dropped_others_continue` |
| `ADR-0004-04` | Sound-card volume and mute are applied at the device output. | automated | `tests/unit/test_sound_output.py::test_volume_and_mute_are_applied` |
| `ADR-0004-05` | Server-mode live input can air and hand control back to automated playout. | automated | `tests/integration/test_live_source.py::test_a_source_is_decoded_goes_on_the_air_and_hands_back` |
| `ADR-0004-06` | Continuity under actual multi-station load requires measured soak evidence. | manual | Run the documented station soak test on the target hardware; report underruns and limits rather than promising hard real-time behavior. |
| `ADR-0004-07` | Automatic saved-point liners use the shared queued-liner cue timing independently of the legacy talk-up flag. | automated | `tests/unit/test_station_liner_cadence.py::test_automatic_liners_use_saved_cues_without_talk_up` |

## Related proposal — 2026-10-17

Prepared editions reuse the server renderer and local speech placement; initial edition playout supports server audio and streaming. Their source identity, approval and recovery proposal is [ADR 15](0015-prepare-speech-for-specific-show-editions.md). This cross-reference does not accept that proposal or change this record's status.

## Amendment, 2026-10-21: automatic liners share saved-point cue timing

In saved-point server playback, interval-triggered liners use the existing handoff
renderer and saved liner/music cues, as queued liners already do. The legacy
`liners_talk_up` flag no longer gates this path; its settings checkbox is labelled
for live server overlays.
Trigger selection, heard-only reservation commits, rotation and preview isolation
remain unchanged. The flag remains compatible with non-saved-point live scheduling
and time checks; browser playback is unchanged. This consolidates current cue-based
placement without extending beatmixed or browser playback in this change.
