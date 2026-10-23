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

## Amendment, 2026-10-22: progressive live mixing and queue edits

[Issue #519](https://github.com/blindndangerous/AutoDJ/issues/519) adds live
stream mixing alongside the existing offline/local `RenderAhead` policy. Each
live decoder progressively fills a backpressured cushion capped at two seconds.
The mix bus layers live sources on its sample clock; decoder calibration,
metadata work and bounded transition-effect windows stay with background workers.
This is a buffering and ownership bound, not a zero-gap or hardware timing
guarantee.

Under the #519 model, queue-edit preflight projected the current editable
handoff, the render-ahead ready handoff, and any affected pinned slot plan.
One revision-bound choice covers the voice contexts affected by that queue
replan. If an edit replaces
music named by the live handoff, the bus holds its boundary until the replacement
is ready; it does not fall back to the displaced incoming song. If a voice or
liner chain has already started, its original timing and layers remain in place
while the unheard incoming music is replaced at the original boundary, so speech
does not repeat. If a needed source is unavailable, the bus emits silence while
holding programme position. These live-path rules do not alter the established
offline/local soft-refresh fallback, and they do not guarantee zero gaps. Future
points in Hour Preview remain predictions, not booked or committed playout.

Browser voice tracking targets the currently airing song and its selected
successor. Its booking window closes at the current song's outro, or earlier
if existing speech or a tail begins before it. The cutoff is calculated after
decoding and checked again atomically when the take is
installed; booking after the outro is not supported. In the #519 model, an
installed voice track could be cancelled while its current handoff's affected
speech and ducking remained unheard. Cancellation prepared the remaining
layers off the bus and rechecked the active handoff, queue revision and safe
boundary before replacing its unheard tail.

## Amendment, 2026-10-22: stream playout and speech admission

Issues [#521](https://github.com/blindndangerous/AutoDJ/issues/521),
[#522](https://github.com/blindndangerous/AutoDJ/issues/522) and
[#523](https://github.com/blindndangerous/AutoDJ/issues/523) revise the live
worker and speech lifecycle described above.

Streaming playout uses the independent `LivePlayout` controller and its
progressive sources; `RenderAhead` remains the local path. Hour Preview is an
estimate, and whole-show preview/export remains separate from either live
worker. Streaming no longer creates future pinned `SlotPlan`s: queue preflight
and cancellation target the current or ready `LivePlayout` handoff. Local
playback retains its future slot planner. Stream announcer uploads and scheduled
speech use the currently airing song change. Decode and preparation happen
before final admission, which atomically rechecks the handoff, revision, outro cutoff and applicable hard
schedule boundary. The scheduler keeps its independent due/expiry tick and
retries a batch refused by a changed handoff. Cancellation may remove speech
from an unheard ready occurrence, extending the prior current-handoff-only
cancellation rule, while preserving other speech and approved assets. These rules
do not claim hard real-time or hardware timing guarantees.

Implementation: [live playout controller](../../src/autodj/live_playout.py),
[stream speech admission and cancellation](../../src/autodj/live_voice.py),
[announcer API](../../src/autodj/breaks_api.py), [scheduled ads](../../src/autodj/ads.py),
[live audio](../../src/autodj/live_audio.py),
[stream decoder](../../src/autodj/stream_decode.py),
[live transitions](../../src/autodj/live_transitions.py), and
[live queue planning](../../src/autodj/live_queue.py).

Current boundaries: server audio is stereo float32 at 44.1 kHz; expensive decode,
beat matching, and track preparation happen outside the bus callback. The
local render-ahead soft-refresh path keeps a usable fallback while replacing
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
| `ADR-0004-02` | Offline/local `RenderAhead` soft refresh keeps the stale prepared track usable until its replacement is ready. | automated | `tests/unit/test_render_ahead.py::test_stale_ready_track_stays_available_until_replaced` |
| `ADR-0004-03` | A slow stream listener is dropped without preventing other listeners receiving audio. | automated | `tests/unit/test_stream_output.py::test_slow_listener_is_dropped_others_continue` |
| `ADR-0004-04` | Sound-card volume and mute are applied at the device output. | automated | `tests/unit/test_sound_output.py::test_volume_and_mute_are_applied` |
| `ADR-0004-05` | Server-mode live input can air and hand control back to automated playout. | automated | `tests/integration/test_live_source.py::test_a_source_is_decoded_goes_on_the_air_and_hands_back` |
| `ADR-0004-06` | Continuity under actual multi-station load requires measured soak evidence. | manual | Run the documented station soak test on the target hardware; report underruns and limits rather than promising hard real-time behavior. |
| `ADR-0004-07` | Automatic saved-point liners use the shared queued-liner cue timing independently of the legacy talk-up flag. | automated | `tests/unit/test_station_liner_cadence.py::test_automatic_liners_use_saved_cues_without_talk_up` |
| `ADR-0004-08` | Live decoders progressively fill a backpressured two-second PCM cushion. | automated | `tests/unit/test_stream_decode.py::test_progressive_decoder_backpressures_and_preserves_every_frame` |
| `ADR-0004-09` | Live layers follow the bus sample clock; preflight covers current and ready handoffs; edits hold the boundary without repeating started speech or airing displaced music; internal edits defer without an operator choice. | automated | `tests/unit/test_live_mixbus.py::test_pending_live_layer_holds_cursor_and_does_not_consume_ready_companion`; `tests/unit/test_live_mixbus.py::test_held_handoff_never_airs_displaced_incoming_and_atomic_replace_airs_new`; `tests/unit/test_live_queue_replan.py::test_live_edit_preserves_heard_prefix_and_bus_cursor`; `tests/unit/test_live_queue_replan.py::test_after_incoming_is_audible_edit_targets_render_ahead_handoff`; `tests/unit/test_live_queue_replan.py::test_concurrent_queue_revision_does_not_install_stale_replacement`; `tests/unit/test_live_queue_replan.py::test_failed_replacement_leaves_boundary_held`; `tests/unit/test_server_audio_queue.py::test_internal_show_queue_insert_defers_when_it_needs_a_voice_choice`; `tests/unit/test_server_audio_queue.py::test_internal_show_queue_remove_defers_when_it_needs_a_voice_choice`; `tests/unit/test_live_stream_lifecycle.py::test_real_stream_selection_replans_audible_handoff_before_stale_queue_item_airs` |
| `ADR-0004-10` | Live transition effects use bounded windows and worker dispatch; future live queue and show points remain estimates. | automated | `tests/unit/test_live_transitions.py::test_live_transition_mixes_only_bounded_tail`; `tests/unit/test_live_transitions.py::test_player_dispatches_live_non_saved_render_without_decoding_whole_track`; `tests/unit/test_hour_preview.py::test_live_mixing_keeps_future_queue_and_show_as_estimates` |
| `ADR-0004-11` | Streaming speech admission (browser voice, announcer and scheduled batches) uses the current handoff and atomically rechecks pair/revision, outro cutoff and hard schedule boundaries; cancellation may remove unheard current/ready speech while preserving other speech and approved assets. | automated | `tests/unit/test_live_voice.py::test_current_live_voice_books_before_outro_and_keeps_future_slot`; `tests/unit/test_live_voice.py::test_live_voice_render_race_drops_item_and_keeps_old_audio`; `tests/unit/test_live_voice.py::test_live_voice_cancel_removes_unheard_audio_and_keeps_ready_voice`; `tests/unit/test_live_voice.py::test_live_voice_cancel_replaces_exact_ready_occurrence_and_keeps_other_speech`; `tests/unit/test_live_voice.py::test_live_voice_cancel_rejects_ready_promotion_race_without_dropping_source`; `tests/unit/test_breaks_api.py::test_stream_announcer_slot_is_the_current_song_change`; `tests/unit/test_breaks_api.py::test_active_live_chain_keeps_transferred_announcer_break_from_old_slot`; `tests/unit/test_breaks_api.py::test_stream_announcer_upload_joins_current_song_change`; `tests/unit/test_breaks_api.py::test_stream_announcer_can_cancel_unheard_current_audio`; `tests/unit/test_breaks_api.py::test_stream_announcer_rejects_handoff_changed_during_decode`; `tests/unit/test_ads.py::test_schedule_booking_retries_after_atomic_handoff_refusal` |
| `ADR-0004-12` | Streaming queue preparation is owned by the independent live playout controller; local render-ahead and whole-show preview/export retain their separate paths. | automated | `tests/unit/test_live_playout_controller.py::test_live_playout_prepares_one_handoff_and_pop_schedules_the_next`; `tests/unit/test_hour_preview.py::test_live_mixing_keeps_future_queue_and_show_as_estimates` |

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
