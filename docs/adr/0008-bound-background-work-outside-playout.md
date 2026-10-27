# 8. Bound background work outside live playout

Date: 2026-10-16
Status: Proposed; retrospective description of existing implementation.
Decision authority: Project maintainer; explicit acceptance pending.

## Context and scope

Indexing, previewing, synthesis/decoding, and remote analysis have costs and
failure modes different from the clock-paced mix bus in
[ADR 4](0004-server-mix-bus-and-stream-mode.md). This record covers ownership,
isolation, and shutdown; it introduces no general-purpose job platform.

This record reconstructs the current boundary from the linked implementation,
tests, and operational docs. Original decision dates and deliberations are not
established. The options below are tradeoffs for review, not historical claims.

## Proposed decision and constraints

Use the existing bounded mechanisms for each workload: a single-slot CLI
subprocess runner for maintenance, leased catalog analysis for workers, isolated
simulation workers, and render-ahead preparation for audio.

- Maintenance accepts only vetted commands and flags, runs without a shell,
  refuses a concurrent start, and retains bounded recent output.
- Server and remote analysis coordinate through SQLite leases; expiration and
  stale/failed holds must avoid permanently lost work or tight retry loops.
- Preview and rotation work use their existing worker, cancellation, duration,
  queue, and output bounds. Read those constants at the implementation; they
  are resource controls, not promised response-time service levels.
- Render-ahead may prepare a replacement while retaining a usable fallback;
  normal queue refresh must not discard the fallback prematurely. Hard resets
  have different semantics and can temporarily leave no prepared audio.
- Lifecycle cleanup must not release a shared library, mastering process, or
  licence while a worker still owns it. Shutdown failure is observable; it is
  not permission to start another owner on top of an unfinished one.

## Alternatives for review

- Workload-specific bounded workers: reuse current failure/resource controls,
  but each mechanism needs lifecycle tests.
- Inline work on request/audio paths: fewer components, but slow work or model
  failures can stall control or playout.
- A durable distributed queue: broader scheduling/retry capabilities, but adds
  a service and operational ownership not required by current workloads.

## Consequences and revisit conditions

One limit cannot be assumed to govern every workload. New background work
must choose an owner, bounded admission/output, cancellation, and cleanup path.
Revisit if concurrent jobs or durable resumable command execution become an
actual requirement. Unit tests do not establish a hard real-time guarantee.

## Implementation and operational references

- [Jobs](../../src/autodj/jobs.py), [analysis leases](../../src/autodj/analysis_queue.py),
  [worker protocol](../../src/autodj/worker_api.py), and [render ahead](../../src/autodj/render_ahead.py).
- [Preview](../../src/autodj/hour_preview.py), [simulation](../../src/autodj/rotation_sim.py),
  [station lifecycle](../../src/autodj/stations.py), and [child ownership](../../src/autodj/child_jobs.py).
