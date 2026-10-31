# 4. Server mix bus for --server-audio and stream mode

Date: 2026-09-27
Status: Accepted. Supersedes part of [ADR 3](0003-browser-driven-playback.md).

> Current scope and implementation evidence are in the dated amendment
> at the end of this record. Original sections below are retained as history.

Amendment, 2026-10-30: [ADR 20](0020-local-browser-programme-ownership.md)
designs browser-owned stereo programme input before the existing master and
outputs. The maintainer accepted the opt-in input boundary. Existing automation and
external-live contracts remain unchanged. Phase 2a had no public on-air activation;
Phase 2b connects the opt-in browser owner through the same master and outputs,
with bounded consumption receipts and return to the next unheard song.

The 2026-10-30 amendment also decides #524's future-selection contract under
delegated owner authority. It is implemented; its manual speech and audio
evidence rows remain open.

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

## Amendment, 2026-10-22: select the next stream handoff at promotion (#537)

[Issue #537](https://github.com/blindndangerous/AutoDJ/issues/537) changes
streaming's future-handoff lifecycle while retaining its current A→B handoff.
While A is current, ready B is a cursor-only continuation of the incoming
decoder: it retains the exact source offset, deck, glide and occurrence metadata
needed to continue B, but does not render PCM, select C, open C's source, prepare
B→C, or reserve speech for B. At the bus request after A ends, the worker makes
B current and prepares B→C. Until B→C is ready, the bus emits silence and holds
programme position; it never falls back to stale music. This boundary supports
overlaps, cuts, short tracks, repeated files and incoming glides without moving
selection or transition work into the audio callback. It does not guarantee a
zero-gap boundary; the added selection and preparation latency requires manual
listening and soak evidence on deployment hardware.

Queue edits and keep/discard cancellation project only the current and imminent
transition. Cancellation remains available for unheard speech attached to the
current selected occurrence; pending continuation cursors are not speech
targets. Future approved speech stays owned by its occurrence and is installed
only when that occurrence becomes the selected upcoming change. Local
`RenderAhead` fallback and whole-show preview/export retain their existing
behavior. This amendment supersedes the ready-following-handoff projection and
ready-occurrence cancellation described in the 2026-10-22 #519/#521-#523
amendments above.

Implementation entrypoints remain [live playout](../../src/autodj/live_playout.py),
[live queue planning](../../src/autodj/live_queue.py), and
[stream speech admission](../../src/autodj/live_voice.py).

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

## Amendment, 2026-10-23: one live engine for server outputs

[Issue #538](https://github.com/blindndangerous/AutoDJ/issues/538) moves
local sound-card playback onto the same progressive decoder, live handoff and
speech lifecycle as streaming. Output selection does not choose the mixing
strategy. Local-only playback still creates no stream encoder or listener-driven
set controller; device selection, volume and mute remain at the sound-card output.
All server-mixed playback now requires FFmpeg, including formats that the old
local whole-file decoder could read with soundfile alone.

Local speech and prepared-edition changes use the live admission, occurrence,
cancellation and heard-only accounting rules. The former future local slot
planner is no longer the policy for sound-card playback. Saved recordings and
approval remain under [ADR 15](0015-prepare-speech-for-specific-show-editions.md).
The isolated programme review/export renderer remains offline under
[ADR 14](0014-isolate-programme-review-and-export.md).

This replaces the local/stream distinction in the earlier amendments, not the
browser playback contract. [Issue #537](https://github.com/blindndangerous/AutoDJ/issues/537)
separately changes when the shared live worker prepares the next handoff.
Decoder and effect work stay off the bus callback. Hardware continuity, seeking,
takeover and multi-output operation still need listening and soak evidence.

## Current contracts

IDs are stable and are never recycled. Automated means an existing regression
test is mapped; structural validation does not execute it, prove platform
coverage, or accept this ADR. Manual and gap rows remain explicit obligations
or evidence limits. Narrative above retains rationale and scope.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0004-01` | Paused server playout emits silence while holding track position. | automated | `tests/unit/test_mixbus.py::test_pause_emits_silence_and_holds_position` |
| `ADR-0004-02` | Retired for local sound-card output by #538: server outputs use live playout instead of a stale prepared-audio fallback. The RenderAhead utility's test remains historical implementation coverage. | retired | Replaced by the shared live ownership and editing contracts ADR-0004-09 and ADR-0004-14. |
| `ADR-0004-03` | A slow stream listener is dropped without preventing other listeners receiving audio. | automated | `tests/unit/test_stream_output.py::test_slow_listener_is_dropped_others_continue` |
| `ADR-0004-04` | Sound-card volume and mute are applied at the device output. | automated | `tests/unit/test_sound_output.py::test_volume_and_mute_are_applied` |
| `ADR-0004-05` | Server-mode live input can air and hand control back to automated playout. | automated | `tests/integration/test_live_source.py::test_a_source_is_decoded_goes_on_the_air_and_hands_back` |
| `ADR-0004-06` | Continuity under actual multi-station load requires measured soak evidence. | manual | Run the documented station soak test on the target hardware; report underruns and limits rather than promising hard real-time behavior. |
| `ADR-0004-07` | Automatic saved-point liners use the shared queued-liner cue timing independently of the legacy talk-up flag. | automated | `tests/unit/test_station_liner_cadence.py::test_automatic_liners_use_saved_cues_without_talk_up` |
| `ADR-0004-08` | Live decoders progressively fill a backpressured two-second PCM cushion. | automated | `tests/unit/test_stream_decode.py::test_progressive_decoder_backpressures_and_preserves_every_frame` |
| `ADR-0004-09` | Live layers follow the bus sample clock; current/imminent edits hold the boundary without repeating started speech or airing displaced music; internal edits defer without an operator choice. | automated | `tests/unit/test_live_mixbus.py::test_pending_live_layer_holds_cursor_and_does_not_consume_ready_companion`; `tests/unit/test_live_mixbus.py::test_held_handoff_never_airs_displaced_incoming_and_atomic_replace_airs_new`; `tests/unit/test_live_queue_replan.py::test_live_edit_preserves_heard_prefix_and_bus_cursor`; `tests/unit/test_live_queue_replan.py::test_after_incoming_is_audible_queue_edit_waits_for_continuation_promotion`; `tests/unit/test_live_queue_replan.py::test_ready_only_queue_edit_waits_for_later_selection_and_preserves_current_audio`; `tests/unit/test_live_queue_replan.py::test_concurrent_queue_revision_does_not_install_stale_replacement`; `tests/unit/test_live_queue_replan.py::test_failed_replacement_leaves_boundary_held`; `tests/unit/test_server_audio_queue.py::test_internal_show_queue_insert_defers_when_it_needs_a_voice_choice`; `tests/unit/test_server_audio_queue.py::test_internal_show_queue_remove_defers_when_it_needs_a_voice_choice`; `tests/unit/test_live_stream_lifecycle.py::test_real_stream_selection_replans_audible_handoff_before_stale_queue_item_airs` |
| `ADR-0004-10` | Live transition effects use bounded windows and worker dispatch; future live queue and show points remain estimates. | automated | `tests/unit/test_live_transitions.py::test_live_transition_mixes_only_bounded_tail`; `tests/unit/test_live_transitions.py::test_player_dispatches_live_non_saved_render_without_decoding_whole_track`; `tests/unit/test_hour_preview.py::test_live_mixing_keeps_future_queue_and_show_as_estimates` |
| `ADR-0004-11` | Streaming speech admission uses the selected current/upcoming occurrence and atomically rechecks pair/revision, outro cutoff and hard schedule boundaries; pending cursors are not speech targets and cancellation preserves other speech and approved assets. | automated | `tests/unit/test_live_voice.py::test_current_live_voice_books_before_outro_and_keeps_future_slot`; `tests/unit/test_live_voice.py::test_live_voice_render_race_drops_item_and_keeps_old_audio`; `tests/unit/test_live_voice.py::test_live_voice_cancel_removes_unheard_audio_without_preparing_ready_voice`; `tests/unit/test_live_voice.py::test_live_voice_cancel_replaces_exact_ready_occurrence_and_keeps_other_speech`; `tests/unit/test_breaks_api.py::test_stream_announcer_slot_is_the_current_song_change`; `tests/unit/test_breaks_api.py::test_active_live_chain_keeps_transferred_announcer_break_from_old_slot`; `tests/unit/test_breaks_api.py::test_stream_announcer_upload_joins_current_song_change`; `tests/unit/test_breaks_api.py::test_stream_announcer_can_cancel_unheard_current_audio`; `tests/unit/test_breaks_api.py::test_stream_announcer_rejects_handoff_changed_during_decode`; `tests/unit/test_ads.py::test_schedule_booking_retries_after_atomic_handoff_refusal` |
| `ADR-0004-12` | Both server outputs play the incoming continuation from its existing source while selecting and preparing its successor after promotion; no extra future handoff or whole-song prewarm is required. Whole-show preview/export stays offline. | automated | `tests/unit/test_live_playout_controller.py::test_live_playout_pop_waits_for_explicit_cursor_promotion`; `tests/unit/test_live_stream_lifecycle.py::test_stream_selects_successor_only_when_continuation_becomes_current`; `tests/unit/test_live_continuation.py::test_continuation_stays_audible_while_successor_preparation_is_blocked`; `tests/unit/test_live_playout.py::test_missing_queued_live_successor_is_skipped_and_current_track_plays`; `tests/unit/test_live_playout.py::test_unplayable_live_current_is_skipped_in_favor_of_selected_successor`; `tests/unit/test_live_playout.py::test_clock_pick_reselects_failed_candidate_and_keeps_following_cursor`; `tests/unit/test_live_playout.py::test_exhausted_live_successor_retries_keep_current_and_do_not_reopen_failure`; `tests/unit/test_live_playout.py::test_failed_duplicate_queue_occurrence_skips_reserved_current`; `tests/unit/test_hour_preview.py::test_live_mixing_keeps_future_queue_and_show_as_estimates` |
| `ADR-0004-13` | Unavailable sources and exhausted continuations may still cause silence; late preparation may lose a transition. Zero-gap behavior requires manual listening and soak evidence. | manual | Listen through repeated files, short tracks, cuts, overlaps and incoming glides, then run the documented station soak on target hardware; record boundary latency, silence and underruns without claiming a zero-gap guarantee. |
| `ADR-0004-14` | Server sound-card and stream playback share live playout; local-only output creates no streaming lifecycle, and whole-show preview/export stays offline. | automated | `tests/unit/test_player_stream_mode.py::test_local_soundcard_uses_live_playout_without_stream_lifecycle` |
| `ADR-0004-15` | While any mix bus runs, the process uses the 0.5 ms thread switch interval, restoring the earlier interval after the last bus stops unless it was changed meanwhile; a shorter interval is kept. Continuity under load on target hardware remains manual soak evidence (ADR-0004-06). | automated | `tests/unit/test_mixbus.py::test_run_shortens_the_switch_interval_until_the_last_bus_stops`; `tests/unit/test_mixbus.py::test_run_leaves_an_interval_someone_else_chose` |
| `ADR-0004-16` | A streaming station keeps, in its buffered ledger settings, the clockwheel song picked to follow the playing one. After a restart that song starts the set without a successor only when it was picked within 30 minutes, is still a playable library song, has not played since, the wheel and its restart time are unchanged, no show holds the air, no dayparted category or step has opened or closed since, and the repeat rules and DMCA limits allow it; queued songs keep their precedence, and every refusal is logged. Its successor is picked while it plays and spliced in like a queue edit; when the song ends first, the usual start pick follows. | automated | `tests/unit/test_restored_pick.py::test_a_fresh_clock_pick_starts_the_set_once`; `tests/unit/test_restored_pick.py::test_a_saved_song_that_no_longer_fits_falls_back_to_the_usual_pick`; `tests/unit/test_restored_pick.py::test_only_a_clockwheel_pick_is_kept_for_a_restart`; `tests/unit/test_restored_pick.py::test_a_restored_song_starts_alone_and_its_successor_is_spliced_in`; `tests/unit/test_restored_pick.py::test_a_successor_too_late_for_the_restored_song_hands_over_to_a_fresh_pick`; `tests/unit/test_restored_pick.py::test_the_station_ticks_before_its_first_wait`; `tests/unit/test_restored_pick.py::test_the_restore_check_waits_for_neither_hours_nor_song_versions` |
| `ADR-0004-17` | Stream encoders do not inherit terminal interrupt signals, and a station does not recover an encoder after shutdown begins; newly created outputs retain normal recovery. | automated | `tests/unit/test_stream_output_formats.py::test_ffmpeg_encoder_isolated_from_terminal_interrupt`; `tests/unit/test_stream_output_formats.py::test_shutdown_stops_recovery_on_station_outputs_but_not_new_instances`; `tests/unit/test_stream_output_formats.py::test_shutdown_closes_encoder_created_by_an_inflight_retry`; `tests/unit/test_stream_mounts.py::test_shutdown_does_not_wait_for_an_inflight_mount_factory`; `tests/unit/test_station_runtime.py::test_a_start_cut_off_at_shutdown_still_ends_stopped`; `tests/integration/test_stations.py::test_registry_shutdown_stops_and_closes_every_stream_output` |
| `ADR-0004-18` | A playable continuation's first file deck is sought to the continuation's first read while the outgoing song plays, unless the playing render still reads that deck. | automated | `tests/unit/test_live_continuation_cue.py::test_continuation_deck_is_cued_before_promotion` |
| `ADR-0004-19` | A file deck seek decodes up to 100 ms before its target and drops it, so a sought deck starts on decoded audio rather than decoder-delay silence. | automated | `tests/unit/test_stream_decode.py::test_lead_in_is_decoded_before_the_start_and_dropped`; `tests/unit/test_stream_decode.py::test_real_mp3_seek_with_lead_in_starts_on_audio`; `tests/unit/test_live_audio.py::test_buffered_file_seeks_asynchronously_and_closes_decoders` |
| `ADR-0004-20` | Closing a decoder stops a producer waiting for buffer room, then closes its stdout before stopping FFmpeg, so a writer blocked on a full pipe exits on EPIPE without the SIGTERM wait; pipes close only after their readers leave or a short grace. A stopped mastering pipeline is killed. | automated | `tests/unit/test_stream_decode.py::test_close_ends_a_decoder_blocked_on_a_full_pipe_without_a_kill`; `tests/unit/test_stream_decode.py::test_closing_a_decoder_blocked_on_a_full_pipe_is_quick`; `tests/unit/test_stream_decode.py::test_closing_a_running_decoder_raises_in_no_reader_thread`; `tests/unit/test_mastering.py::test_output_limiter_start_failure_cleans_up_stereotool` |
| `ADR-0004-21` | After a set's first track, every block the bus fills with held silence because the audio due is not ready is counted, and a rate-limited warning (one a minute per bus) names its cause, length and track, and `/api/status` reports the count as `playout.held_blocks`; a hold reaching 10 s is reported once while it lasts, outside that limit. | automated | `tests/unit/test_mixbus.py::test_held_silence_between_tracks_is_counted_and_named`; `tests/unit/test_mixbus.py::test_silence_before_the_first_track_is_not_a_hold`; `tests/unit/test_mixbus.py::test_hold_warnings_are_limited_to_one_a_minute`; `tests/unit/test_mixbus.py::test_a_long_hold_is_reported_while_it_lasts`; `tests/unit/test_mixbus.py::test_a_long_hold_is_reported_even_just_after_another_warning`; `tests/unit/test_mixbus.py::test_a_deck_still_seeking_is_counted_as_held`; `tests/unit/test_mixbus.py::test_master_warmup_after_a_track_started_is_counted_as_held`; `tests/integration/test_bridge.py::test_state_reports_the_mix_bus_held_silence_count` |
| `ADR-0004-22` | Confirming a future target materializes its music prefix in the ordinary station queue, with distinct occurrence identities; viewing or refreshing never confirms or reserves anything. | automated | `tests/unit/test_hour_preview.py::test_future_targets_are_read_only_and_distinguish_repeated_occurrences`; `tests/unit/test_hour_preview.py::test_future_preview_fill_keeps_target_identity_and_transition_metadata`; `tests/unit/test_live_queue_replan.py::test_repeated_occurrence_edit_never_consumes_its_sibling`; `tests/unit/test_live_queue_replan.py::test_confirmation_materializes_prefix_once_without_replacing_owned_entries`; `tests/unit/test_live_queue_replan.py::test_confirmation_revalidates_entire_prefix_before_mutating`; `tests/unit/test_live_queue_replan.py::test_two_confirmation_editors_cannot_install_the_same_prefix_twice`; `tests/unit/test_live_queue_replan.py::test_confirmation_counts_only_new_songs_not_owned_music_or_liners`; `tests/unit/test_live_queue_replan.py::test_confirmation_catalog_reads_precede_bus_guard`; `tests/unit/test_voice_tracks_api.py::test_repeated_play_next_after_consumption_uses_a_new_occurrence`; `tests/unit/test_live_voice.py::test_catalog_snapshot_window_ends_before_refresh_or_promotion`; `tests/unit/test_live_queue_replan.py::test_proposal_revision_change_prunes_cache_before_allocating`; `tests/unit/test_live_queue_replan.py::test_proposal_cache_is_bounded_and_retains_recent_conflict_retries`. |
| `ADR-0004-23` | Explicit confirmation precedes future recording; abandon/discard of a browser take leaves confirmed music but no speech booking. Upload admission rechecks the station run, exact pair and revision and leaves a refused take available for explicit retry. | automated | `tests/unit/test_voice_tracks_api.py::test_future_upload_rechecks_target_after_decode`; `tests/unit/test_voice_tracks_api.py::test_selected_future_slot_carries_exact_occurrence_binding`; `tests/jsmodules/voice-tracker.test.js::distinguishes repeated song occurrences and requires listen-back before retargeting`; `tests/jsmodules/voice-tracker.test.js::abandoning a take leaves confirmed songs and no speech`; `tests/unit/test_live_voice.py::test_future_upload_catalog_reads_precede_bus_guard`. |
| `ADR-0004-24` | Queue replace/remove/reorder/Play next resolves every affected future speech association through one revision-bound keep/discard/cancel decision; inserting between a bound pair is a conflict, and unchanged pairs keep their speech. | automated | `tests/unit/test_live_queue_replan.py::test_future_pair_edits_require_one_fresh_voice_choice`; `tests/unit/test_live_queue_replan.py::test_intact_future_pair_move_needs_no_voice_choice`; `tests/unit/test_live_queue_replan.py::test_removing_both_future_endpoints_disables_keep`; `tests/unit/test_live_queue_replan.py::test_combined_current_and_future_conflict_names_every_take`; `tests/unit/test_live_queue_replan.py::test_merging_frozen_future_chains_requires_discard`; `tests/unit/test_live_voice.py::test_rebind_cap_counts_independent_speech_and_upload_reservations`; `tests/unit/test_shows.py::test_manual_show_preflight_preserves_runner_until_voice_choice`; `tests/unit/test_shows.py::test_manual_immediate_show_still_requires_voice_choice`; `tests/unit/test_live_voice.py::test_queue_conflict_catalog_reads_precede_bus_guard`. |
| `ADR-0004-25` | Future speech stays queued until its exact handoff is selected, then enters the existing live admission and heard-only lifecycle once; cancellation uses #523 boundaries and preserves music and sibling speech. | automated | `tests/unit/test_live_voice.py::test_future_voice_promotes_once_only_for_its_exact_pair`; `tests/unit/test_live_voice.py::test_future_claim_survives_stale_preparation_and_retries_once`; `tests/unit/test_live_voice.py::test_failed_future_claim_keeps_the_current_liner_reservation`; `tests/unit/test_live_voice.py::test_future_upload_promoted_during_decode_obeys_current_cutoff`; `tests/unit/test_live_voice.py::test_future_promotion_refuses_unavailable_frozen_liner`; `tests/unit/test_live_voice.py::test_future_cancel_serializes_with_desk_take_and_removes_installed_audio`; `tests/unit/test_live_voice.py::test_waiting_future_voice_prevents_competing_automatic_choice`; `tests/unit/test_live_voice.py::test_future_promotion_adopts_its_existing_intro_reservation`; `tests/unit/test_live_voice.py::test_failed_live_booking_returns_claimed_siblings`; `tests/unit/test_live_voice.py::test_failed_future_install_restores_cached_plan`; `tests/unit/test_live_voice.py::test_expired_future_intro_measurement_leaves_no_reservation_or_cooldown`; `tests/unit/test_live_voice.py::test_catalog_snapshot_window_ends_before_refresh_or_promotion`. |
| `ADR-0004-26` | Hard interruptions and live takeover may end unheard bookings with reasons; ordinary automatic edits defer conflicts. Stop/restart never restores live speech or a stale target; saved edition assets, approvals and resume rules remain separate. | automated | `tests/integration/test_live_source.py::test_takeover_drops_future_voice_and_return_does_not_replay_it`; `tests/unit/test_live_voice.py::test_future_bindings_end_with_the_station_run`; `tests/unit/test_live_voice.py::test_schedule_change_marks_confirmed_target_at_risk`; `tests/unit/test_live_voice.py::test_future_promotion_rechecks_schedule_at_final_install`; `tests/unit/test_live_voice.py::test_crash_style_recreation_does_not_restore_future_bindings`; `tests/unit/test_scheduler.py::test_hard_event_wait_preserves_priority_without_fading`; `tests/unit/test_shows.py::test_hard_show_releases_future_bindings_before_queue_cleanup`; `tests/unit/test_voice_tracks_api.py::test_future_upload_rejects_lifecycle_changes_during_decode`; `tests/unit/test_voice_tracks_api.py::test_future_context_explains_run_local_recovery`; `tests/jsmodules/voice-tracker.test.js::marks lost bookings unknown after a run change and preserves cancellation focus fallback`; `tests/integration/test_stations.py::test_live_dj_title_reaches_extra_mounts`; `tests/unit/test_live_voice.py::test_paused_future_target_has_unknown_airtime_without_schedule_risk`. |
| `ADR-0004-27` | Eligible targets are bounded near-air adjacent music occurrences with known timing and ownership; selection certainty, estimated airtime and unavailability are independent fields. | automated | `tests/unit/test_hour_preview.py::test_future_target_horizon_and_programme_boundaries`; `tests/unit/test_hour_preview.py::test_ineligible_future_row_remains_inspectable_with_reason`; `tests/unit/test_live_voice.py::test_pause_past_future_expiry_drops_speech_without_replay`; `tests/unit/test_voice_tracks_api.py::test_paused_future_target_retains_selection_with_unknown_airtime`; `tests/unit/test_voice_tracks_api.py::test_future_target_cannot_be_confirmed_or_uploaded_during_takeover_or_stop`; `tests/unit/test_live_queue_replan.py::test_confirmation_refuses_missing_duration_without_type_error`; `tests/unit/test_voice_tracks_api.py::test_confirmation_missing_duration_returns_conflict`; `tests/unit/test_live_voice.py::test_future_booking_refuses_unplayable_liner_without_partial_changes`. |
| `ADR-0004-28` | Target refresh preserves the selected identity and focus; a vanished target never selects another implicitly, and actions have one announcement path. | automated | `tests/jsmodules/voice-tracker.test.js::confirms explicitly with a labelled native select and preserves focus on refresh`; `tests/jsmodules/voice-tracker.test.js::retains an unavailable selected occurrence and take without choosing a replacement`; `tests/jsmodules/voice-tracker.test.js::moves focus to the target selector when a transition control disappears`; `tests/jsmodules/voice-tracker.test.js::announces confirmation once and keeps passive airtime refresh quiet`; `tests/jsmodules/voice-tracker.test.js::describes selection and debounces arrow browsing announcements while refresh stays quiet`; `tests/jsmodules/voice-tracker.test.js::explains every disabled confirmation state and counts only the supplied new songs`. |
| `ADR-0004-29` | Future-target selection, confirmation, recording, conflict resolution and cancellation work with keyboard and screen reader and retain audible handoff continuity on the candidate. | manual | Focused Chromium keyboard/axe fixture audit: `tests/playwright/voice_target_audit.mjs`, both themes. Pending #524: rehearse the five actions, repeated songs, refresh, expiry, promotion and takeover on Windows with NVDA in Chrome and Firefox; record revision, versions, focus, speech and audible joins using docs/accessibility-testing.md. Fixture browser tests and structural validation do not establish real speech or output continuity. |

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

## Amendment, 2026-10-24: keep the GIL from starving the mix bus

The bus runs on its own Python thread and gives up the interpreter lock several
times per 20 ms block: pipe writes, locks shared with the master and encoder
threads, and sleeping. Each time it asks for the lock again, a thread running
Python keeps it for a whole switch interval, 5 ms by default in CPython. On a
low-power NAS, a scheduled clockwheel change, which picked and prepared the next
song twice, made the bus fall up to 0.19 s behind, and busier times left both stations of the process
seconds behind together, skipping hundreds of periods. A measurement on Linux,
pinned to two and to four cores, reproduced it: with three Python threads busy
next to a bus with an FFmpeg master and three MP3 encoders, the bus skipped
between a quarter and nearly all of its periods on two cores, and up to a sixth
on four, with single gaps of up to 13 s. With a 0.5 ms interval no run skipped more
than one period; 1 ms still skipped a few.

While at least one bus runs, the process therefore uses a 0.5 ms switch interval
and restores the earlier one when the last bus stops, unless something else has
changed it meanwhile. Background threads lose a little throughput when they
compete with one another; audio is unchanged. The catch-up rule is unchanged: a
late block is still made up with at most eight back-to-back blocks, and a longer
stall still skips the rest. This reduces how often that happens, without promising
real-time behavior: a long call that holds the lock (a garbage collection over a
large heap, say) or page faults on a swapping host still stop every thread.
A scheduled switch to a clockwheel while the station already picks by clockwheel
now plans the next song once, when the wheel is selected.
Contract ADR-0004-15 records the interval rule; continuity on target hardware
remains the manual soak evidence of ADR-0004-06.

## Amendment, 2026-10-25: start a restarted station with the song it had picked (#584)

After a restart, a station's first pick works out every category and clockwheel
condition over the whole library, and the first song waits for that pick and
then for the pick of its successor. On a large library with many Beets query
categories that kept a station silent for about half a minute.

The clockwheel song chosen to follow the playing one has already passed the
wheel, the hours and the repeat rules. A streaming station now keeps it in its
ledger settings, which are buffered and written on the library's tick, together
with when it was picked and expected to start, the wheel's name, a fingerprint of
its definition, the place in the wheel after it, the wheel's next restart time,
and whether a show held the air. A song from the queue (a request or a show) is
never kept. The first start pick after a restart uses it once, and only when it
was picked within 30 minutes, is still a playable song in the library whose file
exists, has not played since, the wheel and its definition are the same and its
restart time has not passed, no show is on the air or waiting to resume, every
dayparted category and step-hours window is open or closed now as it was at the
song's expected start, and the repeat rules and the DMCA limits allow it. These
checks read no category members, so they do not wait for the work the pick
avoids. A play-now song, the queue and waiting requests still come first, as for
any start pick. One info line says the song was restored or why it was not.

The restored song's first render selects no successor, so its audio starts as
soon as its decoder does. When it becomes current, its handoff counts as stale,
and the live queue refresh of ADR-0004-09 selects the successor while it plays
and splices the A to B transition into its unheard tail. If the song ends before
that is ready, the worker makes the usual start pick and the set goes on; in the
unit test that cost one bus block of silence. The startup scan of ADR-0008-35
waits until the successor is spliced in, not only for the first audio, so the
full pick does not run beside it. This changes only the first
handoff after a restart; ADR-0004-12 still governs every later one. Contract
ADR-0004-16 records this; listening and soak evidence remain those of
ADR-0004-06 and ADR-0004-13.

## Amendment, 2026-10-25: no wait before the restored first song (#584)

In the field the restored song started about 6 seconds after its station did. A stream
station decided on its first set only at its first tick, a second after it started, and the
restore check worked out every song's versions for Count versions as one song before checking
the saved song: about 1.2 seconds for each station on 80,000 synthetic songs, several times
that on a low-power host. The station now ticks once as it starts, and the restore check and the
restored song's first render leave versions out. The song was picked with every version of the
recent songs held back, and only the song it followed has played since, which is protected. The
successor's pick works versions out as before. In the restart benchmark, first audio after the
master started fell from 1.10 to 0.09 to 0.12 seconds, and with Count versions as one song from
2.26 to 0.11 to 0.13 seconds; the worst delay of the paced probe was the same as before. Contract
ADR-0004-16 covers this.

## Related amendment, 2026-10-25: no full collector passes while a bus plays (#584)

The 2026-10-24 amendment left garbage-collector passes over a large heap as a long hold that the
switch interval cannot shorten. While any bus runs, the collector now makes no full pass on its
own, and the library makes it at a quiet time; the thresholds follow the bus lifetime as the switch
interval does. [ADR 8](0008-bound-background-work-outside-playout.md) records this as
ADR-0008-36. This cross-reference does not change ADR-0004-15.

## Amendment, 2026-10-27: play the continuation during handoff preparation (#591)

[Issue #591](https://github.com/blindndangerous/AutoDJ/issues/591) addresses the
post-crossfade silence permitted by #537. The incoming song B now has a playable
continuation over its existing progressive decoder, source offset and incoming
glide. The worker prepares this body without selecting C or opening C's source.
When B becomes current, the ordinary live queue refresh selects C and installs
B→C into B's unheard tail while B continues playing. A queued occurrence is
committed once at promotion; successor selection does not skip a repeated copy
of B that is still in the queue.

This supersedes the cursor-only, silent-until-prepared boundary in the
2026-10-22 #537 amendment. The two-second decoder cushions, bounded effect/glide
windows, worker ownership and prohibition on automatic future B→C preparation
while A is current remain. Neither server output prewarms whole music files.
The existing live queue revision and bus identity checks govern installation.

If preparation misses the incoming-music or speech admission point, B finishes
alone and C starts at its cue, without pretending any of the missed overlap was
heard. Unheard speech/intro reservations are released through the existing
discard path; queued liners remain unheard. If B has already ended, C still
continues from its cue rather than inserting an unrelated start pick. Exhausted
continuations and unavailable decoders can still cause silence. This is not a
hardware timing guarantee, and the reported incident's logs do not measure its
actual pause. ADR-0004-12 maps the automated evidence; ADR-0004-06 and
ADR-0004-13 retain deployment listening and soak requirements.

## Amendment 2026-10-27: encoder recovery ends with server shutdown

[Issue #608](https://github.com/blindndangerous/AutoDJ/issues/608) makes shutdown
intent explicit for each station's primary, alternate-format and relay encoders
before station teardown proceeds. An output marked for shutdown starts no recovery
attempt and closes a replacement whose construction was already in flight. A
station finishing startup during shutdown receives the same signal. Later station
instances retain normal recovery; there is no permanent process-wide stop flag.

FFmpeg stream encoders run outside the terminal's interrupt group, so Ctrl+C goes
through server-owned cleanup instead of independently killing encoders while their
owners still consider them active. Cleanup still closes their pipes and processes.
ADR-0004-17 maps the automated evidence; native terminal shutdown remains a
platform verification obligation.

## Amendment, 2026-10-28: no silence at the continuation's start

Streaming stations played exact silence at every song change in modes other than
saved points: about 0.2 s on a desktop, and seconds on a slow host. The incoming
deck opens at the song's saved start. Those modes mix the overlap from decoded
windows, so nothing moves the deck. The continuation then reads it from the end
of the overlap. At promotion the deck restarted its decoder there, and the bus
held the programme in silence until samples arrived. The worker now seeks that
deck while the outgoing song plays (ADR-0004-18). Saved-point overlaps read the
incoming deck themselves and already leave it at the continuation.

An FFmpeg input seek starts some formats with the decoder's delay as exact zeros
(about 550 frames for MP3). Every deck seek now decodes a short lead-in before its
target and drops it (ADR-0004-19).

On Linux, FFmpeg blocked writing to a full pipe ignores SIGTERM. Closing a
decoder or a mastering pipeline therefore waited out the stop grace before a
kill. Each deck seek, transition window and pruned deck paid that wait. A
decoder now closes its stdout first, so the blocked write fails and FFmpeg exits.
A mastering pipeline is killed (ADR-0004-20).

Held silence is now counted and logged with its cause (ADR-0004-21). This makes
the remaining causes of dead air visible in the station log. It does not change
ADR-0004-13: zero-gap behaviour still needs listening and soak evidence.

## Amendment, 2026-10-30: confirmed future voice targets (#524)

Decision status: Accepted under the project owner's explicit delegation of the
five commitment decisions in the Phase 1 task on 2026-10-30. Implementation is
pending a separate Phase 2 request after review. No maintainer review, runtime
implementation or behavioral verification is claimed by this amendment.

Requirements: [#524](https://github.com/blindndangerous/AutoDJ/issues/524).
Prerequisites: [#519](https://github.com/blindndangerous/AutoDJ/issues/519),
[PR #520](https://github.com/blindndangerous/AutoDJ/pull/520), and
[#521](https://github.com/blindndangerous/AutoDJ/issues/521),
[#522](https://github.com/blindndangerous/AutoDJ/issues/522),
[#523](https://github.com/blindndangerous/AutoDJ/issues/523), merged together in
[PR #525](https://github.com/blindndangerous/AutoDJ/pull/525).
Discovery used repository revision `52c9bc9dfad4130f319b399f5d5aa7924b2d1d94`.
The subsequent #537, #538 and #591 amendments above govern the current engine:
both server outputs use live playout, and a playable continuation selects its
successor only at promotion. Future confirmation must not prepare another
handoff, open future decoders or revive the old slot worker.

This extends the current/imminent scope of ADR-0004-09/11 and ADR-0015-09 to
explicitly confirmed future music and queued browser speech. ADR-0004-10's
future **timing** remains estimated; a confirmed **selection** is distinguishable
from a prediction. Implementation evidence is mapped under ADR-0004-22 through ADR-0004-28;
manual screen-reader and audible continuity evidence remains under ADR-0004-29. The
retrospective [ADR 9](0009-share-picking-rules-with-isolated-previews.md),
[ADR 10](0010-preserve-state-through-atomic-writes-and-recovery.md) and
[ADR 11](0011-verify-accessibility-with-browser-and-speech-evidence.md) remain
Proposed. Station ownership remains under
[ADR 5](0005-a-station-owns-its-playout-chain.md); isolated review and saved
production remain under [ADR 14](0014-isolate-programme-review-and-export.md)
and [ADR 15](0015-prepare-speech-for-specific-show-editions.md).

### 1. Confirm a queue prefix; bind speech to an adjacent pair

**Decision.** Confirm the ordered music prefix from the station's existing
selected successor through the target's incoming song. Materialize missing
predicted music as ordinary queue occurrences; retain existing queued entries,
request/show ownership and queued liners in their positions, and never enqueue
the audible current occurrence again. Preserve the already selected successor
and its live source. Overlapping confirmations reuse occurrences instead of
copying the prefix. Confirmation is an explicit queue selection, not a promise
of airtime or a ban on edits. Only the selected outgoing/incoming occurrence
pair owns the recording, not every earlier join in the prefix.

Every queue placement gets its own opaque run-scoped occurrence ID, even two
placements of the same catalog object. Reorder preserves it; replacement or
re-add creates another. Preserve prepared occurrence IDs and ownership as
separate source metadata. A target identifies station run plus outgoing and
incoming occurrence IDs, never song paths, row numbers or ETA. Forecast-only
references identify rows of an immutable preview snapshot; they become live
occurrences only on explicit confirmation. An expired or changed snapshot is
refused, never guessed from matching song names. Reads may retain bounded
preview data, but cannot assign IDs to live entries, consume RNG/cursors,
reserve speech or mutate the queue.

An edit before an intact pair may move its estimated airtime without changing
its speech binding. Moving the intact pair also preserves the binding. Replacing
or removing either endpoint, reversing them, or inserting music between them
requires the existing voice choice. Independent liners/ads/news between music
occurrences remain transition content; changing them revalidates placement and
speech revision rather than inventing another music occurrence.

**Reason and alternatives.** Pair-only confirmation of a dry-run prediction
does not say how it reaches that position after earlier picks change. Freezing
the prefix in the ordinary queue supplies that order using existing precedence.
Freezing the whole forecast unnecessarily restricts later programming. A new
reservation queue would duplicate selection and editing policy. `SlotPlan.pinned`
is expressly rejected: its old meaning keeps a pair regardless of queue edits,
its cursor lifetime is unsuitable for multiple future targets, and live playout
no longer claims future slot plans. `SlotPlan` may still describe the current
handoff; its flag is not this contract.

Converting automatic forecast songs to queue choices deliberately has ordinary
manual-queue semantics. It does not commit forecast clockwheel instructions,
advance the simulated clock cursor or reserve predicted liners/ads. Existing
queue/request clock metadata remains intact; after the queue, automation resumes
through the existing post-queue policy. The confirmation names how many songs
will become queue choices. This tradeoff avoids a second executable clock plan.

**Owners.** [hour_preview.py](../../src/autodj/hour_preview.py): `Item`,
`read_station`, `Decided.signature`, `HourPreviews._signature/get` and `_item_view`
own snapshot references and certainty/timing presentation.
[_bridge.py](../../src/autodj/_bridge.py): queue insertion, `play_next_entry`,
`queue_reorder` and `_queue_mutation_lock` preserve occurrence ownership.
[live_queue.py](../../src/autodj/live_queue.py): `_same_occurrence`,
`_occurrence_key`, `_revision` and `plan_queue_edit` own pair comparison and
atomic prefix preflight. [player.py](../../src/autodj/player.py):
`_station_queue`, `_peek_user_queue`, `_commit_queue_pick`,
`_discard_failed_queue_pick`, `_queue_pick_is_stale` and `accept_live_queue_edit`
must carry and consume exact occurrences instead of the current path fallbacks.

### 2. Confirm explicitly before recording; book only a completed take

**Decision.** Selecting a future row only inspects it. **Confirm songs** submits
that snapshot and revision, validates the complete prefix, and applies the queue
change atomically. Only then can **Record** target it. Already confirmed pairs
need no second confirmation. The existing current-handoff recording route stays
available with its current Outro/earlier-speech cutoff and controls.

Confirmation reserves no speech, upload capacity, liner or intro. Starting,
stopping, abandoning, denying microphone access or discarding a take leaves the
deliberately confirmed music queued and leaves no server recording. **Book voice
track** uses the existing bounded upload/decode/reservation path. A future
booking adds a `Break` in `queued` state bound to the exact pair. Its stable
target ID can reuse `slot_id`; add explicit occurrence references and a
revision rather than overloading the path-derived `ending_id`/`upcoming_id`.

Reuse `TransitionItem` and the existing Before/After/Replace choices for the
future chain. Inspect them without reservation; booking freezes the chosen
content and order, not its future PCM. At promotion, reserve only eligible
frozen candidates through existing liner/intro owners; never reroll a different
clip under an old choice. Independent due ads/news retain their admission rules.
Revalidate the combined chain and report a refusal when its constraints cannot
be satisfied rather than silently moving or omitting the booked voice.

The server checks run identity, pair adjacency, queue/target revision, relevant
schedule/settings/media inputs, expiry and transition choices before processing,
after decode and at atomic admission. Preparation runs outside queue/audio
locks; installation uses existing control serialization and bus-before-queue
lock order. A changed state returns 409 without a partial queue/speech change.
No upload can recreate a cancelled booking using its old revision. Promotion
during processing either admits to that same current handoff before its cutoff,
or refuses; it cannot fall through to another pair. Unrelated revisions may
conservatively require retry, but never erase an existing confirmed selection.

Keep the browser's Blob/WAV after refusal and clean the rejected server upload.
Retry to the same occurrences requires a fresh explicit Book; choosing different
occurrences, even identical song names, requires selection, listen-back and Book.
Change `samePair` accordingly; matching catalog IDs is insufficient.

**Reason and alternatives.** Confirmation before recording makes the music
stable during a take longer than the preview's 45-second cache lifetime.
Confirmation only on upload allows routine reprediction to invalidate that work.
Automatic confirmation on selection or microphone access would turn browsing or
a failed take into an undisclosed queue edit. Temporary recording leases would
need another expiry/ownership mechanism and would not fit ordinary queue editing.

**Owners.** Add `Player.confirm_voice_target` through the existing queue guard
and an operator-only `POST /voice-tracks/targets/confirm` in
[breaks_api.py](../../src/autodj/breaks_api.py): `install_breaks`, `receive`,
`_VoiceFields` and `_voice_context`. Use the station router's default/named
aliases, browser session/Origin rules and existing rate/body caps; announcer
tokens gain no future-selection authority. [breaks.py](../../src/autodj/breaks.py):
`Break`, `BreakDesk.reserve/add/release` retain file and quota ownership.
[live_voice.py](../../src/autodj/live_voice.py): `book_voice` dispatches exact
future/current admission; `book_live_breaks` remains the current install path.
[voice-tracker.js](../../src/autodj/static/modules/voice-tracker.js): `start`,
`showTake`, `target`, `samePair`, `moved`, `book` and `discard` own browser state.

### 3. End speech and music choices separately through existing lifecycles

**Decision.** Cancel removes only the chosen speech association, including a
future queued booking, and increments the target revision. Confirmed music
remains queued, as required by #523. There is no extra music-lock release API:
ordinary remove/replace/reorder/Clear queue releases or changes those choices.
If speech is still associated, that operation first resolves its conflict.
Expiry or failed final admission also ends speech without undoing music choices.

As the outgoing occurrence becomes current, the worker claims its exact queued
speech and carries its identity into the normal handoff. No future PCM, decoder
or speculative speech reservation is created. Queued cancellation and claim
share control serialization, fixing the race between `desk.cancel` and
`desk.take`. Once installed, reuse `_cancel_window` and `_install_handoff` with
the final #523 checks; speech/ducking already started is protected. Claim,
replacement and discard must not install twice or orphan a rendered take on a
retry. On actual playback, queue consumption and break on-air/aired events end
their respective obligations once; never infer an aired take from an elapsed ETA.

A pause retains choices, while ordinary expiry still runs and airtime becomes
unknown. A playout reset/set end releases live target bindings and drops unaired
speech with its reason (or `cut short` when already on air). Ordinary queued
music survives a set reset where it does today, but an old run token cannot book
speech again. Full station destruction/process restart restores neither these
run-scoped confirmations nor temporary live recordings. Ordinary Show resume,
prepared-edition explicit resume and the validated single restored clock pick
retain their existing, separate rules; none resurrects a live voice binding.

Within a running process, Booked voice tracks retains terminal reasons under
the existing bounded history policy. After restart, report **Live recordings
from the previous run were not restored; prior airtime is unknown**, rather
than asserting cancelled, unheard or aired. A browser holding old rows marks
them no longer booked when the run changes; a fresh browser receives that
run-lifetime limitation too. Existing station logs remain the historical
diagnostic source. This does not add durable transmission receipts, replay
state, a backup member or new source-audio storage. Temporary uploads keep the
desk's cleanup rules; saved prepared-edition recordings are never deleted.

**Reason and alternatives.** Cancelling speech must not silently remove music.
Automatic release on closing the page makes programming depend on browser
lifetime. Persisting a replayable live queue and takes would duplicate prepared
editions and risk re-airing speech after an uncertain crash. A separate release
button would add a second way to edit the same queue.

**Owners.** `live_voice.can_cancel/cancel/_cancel_window`,
`Player._next_rendered_serial/_refresh_live_queue/_discard_render/_settle_breaks`,
`reset_playout/end_set/stop`, and [LivePlayout](../../src/autodj/live_playout.py)
`_run_prepare/reset/stop` own claims, cancellation, stale work and generations.
`BreakDesk.take/drop/drop_unaired/maintain` owns terminal states and cleanup;
[stations.py](../../src/autodj/stations.py) `StationRuntime.start/stop` and
`_voice_context` expose run changes and recovery limits. `renderTracks/apply`
in the voice tracker preserve terminal status and focus.

### 4. Make edits explicit; retain hard-event and live-input priority

**Decision.** Extend `plan_queue_edit` to inspect all booked future pairs as
well as current speech. One conflict response lists affected recordings, old
pairs and proposed replacement pairs, with a revision covering the entire edit.
**Keep voice track** authorizes those replacements for this airing only;
**Discard voice track** removes the affected playout associations;
**Cancel change**, a missing choice or a stale revision changes nothing.
**Play next** then takes effect normally. Started speech keeps its original
layers/timing and heard music cannot be replaced.

Keep uses a deterministic proposed pair: anchor to the surviving outgoing
occurrence and its new next song; if it was removed, use the surviving incoming
occurrence and its new predecessor. Explicit endpoint replacement occupies the
same place for this calculation. Thus A/X/B insertion proposes A-to-X for
speech formerly on A-to-B. Show the resulting pair before accepting Keep.
If neither anchor/replacement supplies an eligible pair, Keep is unavailable
with a reason; Discard and Cancel still allow the edit to be resolved. Do not
guess another occurrence with the same path. Rebind/drop all affected takes and
the queue in one revision, preserving unaffected recordings and independent
ads/news. Multiple bookings may share a pair only within existing desk caps;
merging pairs cannot bypass those caps.

Manual show replacement/stop participates in this preflight as one proposed
queue operation, before changing ShowRunner state; thread the same choice
through its API/UI. Soft scheduled changes and ordinary automatic feeds have
no operator choice: defer on conflict and report the affected recording/required
action. They must not mark the show replaced or spin silently in a removal loop.

Hard scheduled actions (including a hard Show replacement) and live takeover
retain their existing priority. Before applying their queue/reset operation,
invalidate displaced target bindings and mark unaired recordings dropped with
the event/takeover reason. This explicit priority exception must run before
ShowRunner's conflict-sensitive cleanup, so protected speech cannot prevent a
hard event. Takeover ends all waiting live voice bookings as today; return
refreshes through `prepare_live_return`, with no replay. Report speech already
started as cut short when interrupted, not safely cancelled. Report affected
recordings in Booked voice tracks and the station event/log; do not retarget them.

Schedule edits alone re-evaluate eligibility, invalidate in-flight revisions
and flag booked targets now at risk, without silently rebinding or deleting
them. Recheck actual chain fit at promotion and final installation. A conflict
then drops the whole unheard take with its reason; music and scheduled priority
continue. Unknown timing or unavailable media likewise never authorizes a
substitute song-specific recording. Clock restart fitting, daypart/DMCA refusal
and decoder skips cannot leave speech attached to a replacement occurrence.

**Reason and alternatives.** An uneditable prefix would defeat #519's Play next
contract. Silently keeping/discarding on ordinary edits would bypass operator
intent. Blocking hard events behind a dialogue would violate scheduler priority;
retargeting on takeover/return could air stale speech in another programme.
The explicit hard-interruption rule is a narrow extension of reset/takeover
behavior, not permission for every automatic caller to discard recordings.

**Owners.** `live_queue.plan_queue_edit/QueueEdit/_revision`,
`Player.accept_live_queue_edit/_refresh_live_queue/voice_track_crosses_hard_boundary`,
and bridge queue mutators own one edit. [shows.py](../../src/autodj/shows.py)
`ShowRunner.start/start_edition/stop/_clear_blocking/_feed_blocking`,
[shows_api.py](../../src/autodj/shows_api.py),
[scheduler.py](../../src/autodj/scheduler.py) `Scheduler.run/tick`, and
`StationRuntime._live_started/_ready_automation` own manual/soft/hard distinctions.
Reuse [voice-track-conflict.js](../../src/autodj/static/modules/voice-track-conflict.js)
and the shared dialog guard. `prepared_entry/prepared_handoff` and
[prepared_editions.py](../../src/autodj/prepared_editions.py)
`Occurrence/Take/EditionStore.runtime_plan` retain source identity and approval;
the live edit may use the existing operator-kept copy but cannot rewrite them.

### 5. Bound eligibility and expose a native target choice

**Decision.** Offer the current handoff plus upcoming adjacent music changes
within the next **15 minutes**, using the existing preview worker and its
80-song work cap, not a second simulation. This is an initial admission bound,
not a timing guarantee. Use the shorter available span when prediction is
partial. Confirm only a complete prefix with known durations and playable
library music, within the station's existing speech expiry allowance. After
decoding, the complete chain must still fit before expiry and every applicable
hard boundary. A delayed confirmed target can remain visible beyond the initial
horizon; its expiry remains authoritative.

Eligibility stops before a clockwheel restart or a scheduled show, profile or
wheel change, even when soft; before unknown source duration; and before an
unqueued Show tail. Already queued ordinary Show occurrences may be selected
without changing their ownership. Future prepared-edition occurrences, primary
speech segments, skipped/missing songs and simulated future shows are not
confirmation targets in this workflow: use Prepared editions for that advance
production. Browser output, stopped stations, live takeover and unsupported
transition modes expose reasons. The existing current-handoff workflow is not
removed by these future-list restrictions.

The Voice tracks page gains **Upcoming song changes**, a native labelled
**Song change** select with both titles and a repeated-occurrence ordinal in
each option. Tab reaches it; arrows choose; Tab reaches **Confirm songs** and
Enter/Space confirms. Selection alone neither confirms nor records. Adjacent
text names **Selection: Predicted / Confirmed** independently of **Estimated
airtime**, plus one short eligibility reason. An unconfirmed current selection
can say **Current handoff** without claiming an operator confirmation. Never
label an estimate as a guaranteed start, or treat a passed ETA as on-air evidence.
Show unavailable rows with reasons; inspectability need not imply eligibility.

Preserve the selected ID, native control node and focus across passive refresh;
update options in place. If the target disappears, keep a selected unavailable
placeholder and the take rather than selecting the first row. Record stays
bound to its starting pair when a user inspects another row. An action announces
its result once via `announceStatus`; countdowns and passive ETA updates stay
quiet. Keep Alt+R and existing recording, listen-back, placement, retry and
Cancel controls. When an action control disappears, focus goes directly to the
target selector or the existing next-row/heading fallback, never page body.
Use the repository dialog guard for speech conflicts, with Cancel last.

**Reason and alternatives.** A short near-air horizon reuses current resource
and expiry bounds; a whole hour or tomorrow belongs to the separate prepared
production workflow. Restricting everything to the current handoff would not
solve #524. Confirming unqueued show tails would need new feeder/progress
semantics, so those rows remain explicitly ineligible. A custom grid/listbox or
one button per forecast row adds focus handling that a native select already
provides. Color-only certainty and a combined “fixed time” label are misleading.

**Owners.** `hour_preview.read_station/build_preview/HourPreviews/_item_view`
and `breaks_api._voice_context` supply one authoritative target list, eligibility
and estimates. `voice-tracker.js` `apply/refresh/renderSlot/renderTracks/start`
and [index.html](../../src/autodj/static/index.html) own native selection and
focus. Phase 2 updates the [spoken-breaks workflow](../guides/operations/spoken-breaks.md),
[preview guide](../guides/operations/rotation-preview.md), queue/show guidance
and [accessibility rehearsal](../accessibility-testing.md). Some current guide
text still describes local pinned slots; reconcile that known prose drift with
the already accepted #538 amendment, not by restoring the retired behavior.

### Consequences, verification and revisit conditions

No new dependency, durable queue, commitment worker or approval service is
required. Confirmation foundations and the UI are ordered steps within #524;
the UI must consume the same target/revision contract rather than shipping a
parallel provisional mechanism. Phase 1 recorded the new contracts as evidence
gaps. Phase 2 implements all six plan steps; rows 22 through 28 now map to
passing occurrence, confirmation, upload, conflict, lifecycle and browser-state
regressions. Hard scheduled authority is separate from manual Play now timing;
a completed hard wait preserves priority without an extra fade. Frozen candidate
ownership is adopted without a second random choice or self-cooldown refusal.
Keep is unavailable when merged frozen chains cannot preserve their content and
order. These are applications of the accepted contracts, not new commitment modes.

The focused Chromium audit uses production markup, styles and controls with
deterministic API responses. Keyboard focus, native confirmation, target sizing
and axe scans cover both themes. Real microphone/output continuity and Windows
NVDA speech in Chrome and Firefox remain separate manual evidence under row 29.
Run reset and crash-style recreation report unknown old airtime without restoring
temporary speech. Ordinary Show/prepared resume and saved source ownership remain
covered by their existing suites.

Revisit the horizon or unqueued-show exclusion only with an explicit requirement
and ownership design. Revisit manual-queue conversion if preserving future
clock instructions becomes required. Session-only recordings can be lost on
restart and crash history is uncertain by design; durable advance production
continues to use prepared editions. Concurrency tests must cover duplicate
paths, two editors, upload/cancel/promotion races and hard-show cleanup before
the implementation is considered complete.