- [GPU workers](../operations.md#using-a-gpu-machine),
  [hour preview](../operations.md#hour-preview-and-backtiming), and
  [station soak testing](../operations.md#soak-test).

This documentation pass adds no runtime dependency, configuration switch, or
migration. Any future change to this boundary needs its own affected-path and
compatibility assessment.

## Current contracts

The separate [programme review proposal](0014-isolate-programme-review-and-export.md)
defines its render-slot, artifact and cancellation boundary. This retrospective
record remains Proposed.

IDs are stable and are never recycled. Automated means an existing regression
test is mapped; structural validation does not execute it, prove platform
coverage, or accept this ADR. Manual and gap rows remain explicit obligations
or evidence limits. Narrative above retains rationale and scope.

| ID | Constraint | Method | Evidence |
| --- | --- | --- | --- |
| `ADR-0008-01` | Maintenance rejects unsupported commands/flags and concurrent starts. | automated | `tests/unit/test_jobs.py::TestJobManagerStart::test_rejects_unknown_subcommand`; `tests/unit/test_jobs.py::TestJobManagerStart::test_rejects_flags_the_web_ui_never_sends`; `tests/unit/test_jobs.py::TestJobManagerStart::test_refuses_concurrent_start` |
| `ADR-0008-02` | Retained job output stays bounded and retains recent progress from a verbose run. | automated | `tests/unit/test_jobs.py::test_verbose_job_keeps_recent_output_without_retaining_the_entire_run` |
| `ADR-0008-03` | Analysis leases exclude competing holders until expiry and failed work receives a retry hold. | automated | `tests/unit/test_analysis_queue.py::test_a_song_is_leased_to_one_holder_until_it_expires`; `tests/unit/test_analysis_queue.py::test_finishing_counts_and_holds_back_failed_songs` |
| `ADR-0008-04` | A preview stops at its declared work limits and runs outside the request event loop. | automated | `tests/unit/test_hour_preview.py::test_the_dry_run_stops_at_its_limits`; `tests/unit/test_hour_preview.py::test_previews_run_on_their_own_worker_thread` |
| `ADR-0008-05` | Mastering child processes exit when AutoDJ ends on Windows (requires a Windows run). | automated | `tests/unit/test_station_runtime.py::test_mastering_children_die_with_autodj_on_windows` |
| `ADR-0008-06` | Capable workers complete deferred music scan decoding through existing leases; older workers preserve local decoding and abandoned work drains serially in one recovery run without a rescan. | automated | `tests/unit/test_library.py::test_scan_worker_capability_controls_music_decoding`; `tests/unit/test_library.py::test_scan_expired_worker_lease_falls_back`; `tests/unit/test_library.py::test_tick_decodes_abandoned_scan_without_rescan`; `tests/unit/test_library.py::test_recovery_drains_backlog_in_one_run`; `tests/unit/test_worker_api.py::test_worker_combined_result_stores_scan_fields` |
| `ADR-0008-07` | Malformed or out-of-range scan results cannot partially update catalog measurements. | automated | `tests/unit/test_worker_api.py::test_worker_rejects_malformed_scan_result` |
| `ADR-0008-08` | Recovery without eligible work uses worker-progress and indexed candidate lookups and takes no scan lock or thread unless a remote cue snapshot refresh is due; unchanged scans add no per-song queue reads or writes. | automated | `tests/unit/test_library.py::test_empty_scan_fallback_uses_bounded_reads_without_decode`; `tests/unit/test_library.py::test_tick_starts_no_recovery_without_recoverable_scan`; `tests/unit/test_analysis_queue.py::test_scan_candidate_queries_use_retry_index`; `tests/unit/test_library.py::test_unchanged_scan_has_no_per_song_queue_work` |
| `ADR-0008-09` | Background scan recovery shares scan ownership, takes abandoned work between files, respects remote claims, and yields ordinary work to a reconnected capable worker. | automated | `tests/unit/test_library.py::test_tick_scan_fallback_respects_scan_lock_and_live_lease`; `tests/unit/test_library.py::test_tick_scan_fallback_yields_to_reconnected_worker`; `tests/unit/test_library.py::test_scan_recovers_abandoned_work_between_files`; `tests/unit/test_library.py::test_recovery_yields_between_candidates`; `tests/unit/test_analysis_queue.py::test_local_scan_retry_preserves_live_remote_lease` |
| `ADR-0008-10` | Recent claims by a connected capable worker preserve its unclaimed scan backlog beyond the initial deadline. | automated | `tests/unit/test_analysis_queue.py::test_scan_deadline_waits_while_capable_worker_claims_progress` |
| `ADR-0008-11` | Scan reservations and bounded retry budgets remain independent of CPU vector leases and holds; mid-decode rewrites wait 60 seconds, and further identity refreshes retain attempts and backoff after local handoff. | automated | `tests/unit/test_analysis_queue.py::test_vector_holds_do_not_block_scan_attempts`; `tests/unit/test_analysis_queue.py::test_scan_attempts_do_not_hold_vector_jobs_and_stop_after_three`; `tests/unit/test_library.py::test_scan_retry_budget_logs_once_and_preserves_vector_queue`; `tests/unit/test_analysis_queue.py::test_remote_scan_failure_releases_vector_work_and_reserves_local_recovery`; `tests/unit/test_library.py::test_pending_decode_rewrites_back_off_and_exhaust_budget`; `tests/unit/test_analysis_queue.py::test_refresh_scan_keeps_local_recovery_budget` |
| `ADR-0008-12` | Scan decoding runs outside the ticker and recovery errors do not stop later maintenance. | automated | `tests/unit/test_library.py::test_tick_does_not_wait_for_scan_decode`; `tests/unit/test_library.py::test_tick_survives_scan_queue_error` |
| `ADR-0008-13` | Negotiated scan versions and bounded durations protect catalog storage; tag estimates permit half to twice their length with three seconds of slack; rejected scan results hand off locally, while vector-only identity conflicts remain releasable without a failed hold; local cue normalization and optional loudness accept real analysis output, and predeferral vector claims cannot strand scans. | automated | `tests/unit/test_worker_api.py::test_advanced_lease_before_scan_deferral_stores_vector_and_retains_scan`; `tests/unit/test_worker_api.py::test_scan_version_mismatch_only_claims_advanced_and_logs_once`; `tests/unit/test_worker_api.py::test_worker_rejects_implausible_scan_duration_without_catalog_changes`; `tests/unit/test_worker_api.py::test_worker_normalizes_basic_points_like_local_scan`; `tests/unit/test_worker_api.py::test_real_scan_analysis_round_trips_dj_and_optional_loudness`; `tests/unit/test_worker_api.py::test_worker_accepts_tag_only_scan_with_inaccurate_duration`; `tests/unit/test_worker_api.py::test_worker_rejected_result_fails_only_owned_lease`; `tests/unit/test_worker_api.py::test_worker_scan_tag_duration_bounds`; `tests/unit/test_worker_api.py::test_vector_only_identity_rejection_has_no_failed_hold` |
| `ADR-0008-14` | Compatible workers release orphaned claimed leases on startup and renew only received lease IDs; omitted lists retain legacy renewal. | automated | `tests/unit/test_worker_api.py::test_heartbeat_renews_only_reported_leases_and_connect_releases_orphans`; `tests/unit/test_worker.py::test_worker_restart_releases_orphans_and_heartbeat_tracks_received_leases` |
| `ADR-0008-15` | Scan mismatch warning history is bounded per token and globally; transient network/server failures preserve negotiated scan support. | automated | `tests/unit/test_worker_api.py::test_scan_mismatch_logs_once_per_token_across_version_strings`; `tests/unit/test_worker_api.py::test_scan_mismatch_warning_history_has_a_token_bound`; `tests/unit/test_worker.py::test_worker_renegotiates_scan_support_after_reconnect` |
| `ADR-0008-16` | Pending tag edits update pending bytes transactionally and retain listing provenance; only pending replacements preserve the previous analysis identity. Playable records stay until replacement unless a known tag duration exceeds tolerance; new or silent listings refresh tags. Claims and results match pending identity and disk bytes; mismatches hand off to recovery. | automated | `tests/unit/test_library.py::test_scan_relists_retagged_pending_song_while_worker_is_busy`; `tests/unit/test_library.py::test_changed_pending_song_stays_playable_until_fallback`; `tests/unit/test_library.py::test_retag_changed_pending_song_still_decodes_locally`; `tests/unit/test_library.py::test_changed_pending_duration_controls_playability`; `tests/unit/test_worker_api.py::test_changed_playable_song_accepts_only_pending_bytes`; `tests/unit/test_worker_api.py::test_claim_hands_identity_mismatch_to_local_scan`; `tests/unit/test_library.py::test_retag_tag_only_pending_tracks_bytes_and_listing_origin` |
| `ADR-0008-17` | Remote scan claims filter retry, handoff and lease state in SQL before per-file reads and writes. | automated | `tests/unit/test_worker_api.py::test_claim_filters_ineligible_scans_before_file_work` |
| `ADR-0008-18` | Same-byte scan-only results preserve advanced metadata, embedding time and existing vectors; changed-byte results invalidate stale vectors when replacing measurements. | automated | `tests/unit/test_worker_api.py::test_worker_scan_only_preserves_advanced_metadata_and_vector` |
| `ADR-0008-19` | Unavailable files preserve catalog identity. Successful deferred local decodes discard measurements if file identity changed during decoding. Decoder rejection or exhausted non-access failures may retire only the pending scan's unchanged, readable new tag listing. Access-only exhaustion and older songs preserve records and history. Later scans without a capable worker retry held or exhausted rows, respecting live claims. | automated | `tests/unit/test_library.py::test_pending_scan_preserves_unreadable_file`; `tests/unit/test_library.py::test_pending_scan_keeps_transient_decode_failures`; `tests/unit/test_library.py::test_pending_scan_preserves_older_corrupt_song_and_history`; `tests/unit/test_library.py::test_pending_scan_preserves_file_changed_during_decode`; `tests/unit/test_library.py::test_pending_decode_rewrite_discards_measurements`; `tests/unit/test_library.py::test_pending_scan_preserves_decoder_io_failure`; `tests/unit/test_library.py::test_scan_removes_corrupt_deferred_file`; `tests/unit/test_library.py::test_exhausted_scan_reports_failure_and_retires_only_new_listing`; `tests/unit/test_library.py::test_access_only_exhaustion_preserves_listing_and_next_scan_retries`; `tests/unit/test_library.py::test_scan_without_worker_retries_pending_holds`; `tests/unit/test_library.py::test_exhausted_scan_preserves_disappeared_or_changed_file`; `tests/unit/test_analysis_queue.py::test_scan_failure_evidence_survives_restart_until_bytes_change` |
| `ADR-0008-20` | Idle recovery checks run outside the library lock at most every 30 seconds; worker progress limits candidate work to indexed handoffs and failed, stale or expired leases. | automated | `tests/unit/test_library.py::test_idle_recovery_checks_are_throttled_outside_library_lock`; `tests/unit/test_analysis_queue.py::test_busy_worker_recovery_does_not_walk_pending_backlog` |
| `ADR-0008-21` | Claims check pending membership per candidate, back off unavailable paths and preserve unpublished rows until their deadline, then hand them to recovery under the scan lock. | automated | `tests/unit/test_worker_api.py::test_claim_backs_off_dead_scans_and_reaches_live_song`; `tests/unit/test_worker_api.py::test_claim_hands_off_expired_pending_without_catalog_record`; `tests/unit/test_worker_api.py::test_claim_during_new_scan_tag_read_preserves_work` |
| `ADR-0008-22` | Decoding recovery reports busy state without completion speech; cue-only refreshes remain idle. Pending cue merges survive snapshot loading. Recovery reloads the catalog at most every 30 seconds plus once on exit. External cue imports run lazily once per pass or drain; remote results request bounded snapshot refreshes off the request path. | automated | `tests/unit/test_library.py::test_recovery_drains_backlog_in_one_run`; `tests/unit/test_library.py::test_recovery_refreshes_catalog_at_bounded_intervals`; `tests/unit/test_library.py::test_external_cues_load_once_per_long_scan_or_drain`; `tests/unit/test_library.py::test_remote_cue_refresh_is_bounded_and_runs_off_request_thread`; `tests/unit/test_library.py::test_remote_scan_before_snapshot_merges_imported_cues`; `tests/unit/test_library.py::test_cue_only_refresh_leaves_import_state_idle`; `tests/unit/test_library.py::test_recovery_resets_count_after_worker_handoff`; `tests/jsmodules/library-folders.test.js::shows recovery progress without completion announcements` |
| `ADR-0008-23` | Pending scan completion preserves all user/imported points, explicit clears and manual key/BPM for matching catalog/pending identities, including NULL snapshots; changed bytes preserve only post-deferral edits. DJ markers follow catalog provenance. Both completion paths clamp saved points to measured bounds, fall back to analysis for impossible bounds, deduplicate aligned cues and discard cues past the new length. | automated | `tests/unit/test_worker_api.py::test_pending_replacement_keeps_current_edits`; `tests/unit/test_library.py::test_pending_tag_refresh_keeps_edits_before_local_decode`; `tests/unit/test_worker_api.py::test_pending_completion_preserves_only_edits_after_defer`; `tests/unit/test_worker_api.py::test_scan_saved_cues_are_unique_and_within_duration`; `tests/unit/test_analysis_queue.py::test_scan_pending_upgrade_retains_rows_without_edit_evidence`; `tests/unit/test_worker_api.py::test_same_bytes_pending_completion_keeps_existing_edits`; `tests/unit/test_worker_api.py::test_scan_replaces_old_byte_markers_with_catalog_points` |
| `ADR-0008-24` | Unique pending identities participate in moves; catalog rekey moves pending provenance, retry state and failed/stale holds transactionally, retaining the later expiry on collision and rolling back the whole move on failure, cancelling only claimed path-bound leases. Ambiguous or present identities never transfer history. | automated | `tests/unit/test_worker_api.py::test_pending_move_rejects_old_worker_result`; `tests/unit/test_library.py::test_pending_move_requires_unique_identity`; `tests/unit/test_library.py::test_pending_move_preserves_retry_state`; `tests/unit/test_worker_api.py::test_pending_move_preserves_lease_hold`; `tests/unit/test_catalog.py::test_rekey_keeps_later_hold`; `tests/unit/test_catalog.py::test_rekey_hold_failure_rolls_back_move` |
| `ADR-0008-25` | Settings stores pause, batch (1 to 16) and idle release (1 to 10,080 minutes) per worker token, with a revision raised only by a change, and sends them in heartbeat and claim replies. A worker applies each revision once; an unset value restores its own, and an option typed on its command line wins and is logged once. | automated | `tests/unit/test_worker_api.py::test_worker_settings_round_trip_with_a_revision_per_change`; `tests/unit/test_worker.py::test_worker_applies_server_settings_once_per_revision`; `tests/unit/test_worker.py::test_command_line_settings_win_and_are_logged_once`; `tests/unit/test_worker.py::test_command_line_values_are_fixed_and_server_idle_release_reaches_the_worker`; `tests/unit/test_worker.py::test_an_older_settings_revision_never_undoes_a_newer_one` |
| `ADR-0008-26` | A paused token is handed no songs whatever the worker's version. A current worker claims nothing, gives back its batch after the song in hand, keeps heartbeats (so server analysis stays paused), then frees the model and runs its after-command at once, idle release or not. A worker paused from the start never prepares; resuming prepares only when songs arrive; paused counts as an empty queue for `--once` and `--idle-exit`. | automated | `tests/unit/test_worker_api.py::test_a_paused_worker_is_handed_no_songs_and_still_pauses_server_analysis`; `tests/unit/test_worker.py::test_pause_gives_back_the_rest_of_the_batch_then_resume_carries_on`; `tests/unit/test_worker.py::test_a_paused_worker_releases_at_once_and_counts_as_an_empty_queue`; `tests/unit/test_worker.py::test_a_worker_started_paused_runs_no_hook_and_exits`; `tests/unit/test_worker.py::test_pausing_mid_batch_restores_the_other_program_and_resuming_reloads`; `tests/unit/test_worker.py::test_a_pause_between_claim_and_load_gives_the_songs_back_unprepared` |
| `ADR-0008-27` | Management fields are negotiated through `worker_manage_v1`: older workers keep working with a managing server, and a current worker sends no management fields to a server that does not advertise them or rejects them. | automated | `tests/unit/test_worker_api.py::test_an_older_worker_keeps_working_with_a_managing_server`; `tests/unit/test_worker.py::test_new_worker_with_an_older_server_sends_no_manage_fields`; `tests/unit/test_worker.py::test_new_worker_drops_manage_fields_a_server_rejects` |
| `ADR-0008-28` | The server sends a worker nothing to run: settings hold only pause, batch and idle release, other names are refused or ignored, and hooks come only from the worker's own command line. | automated | `tests/unit/test_worker_api.py::test_worker_settings_out_of_range_are_refused`; `tests/unit/test_worker.py::test_server_settings_never_carry_a_command_to_run` |
| `ADR-0008-29` | The latest refusal of each known worker token (identity, scan version, revoked, wrong secret or scope) is kept in one bounded row without secrets, shown with advice, cleared by the next successful connect and hidden by Dismiss until the reason changes. | automated | `tests/unit/test_worker_api.py::test_an_identity_mismatch_is_refused_noted_and_cleared_on_the_next_connect`; `tests/unit/test_worker_api.py::test_an_older_workers_mismatched_result_is_noted`; `tests/unit/test_worker_api.py::test_a_scan_version_mismatch_is_noted_until_the_versions_match`; `tests/unit/test_worker_api.py::test_token_refusals_are_noted_once_per_token_without_secrets`; `tests/unit/test_worker_api.py::test_refusal_details_are_cut_short`; `tests/unit/test_worker_api.py::test_a_dismissed_refusal_stays_hidden_until_another_reason`; `tests/unit/test_worker.py::test_a_worker_reports_its_weights_so_the_server_can_say_why_it_refused`; `tests/unit/test_worker_api.py::test_alternating_refusals_write_once_a_minute_per_token` |
| `ADR-0008-30` | Settings, Analysis workers gives each worker labelled native settings with its own Save, keeps unsaved edits through refreshes, links and focuses errors, says results and new refusals through the panel's status region, and shows a pairing code in digits for speech with Copy buttons. NVDA 2026.2 with Firefox 158 passed on 2026-10-23 at 975a9891; its two minor points (saved values not repeated, focus briefly on the page after Dismiss) are fixed. | automated | `tests/jsmodules/analysis-workers.test.js::gives each worker labelled native settings, filled from the server`; `tests/jsmodules/analysis-workers.test.js::saves a row, says so, and a refresh keeps changes not yet saved`; `tests/jsmodules/analysis-workers.test.js::marks a value out of range, says why and moves focus to it`; `tests/jsmodules/analysis-workers.test.js::shows refusal notices in words, says a new one once, and dismisses them`; `tests/jsmodules/analysis-workers.test.js::pairs a worker with a spoken code, a selected command and Copy buttons`; `tests/jsmodules/analysis-workers.test.js::keeps focus on a field in any row while polls reorder last-seen times`; `tests/jsmodules/analysis-workers.test.js::says an ongoing refusal once, though its time keeps moving`; `tests/jsmodules/analysis-workers.test.js::moves focus to the next notice, then Refresh workers, before a notice goes` |
| `ADR-0008-31` | Shutdown tells the library scan to stop when it begins; listing folders and the DJ backlog stop at the request; neither a stopping scan nor the tick reloads the catalog, a scan cut short is logged as stopped, and a closing library starts no scan or analysis job. The cache closes only after the scan ends within the shutdown timeout; a scan still running keeps the cache and the library open, logs it and still saves buffered plays. Independent workers stop concurrently within their own timeouts, and each shutdown step logs its duration. | automated | `tests/unit/test_library.py::test_shutdown_stops_the_scan_before_the_dj_meta_cache_closes`; `tests/unit/test_library.py::test_stop_scan_is_bounded_for_a_scan_that_has_not_stopped`; `tests/unit/test_library.py::test_discovery_stops_between_folders_once_the_library_closes`; `tests/unit/test_library.py::test_the_dj_backlog_stops_reading_the_cache_once_the_library_closes`; `tests/unit/test_library.py::test_a_closing_library_starts_no_analysis_job`; `tests/unit/test_library.py::test_the_tick_does_not_reload_a_library_that_is_closing`; `tests/integration/test_stations.py::test_a_scan_that_will_not_stop_keeps_the_cache_and_library_open`; `tests/integration/test_stations.py::test_each_shutdown_step_logs_how_long_it_took`; `tests/integration/test_server.py::TestLifespan::test_independent_shutdown_steps_stop_together_and_are_timed` |
| `ADR-0008-32` | A prepared worker that cannot reach the server for its idle release, or 10 minutes without one, frees the model and runs its after-command during the retries, logs it once and prepares again, running its before-command, for the next songs, including a song whose download outlasted the outage. | automated | `tests/unit/test_worker.py::test_an_unreachable_server_releases_the_model_and_songs_prepare_it_again`; `tests/unit/test_worker.py::test_an_outage_during_a_download_releases_and_reloads_before_analysis`; `tests/unit/test_worker.py::test_the_after_command_runs_while_the_server_is_down_and_before_runs_again` |
| `ADR-0008-33` | The DJ backlog reads only stored paths and analysed flags, keeps no song's beats or cues in memory, and agrees with the cache's per-song answer. The server freezes the objects alive once the library and stations have loaded, and again after each refresh that reloads the entries; replaced entries are still freed by reference counting, and the young generations are collected first, so repeated reloads keep the frozen objects flat. | automated | `tests/unit/test_dj_meta.py::TestAnalysedAmong::test_agrees_with_get_and_keeps_no_rows`; `tests/unit/test_library.py::test_the_dj_backlog_finds_unanalysed_songs_without_loading_any_beats`; `tests/unit/test_library.py::test_a_refresh_freezes_the_new_entries_and_frees_the_old_ones`; `tests/unit/test_library.py::test_repeated_refreshes_keep_the_frozen_objects_flat` |
| `ADR-0008-34` | A rescan opens no file whose size and modification time match its catalog record. A changed size or time, a record without a stored time, a time within 2 seconds of the clock or ahead of it, and a rescan with Override with ID3 tags read the fingerprint; moves still read only the new paths. New bytes under the old size and time wait for the next change of either. | automated | `tests/unit/test_library.py::test_rescan_opens_no_unchanged_file`; `tests/unit/test_library.py::test_rescan_reads_a_file_whose_size_or_time_changed`; `tests/unit/test_library.py::test_same_size_and_time_still_read_when_the_time_cannot_vouch`; `tests/unit/test_library.py::test_same_size_and_time_replacement_waits_for_a_new_time`; `tests/unit/test_library.py::test_moved_file_is_rekeyed_without_opening_unchanged_files` |
| `ADR-0008-35` | The startup scan waits until every station with a mix bus has started its first song, at most 2 minutes, and a shutdown before then starts none. One library-wide Beets reader serves the clockwheel conditions and every station's categories, reading every field announced or asked for in one read after each change. Each time the categories are worked out again it logs the trigger and the time taken. | automated | `tests/integration/test_stations.py::test_the_startup_scan_waits_for_every_station_s_first_song`; `tests/integration/test_stations.py::test_the_startup_scan_starts_anyway_after_the_wait`; `tests/integration/test_stations.py::test_a_shutdown_before_the_first_song_starts_no_scan`; `tests/unit/test_categories.py::test_the_clockwheel_and_every_station_read_beets_once`; `tests/unit/test_categories.py::test_working_categories_out_again_says_why` |
| `ADR-0008-36` | While any mix bus runs, the garbage collector makes no full pass on its own: the oldest generation's threshold is raised when the first bus starts and put back when the last one stops, unless it was changed meanwhile, and a collector switched off is left alone. The young generations are still collected. The library tick makes the full pass itself every 10 minutes, and never while a scan (other than its loudness measurement), a reload, a category build or a Beets read runs. | automated | `tests/unit/test_mixbus.py::test_run_holds_off_full_collections_until_the_last_bus_stops`; `tests/unit/test_mixbus.py::test_run_leaves_a_collector_someone_else_set`; `tests/unit/test_library.py::test_the_tick_makes_the_full_collection_running_buses_hold_off_when_quiet` |
| `ADR-0008-37` | Songs a scan finds gone from a folder that is there are saved in the catalog, so a restarted server leaves them out of the library, its vectors, categories and totals before its first scan, and a scan counts only newly gone songs. The record stays until its file returns or the user removes missing tracks; it follows a move. A folder that is missing or could not be read marks nothing gone and keeps what was saved. | automated | `tests/unit/test_library.py::test_gone_songs_stay_gone_after_a_restart_and_are_reported_once`; `tests/unit/test_library.py::test_a_share_away_at_restart_neither_wipes_nor_forgets_gone_songs`; `tests/unit/test_catalog.py::test_gone_records_are_saved_follow_a_move_and_leave_with_their_record` |
| `ADR-0008-38` | Worker-produced shared audio features are accepted only for compatible analysis versions and current file identity; unsupported scan decoding stays local. | automated | `tests/unit/test_worker.py::test_worker_downloads_once_for_scan_and_vector`; identity/version rejection remains covered by ADR-0006-04. |

## Verification

Run the following checks for affected changes and record results and platform
limits in the relevant PR. Test links do not establish execution or change this
record's Proposed status.

- [ ] Verify allowlists, one job slot, bounded logs, and stop behavior with
  [job tests](../../tests/unit/test_jobs.py).
- [ ] Verify exclusive leases, expiration, renewal, and failed-work holds with
  [analysis queue tests](../../tests/unit/test_analysis_queue.py).
- [ ] Verify soft-refresh fallback and reset races with
  [render-ahead tests](../../tests/unit/test_render_ahead.py).
- [ ] Verify lifecycle failure and cleanup ownership with
  [station runtime tests](../../tests/unit/test_station_runtime.py).
- [ ] Verify isolated/bounded previews with
  [preview tests](../../tests/unit/test_hour_preview.py).

## Evidence gaps

CPU/memory limits, audio continuity under load, and Windows process cleanup
need applicable platform/soak evidence. Timing constants alone prove none of
those outcomes. Historical rationale and final acceptance remain pending.

## Amendment 2026-10-22: Worker decoding for music scans

Status remains Proposed. Workers negotiate both `scan_decode_v1` and the scan algorithm
version before taking new and changed music. A version mismatch leaves scan decoding local
and logs once per retained token; it does not repeatedly hand out incompatible work.
Discovery, tags, lyric sidecars, moves, external cue imports, liners and on-demand playback
analysis keep their existing owners. Local and remote scans use the same measurement functions.

Two additive queue tables retain negotiated capability and pending scan identity. Compatible
additive columns record the last successful worker claim and local scan attempts/retry time.
Remote scan work uses the analysis lease. Local scans reserve `scan_pending` independently:
CPU vector claims and failed holds cannot delay scan decoding, nor can a local scan failure
hold the vector queue. Each token belongs to one worker process; shared tokens are unsupported.

The existing two-second library tick admits a serial recovery run on the library's
background scan slot. Decoding never runs on the ticker. A scan holding the lock checks for
recovery between files at most every five seconds, using cached capability. This is preferred
to concurrent independent decoders because it preserves scan, cue-cache and shutdown ownership.
Queued scans run after the current recovery song on the same thread. A slow file can still
delay recovery. Exceptions in scheduling or recovery are caught and warned at most once a minute.

Unclaimed work becomes eligible after two minutes without a capable heartbeat. Live remote
claims remain protected for ten minutes without renewal. Remote failed/stale reports release the analysis lease and reserve pending scan work for
local recovery immediately; they do not impose a vector hold. A zero scan deadline records
this handoff and excludes repeated remote claims. Older remote failure holds also allow recovery. After its initial ten-minute deadline, unclaimed work is recovered
only if no connected capable worker has claimed anything in ten minutes. Heartbeats alone do
not count as progress. Each batch reads one candidate with `LIMIT 1` and a separate `COUNT`
for its start log; no fallback batch loads the whole pending queue.

Scan attempts have a separate six-hour retry interval and a three-attempt local budget.
Exhaustion logs one WARNING naming the file and prevents further automatic attempts on those
bytes. Changed bytes reset the budget. Later local scans can retry unchanged pending bytes;
exhausted failures can retire new tag-only listings as described in the clarification below.
Loudness failure is non-fatal
on both paths and may produce null loudness. External cue libraries load lazily on the scan
thread once per scan pass or recovery run; HTTP result storage uses the most recent snapshot.
Between scans, remote DJ results request a snapshot refresh on the same background slot,
at most once a minute, without parsing external libraries on the request path.

A valid vector-only result from a claim issued before deferral is stored and finishes the
analysis lease, preserving pending scan work. Workers release rejected/unacknowledged results
and re-negotiate capability after actual refusal or incompatible server responses. Transient
network errors and server outages retain negotiated support. This also prevents older workers
from stranding a song whose scan was deferred after its claim.

Worker scan lengths are finite, positive, at most 24 hours, and within the larger of two
seconds or two percent of a known catalog duration. Automatic basic points are normalized
using the local scan rule, including accepting Intro after Outro. Other result bounds are
recorded in the [threat model](../../THREAT_MODEL.md#machine-tokens-analysis-workers-and-announcer-breaks).
Validation precedes catalog mutation. Catalog measurements and vectors share the identity-checked
transaction. DJ metadata is a subsequent, separate cache write: cache failure can leave committed
catalog measurements, and pending scan work remains for local recovery. The no-partial-update
contract applies to rejected catalog measurements, not a transaction across both databases.

The idle tick checks for eligible recovery before taking the scan lock or starting a thread,
using the throttling and indexed candidate selection clarified below.
A partial retry index excludes exhausted rows and seeks eligible retry
times; live remote leases and active worker progress still protect their work. No eligible
row means no recovery thread, scan lock acquisition or write; a due remote cue snapshot refresh
can use the same slot without decoding. Due automatic rescans run first. Scans load pending paths once per
pass, cache capability for five seconds, and avoid per-song queue work for unchanged music with
valid points. Structural and automated tests establish neither GPU throughput nor uninterrupted
live audio under load. See the [worker guide](../guides/operations/gpu-worker.md) for recovery.

### Amendment clarification 2026-10-22: Recovery admission and process ownership

The worker protocol adds optional `leases` and `connect` heartbeat fields, negotiated through
`lease_tracking` in a legacy-shaped heartbeat response. The startup connect/check handshake
releases that token's prior claimed leases; it preserves failed/stale holds and other holders.
Subsequent heartbeats renew only reported IDs owned by that token. Empty lists renew none;
omitting the list preserves older workers' holder-wide renewal. New workers retain legacy
request shapes on older servers. One token per process remains required: a second process
using the token can release the first process's claims. This also recovers forced shutdowns
without waiting for orphaned claims that a restarted worker would otherwise keep renewing.

A scan that refreshes pending file identity also re-lists changed bytes from tags. A worker
claim that skips a stale catalog identity sets the pending deadline to zero for local repair.
Claims select pending paths with attempts below the limit, retry time due, positive deadline,
and no live lease, limited to the requested batch before any per-file work. Transactional
claim checks remain in place for concurrent results, reservations and other workers.

Scan-only results update only measured fields, including scan energy. Existing key, mode,
BPM, tempo confidence, embedding time and vector survive. Scan mismatch warning history holds
one entry per token, at most 1,024 tokens, evicting the oldest and removing revoked tokens.
Changing a reported version cannot grow the history or generate another warning for a
retained token. These changes refine the proposed worker boundary without accepting the ADR.

### Amendment clarification 2026-10-22: Unavailable files and backlog cost

Unreadable or missing files must never replace a pending identity with an unknown fingerprint
or acquire a local decode reservation. They receive six-hour backoff and retain their catalog
entry; confirmed missing-file cleanup owns deletion. Other analysis exceptions consume the
reserved local attempt and back off. Decoder rejection can retire only the unchanged tag-only
listing created by that pending row, after rechecking readable bytes with the same identity.
An additive provenance column defaults to false for existing rows, preserving records whose
origin cannot be established. Older songs with invalid cues or history are never disposable.

Idle recovery admission runs at most every 30 seconds, outside the library lock, and rechecks
ownership before starting the scan thread. A worker-progress lookup selects a query driven by
the handoff and lease indexes while a connected capable worker is actively claiming. This avoids
walking the ordinary backlog to discover that none is abandoned. The 80,000-row regression uses
SQLite instruction counts and query plans, not wall-clock thresholds; it establishes query work,
not a latency guarantee on every filesystem. Recovery without worker progress retains the retry
index and existing eligibility rules.

Remote claims look up pending membership by candidate path rather than loading the whole set.
Each request still inspects at most one requested batch of pending files. Missing or unreadable
paths receive backoff, so subsequent requests reach later live files without deleting records.
Contracts ADR-0008-19 through ADR-0008-21 record these refinements; this ADR remains Proposed.

### Amendment clarification 2026-10-22: Serial recovery and failed import completion

Recovery admission retains its 30-second idle throttle. Once admitted, one background thread
keeps selecting and decoding one candidate at a time under the existing scan lock. It stops
when no eligible candidate remains, shutdown is requested, a scan is queued, or a capable
worker resumes claiming. Queued scans inherit the same thread and lock after the current song.
The throttle never imposes a delay between songs within that recovery run.

This clarification extends the earlier preservation rule to retain the predeferral import end
state. A new tag-only listing is removed on exhaustion if at least one local attempt failed
for a reason other than file access or the decoder's recognized resource errors. Missing FFmpeg,
decode timeouts, unsupported decoders and analysis exceptions count as non-access failures.
Deletion uses the same provenance, unchanged catalog, unchanged readable bytes and catalog
version guards as immediate decoder rejection. It removes the pending row, never the source
file. Older records and their history remain; local failures are recorded in import errors.

An additive `non_access_failure` column preserves failure classification across retries and
restarts, defaulting to false for existing rows with unknown failure history. Changed bytes
clear that evidence along with attempts and backoff. Access-only exhaustion preserves the entry
and reopens its retry budget on the next scan, for a capable worker or local decoding, leaving
real deletions to missing-file cleanup. A later scan without a capable
worker retries pending rows despite backoff or exhaustion, while still honoring live remote
claims. The three-attempt budget and six-hour interval continue to bound automatic recovery.

Worker claims leave pending rows without catalog records untouched before their deadline,
then hand them to local recovery. Recovery cancels rows still without records under the scan
lock, so claims cannot race tag publication. Contracts ADR-0008-06, ADR-0008-09, ADR-0008-19 and ADR-0008-21 cover
these refinements. This ADR remains Proposed; these refinements do not accept the broader
retrospective proposal.

### Amendment clarification 2026-10-22: Publication races and bounded refreshes

A scan that finds its just-deferred row missing checks and decodes the song locally instead
of treating it as complete. The timestamp-only guard still preserves DJ cues for non-pending
songs with invalid basic points; pending work can repair those points despite a timestamp change.

A recovery run reports "recovering analysis" through the existing library state. It refreshes
the full catalog at most every 30 seconds and once on exit, while per-song publication updates
cached lookups. External libraries load only when DJ analysis needs them, once per scan pass
or recovery run, including failed import attempts. Remote results retain the latest snapshot
and can request a bounded refresh between scans as described above. Contract ADR-0008-22
records these cost bounds; it establishes neither live audio continuity nor a platform latency
guarantee. This ADR remains Proposed.

### Amendment clarification 2026-10-22: Playable replacement and quiet recovery

Changed music keeps its existing playable catalog record, including energy, basic points,
loudness and vector, until the worker or local fallback replaces it. Pending scan identity
describes the new bytes independently. New and already silent listings retain tag-only
publication. This refines the earlier statement that every changed pending song is re-listed.
The preserved playable record is left unchanged during deferral. Successful replacement reads
current tags on the server and resets stale analysis metadata in the same transaction.

Worker claims use pending size and fingerprint and compare them with the file on disk.
Claims recheck pending identity transactionally. Result storage requires the same pending
identity and readable matching disk bytes, even when catalog identity differs. Catalog
measurements and identity change atomically; vectors for older bytes are replaced or removed
in that transaction. A second file change rejects a stale result without replacing the
playable record. Local recovery refreshes pending identity and decodes the current bytes.
Vector-only results retain the stricter catalog-identity requirement.
Duration tolerance uses the catalog duration only for matching bytes; the finite positive
24-hour limit still applies to every result. Changed songs in advanced sources are offered
embedding provisionally, with the worker enforcing the cutoff against the new measured length.

The timestamp-only guard applies before deferral even with a capable worker connected.
Forced DJ results merge saved user and imported cues with automatic cues. Remote results
request a bounded background snapshot refresh; their paths remain pending for cue merging
until that snapshot loads successfully, including results received just after restart.

Background decoding displays "Recovering analysis: n of m" as static progress text and
makes no completion announcement. User-started scans keep their polite completion message.
Cue-only refreshes never set import running or phase. Recovery resets its run flag on every
exit, including worker handoff, so the next run counts its own backlog. Contracts ADR-0008-16,
ADR-0008-18 and ADR-0008-22 govern this refinement. Automated DOM checks establish announcement
routing, not actual speech or WCAG conformance; browser and screen-reader evidence remain
separate. This ADR remains Proposed.

### Amendment clarification 2026-10-22: Pending edits, duration changes and local decode identity

A tag edit while pending identity differs from catalog identity preserves the catalog's
analysis identity, embedding timestamp and loudness stamp. The same catalog transaction
updates the pending identity to the rewritten bytes and resets retries for changed bytes.
The replacement remains pending until worker or local measurements replace the old analysis.

Preserving a playable record now depends on duration evidence. A new finite, positive tag
duration differing by more than the larger of two seconds or two percent of the old catalog
duration publishes a silent tag-only replacement. Unknown durations and differences within
tolerance preserve the playable record. Both initial deferral and later pending scans apply
this rule, preventing selection with stale duration and cues for a known different recording.

Deferred local scans and recovery capture identity before decoding and compare it afterward,
before publishing measurements. Changed readable bytes refresh the pending row and hand it to
local recovery; the stale measurements are discarded. Unavailable bytes retain existing
failure backoff. The plain no-worker scan path keeps its previous behavior. Contracts
ADR-0008-16 and ADR-0008-19 cover these refinements. This ADR remains Proposed.

### Amendment clarification 2026-10-22: Settling files and tag-only result validation

A readable identity change during deferred local decoding retains the attempt already counted
by the local reservation. The pending identity moves only if it still matches that reservation,
receives a 60-second settle delay, and stays assigned to local recovery. Other eligible rows
can drain during the delay. Repeated mid-decode changes exhaust the three-attempt automatic
budget rather than resetting it. Failure evidence about the previous bytes is cleared; a rewrite
alone never retires a listing. Identity changes discovered before decoding retain the existing
budget-reset behavior. Contract ADR-0008-11 covers bounded recovery across these rewrites.

Tag edits move every existing pending row to the rewritten bytes in the catalog transaction,
including new and replacement tag-only listings whose catalog identity already matches the
pending identity. The new-listing marker survives. Catalog identity moves with the edit only
when it is not preserving measurements for a pending replacement. Contract ADR-0008-16 covers
identity agreement and safe retirement after an edited new listing proves unplayable.

The catalog-duration cross-check applies only to matching measured records. A silent tag-only
listing, identified by zero energy and no points, has an estimated duration and cannot reject
a worker's decoded length on that basis. Finite positive duration, the 24-hour absolute bound,
and all other validation remain required. Result validation failures, including body validation,
immediately fail an owned lease: pending scan work passes to local recovery and advanced-only
work receives the existing failure hold. Another token cannot fail the holder's lease.
Contract ADR-0008-13 covers these refinements. This ADR remains Proposed.

### Amendment clarification 2026-10-22: Loose tag bounds and persistent handoff budgets

Tag-only listings and replacements now accept half to twice a known positive tag duration,
with three seconds of slack at each end; replacements use tags from the new bytes.
Unknown tag durations retain only the absolute duration bound. Invalid scan results hand off
locally, so inaccurate estimates cannot cause repeated remote claims. Vector-only model-identity
conflicts retain immediate worker release without the invalid-content failure hold.

Further identity refreshes during settling or after local handoff preserve attempts, backoff
and local ownership for the same pending lifetime, while clearing old failure evidence.
The full-scan access-only exhaustion reset remains unchanged. These refinements update
contracts ADR-0008-11 and ADR-0008-13; this ADR remains Proposed.

### Amendment clarification 2026-10-22: Edits and moves during pending decoding

Completion compares current catalog choices with a snapshot stored in `scan_pending` at
initial deferral. The nullable `edit_snapshot` column is added through the queue's existing
additive schema migration. It records points, point sources, manual tag provenance and
BPM/key/mode values. Repeated deferral of the same bytes and identity refreshes retain this
baseline. At storage, worker results and local fallback compare the pending size/fingerprint
with the catalog identity. Matching identities preserve all current user/imported points,
explicit clears and manual tags, including pending rows with a NULL snapshot. Different
identities preserve only choices that differ from the snapshot, including newly explicit
clears; without a snapshot they preserve none. Replacement tag refreshes apply this filter
before advancing the catalog identity, and retagging a playable replacement retains its old
analysis identity. Thus same-byte rereads retain earlier edits while changed-byte rescans
discard edits made before replacement deferral. Retained provenance follows the new fingerprint.

Saved Start/End are clamped to the measured duration first. Bounds that cannot form a valid
interval yield to analysis with analysis provenance. Automatic trim expands to include valid
saved mix points. Intro/Outro/Segue then clamp into the resulting Start/End interval, in the
same validation order as local normalization. Explicit clears remain clears; numeric points
never become permanent user clears because the replacement is shorter. Overlap is limited
to that range.

DJ result storage merges current saved cues, drops cues past the new measured length with a
debug log, and aligns mix markers with the catalog's validated points. When external cue
import is on, cached marker cues whose catalog point is no longer user/imported are dropped
before the external libraries are merged again, so current Mixxx, Rekordbox, Traktor and Serato
markers apply and stale ones from old bytes do not; with import off, cached cues stay as before.
Markers backed by user/imported catalog points take the catalog value. Non-marker saved cues
within the new length remain. A second cue
merge removes duplicates created by alignment, preferring user over imported over analysis.
Catalog and DJ writes remain separate transactions, with existing recovery on cache failure.
Editing a tag-only listing does not establish measured duration or let recovery skip decoding.
A pending tag refresh retains eligible saved choices until decoded measurements validate them.

Move detection uses pending size/fingerprint for pending records. A present pending record
blocks matches on both its catalog and pending identities. The one-missing/one-candidate rule
remains required. Rekey moves the pending row, including new-listing provenance, attempts,
failure evidence, deadline and retry time, with the catalog key and shared historical rows in
one transaction. Station-owned history continues through the existing station rekey path.
Claimed path-bound analysis leases are cancelled: a downloaded result cannot gain authority
over another path. Failed and stale holds move with their original expiry. If both paths have
holds, the later expiry wins; equal expiries retain the destination hold. Hold reconciliation
shares the catalog/pending move transaction, so a failure restores both holds and the original
record and pending paths. Old lease submissions are rejected; the new path can receive a fresh
claim subject to the retained hold and retry state. Contracts ADR-0008-23 and ADR-0008-24
cover these refinements. This ADR remains Proposed.

## Amendment 2026-10-23: Managing workers from the server

Status remains Proposed. Issue #554 lets the server manage the workers it already leases songs
to, without connecting to them: the worker machine needs no open port.

The token database keeps one settings row per analysis-worker token: paused, batch and idle
release, each optional except paused, and a revision raised only when a value changes. Heartbeat
and claim replies carry the settings with `worker_manage_v1`. A worker that sees that capability
sends `worker_manage_v1`, the settings revision it applied and the analysis identity it knows, so
Settings can say whether a change reached it. It applies each revision once, from whichever reply
arrives first. Options typed on its command line win over the server's values and are logged once;
an unset value restores the worker's own. A server that rejects these fields gets the request
again without them, as for the other optional fields, and an older server never sees them.

A paused token is handed no songs at claim time, so pause works for older workers too; they finish
the batch they hold. A current worker stops claiming, gives back its batch after the song in hand,
and keeps its heartbeats. It therefore still counts as connected and the server's CPU job stays
paused: the operator paused analysis on purpose, and resuming the CPU job instead would turn a
pause into a hand-over.

Clarification 2026-10-23 (review of #556): pausing always frees the model and runs the
after-command once the song in hand is done and the claims are back, with or without an idle
release, so another program sharing the GPU gets it back. A worker the server has paused when it
starts never runs its before-command or loads the model; resuming prepares again only when songs
arrive. Paused counts as an empty queue for `--once` and `--idle-exit`, so a `--once` run never
waits on a pause. A worker ignores a settings revision at or below the one it applied, so a slow
reply cannot undo a newer one. Refusal rows are written at most once a minute per token whatever
the kind, except that a refusal that stops the worker (identity) may replace a lesser note sooner.

The server never sends anything for a worker to run. Settings hold only pause, batch and idle
release; `--before-command` and `--after-command` remain the worker's own command-line options, so a
compromised server cannot run code on the worker machine.

The server records the latest refusal of each known worker token: a reported or submitted model
identity that differs from its own (refused with 409), a scan analysis version that differs, or a
revoked token, wrong secret or missing worker scope for a token id that exists. One row per token,
with server-made text cut to 600 characters and no secret, bounds the record by the number of
tokens; repeats write at most once a minute. A successful connect clears the row; Dismiss hides it
until the reason changes. Older workers do not report their identity, so a weights mismatch shows
for them only if they post a result.

Worker pairing changes the access boundary in [ADR 7](0007-separate-browser-worker-and-listener-access.md#amendment-2026-10-23-worker-pairing-codes).
Contracts ADR-0008-25 to ADR-0008-30 cover this amendment. An NVDA 2026.2 and Firefox 158
sample of the new Settings controls passed on 2026-10-23 at 975a9891 (recorded in PR #556); its
two minor points are fixed in the next commit.
## Amendment clarification 2026-10-23: Bounded server shutdown

The lifecycle rule above applies to the library scan: it writes the DJ-meta cache, so the cache
must not close while the scan still runs. Shutdown used to close the cache first and only then
stop the scan, which reopened the cache to list its backlog and reloaded the catalog before it
noticed the stop. On a large library that took far longer than a container's default stop
timeout.

Shutdown now tells the scan to stop as soon as it begins, so the scan winds down while the other
workers and the stations stop. Folder listing checks the stop between folders; a partial listing
marks nothing gone or moved. The DJ backlog checks it while listing songs. A stopping library
does not reload the catalog, from the scan or its tick, and starts no scan or analysis job, since
the job runner has already stopped. Its summary says the scan stopped rather than finished. The
cache closes only after the scan thread ends, so the scan cannot open it again. The wait for the
scan uses the existing shutdown timeout, counted from the stop request. A scan still
running then keeps the cache and the library open, which is logged and reported as a degraded
shutdown; buffered station plays are written anyway. The next scan repeats work cut short.

The maintenance job, speech, rotation tests, show generations, hour previews, cue rescans and
review renderers stop concurrently, each within its own existing timeout, before the stations
stop. Each shutdown step logs its duration at debug level, or at info level above two seconds.
These are bounds and diagnostics, not a promised shutdown time. Contract ADR-0008-31 records them;
this ADR remains Proposed.

## Amendment clarification 2026-10-24: Releasing the model while the server is unreachable

Issue #560: a worker retries a server it cannot reach, and those retries used to run inside the
claim call, so neither idle release nor pause could fire. A prepared worker kept the model, and the
effect of its before-command (another program sharing the GPU stopped) lasted for the whole outage.

Every retried call now checks how long the server has been unreachable, counted from the first
failed attempt and reset by any answered call. Once that reaches the idle release, or 10 minutes
when no idle release is set, the worker frees the model and runs its after-command, logs that once
at info level, and keeps retrying with the usual backoff, the last wait cut short so the release
lands on the limit. An outage during a song's download releases the same way; the song in hand
prepares again before it is analysed. Otherwise the next claim that returns songs prepares again,
running the before-command. Ctrl+C and exit cleanup are unchanged: the after-command runs at most
once per preparation. Contract ADR-0008-32 records this; the ADR remains Proposed.

## Amendment clarification 2026-10-24: Keeping a scan from stalling the mix bus

Issue #577: while a library scan ran, every station's mix bus fell seconds behind. The cause was
the cyclic garbage collector. Python 3.14 collects in full passes that walk every tracked object
while holding the GIL. To list the songs still waiting for beat analysis, the scan read every
song's DJ-meta row through the cache, which keeps each analysed row, beats and cues included, for
the rest of the run. On a large library that grew the server's memory by gigabytes, and the many
new objects set off full passes over that bigger heap. With 80,000 synthetic songs the worst
probe delay of a paced 20 ms thread was about 300 ms, 400 ms with a further 1.2 GB of heap, and
memory grew by 2.4 GB. The stalls went on after the scan while a remote worker's results arrived:
each stored result changes the catalog, and the next tick reloads every entry and rebuilds the
vector index, so each reload set off a full pass over the grown heap, about 300 ms each in the
same benchmark when 100 results arrived in batches of 4.

The backlog now asks the catalog for paths and the cache for analysed flags in one query each,
and keeps nothing. Once the library and the stations have loaded, and after each refresh that
reloads the entries, the server calls `gc.freeze()`, so the long-lived library objects leave the
collector's passes. Frozen objects are still freed when their reference count drops; only a
reference cycle among them would stay, and the entries a refresh replaces hold none. Each freeze
first collects the two young generations, which is quick, so garbage cycles made since the last
one are not frozen; over 50 reloads the frozen count stays flat. On the same
benchmark the worst delay fell to 25 to 50 ms during a scan and to about 20 ms while results
arrived, and memory grew by 0.65 GB. A reload still costs about a second of work per result. These are measurements
on one machine, not a latency guarantee. Running the scan in a child process remains an option if
the field shows further stalls. Contract ADR-0008-33 records this; the ADR remains Proposed.

## Amendment clarification 2026-10-24: Rescanning without reading unchanged files

Issue #578: every rescan read the 64 KiB fingerprint of every file that already had a catalog
record, about 5 GB on a library of 80,000 songs, which is slow over a network share. The
fingerprint was added so that moved files keep their analysis; the stored modification time
was added later so that a rescan notices tags edited by another program. A tag edit inside
existing padding does not change the fingerprint, so the time was already what caught it.

A rescan now compares the file's size and modification time with the record first, as the
stored loudness measurement already does, and reads the fingerprint only when either differs,
when the record has no stored time (a record from before times were kept), or when the time is
within 2 seconds of the clock or ahead of it, since a coarse file system clock (FAT keeps
2 seconds) or a share whose clock runs fast may not show a change yet. A rescan with
**Override with ID3 tags** opens every file anyway, so it compares the fingerprint too. Move
detection is unchanged: it reads only new paths whose size matches a missing record.

One case is given up: different bytes in the middle of a file under the same size and the same
modification time, such as a gain tool that rewrites audio frames and restores the time. Such a
file is analysed again at its next size or time change, or on a rescan with Override with ID3
tags. A time that moves without a change, such as after a remount, costs only the old read. In a
synthetic benchmark the rescan of unchanged files read no fingerprints instead of one per file.
Contract ADR-0008-34 records this; the ADR remains Proposed.

## Amendment clarification 2026-10-25: Starting the first songs sooner after a restart

Issue #584: after a restart, a station with a clockwheel of category and Beets conditions was
silent for about a minute on a large library. A station's first pick works out every category
and every Beets value over the whole library before it chooses a song, and the first song also
waits for the pick that follows it. That work ran beside the library scan, which the server
started at once, and beside the other station's first pick. Each station's categories and the
clockwheel also read the Beets database separately, so two stations read it three times. The
new objects set off full garbage-collector passes, which held up the mix bus.

The startup scan now waits until every station with a mix bus has started its first song, for
at most 2 minutes; a station that plays nothing on its own, such as one driven by the browser,
is not waited for, and a shutdown before then starts no scan. The library keeps one Beets reader
for the clockwheel conditions and every station's categories. Each read takes every field asked
for since the start, and a clockwheel announces its fields when it loads, so after the database
or the library changes, the first reader reads Beets once for all of them. When the categories
are worked out again, one line at info level says why (first time, the library, the Beets
database, the categories, a playlist, the folders, or the hour for relative dates) and how long it
took.

In a benchmark of a real two-station server on 80,000 synthetic songs, pinned to 4 cores, the
time from each station's start to its first audio fell from 15.1 to 15.7 seconds to 4.5 to
4.7 seconds, and the worst delay of a paced 20 ms thread from 166 to 178 ms to 64 to 66 ms.
Without the scan wait it was about 8.1 seconds, and without the shared Beets read about 7.0 to
7.4 seconds. While songs played, a change to one song made the next picks work every category
out again; the worst delay then was 103 to 108 ms before and 25 to 27 ms after. Freezing the
rebuilt categories and Beets values out of the collector made no measurable difference, so they
are not frozen. Making that rebuild incremental is left for later. These are measurements on one machine, not a latency guarantee.
Contract ADR-0008-35 records this; the ADR remains Proposed.

## Amendment clarification 2026-10-25: No full garbage-collector passes while a bus plays

Issue #584, reopened: on a host with a 4-core Celeron J4125, about 79,000 songs, a Beets database
of 4.8 million flexible-attribute rows and 21 query categories on two stations, both stations' mix
buses fell behind together, by up to 0.5 seconds, while the categories were worked out, while the
library scan ran and while Beets was read. A benchmark of a real two-station server at that shape,
pinned to 4 cores of a faster machine, showed each stall at the moment of a full collector pass
that freed nothing. Such a pass walks every tracked object the server has not frozen and holds the
GIL throughout, so the 0.5 ms switch interval of ADR-0004-15 does not help. The library and
stations are frozen once loaded (ADR-0008-33), so the old generation is almost empty, and CPython's
rule of a full pass once the objects aged into it reach a quarter of those already there no longer
spaces the passes out. A category build, a Beets read, the scan's look for moved files and a
reload keep hundreds of thousands of new objects alive for a few seconds; as they aged, pass after
pass walked them, the longest about 190 ms on the benchmark machine.

While any bus runs, the collector now makes no full pass on its own. The oldest generation's
threshold is raised when the first bus starts and put back when the last stops, as the switch
interval is, unless something else changed it meanwhile; a collector someone switched off is left
alone. The young generations are still collected as before, and the objects such work keeps alive
are freed by reference counting when it ends. Reference cycles that outlive the young generations
are rare, so the library tick makes the full pass itself every 10 minutes, and only while no scan,
reload, category build or Beets read runs, when it walks only the objects that outlived their work.
A scan measuring loudness, one file at a time and perhaps for hours, does not hold it up.
In the benchmark, pinned to 4 efficiency cores, where a full category build took about 6 seconds
against 16.5 in the field, three runs each: the longest gap between two blocks of a bus over the
first builds, the scan, the update after removed songs and a Beets read fell from 134 to 143 ms
to 39 to 48 ms; gaps over 50 ms fell from 10 per run to none; the longest collector pause fell
from 119 to 124 ms to about 20 ms, a young collection. The tick's own full pass took 39 ms there,
no longer than one of the passes the server made by itself while idle. These are measurements on
one machine, not a latency guarantee.
Contract ADR-0008-36 records this; the ADR remains Proposed.

## Amendment clarification 2026-10-25: Gone songs stay gone after a restart

Issue #589: on a library of about 79,000 songs, every scan after a restart reported the same 739
songs gone, and the server held 79,079 songs at startup where Beets held 78,340, the difference
being exactly those songs. A scan already kept a gone song's record, since only the confirmed
Remove missing tracks action deletes records, and left the song out of the playable library. But
it kept the list of gone songs in memory only. After each restart the gone songs loaded as
playable again, the categories were worked out for them, they could be picked until the startup
scan ran, and that scan counted all of them gone again and loaded the library a second time.

The list is now saved in the catalog, as a table of the records found gone, which a record
leaves when it is removed and which follows a record when the scan finds it moved. A restarted
server reads it before it first loads the library, so gone songs stay out of the playable songs,
the vector index, the categories and the library totals, and a scan counts only songs newly
gone. The table changes no catalog change counter, as it is the library's reading of the folders,
not a change to a record. It is written only when a scan changes the set. A song whose file
returns comes back at the next scan. The safety rules are unchanged: a source folder that is
missing, such as an unmounted share, or a folder the scan could not read marks nothing gone, and
the songs saved as gone under it stay gone. The first scan after this update still counts the
songs gone before it once, as nothing was saved yet. Contract ADR-0008-37 records this; the ADR
remains Proposed.

## Amendment clarification 2026-10-27: reuse audio work across local and remote analysis

[Issue #594](https://github.com/blindndangerous/AutoDJ/issues/594) adds a versioned audio-feature
stage shared by local scans, MuQ vector analysis and compatible workers. Audio features and DJ
metadata can be saved without a MuQ vector; Similarity adds the vector using the same feature
result. The catalog reuses a matching feature cache independently of its vector, and recalculates
it when file identity or feature version changes. For tracks over 15 minutes, feature work uses
the first and last five minutes. The Similarity maximum-length setting decides whether MuQ vector
work is scheduled; longer tracks can still receive Audio analysis.

Workers and servers negotiate a versioned scan protocol. A worker with a different scan-analysis
version does not publish incompatible scan features; the server handles those scans locally.
Existing compatible vector work remains available under the worker's model and embedding identity
checks. These additions document the Issue #594 boundary and retain this ADR's Proposed status.
